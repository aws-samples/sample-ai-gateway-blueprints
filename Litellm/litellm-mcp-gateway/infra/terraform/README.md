# Infrastructure: private LiteLLM MCP gateway on Amazon ECS

Terraform for both options. `edition = "enterprise"` deploys [option A](../../option-a-enterprise-idp/) and `edition = "community"` deploys [option B](../../option-b-virtual-key/). Nothing in this stack is reachable from the internet: the only public resource is the NAT gateway, which carries outbound traffic only.

## What it creates

| Area | Resources |
|---|---|
| Network | A new VPC (or your existing one with `create_vpc = false`): private subnets for the gateway and the load balancer, isolated data subnets with no internet route for Aurora (with `create_vpc = false`, Aurora uses the private subnets you pass), and public subnets that hold only the NAT gateway. VPC flow logs. The default security group is emptied. |
| Private AWS access | Interface endpoints for Secrets Manager, CloudWatch Logs, AWS KMS, AWS STS and Bedrock Runtime; Systems Manager endpoints when the bastion is created; Amazon ECR endpoints when the image is in ECR; an Amazon S3 gateway endpoint when the stack creates the VPC. Endpoints accept HTTPS from the gateway tasks and the bastion only. Set `enable_interface_endpoints = false` if your VPC already has them. |
| Entry point | An internal Application Load Balancer, HTTPS only, with the `ELBSecurityPolicy-TLS13-1-2-Res-PQ-2025-09` policy (TLS 1.2 and 1.3). Reachable from `allowed_client_cidrs` (private ranges only, enforced by validation), the `gateway_clients` security group and the bastion. TLS ends at the load balancer; it forwards to the tasks over HTTP on port 4000 inside the VPC, and only the load balancer security group can reach that port. Optional access logs and Route 53 private alias record. |
| AWS WAF | A web ACL on the load balancer, created by default (see [AWS WAF](#aws-waf)). |
| Gateway | ECS Fargate (ARM64) service with the LiteLLM image pinned by digest, no public IP, ECS Exec off, all Linux capabilities dropped, deployment circuit breaker with rollback, Container Insights (enhanced). An init container fetches the pinned config version from S3 and the Amazon RDS CA bundle. |
| Database | Aurora PostgreSQL Serverless v2: encrypted, TLS required and verified by LiteLLM (`verify-full`), DDL-only statement logging, deletion protection, 14-day backups and a final snapshot, CloudWatch Database Insights (standard) with Performance Insights, enhanced monitoring. |
| Configuration | A versioned, encrypted S3 bucket (public access blocked, TLS-only policy) with the option's `proxy/config.yaml`. The task definition references the exact object version, so each config change is a new deployment you can roll back. |
| Secrets | Secrets Manager. The master key, salt key and database password are generated as ephemeral values and written with write-only arguments, so they never enter the Terraform plan or state. The Atlassian OAuth client and the license are created empty and filled by an administrator. |
| Encryption | One customer managed AWS KMS key with rotation, for logs, secrets, S3, Aurora, Performance Insights and EBS. |
| IAM | Execution role with the AWS managed `AmazonECSTaskExecutionRolePolicy` (image pull and logs) plus read access to the gateway's secrets only, and to the key only through Secrets Manager. Task role limited to reading the config object and invoking the configured Bedrock inference profiles (and the guardrails in `bedrock_guardrail_arns`, if any). |
| Operations | A private SSM-only bastion (Amazon Linux 2023, t4g.nano, no inbound rules, no public IP, no SSH key, IMDSv2) for port forwarding to the gateway and the database. |

## Deploy

Run these from the `litellm-mcp-gateway` folder, starting with `cd infra/terraform`. You need the AWS CLI v2 with the Session Manager plugin, Terraform 1.11 or later, and Python 3.10 or later.

1. **Configure and apply with no tasks.**
   ```bash
   cd infra/terraform
   cp terraform.tfvars.example terraform.tfvars   # edit: edition, Region, hostname, certificate, client ranges, IdP
   terraform init
   terraform plan -out tfplan
   terraform apply tfplan
   ```
   Keep `desired_count = 0` for this first apply: the Atlassian client secret is still empty.

   **Region and model.** The sample uses `eu-west-1` and the `eu.anthropic.claude-sonnet-4-6` inference profile. To deploy elsewhere, change `region` and `bedrock_inference_profile_ids` here, and `model` and `aws_region_name` in the option's `proxy/config.yaml`.
2. **Register the gateway at Atlassian** with the redirect URI from the outputs, then store the client. The JSON file holds the client secret, so the command pipes it to the AWS CLI (nothing on the command line) and then deletes the file.
   ```bash
   python3 ../../scripts/register_atlassian_client.py --redirect-uri "$(terraform output -raw atlassian_redirect_uri)"
   python3 -c 'import json; d = json.load(open("atlassian-oauth-client.json")); print(json.dumps({"client_id": d["client_id"], "client_secret": d["client_secret"]}))' \
     | aws secretsmanager put-secret-value \
         --secret-id "$(terraform output -raw atlassian_client_secret_arn)" --secret-string file:///dev/stdin
   rm atlassian-oauth-client.json
   ```
3. **Option A only: store the license.** The prompt does not echo the value.
   ```bash
   printf 'LiteLLM license: '; read -rs LICENSE; printf '\n'
   printf '%s' "$LICENSE" | aws secretsmanager put-secret-value \
     --secret-id "$(terraform output -raw license_secret_arn)" --secret-string file:///dev/stdin
   unset LICENSE
   ```
4. **Start the gateway.** Set `desired_count = 2` in `terraform.tfvars`, then run `terraform plan -out tfplan` and `terraform apply tfplan` again.
5. **DNS.** If you did not set `route53_private_zone_id`, point `gateway_hostname` at `load_balancer_dns_name` in your private DNS.
6. **Check health.** From a client on an allowed range (or through the [port forward](#reach-the-gateway-as-an-administrator), on port 8443):
   ```bash
   curl -fsS https://litellm.internal.example.com/health/readiness
   ```
   The response reports `"status": "healthy"` and `"db": "connected"`.
7. **Teams, users and consent.** Follow the [option A](../../option-a-enterprise-idp/README.md#set-up) or [option B](../../option-b-virtual-key/README.md#set-up) set-up.

## Reach the gateway as an administrator

Clients on the allowed private ranges use `https://<gateway_hostname>` directly. From a workstation without a network path, use the bastion. In one terminal, start the port forward:

```bash
eval "$(terraform output -raw ssm_port_forward_gateway)"
```

In a second terminal, map the gateway name to the local end of the tunnel (once), then call the gateway on port 8443 so the certificate name matches:

```bash
echo "127.0.0.1 litellm.internal.example.com" | sudo tee -a /etc/hosts
curl -fsS https://litellm.internal.example.com:8443/health/liveliness
```

Load the master key into your shell only when you need it, and only for the first setup:

```bash
export LITELLM_ADMIN_KEY="$(aws secretsmanager get-secret-value \
  --secret-id "$(terraform output -raw master_key_secret_arn)" --query SecretString --output text)"
```

Then [configure SSO for the Admin UI](https://docs.litellm.ai/docs/proxy/admin_ui_sso) through `gateway_extra_environment` and `gateway_extra_secrets`, so every change is attributed to a named administrator, and keep the master key as break-glass only.

`connect_atlassian.py` also works through the port forward: set `GATEWAY_URL=https://<gateway_hostname>:8443`. After consent the browser page does not load, because Atlassian redirects to port 443; copy its address anyway.

## AWS WAF

`create_waf = true` (the default) creates a web ACL and associates it with the load balancer. To use your own web ACL instead, set `waf_web_acl_arn`.

| Rule | Why |
|---|---|
| Block `refresh_token` grants to the gateway's `/token` endpoints | That endpoint accepts refresh grants from anyone who holds a refresh token and returns raw Atlassian tokens. The gateway refreshes stored tokens internally and never needs it. Authorization-code exchanges from `connect_atlassian.py` still pass. |
| Rate limit on `/v1/mcp/server/oauth/*` and `/callback` | Limits consent-flow abuse per source address (`waf_oauth_rate_limit`, default 100 requests per 5 minutes). |
| `AWSManagedRulesKnownBadInputsRuleSet` | AWS managed rules for known bad request patterns. |

The web ACL logs to the CloudWatch log group `aws-waf-logs-<name>`, encrypted with the stack key, with the query string (one-time codes) and the `Authorization` header redacted.

## Change the configuration

Edit the option's `proxy/config.yaml`, review it, and apply. Terraform uploads a new object version and rolls out a new task definition that references it. To roll back, revert the file and apply. In LiteLLM 1.109.1, MCP server changes made in the Admin UI or API are not audited, so make them in the file.

To screen Confluence content with an Amazon Bedrock guardrail, create the guardrail, add its ARN to `bedrock_guardrail_arns`, pass `BEDROCK_GUARDRAIL_ID` and `BEDROCK_GUARDRAIL_VERSION` through `gateway_extra_environment`, and uncomment the `guardrails` block in `config.yaml`.

## Rotate the database password

Increase `db_password_version` and apply. Terraform generates a new password, writes it to Aurora and to the database secret, and the next deployment of the gateway uses it. Force a new deployment right after (`aws ecs update-service --cluster "$(terraform output -raw ecs_cluster_name)" --service "$(terraform output -raw ecs_service_name)" --force-new-deployment`), because the tasks read the secret only at start: until they restart, their new database connections fail.

## Upgrade LiteLLM

Set `litellm_image` to the new digest (`image@sha256:...`; tags are rejected) and apply. Read the LiteLLM release notes and rerun the tests in the option READMEs before production. For production, mirror the image to Amazon ECR, enable image scanning, and point `litellm_image` at the ECR digest; the stack then also creates the ECR interface endpoints.

## Outbound traffic

The gateway needs outbound HTTPS to `mcp.atlassian.com`, `auth.atlassian.com`, your IdP's JWKS host (option A), `ghcr.io` and `pkg-containers.githubusercontent.com` (image layers; not needed when the image is in Amazon ECR) and `truststore.pki.rds.amazonaws.com` (RDS CA bundle). The tasks hold the salt key and every stored token, so in production restrict egress to those domains with [AWS Network Firewall](https://docs.aws.amazon.com/network-firewall/latest/developerguide/what-is-aws-network-firewall.html) or an egress proxy. If the Atlassian organization uses an IP allowlist, add `nat_public_ips` from the outputs.

## Validation

The stack passes `terraform fmt`, `terraform validate`, `tflint` with the AWS ruleset ([`.tflint.hcl`](.tflint.hcl)) and Checkov (run in this repository's GitHub workflow). Checks that do not apply are skipped inline, each with its reason next to the resource:

| Check | Where | Reason |
|---|---|---|
| `CKV2_AWS_57` (secret rotation) | `secrets.tf` | The salt key must never change; the master key, database secret, Atlassian client and license are rotated by hand with a planned redeploy, or by the issuer |
| `CKV2_AWS_5` (security group attached) | `alb.tf`, `bastion.tf`, `endpoints.tf` | `gateway_clients` is attached by callers outside the stack; the others are attached to count or for_each resources the check does not resolve |
| `CKV2_AWS_11`, `CKV2_AWS_12` | `network.tf` | Flow logs and the emptied default security group exist as separate resources |
| `CKV2_AWS_27`, `CKV_AWS_162`, `CKV2_AWS_8`, `CKV_AWS_353` | `database.tf` | Statement logging is DDL only so stored credentials never reach logs; password auth from Secrets Manager; Aurora backups plus final snapshot instead of an AWS Backup plan; Performance Insights is on at the cluster level, which [RDS applies to every instance](https://docs.aws.amazon.com/AmazonRDS/latest/AuroraUserGuide/USER_PerfInsights.Overview.html) |
| `CKV_AWS_18`, `CKV2_AWS_62`, `CKV_AWS_144` | `config.tf` | The config bucket holds one reviewed file that is reproducible from version control |

## Clean up

1. While the gateway is still running, delete each user's stored Atlassian token and ask the Atlassian admin to remove the gateway app's access, as described in the [main README](../../README.md#clean-up).
2. Lift deletion protection, then destroy:
   ```bash
   terraform apply -var deletion_protection=false     # Aurora and the load balancer
   terraform destroy -var deletion_protection=false
   ```
3. Aurora keeps a final snapshot named `<name>-final-<suffix>`. It is encrypted with the stack's KMS key, which enters a 30-day deletion window: to keep the snapshot, copy it with another key before the window ends (or cancel the key deletion); otherwise delete it. Secrets enter a 7-day recovery window.

## Cost

Billable while deployed: NAT gateway (hourly and per GB), internal Application Load Balancer, AWS WAF (web ACL, rules and requests), Fargate tasks, Aurora Serverless v2 (minimum 0.5 ACU per instance, 2 instances by default), Database Insights and enhanced monitoring, interface VPC endpoints (per endpoint per Availability Zone), the bastion, AWS KMS, Secrets Manager, CloudWatch Logs and Container Insights, plus Amazon Bedrock tokens. Use the [AWS Pricing Calculator](https://calculator.aws/) for an estimate. To reduce cost in a test account, set `db_instance_count = 1`, set `create_ssm_bastion = false` when you have a network path, and destroy the stack when you finish.
