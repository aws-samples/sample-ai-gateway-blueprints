"""
Enable Amazon Bedrock Model Invocation Logging in multiple regions, delivering
to BOTH an S3 bucket and a CloudWatch log group per region.

Why per-region resources: Bedrock requires the S3 bucket and CloudWatch log
group to be in the SAME account and region as the logging configuration
(documented constraint). So each region gets its own bucket, log group, and the
IAM role Bedrock assumes to write to CloudWatch.

This is what makes caller-supplied `requestMetadata` tags queryable: the tags
appear in these invocation logs (they do NOT appear in CloudTrail). You can then
pass requestMetadata through the LiteLLM SDK and filter the logs by your tag.

Idempotent: safe to re-run. Creates what is missing, updates policies in place.

Regions: us-east-1, us-west-2, eu-west-1, eu-north-1, eu-central-1, eu-west-3.
(eu-south-1 / eu-south-2 are opt-in regions that may be DISABLED in your account
and are intentionally skipped -- enabling them is a separate account decision.)
"""

import json
import sys
import time

import boto3
from botocore.exceptions import ClientError

ACCOUNT_ID = boto3.client("sts").get_caller_identity()["Account"]

REGIONS = [
    # All regions currently ENABLED on the account that have Bedrock reachable.
    "af-south-1",
    "ap-northeast-1",
    "ap-northeast-2",
    "ap-northeast-3",
    "ap-south-1",
    "ap-southeast-1",
    "ap-southeast-2",
    "ca-central-1",
    "eu-central-1",
    "eu-north-1",
    "eu-west-1",
    "eu-west-2",
    "eu-west-3",
    "sa-east-1",
    "us-east-1",
    "us-east-2",
    "us-west-1",
    "us-west-2",
    # Opt-in regions. Must be enabled in the account's Regions settings first (region
    # opt status is managed by the account and cannot be changed via
    # the standard account API). Once ENABLED, this script picks them up; while
    # DISABLED they are detected and skipped automatically.
    "eu-south-1",
    "eu-south-2",
]

# CloudWatch log-group retention and S3 expiry for the (sensitive) logs.
LOG_RETENTION_DAYS = 90
S3_EXPIRY_DAYS = 90

LOG_GROUP = "/aws/bedrock/model-invocations"
LOG_STREAM_PREFIX = "aws/bedrock/modelinvocations"  # the stream Bedrock writes to


def bucket_name(region: str) -> str:
    return f"bedrock-invocation-logs-{ACCOUNT_ID}-{region}"


def role_name(region: str) -> str:
    return f"BedrockInvocationLogging-{region}"


def region_reachable(region: str) -> bool:
    """A disabled opt-in region rejects calls with an invalid-token error."""
    try:
        boto3.client("sts", region_name=region).get_caller_identity()
        return True
    except ClientError as e:
        code = e.response["Error"].get("Code", "")
        if code in ("UnrecognizedClientException", "InvalidClientTokenId",
                    "AuthFailure"):
            return False
        raise


# ---------------------------------------------------------------- S3 ----------
def ensure_bucket(region: str) -> str:
    name = bucket_name(region)
    s3 = boto3.client("s3", region_name=region)
    try:
        s3.head_bucket(Bucket=name)
        print(f"    S3 bucket exists: {name}")
    except ClientError:
        kwargs = {"Bucket": name}
        if region != "us-east-1":  # us-east-1 rejects an explicit constraint
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        print(f"    Created S3 bucket: {name}")

    # Block public access (defense in depth).
    s3.put_public_access_block(
        Bucket=name,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    # Default SSE-S3 encryption.
    s3.put_bucket_encryption(
        Bucket=name,
        ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault":
                       {"SSEAlgorithm": "AES256"}}]
        },
    )
    # Lifecycle: expire log objects after S3_EXPIRY_DAYS (these contain full
    # prompt/response content, so don't retain them indefinitely). Also abort
    # incomplete multipart uploads.
    s3.put_bucket_lifecycle_configuration(
        Bucket=name,
        LifecycleConfiguration={
            "Rules": [{
                "ID": "expire-bedrock-logs",
                "Filter": {"Prefix": ""},
                "Status": "Enabled",
                "Expiration": {"Days": S3_EXPIRY_DAYS},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
            }]
        },
    )
    print(f"    Lifecycle set ({S3_EXPIRY_DAYS}d expiry): {name}")
    # Bucket policy allowing Bedrock to write invocation logs (per AWS docs).
    policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AmazonBedrockLogsWrite",
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": ["s3:PutObject"],
            "Resource": [
                f"arn:aws:s3:::{name}/AWSLogs/{ACCOUNT_ID}/BedrockModelInvocationLogs/*"
            ],
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{ACCOUNT_ID}:*"},
            },
        }],
    }
    s3.put_bucket_policy(Bucket=name, Policy=json.dumps(policy))
    print(f"    Applied bucket policy: {name}")
    return name


