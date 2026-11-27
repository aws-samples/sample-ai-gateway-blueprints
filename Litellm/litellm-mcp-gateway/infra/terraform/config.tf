# The reviewed LiteLLM config (models, MCP servers, tool allowlists) is versioned in S3. Each task
# definition pins one object version, so a config change is a reviewed terraform apply followed by
# a rolling deployment. In LiteLLM 1.109.1, MCP server changes made through the API are not
# written to the audit log, which is why the MCP server lives in this file instead.

resource "aws_s3_bucket" "config" {
  #checkov:skip=CKV_AWS_18:Config-only bucket, write access is Terraform; enable CloudTrail S3 data events if you need read auditing
  #checkov:skip=CKV2_AWS_62:No consumers for object events
  #checkov:skip=CKV_AWS_144:Config is reproducible from version control; no cross-Region copy needed
  bucket_prefix = "${var.name}-config-"
}

resource "aws_s3_bucket_public_access_block" "config" {
  bucket                  = aws_s3_bucket.config.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_versioning" "config" {
  bucket = aws_s3_bucket.config.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.this.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "config" {
  bucket = aws_s3_bucket.config.id

  rule {
    id     = "expire-old-config-versions"
    status = "Enabled"

    filter {}

    noncurrent_version_expiration {
      noncurrent_days = 365
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }

  depends_on = [aws_s3_bucket_versioning.config]
}

resource "aws_s3_bucket_policy" "config" {
  bucket = aws_s3_bucket.config.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyInsecureTransport"
      Effect    = "Deny"
      Principal = "*"
      Action    = "s3:*"
      Resource  = [aws_s3_bucket.config.arn, "${aws_s3_bucket.config.arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })

  depends_on = [aws_s3_bucket_public_access_block.config]
}

resource "aws_s3_object" "config" {
  bucket                 = aws_s3_bucket.config.id
  key                    = "litellm/config.yaml"
  source                 = local.config_path
  source_hash            = filemd5(local.config_path)
  content_type           = "application/yaml"
  server_side_encryption = "aws:kms"
  kms_key_id             = aws_kms_key.this.arn

  depends_on = [aws_s3_bucket_versioning.config]
}
