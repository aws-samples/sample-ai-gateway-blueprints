"""
Detect the AWS region that actually served a Bedrock cross-region inference
(CRIS) request -- using the LiteLLM SDK and Bedrock Model Invocation Logging.
Works for either a Geo profile or the Global profile. See:
  https://docs.aws.amazon.com/bedrock/latest/userguide/cross-region-inference.html

Usage:
  python litellm-sdk-cris-region-detector.py geo                  # EU geo profile, default source region
  python litellm-sdk-cris-region-detector.py global               # global profile
  python litellm-sdk-cris-region-detector.py eu     # or: geo
  python litellm-sdk-cris-region-detector.py us
  python litellm-sdk-cris-region-detector.py au     # or: apac
  python litellm-sdk-cris-region-detector.py jp
  python litellm-sdk-cris-region-detector.py geo --region eu-west-2   # call FROM eu-west-2
  python litellm-sdk-cris-region-detector.py us                    # defaults to us-east-1 entry
  python litellm-sdk-cris-region-detector.py us --region us-west-2
  python litellm-sdk-cris-region-detector.py au                    # defaults to ap-southeast-2
  python litellm-sdk-cris-region-detector.py eu --region eu-west-3
  (scope defaults to "geo"; source region defaults to
   $AWS_REGION_NAME / $AWS_REGION / eu-west-1)

Why this approach (verified against Bedrock, Oct 2026):

  * Bedrock does NOT return the fulfilling region in the response body/headers,
    and LiteLLM does NOT expose Bedrock's x-amzn-RequestId -- so you cannot
    correlate a LiteLLM call to CloudTrail by request id.
  * Instead we tag the call with a unique value via Bedrock's `requestMetadata`
    field, which LiteLLM DOES forward (completion(..., requestMetadata={...})).
  * That tag lands in Bedrock MODEL INVOCATION LOGGING (CloudWatch/S3) -- it is
    the only caller-supplied field in the log record. The log record contains
    `inferenceRegion` (the region that ACTUALLY ran the model), `region` (the
    entry region = where you called), plus the Bedrock requestId, modelId and
    token counts.
  * The invocation log is written in the ENTRY region's log group (NOT the
    serving region's), so we query the entry region's log group and read
    `inferenceRegion`.

Prerequisite: Bedrock model invocation logging must be enabled (S3+CloudWatch)
in the region you CALL FROM (the entry region) -- the log is written there, not
in the serving region. Set up here via:
  inference-profile/enable_bedrock_invocation_logging.py
Enabled entry regions in the test account: us-east-1, us-west-2, eu-west-1,
eu-north-1, eu-central-1, eu-west-3.
"""

import os
import argparse
import json
import time
import uuid

import boto3
from litellm import completion
from aws_bedrock_token_generator import provide_token

# Use my local IAM credential

LOG_GROUP = "/aws/bedrock/model-invocations"

# Regions where Bedrock model invocation logging is enabled in the test account.
# The invocation log is written in the ENTRY region (the region you call), so in
# practice you only need logging enabled in the regions you call FROM. This list
# is kept for reference / sanity-checking the entry region.
# Kept in sync with inference-profile/enable_bedrock_invocation_logging.py.
LOGGING_REGIONS = [
    "af-south-1",
    "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1",
    "ap-southeast-1", "ap-southeast-2",
    "ca-central-1",
    "eu-central-1", "eu-north-1",
    "eu-west-1", "eu-west-2", "eu-west-3",
    "sa-east-1",
    "us-east-1", "us-east-2", "us-west-1", "us-west-2",
    # Opt-in regions being enabled (in the account's Regions settings) + logging set up via the
    # enable script. Remove if you roll them back.
    "eu-south-1", "eu-south-2",
]

# ---- CLI arguments: CRIS scope + source (entry) region --------------------
# The scope is the inference-profile geography prefix. Supported prefixes for
# Opus 5.5 (verified Oct 2026): eu, us, au (the APAC/Australia geo), jp (Japan),
# and global.
#   * A geography-scoped profile (eu/us/au/jp) routes only within a fixed set of
#     regions in that geography. You must CALL FROM a region in that geography
#     (e.g. a us.* profile needs a US entry region) or the call is rejected.
#   * "global" can route to Bedrock regions worldwide.
# "geo" is kept as a backward-compatible alias for "eu".
#
# --region overrides the source/entry region (the region you call FROM). This
# lets you test whether the source region affects which region CRIS picks for
# inference. The invocation log is written in the ENTRY region, so detection
# needs logging enabled there (see LOGGING_REGIONS).
BASE_MODEL = "anthropic.claude-opus-5-5"