# ------------------------------------------------------ CloudWatch + IAM ------
def ensure_log_group(region: str):
    logs = boto3.client("logs", region_name=region)
    try:
        logs.create_log_group(logGroupName=LOG_GROUP)
        print(f"    Created log group: {LOG_GROUP}")
    except logs.exceptions.ResourceAlreadyExistsException:
        print(f"    Log group exists: {LOG_GROUP}")
    # Retention (these logs contain prompt/response content).
    logs.put_retention_policy(logGroupName=LOG_GROUP,
                              retentionInDays=LOG_RETENTION_DAYS)
    print(f"    Retention set ({LOG_RETENTION_DAYS}d): {LOG_GROUP}")


def ensure_role(region: str) -> str:
    """One role per region; IAM is global but trust/policy are region-scoped."""
    iam = boto3.client("iam")  # global
    rname = role_name(region)
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
            "Condition": {
                "StringEquals": {"aws:SourceAccount": ACCOUNT_ID},
                "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock:{region}:{ACCOUNT_ID}:*"},
            },
        }],
    }
    try:
        iam.create_role(
            RoleName=rname,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description=f"Bedrock model invocation logging -> CloudWatch ({region})",
        )
        print(f"    Created IAM role: {rname}")
    except iam.exceptions.EntityAlreadyExistsException:
        iam.update_assume_role_policy(RoleName=rname,
                                      PolicyDocument=json.dumps(trust))
        print(f"    IAM role exists (trust refreshed): {rname}")

    perm = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
            "Resource":
                f"arn:aws:logs:{region}:{ACCOUNT_ID}:log-group:{LOG_GROUP}:log-stream:{LOG_STREAM_PREFIX}",
        }],
    }
    iam.put_role_policy(RoleName=rname, PolicyName="WriteToCloudWatchLogs",
                        PolicyDocument=json.dumps(perm))
    print(f"    Attached inline policy to role: {rname}")
    return f"arn:aws:iam::{ACCOUNT_ID}:role/{rname}"


# ----------------------------------------------------- enable logging ---------
def enable_logging(region: str, bucket: str, role_arn: str):
    bedrock = boto3.client("bedrock", region_name=region)
    # IAM propagation can lag; retry the Put until the role is assumable.
    cfg = {
        "cloudWatchConfig": {
            "logGroupName": LOG_GROUP,
            "roleArn": role_arn,
            "largeDataDeliveryS3Config": {"bucketName": bucket},
        },
        "s3Config": {"bucketName": bucket},
        "textDataDeliveryEnabled": True,
        "imageDataDeliveryEnabled": True,
        "embeddingDataDeliveryEnabled": True,
        "videoDataDeliveryEnabled": True,
        "audioDataDeliveryEnabled": True,
    }
    last = None
    for attempt in range(1, 9):
        try:
            bedrock.put_model_invocation_logging_configuration(loggingConfig=cfg)
            print(f"    Logging ENABLED in {region} (S3 + CloudWatch)")
            return
        except ClientError as e:
            last = e
            msg = e.response["Error"].get("Message", "")
            if "not authorized" in msg or "assume" in msg.lower() or "role" in msg.lower():
                print(f"    waiting for IAM propagation (attempt {attempt})...")
                time.sleep(10)
                continue
            raise
    raise last


def main():
    print(f"Account: {ACCOUNT_ID}")
    print(f"Regions: {', '.join(REGIONS)}\n")
    results = {}
    skipped_disabled = []
    for region in REGIONS:
        print(f"[{region}]")
        if not region_reachable(region):
            skipped_disabled.append(region)
            print("    SKIPPED: region is opt-in and DISABLED in this account "
                  "(enable it in the account's Regions settings, then re-run).\n")
            continue
        try:
            bucket = ensure_bucket(region)
            ensure_log_group(region)
            role_arn = ensure_role(region)
            enable_logging(region, bucket, role_arn)
            results[region] = "OK"
        except Exception as e:
            results[region] = f"FAILED: {type(e).__name__}: {e}"
            print(f"    ERROR: {results[region]}")
        print()

    print("=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for region, status in results.items():
        print(f"  {region:14} {status}")
    if skipped_disabled:
        print("\n  SKIPPED (opt-in regions DISABLED in this account):")
        print(f"    {', '.join(skipped_disabled)}")
        print("    Enable them in the account's Regions settings, then re-run this script.")
    if any(v != "OK" for v in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
