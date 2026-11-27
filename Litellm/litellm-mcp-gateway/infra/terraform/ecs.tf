# LiteLLM on ECS Fargate in the private subnets. A short-lived init container fetches the pinned
# config version from S3 and the Amazon RDS CA bundle, so LiteLLM verifies the database
# certificate; the gateway container then starts from that file.

locals {
  bootstrap_script = <<-PY
    import os, urllib.request, boto3
    obj = boto3.client("s3").get_object(Bucket=os.environ["CONFIG_BUCKET"], Key=os.environ["CONFIG_KEY"], VersionId=os.environ["CONFIG_VERSION_ID"])
    open("/bootstrap/config.yaml", "wb").write(obj["Body"].read())
    urllib.request.urlretrieve(os.environ["RDS_CA_BUNDLE_URL"], "/bootstrap/rds-ca-bundle.pem")
    print("bootstrap: config version", os.environ["CONFIG_VERSION_ID"], "and RDS CA bundle ready")
  PY

  base_environment = {
    PROXY_BASE_URL       = local.gateway_url # Atlassian redirects consent to <this>/callback
    AWS_REGION           = var.region
    DATABASE_HOST        = "${aws_rds_cluster.this.endpoint}:${aws_rds_cluster.this.port}"
    DATABASE_NAME        = local.db_name
    DATABASE_SSLMODE     = "verify-full"
    DATABASE_SSLROOTCERT = "/bootstrap/rds-ca-bundle.pem"
    # /callback carries the one-time authorization code in its query string; keep it out of logs.
    LITELLM_DISABLE_ACCESS_LOG_PATHS = "/callback"
    # Keep-alive must outlast the load balancer idle timeout, or reused connections return 502.
    KEEPALIVE_TIMEOUT = tostring(aws_lb.this.idle_timeout + 10)
  }

  enterprise_environment = local.enterprise ? {
    JWT_PUBLIC_KEY_URL = var.jwt_public_key_url
    JWT_ISSUER         = var.jwt_issuer
    JWT_AUDIENCE       = var.jwt_audience
  } : {}

  environment = merge(local.base_environment, local.enterprise_environment, var.gateway_extra_environment)

  base_secrets = {
    LITELLM_MASTER_KEY      = aws_secretsmanager_secret.master_key.arn
    LITELLM_SALT_KEY        = aws_secretsmanager_secret.salt_key.arn
    DATABASE_USERNAME       = "${aws_secretsmanager_secret.db.arn}:username::"
    DATABASE_PASSWORD       = "${aws_secretsmanager_secret.db.arn}:password::"
    ATLASSIAN_CLIENT_ID     = "${aws_secretsmanager_secret.atlassian_client.arn}:client_id::"
    ATLASSIAN_CLIENT_SECRET = "${aws_secretsmanager_secret.atlassian_client.arn}:client_secret::"
  }

  enterprise_secrets = local.enterprise ? { LITELLM_LICENSE = aws_secretsmanager_secret.license[0].arn } : {}

  secrets = merge(local.base_secrets, local.enterprise_secrets, var.gateway_extra_secrets)

  # Secret ARNs without the optional :json-key:: suffix, for IAM.
  secret_arns = distinct([for value in values(local.secrets) : join(":", slice(split(":", value), 0, 7))])

  # LiteLLM needs no Linux capabilities (port 4000 is unprivileged); tested with all dropped.
  # The empty lists here and in the container definitions match what ECS returns, so plans do
  # not replace the task definition on every run.
  linux_parameters = {
    capabilities       = { add = [], drop = ["ALL"] }
    initProcessEnabled = true
  }

  log_config = {
    logDriver = "awslogs"
    options = {
      awslogs-group         = aws_cloudwatch_log_group.gateway.name
      awslogs-region        = var.region
      awslogs-stream-prefix = "gateway"
    }
  }
}

resource "aws_cloudwatch_log_group" "gateway" {
  name              = "/${var.name}/gateway"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_ecs_cluster" "this" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enhanced"
  }

  depends_on = [aws_cloudwatch_log_group.container_insights]
}