# scope argument -> inference-profile prefix. "geo" aliases "eu".
SCOPE_PREFIX = {
    "geo": "eu",
    "eu": "eu",
    "us": "us",
    "au": "au",        # APAC / Australia geo
    "apac": "au",      # convenience alias
    "jp": "jp",        # Japan geo
    "global": "global",
}

# A sensible default entry region per geography (used only if you don't pass
# --region / env and the default eu-west-1 wouldn't match the chosen geo).
GEO_DEFAULT_REGION = {
    "eu": "eu-west-1",
    "us": "us-east-1",
    "au": "ap-southeast-2",
    "jp": "ap-northeast-1",
    "global": "eu-west-1",
}

_parser = argparse.ArgumentParser(
    description="Detect which AWS region actually serves a Bedrock CRIS call, "
                "via LiteLLM + model invocation logging.")
_parser.add_argument("scope", nargs="?", default="geo",
                     choices=sorted(SCOPE_PREFIX),
                     help="CRIS geography/scope to test: eu (alias geo), us, "
                          "au (alias apac), jp, or global (default: geo=eu)")
_parser.add_argument("--region", "-r", default=None,
                     help="source/entry region to call from. Must be in the "
                          "chosen geography for geo-scoped profiles. "
                          "(default: $AWS_REGION_NAME / $AWS_REGION / a "
                          "geo-appropriate default)")
_args = _parser.parse_args()

scope = _args.scope
prefix = SCOPE_PREFIX[scope]
MODEL_ID = f"{prefix}.{BASE_MODEL}"   # e.g. eu.anthropic.claude-opus-5-5

# Precedence: --region > AWS_REGION_NAME > AWS_REGION > geo-appropriate default.
region = (_args.region
          or os.environ.get("AWS_REGION_NAME")
          or os.environ.get("AWS_REGION")
          or GEO_DEFAULT_REGION.get(prefix, "eu-west-1"))

if region not in LOGGING_REGIONS:
    print(f"WARNING: entry region {region!r} is not in LOGGING_REGIONS "
          f"{LOGGING_REGIONS}.")
    print("         The invocation log is written in the entry region, so "
          "detection may fail")
    print("         unless model invocation logging is enabled there.\n")


def cris_candidate_regions(profile_id: str, call_region: str):
    """
    Return (geo, [candidate_regions]) for a CRIS profile, or (None, []).

    For a geography-scoped profile (eu/us/au/apac) the member-region ARNs from
    get_inference_profile enumerate the regions it may route to. For the GLOBAL
    profile this list is NOT meaningful (global can route almost anywhere), so
    we return an empty list and search all logging-enabled regions instead.
    """
    geo = profile_id.split(".", 1)[0] if "." in profile_id else None
    if geo not in ("eu", "us", "au", "apac", "jp", "global"):
        return None, []
    if geo == "global":
        return geo, []
    try:
        c = boto3.client("bedrock", region_name=call_region)
        d = c.get_inference_profile(inferenceProfileIdentifier=profile_id)
        regions = sorted({
            m.get("modelArn", "").split(":")[3]
            for m in d.get("models", [])
            if len(m.get("modelArn", "").split(":")) > 3
        })
        return geo, regions
    except Exception as e:
        print(f"  (could not describe profile: {e})")
        return geo, []


