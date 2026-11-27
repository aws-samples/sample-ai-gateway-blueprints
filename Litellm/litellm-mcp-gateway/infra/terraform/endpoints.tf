# Interface endpoints keep AWS API calls (secrets, logs, keys, Bedrock, SSM) off the internet path.
locals {
  interface_endpoint_services = var.enable_interface_endpoints ? concat(
    ["secretsmanager", "logs", "kms", "sts", "bedrock-runtime"],
    can(regex("\\.dkr\\.ecr\\.", var.litellm_image)) ? ["ecr.api", "ecr.dkr"] : [],
    var.create_ssm_bastion ? ["ssm", "ssmmessages", "ec2messages"] : [],
  ) : []
}

resource "aws_security_group" "endpoints" {
  #checkov:skip=CKV2_AWS_5:Attached to aws_vpc_endpoint.interface (for_each, not resolved by the check)
  count       = var.enable_interface_endpoints ? 1 : 0
  name        = "${var.name}-endpoints"
  description = "Interface VPC endpoints, HTTPS from inside the VPC"
  vpc_id      = local.vpc_id

  ingress {
    description     = "HTTPS from the gateway tasks and the SSM bastion"
    from_port       = 443
    to_port         = 443
    protocol        = "tcp"
    security_groups = concat([aws_security_group.tasks.id], aws_security_group.bastion[*].id)
  }

  tags = { Name = "${var.name}-endpoints" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(local.interface_endpoint_services)
  vpc_id              = local.vpc_id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = local.app_subnet_ids
  security_group_ids  = [aws_security_group.endpoints[0].id]
  private_dns_enabled = true

  tags = { Name = "${var.name}-${each.key}" }
}

# S3 gateway endpoint (free) for the config bucket, only when this stack owns the route tables.
resource "aws_vpc_endpoint" "s3" {
  count             = var.create_vpc ? 1 : 0
  vpc_id            = local.vpc_id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = concat(aws_route_table.private[*].id, aws_route_table.data[*].id)

  tags = { Name = "${var.name}-s3" }
}
