data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}

data "aws_availability_zones" "available" {
  state = "available"

  filter {
    name   = "opt-in-status"
    values = ["opt-in-not-required"] # Availability Zones only, no Local Zones
  }
}

data "aws_vpc" "existing" {
  count = var.create_vpc ? 0 : 1
  id    = var.vpc_id
}

locals {
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  enterprise = var.edition == "enterprise"

  option_folder = local.enterprise ? "option-a-enterprise-idp" : "option-b-virtual-key"
  config_path   = coalesce(var.litellm_config_path, "${path.module}/../../${local.option_folder}/proxy/config.yaml")

  gateway_url = "https://${var.gateway_hostname}"

  vpc_id         = var.create_vpc ? aws_vpc.this[0].id : var.vpc_id
  vpc_cidr       = var.create_vpc ? aws_vpc.this[0].cidr_block : data.aws_vpc.existing[0].cidr_block
  app_subnet_ids = var.create_vpc ? aws_subnet.private[*].id : var.private_subnet_ids
  db_subnet_ids  = var.create_vpc ? aws_subnet.data[*].id : var.private_subnet_ids

  # Bedrock: the account-scoped inference profiles and the foundation models behind them.
  inference_profile_arns = [
    for id in var.bedrock_inference_profile_ids :
    "arn:${local.partition}:bedrock:${var.region}:${local.account_id}:inference-profile/${id}"
  ]
  foundation_model_arns = [
    for id in var.bedrock_inference_profile_ids :
    "arn:${local.partition}:bedrock:*::foundation-model/${join(".", slice(split(".", id), 1, length(split(".", id))))}"
  ]
}