def find_serving_record(trace_tag: str, entry_region: str, *, attempts=12, delay=20):
    """
    Search the Bedrock invocation-log group IN THE ENTRY REGION for the record
    carrying our unique `trace_tag` in requestMetadata.

    Note (verified Oct 2026): the invocation log is written in the ENTRY region
    (where the request was received = the region you called), NOT in the region
    that served it. The record has two region fields:
      * `region`          = the entry region (same as where you called)
      * `inferenceRegion` = the region that ACTUALLY ran the model
    So we query only the entry region's log group and read inferenceRegion.

    Returns (serving_region, record_dict) or (None, None). Invocation logs
    typically appear within ~1 minute.
    """
    start_ms = int((time.time() - 300) * 1000)
    logs = boto3.client("logs", region_name=entry_region)
    for attempt in range(1, attempts + 1):
        try:
            res = logs.filter_log_events(
                logGroupName=LOG_GROUP,
                startTime=start_ms,
                filterPattern=f'"{trace_tag}"',
                limit=10,
            )
        except logs.exceptions.ResourceNotFoundException:
            print(f"  log group {LOG_GROUP} not found in {entry_region} "
                  "(is invocation logging enabled there?)")
            return None, None
        except Exception:
            res = {"events": []}
        for ev in res.get("events", []):
            if trace_tag in ev["message"]:
                rec = json.loads(ev["message"])
                # inferenceRegion = the region that actually ran the model;
                # fall back to `region` (entry) only if it's absent.
                served = rec.get("inferenceRegion") or rec.get("region")
                return served, rec
        if attempt < attempts:
            print(f"  invocation log not populated yet "
                  f"(attempt {attempt}/{attempts}); waiting {delay}s...")
            time.sleep(delay)
    return None, None


# ---- Make the call via LiteLLM, tagged with a unique trace id ---------------
trace_tag = f"cris-{scope}-{uuid.uuid4().hex[:12]}"
token = provide_token(region=region)

response = completion(
    model=f"bedrock/{MODEL_ID}",
    messages=[{"content": "Hello, can you explain Thermodynamics to me?", "role": "user"}],
    aws_region_name=region,
    api_key=token,
    requestMetadata={
        "trace_tag": trace_tag,
        "scope": scope,
        "profile": MODEL_ID,
    },
)

reply = response.choices[0].message.content
usage = response.usage

print(f"Model reply : {reply}")
print(f"Tokens      : in={usage.prompt_tokens} out={usage.completion_tokens}")
print(f"Trace tag   : {trace_tag}")

# ---- Resolve the serving region from invocation logs ------------------------
geo, candidates = cris_candidate_regions(MODEL_ID, region)

print("\n" + "=" * 62)
print("AWS REGION REPORT (Bedrock cross-region inference, via LiteLLM)")
print("=" * 62)
print(f"Inference profile : {MODEL_ID}")
print(f"Requested scope   : {scope}  (CLI arg)")
print(f"Called via region : {region}  (the region your SDK/token targeted)")

if geo:
    print(f"Geo / CRIS scope  : {geo!r}")
    if geo == "global":
        print("Candidate regions : (global -- may route to Bedrock regions "
              "worldwide)")
    else:
        print(f"Candidate regions : "
              f"{', '.join(candidates) if candidates else '(unavailable)'}")
    print(f"Querying log group: {LOG_GROUP} in {region} (the entry region)")
    print("Resolving the serving region from invocation logs "
          "(this can take ~1 minute)...")

    served, rec = find_serving_record(trace_tag, region)
    if served:
        entry = rec.get("region")
        print(f"SERVED FROM       : {served}   <-- inferenceRegion "
              "(exact region that ran the model)")
        print(f"Entry region      : {entry}  (rec.region -- where the request "
              "was received)")
        print(f"Bedrock requestId : {rec.get('requestId')}")
        print(f"Operation         : {rec.get('operation')}")
        print(f"requestMetadata   : {json.dumps(rec.get('requestMetadata'))}")
        print(f"Tokens (log)      : in={rec.get('input', {}).get('inputTokenCount')} "
              f"out={rec.get('output', {}).get('outputTokenCount')}")
        if candidates and served not in candidates:
            print("                    (note: not in the listed candidates)")
    else:
        print("SERVED FROM       : not found in invocation logs within the window.")
        print("                    Possible causes:")
        print("                      - logs still propagating (retry)")
        print("                      - invocation logging not enabled in the "
              f"entry region {region}")
        print(f"                    Query manually with trace_tag={trace_tag}")
        print(f"                    in log group {LOG_GROUP} ({region}).")
else:
    print("Geo / CRIS scope  : not a cross-region profile -- served from "
          f"{region}.")
print("=" * 62)