# Created here so Container Insights data is encrypted, expires and is removed on destroy.
resource "aws_cloudwatch_log_group" "container_insights" {
  name              = "/aws/ecs/containerinsights/${var.name}/performance"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_security_group" "tasks" {
  name        = "${var.name}-tasks"
  description = "LiteLLM gateway tasks"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name}-tasks" }
}

resource "aws_vpc_security_group_ingress_rule" "tasks_from_alb" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "Gateway port from the load balancer only"
  ip_protocol                  = "tcp"
  from_port                    = 4000
  to_port                      = 4000
  referenced_security_group_id = aws_security_group.alb.id
}

resource "aws_vpc_security_group_egress_rule" "tasks_https" {
  security_group_id = aws_security_group.tasks.id
  description       = "Outbound HTTPS: Atlassian MCP server, identity provider JWKS, registry, AWS APIs"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"
}

resource "aws_vpc_security_group_egress_rule" "tasks_db" {
  security_group_id            = aws_security_group.tasks.id
  description                  = "PostgreSQL to Aurora"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.db.id
}

resource "aws_ecs_task_definition" "gateway" {
  family                   = var.name
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.task_cpu
  memory                   = var.task_memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "ARM64"
  }

  volume {
    name = "bootstrap"
  }

  container_definitions = jsonencode([
    {
      name            = "bootstrap"
      image           = var.litellm_image
      essential       = false
      entryPoint      = ["python3", "-c"]
      command         = [local.bootstrap_script]
      mountPoints     = [{ sourceVolume = "bootstrap", containerPath = "/bootstrap", readOnly = false }]
      portMappings    = []
      systemControls  = []
      volumesFrom     = []
      linuxParameters = local.linux_parameters
      environment = [
        { name = "CONFIG_BUCKET", value = aws_s3_bucket.config.id },
        { name = "CONFIG_KEY", value = aws_s3_object.config.key },
        { name = "CONFIG_VERSION_ID", value = aws_s3_object.config.version_id },
        { name = "RDS_CA_BUNDLE_URL", value = "https://truststore.pki.rds.amazonaws.com/${var.region}/${var.region}-bundle.pem" },
        { name = "AWS_REGION", value = var.region },
      ]
      logConfiguration = local.log_config
    },
    {
      name            = "litellm"
      image           = var.litellm_image
      essential       = true
      command         = ["--config", "/bootstrap/config.yaml", "--port", "4000"]
      dependsOn       = [{ containerName = "bootstrap", condition = "SUCCESS" }]
      mountPoints     = [{ sourceVolume = "bootstrap", containerPath = "/bootstrap", readOnly = true }]
      portMappings    = [{ containerPort = 4000, hostPort = 4000, protocol = "tcp" }]
      systemControls  = []
      volumesFrom     = []
      linuxParameters = local.linux_parameters
      environment     = [for name, value in local.environment : { name = name, value = value }]
      secrets         = [for name, arn in local.secrets : { name = name, valueFrom = arn }]
      healthCheck = {
        command     = ["CMD-SHELL", "python3 -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1:4000/health/liveliness', timeout=5)\""]
        interval    = 30
        timeout     = 10
        retries     = 3
        startPeriod = 120
      }
      logConfiguration = local.log_config
    },
  ])
}

resource "aws_ecs_service" "gateway" {
  name                              = var.name
  cluster                           = aws_ecs_cluster.this.id
  task_definition                   = aws_ecs_task_definition.gateway.arn
  desired_count                     = var.desired_count
  launch_type                       = "FARGATE"
  platform_version                  = "LATEST"
  health_check_grace_period_seconds = 180
  enable_execute_command            = false
  propagate_tags                    = "SERVICE"

  network_configuration {
    subnets          = local.app_subnet_ids
    security_groups  = [aws_security_group.tasks.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.this.arn
    container_name   = "litellm"
    container_port   = 4000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]
}
