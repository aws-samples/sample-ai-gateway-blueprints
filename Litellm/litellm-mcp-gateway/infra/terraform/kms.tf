# One customer managed key for the stack: secrets, database, config bucket, logs and EBS.
resource "aws_kms_key" "this" {
  description             = "${var.name} data encryption"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AccountAdministration"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.partition}:iam::${local.account_id}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogs"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.region}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = [
              "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/${var.name}/*",
              "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/rds/cluster/${var.name}/*",
              "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:/aws/ecs/containerinsights/${var.name}/*",
              "arn:${local.partition}:logs:${var.region}:${local.account_id}:log-group:aws-waf-logs-${var.name}",
            ]
          }
        }
      },
    ]
  })
}

resource "aws_kms_alias" "this" {
  name          = "alias/${var.name}"
  target_key_id = aws_kms_key.this.key_id
}
