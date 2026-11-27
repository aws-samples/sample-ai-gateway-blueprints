# Generated secrets use ephemeral values and write-only arguments, so they never appear in the
# Terraform plan or state. Secrets that come from outside (Atlassian OAuth client, license) are
# created empty here; an administrator stores their values with the AWS CLI (see README).

ephemeral "random_password" "master_key" {
  length  = 48
  special = false
}

ephemeral "random_password" "salt_key" {
  length  = 48
  special = false
}

ephemeral "random_password" "db_password" {
  length           = 40
  special          = true
  override_special = "!#%*-_=+"
}

resource "aws_secretsmanager_secret" "master_key" {
  #checkov:skip=CKV2_AWS_57:Break-glass admin key; rotate manually with a planned redeploy
  name_prefix             = "${var.name}/litellm-master-key-"
  description             = "LiteLLM master key (break-glass admin). Day-to-day admins sign in through SSO."
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "master_key" {
  secret_id                = aws_secretsmanager_secret.master_key.id
  secret_string_wo         = "sk-${ephemeral.random_password.master_key.result}"
  secret_string_wo_version = 1
}

# The salt key encrypts every stored Atlassian token. Never change it after the first user
# connects: tokens encrypted with the old value become unreadable.
resource "aws_secretsmanager_secret" "salt_key" {
  #checkov:skip=CKV2_AWS_57:Must never change: it encrypts the stored per-user tokens
  name_prefix             = "${var.name}/litellm-salt-key-"
  description             = "LiteLLM salt key: encrypts stored credentials, including per-user Atlassian tokens. Do not rotate."
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "salt_key" {
  secret_id                = aws_secretsmanager_secret.salt_key.id
  secret_string_wo         = ephemeral.random_password.salt_key.result
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret" "db" {
  #checkov:skip=CKV2_AWS_57:Rotation needs a coordinated gateway redeploy; rotate manually
  name_prefix             = "${var.name}/aurora-"
  description             = "Aurora PostgreSQL credentials used by LiteLLM"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret_version" "db" {
  secret_id = aws_secretsmanager_secret.db.id
  secret_string_wo = jsonencode({
    username = local.db_username
    password = ephemeral.random_password.db_password.result
  })
  secret_string_wo_version = var.db_password_version
}

# {"client_id": "...", "client_secret": "..."} from scripts/register_atlassian_client.py
resource "aws_secretsmanager_secret" "atlassian_client" {
  #checkov:skip=CKV2_AWS_57:Issued by Atlassian; rotate by registering a new client
  name_prefix             = "${var.name}/atlassian-oauth-client-"
  description             = "OAuth client of the gateway at the Atlassian MCP server v2 authorization server"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 7
}

resource "aws_secretsmanager_secret" "license" {
  #checkov:skip=CKV2_AWS_57:Issued by BerriAI
  count                   = local.enterprise ? 1 : 0
  name_prefix             = "${var.name}/litellm-license-"
  description             = "LiteLLM Enterprise license key"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 7
}
