# Internal Application Load Balancer with TLS. It has no public IP; clients reach it only from the
# CIDR ranges and security groups you allow.

resource "aws_security_group" "alb" {
  name        = "${var.name}-alb"
  description = "Internal gateway load balancer"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name}-alb" }
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_cidrs" {
  for_each          = toset(var.allowed_client_cidrs)
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS from allowed private range"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = each.value
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_clients" {
  security_group_id            = aws_security_group.alb.id
  description                  = "HTTPS from agents in the gateway_clients security group"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.gateway_clients.id
}

resource "aws_vpc_security_group_ingress_rule" "alb_from_bastion" {
  count                        = var.create_ssm_bastion ? 1 : 0
  security_group_id            = aws_security_group.alb.id
  description                  = "HTTPS from the SSM bastion (port forwarding for admins)"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.bastion[0].id
}

resource "aws_vpc_security_group_egress_rule" "alb_to_tasks" {
  security_group_id            = aws_security_group.alb.id
  description                  = "To gateway tasks"
  ip_protocol                  = "tcp"
  from_port                    = 4000
  to_port                      = 4000
  referenced_security_group_id = aws_security_group.tasks.id
}

# Attach this group to agents (for example an AgentCore Runtime in VPC mode) that call the gateway.
resource "aws_security_group" "gateway_clients" {
  #checkov:skip=CKV2_AWS_5:Attached by callers (for example AgentCore Runtime) outside this stack
  name        = "${var.name}-clients"
  description = "Attach to callers of the gateway; grants HTTPS to the internal load balancer"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name}-clients" }
}

resource "aws_vpc_security_group_egress_rule" "clients_to_alb" {
  security_group_id            = aws_security_group.gateway_clients.id
  description                  = "HTTPS to the gateway load balancer"
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_lb" "this" {
  name                       = var.name
  internal                   = true
  load_balancer_type         = "application"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = local.app_subnet_ids
  drop_invalid_header_fields = true
  enable_deletion_protection = var.deletion_protection
  idle_timeout               = 300 # MCP streamable HTTP keeps responses open

  dynamic "access_logs" {
    for_each = var.alb_access_logs_bucket == null ? [] : [var.alb_access_logs_bucket]
    content {
      bucket  = access_logs.value
      prefix  = var.name
      enabled = true
    }
  }
}

resource "aws_lb_target_group" "this" {
  name                 = var.name
  port                 = 4000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = local.vpc_id
  deregistration_delay = 30

  stickiness {
    type            = "lb_cookie"
    enabled         = var.target_group_stickiness
    cookie_duration = 86400
  }

  health_check {
    path                = "/health/liveliness"
    matcher             = "200"
    interval            = 15
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09" # TLS 1.3 and 1.2, hybrid post-quantum key exchange
  certificate_arn   = var.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.this.arn
  }
}


resource "aws_route53_record" "gateway" {
  count   = var.route53_private_zone_id == null ? 0 : 1
  zone_id = var.route53_private_zone_id
  name    = var.gateway_hostname
  type    = "A"

  alias {
    name                   = aws_lb.this.dns_name
    zone_id                = aws_lb.this.zone_id
    evaluate_target_health = true
  }
}
