output "gateway_url" {
  description = "PROXY_BASE_URL of the gateway. Register the Atlassian client with <gateway_url>/callback."
  value       = local.gateway_url
}

output "atlassian_redirect_uri" {
  description = "Redirect URI for scripts/register_atlassian_client.py."
  value       = "${local.gateway_url}/callback"
}

output "load_balancer_dns_name" {
  description = "Internal load balancer DNS name. Point gateway_hostname at it if you did not set route53_private_zone_id."
  value       = aws_lb.this.dns_name
}

output "gateway_clients_security_group_id" {
  description = "Attach to agents (for example AgentCore Runtime in VPC mode) that call the gateway."
  value       = aws_security_group.gateway_clients.id
}

output "atlassian_client_secret_arn" {
  description = "Store {\"client_id\":...,\"client_secret\":...} here before raising desired_count."
  value       = aws_secretsmanager_secret.atlassian_client.arn
}

output "license_secret_arn" {
  description = "Option A: store the LiteLLM Enterprise license here before raising desired_count."
  value       = local.enterprise ? aws_secretsmanager_secret.license[0].arn : null
}

output "master_key_secret_arn" {
  description = "Break-glass admin key for the first admin setup."
  value       = aws_secretsmanager_secret.master_key.arn
}

output "config_object_version" {
  description = "S3 version of the LiteLLM config the current task definition runs."
  value       = aws_s3_object.config.version_id
}

output "ssm_port_forward_gateway" {
  description = "Port forward to the gateway through the bastion (run with eval). Then map <gateway_hostname> to 127.0.0.1 and use https://<gateway_hostname>:8443."
  value       = var.create_ssm_bastion ? "aws ssm start-session --region ${var.region} --target ${aws_instance.bastion[0].id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=${aws_lb.this.dns_name},portNumber=443,localPortNumber=8443" : null
}

output "ssm_port_forward_database" {
  description = "Reach Aurora from your workstation through the bastion (psql on localhost:15432, sslmode=verify-full needs the hosts entry for the cluster endpoint)."
  value       = var.create_ssm_bastion ? "aws ssm start-session --region ${var.region} --target ${aws_instance.bastion[0].id} --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters host=${aws_rds_cluster.this.endpoint},portNumber=5432,localPortNumber=15432" : null
}

output "ecs_cluster_name" {
  description = "ECS cluster of the gateway."
  value       = aws_ecs_cluster.this.name
}

output "ecs_service_name" {
  description = "ECS service of the gateway (aws ecs update-service --force-new-deployment restarts every task)."
  value       = aws_ecs_service.gateway.name
}

output "db_cluster_endpoint" {
  description = "Aurora writer endpoint."
  value       = aws_rds_cluster.this.endpoint
}

output "db_secret_arn" {
  description = "Aurora credentials used by LiteLLM."
  value       = aws_secretsmanager_secret.db.arn
}

output "kms_key_arn" {
  description = "Customer managed key of the stack."
  value       = aws_kms_key.this.arn
}

output "config_bucket" {
  description = "Bucket that holds the versioned LiteLLM config."
  value       = aws_s3_bucket.config.id
}

output "bastion_instance_id" {
  description = "SSM bastion instance, if created."
  value       = try(aws_instance.bastion[0].id, null)
}

output "nat_public_ips" {
  description = "Outbound addresses of the gateway. Add them to the Atlassian IP allowlist if your organization uses one."
  value       = aws_eip.nat[*].public_ip
}

output "waf_web_acl_arn" {
  description = "Web ACL protecting the load balancer."
  value       = local.waf_web_acl_arn
}
