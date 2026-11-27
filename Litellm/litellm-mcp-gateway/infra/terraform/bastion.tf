# Private admin host reachable only through AWS Systems Manager Session Manager: no inbound rules,
# no public IP, no SSH key. Use it for port forwarding to Aurora and to the internal load balancer.

data "aws_ssm_parameter" "al2023_arm64" {
  count = var.create_ssm_bastion ? 1 : 0
  name  = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

resource "aws_security_group" "bastion" {
  #checkov:skip=CKV2_AWS_5:Attached to aws_instance.bastion (count-indexed, not resolved by the check)
  count       = var.create_ssm_bastion ? 1 : 0
  name        = "${var.name}-bastion"
  description = "SSM bastion: no inbound rules"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name}-bastion" }
}

resource "aws_vpc_security_group_egress_rule" "bastion_https" {
  count             = var.create_ssm_bastion ? 1 : 0
  security_group_id = aws_security_group.bastion[0].id
  description       = "HTTPS to SSM endpoints and the gateway load balancer"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = local.vpc_cidr
}

resource "aws_vpc_security_group_egress_rule" "bastion_ssm_nat" {
  count             = var.create_ssm_bastion && !var.enable_interface_endpoints ? 1 : 0
  security_group_id = aws_security_group.bastion[0].id
  description       = "HTTPS to public SSM endpoints through NAT when no interface endpoints exist"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "bastion_db" {
  count                        = var.create_ssm_bastion ? 1 : 0
  security_group_id            = aws_security_group.bastion[0].id
  description                  = "PostgreSQL to Aurora"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

resource "aws_iam_role" "bastion" {
  count = var.create_ssm_bastion ? 1 : 0
  name  = "${var.name}-bastion"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "bastion_ssm" {
  count      = var.create_ssm_bastion ? 1 : 0
  role       = aws_iam_role.bastion[0].name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "bastion" {
  count = var.create_ssm_bastion ? 1 : 0
  name  = "${var.name}-bastion"
  role  = aws_iam_role.bastion[0].name
}

resource "aws_instance" "bastion" {
  count                       = var.create_ssm_bastion ? 1 : 0
  ami                         = data.aws_ssm_parameter.al2023_arm64[0].value
  instance_type               = "t4g.nano"
  subnet_id                   = local.app_subnet_ids[0]
  vpc_security_group_ids      = [aws_security_group.bastion[0].id]
  iam_instance_profile        = aws_iam_instance_profile.bastion[0].name
  associate_public_ip_address = false
  monitoring                  = true
  ebs_optimized               = true

  metadata_options {
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
    http_endpoint               = "enabled"
  }

  root_block_device {
    encrypted   = true
    kms_key_id  = aws_kms_key.this.arn
    volume_type = "gp3"
    volume_size = 8
  }

  lifecycle {
    ignore_changes = [ami]
  }

  tags = { Name = "${var.name}-bastion" }
}
