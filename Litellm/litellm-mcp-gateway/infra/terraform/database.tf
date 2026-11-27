# Aurora PostgreSQL Serverless v2 in the data subnets. LiteLLM stores users, teams, keys, spend
# logs and the encrypted per-user Atlassian tokens here.

locals {
  db_username = "litellm"
  db_name     = "litellm"
}

resource "aws_db_subnet_group" "this" {
  name       = var.name
  subnet_ids = local.db_subnet_ids
}

resource "aws_security_group" "db" {
  name        = "${var.name}-db"
  description = "Aurora PostgreSQL, reachable from the gateway tasks and the SSM bastion only"
  vpc_id      = local.vpc_id

  tags = { Name = "${var.name}-db" }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_tasks" {
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from gateway tasks"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.tasks.id
}

resource "aws_vpc_security_group_ingress_rule" "db_from_bastion" {
  count                        = var.create_ssm_bastion ? 1 : 0
  security_group_id            = aws_security_group.db.id
  description                  = "PostgreSQL from the SSM bastion (port forwarding)"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.bastion[0].id
}

resource "aws_rds_cluster_parameter_group" "this" {
  name_prefix = "${var.name}-"
  family      = "aurora-postgresql${split(".", var.db_engine_version)[0]}"
  description = "Require TLS and log connections"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "log_statement"
    value = "ddl"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_rds_cluster" "this" {
  #checkov:skip=CKV2_AWS_27:Statement logging is limited to DDL so query data, including stored credentials, never reaches the logs
  #checkov:skip=CKV_AWS_162:Password auth from Secrets Manager; IAM database authentication is not used by this sample
  #checkov:skip=CKV2_AWS_8:Aurora automated backups (14 days) and a final snapshot; add an AWS Backup plan per your backup policy
  cluster_identifier              = var.name
  engine                          = "aurora-postgresql"
  engine_mode                     = "provisioned"
  engine_version                  = var.db_engine_version
  database_name                   = local.db_name
  master_username                 = local.db_username
  master_password_wo              = ephemeral.random_password.db_password.result
  master_password_wo_version      = var.db_password_version
  db_subnet_group_name            = aws_db_subnet_group.this.name
  vpc_security_group_ids          = [aws_security_group.db.id]
  db_cluster_parameter_group_name = aws_rds_cluster_parameter_group.this.name
  storage_encrypted               = true
  kms_key_id                      = aws_kms_key.this.arn
  backup_retention_period         = 14
  preferred_backup_window         = "02:00-03:00"
  copy_tags_to_snapshot           = true
  deletion_protection             = var.deletion_protection
  skip_final_snapshot             = false
  final_snapshot_identifier       = "${var.name}-final-${random_id.final_snapshot.hex}"
  enabled_cloudwatch_logs_exports = ["postgresql"]

  # CloudWatch Database Insights (standard) with Performance Insights, encrypted with the stack key.
  database_insights_mode                = "standard"
  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.this.arn
  performance_insights_retention_period = 7

  serverlessv2_scaling_configuration {
    min_capacity = var.db_min_capacity
    max_capacity = var.db_max_capacity
  }

  depends_on = [aws_cloudwatch_log_group.aurora]
}

# Created here so the PostgreSQL log is encrypted with the stack key, has a retention period and
# is removed on destroy. It records connections and DDL (never set passwords with plain SQL:
# use psql \password, which sends only a hash).
resource "aws_cloudwatch_log_group" "aurora" {
  name              = "/aws/rds/cluster/${var.name}/postgresql"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
}

# A new suffix per stack, so a later deploy and destroy does not collide with an old snapshot.
resource "random_id" "final_snapshot" {
  byte_length = 4
}

resource "aws_iam_role" "rds_monitoring" {
  name = "${var.name}-rds-monitoring"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_rds_cluster_instance" "this" {
  #checkov:skip=CKV_AWS_353:Performance Insights is on at the cluster level, which RDS applies to every instance in the cluster
  count                      = var.db_instance_count
  identifier                 = "${var.name}-${count.index}"
  cluster_identifier         = aws_rds_cluster.this.id
  engine                     = aws_rds_cluster.this.engine
  engine_version             = aws_rds_cluster.this.engine_version
  instance_class             = "db.serverless"
  db_subnet_group_name       = aws_db_subnet_group.this.name
  publicly_accessible        = false
  auto_minor_version_upgrade = true
  monitoring_interval        = 60
  monitoring_role_arn        = aws_iam_role.rds_monitoring.arn
}
