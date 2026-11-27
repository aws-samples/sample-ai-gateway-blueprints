# New VPC (create_vpc = true): private subnets for the gateway and load balancer, isolated data
# subnets for Aurora, and small public subnets that hold only the NAT gateway. Nothing that runs
# the workload gets a public IP; the NAT gateway gives outbound access to the Atlassian MCP
# server, the identity provider and the container registry.

locals {
  azs        = slice(data.aws_availability_zones.available.names, 0, var.az_count)
  nat_count  = var.create_vpc ? (var.nat_gateway_per_az ? var.az_count : 1) : 0
  vpc_create = var.create_vpc ? 1 : 0
}

resource "aws_vpc" "this" {
  #checkov:skip=CKV2_AWS_11:Flow logs are aws_flow_log.this (count-indexed, not resolved by the check)
  #checkov:skip=CKV2_AWS_12:Default security group is emptied by aws_default_security_group.this
  count                = local.vpc_create
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = var.name }
}

# Remove all rules from the default security group so nothing uses it by accident.
resource "aws_default_security_group" "this" {
  count  = local.vpc_create
  vpc_id = aws_vpc.this[0].id
}

resource "aws_subnet" "private" {
  count             = var.create_vpc ? var.az_count : 0
  vpc_id            = aws_vpc.this[0].id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index)

  tags = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_subnet" "data" {
  count             = var.create_vpc ? var.az_count : 0
  vpc_id            = aws_vpc.this[0].id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 4)

  tags = { Name = "${var.name}-data-${local.azs[count.index]}" }
}

# Holds only the NAT gateway. No instance, task or load balancer is placed here.
resource "aws_subnet" "nat" {
  count                   = var.create_vpc ? var.az_count : 0
  vpc_id                  = aws_vpc.this[0].id
  availability_zone       = local.azs[count.index]
  cidr_block              = cidrsubnet(var.vpc_cidr, 8, 240 + count.index)
  map_public_ip_on_launch = false

  tags = { Name = "${var.name}-nat-${local.azs[count.index]}" }
}

resource "aws_internet_gateway" "this" {
  count  = local.vpc_create
  vpc_id = aws_vpc.this[0].id

  tags = { Name = var.name }
}

resource "aws_eip" "nat" {
  count  = local.nat_count
  domain = "vpc"

  tags = { Name = "${var.name}-nat-${count.index}" }
}

resource "aws_nat_gateway" "this" {
  count         = local.nat_count
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.nat[count.index].id

  tags       = { Name = "${var.name}-${count.index}" }
  depends_on = [aws_internet_gateway.this]
}

resource "aws_route_table" "nat" {
  count  = local.vpc_create
  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.this[0].id
  }

  tags = { Name = "${var.name}-nat" }
}

resource "aws_route_table_association" "nat" {
  count          = var.create_vpc ? var.az_count : 0
  subnet_id      = aws_subnet.nat[count.index].id
  route_table_id = aws_route_table.nat[0].id
}

resource "aws_route_table" "private" {
  count  = var.create_vpc ? var.az_count : 0
  vpc_id = aws_vpc.this[0].id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.this[var.nat_gateway_per_az ? count.index : 0].id
  }

  tags = { Name = "${var.name}-private-${local.azs[count.index]}" }
}

resource "aws_route_table_association" "private" {
  count          = var.create_vpc ? var.az_count : 0
  subnet_id      = aws_subnet.private[count.index].id
  route_table_id = aws_route_table.private[count.index].id
}

# Data subnets have no route to the internet.
resource "aws_route_table" "data" {
  count  = local.vpc_create
  vpc_id = aws_vpc.this[0].id

  tags = { Name = "${var.name}-data" }
}

resource "aws_route_table_association" "data" {
  count          = var.create_vpc ? var.az_count : 0
  subnet_id      = aws_subnet.data[count.index].id
  route_table_id = aws_route_table.data[0].id
}

# VPC flow logs, encrypted with the stack key.
resource "aws_cloudwatch_log_group" "flow_logs" {
  count             = local.vpc_create
  name              = "/${var.name}/vpc-flow-logs"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_iam_role" "flow_logs" {
  count = local.vpc_create
  name  = "${var.name}-flow-logs"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "flow_logs" {
  count = local.vpc_create
  name  = "write-flow-logs"
  role  = aws_iam_role.flow_logs[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.flow_logs[0].arn}:*"
    }]
  })
}

resource "aws_flow_log" "this" {
  count           = local.vpc_create
  vpc_id          = aws_vpc.this[0].id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.flow_logs[0].arn
  iam_role_arn    = aws_iam_role.flow_logs[0].arn
}
