variable "name" {
  description = "Prefix for resource names."
  type        = string
  default     = "litellm-mcp-gw"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,20}$", var.name))
    error_message = "Use 3 to 21 lowercase letters, digits or hyphens, starting with a letter."
  }
}

variable "region" {
  description = "AWS Region for the gateway. Must match aws_region_name in the LiteLLM config."
  type        = string
  default     = "eu-west-1"
}

variable "edition" {
  description = "enterprise = option A (IdP JWT sign-in, needs a LiteLLM Enterprise license); community = option B (per-user virtual keys)."
  type        = string

  validation {
    condition     = contains(["enterprise", "community"], var.edition)
    error_message = "edition must be \"enterprise\" or \"community\"."
  }
}

variable "litellm_config_path" {
  description = "LiteLLM config to deploy. Defaults to the config of the selected option folder."
  type        = string
  default     = null
}

variable "litellm_image" {
  description = "LiteLLM container image, pinned by digest. Mirror it to Amazon ECR for production."
  type        = string
  # ghcr.io/berriai/litellm-database:v1.109.1 (multi-architecture index)
  default = "ghcr.io/berriai/litellm-database@sha256:932c54440654f8f8466a85887612d95dcfb04ae2fc01257844332f4bf3b96986"

  validation {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", var.litellm_image))
    error_message = "Pin the image by digest (image@sha256:...), not by tag."
  }
}

variable "gateway_hostname" {
  description = "Private DNS name of the gateway, for example litellm.internal.example.com. Becomes PROXY_BASE_URL, so the Atlassian redirect URI is https://<gateway_hostname>/callback."
  type        = string

  validation {
    condition     = can(regex("^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\\.)+[a-z]{2,}$", var.gateway_hostname))
    error_message = "Use a lowercase DNS name, for example litellm.internal.example.com."
  }
}

variable "certificate_arn" {
  description = "ACM certificate for gateway_hostname in the deployment Region (AWS Private CA or imported). Every client must trust its issuing CA."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-z-]*:acm:[a-z0-9-]+:[0-9]{12}:certificate/", var.certificate_arn))
    error_message = "Use an ACM certificate ARN."
  }
}

variable "route53_private_zone_id" {
  description = "Optional Route 53 private hosted zone for gateway_hostname. When set, an alias record to the load balancer is created."
  type        = string
  default     = null
}

variable "allowed_client_cidrs" {
  description = "Private CIDR ranges allowed to reach the gateway on 443 (corporate network, VPN, peered VPCs). Agents can also use the gateway_clients security group instead."
  type        = list(string)
  default     = []

  # Each entry must sit inside RFC 1918 or 100.64.0.0/10 (shared address space), so no public
  # range (including splits such as 0.0.0.0/1) can reach the load balancer.
  validation {
    condition = alltrue([
      for cidr in var.allowed_client_cidrs : try(anytrue([
        for block in ["10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"] :
        can(cidrnetmask(cidr)) &&
        tonumber(split("/", cidr)[1]) >= tonumber(split("/", block)[1]) &&
        cidrhost("${cidrhost(cidr, 0)}/${split("/", block)[1]}", 0) == cidrhost(block, 0)
      ]), false)
    ])
    error_message = "Each entry must be an IPv4 CIDR inside 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 or 100.64.0.0/10. Public ranges are not allowed: the gateway is internal only."
  }
}

# ---- Network -------------------------------------------------------------

variable "create_vpc" {
  description = "Create a new VPC. Set to false to deploy into an existing VPC (vpc_id and private_subnet_ids)."
  type        = bool
  default     = true
}

variable "vpc_cidr" {
  description = "CIDR of the new VPC (create_vpc = true)."
  type        = string
  default     = "10.40.0.0/16"

  validation {
    condition     = can(cidrnetmask(var.vpc_cidr)) && tonumber(split("/", var.vpc_cidr)[1]) >= 16 && tonumber(split("/", var.vpc_cidr)[1]) <= 20
    error_message = "Use an IPv4 CIDR from /16 to /20 (the NAT subnets are carved as /28 from it)."
  }
}

variable "availability_zones" {
  description = "Availability Zone names to use (create_vpc = true), for example [\"eu-west-1a\", \"eu-west-1b\"]. Empty: the first az_count of the Region's zones a to f."
  type        = list(string)
  default     = []

  validation {
    condition     = alltrue([for z in var.availability_zones : can(regex("^[a-z]{2}(-[a-z]+)+-[0-9][a-z]$", z))])
    error_message = "Use Availability Zone names such as eu-west-1a."
  }
}

variable "az_count" {
  description = "Number of Availability Zones (create_vpc = true)."
  type        = number
  default     = 2

  validation {
    condition     = var.az_count >= 2 && var.az_count <= 3
    error_message = "Use 2 or 3 Availability Zones."
  }

  validation {
    condition     = length(var.availability_zones) == 0 || length(var.availability_zones) >= var.az_count
    error_message = "List at least az_count names in availability_zones."
  }
}

variable "nat_gateway_per_az" {
  description = "One NAT gateway per AZ (resilient) instead of one shared NAT gateway (cheaper)."
  type        = bool
  default     = false
}

variable "vpc_id" {
  description = "Existing VPC (create_vpc = false)."
  type        = string
  default     = null

  validation {
    condition     = var.create_vpc || try(length(var.vpc_id) > 0, false)
    error_message = "create_vpc = false needs vpc_id."
  }
}

variable "private_subnet_ids" {
  description = "Existing private subnets with outbound internet access through NAT, in at least two AZs (create_vpc = false). Used for the gateway, the load balancer and the database."
  type        = list(string)
  default     = []

  validation {
    condition     = var.create_vpc || length(var.private_subnet_ids) >= 2
    error_message = "create_vpc = false needs at least two private_subnet_ids in different AZs."
  }
}

variable "enable_interface_endpoints" {
  description = "Create interface VPC endpoints (Secrets Manager, CloudWatch Logs, KMS, STS, Bedrock Runtime, ECR when the image is in ECR, and SSM when the bastion is created) so AWS API traffic stays on the AWS network. Set to false when deploying into a VPC that already has these endpoints with private DNS."
  type        = bool
  default     = true
}

# ---- Gateway -------------------------------------------------------------

variable "desired_count" {
  description = "Number of gateway tasks. Keep 0 for the first apply, store the Atlassian client (and license) secrets, then raise it."
  type        = number
  default     = 0

  validation {
    condition     = var.desired_count >= 0 && floor(var.desired_count) == var.desired_count
    error_message = "Use a whole number, 0 or more."
  }
}

variable "task_cpu" {
  description = "Fargate task CPU units."
  type        = number
  default     = 1024

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096, 8192, 16384], var.task_cpu)
    error_message = "Use a Fargate CPU size: 256, 512, 1024, 2048, 4096, 8192 or 16384."
  }
}

variable "task_memory" {
  description = "Fargate task memory (MiB)."
  type        = number
  default     = 2048
}

variable "bedrock_inference_profile_ids" {
  description = "Bedrock cross-Region inference profiles the gateway may invoke (model ids in the LiteLLM config, without the bedrock/ prefix)."
  type        = list(string)
  default     = ["eu.anthropic.claude-sonnet-4-6"]

  validation {
    condition = length(var.bedrock_inference_profile_ids) > 0 && alltrue([
      for id in var.bedrock_inference_profile_ids : can(regex("^(global|us|us-gov|eu|apac|jp|au|ca)\\.[a-z0-9-]+\\.", id))
    ])
    error_message = "Use cross-Region inference profile IDs such as eu.anthropic.claude-sonnet-4-6, not in-Region model IDs."
  }
}

variable "bedrock_guardrail_arns" {
  description = "Optional Amazon Bedrock guardrails the gateway may apply (bedrock:ApplyGuardrail), for example to screen Confluence content in tool results."
  type        = list(string)
  default     = []
}

variable "jwt_public_key_url" {
  description = "Option A: JWKS URL of the company identity provider."
  type        = string
  default     = null

  validation {
    condition     = var.edition != "enterprise" || try(length(var.jwt_public_key_url) > 0, false)
    error_message = "edition = \"enterprise\" needs the identity provider JWKS URL (jwt_public_key_url)."
  }
}

variable "jwt_issuer" {
  description = "Option A: expected iss claim of user access tokens."
  type        = string
  default     = null

  validation {
    condition     = var.edition != "enterprise" || try(length(var.jwt_issuer) > 0, false)
    error_message = "edition = \"enterprise\" needs the identity provider issuer (jwt_issuer)."
  }
}

variable "jwt_audience" {
  description = "Option A: expected aud claim of user access tokens."
  type        = string
  default     = null

  validation {
    condition     = var.edition != "enterprise" || try(length(var.jwt_audience) > 0, false)
    error_message = "edition = \"enterprise\" needs the identity provider audience (jwt_audience)."
  }
}

variable "create_waf" {
  description = "Create the AWS WAF web ACL in waf.tf for the load balancer. Ignored when waf_web_acl_arn is set."
  type        = bool
  default     = true
}

variable "waf_web_acl_arn" {
  description = "Use an existing AWS WAF web ACL (REGIONAL) instead of the one in waf.tf. Include an equivalent of its rules."
  type        = string
  default     = null
}

variable "waf_oauth_rate_limit" {
  description = "Requests per 5 minutes per source address to the OAuth endpoints (/v1/mcp/server/oauth/*, /callback) before AWS WAF blocks."
  type        = number
  default     = 100

  validation {
    condition     = var.waf_oauth_rate_limit >= 10
    error_message = "AWS WAF rate-based rules need a limit of at least 10."
  }
}

variable "alb_access_logs_bucket" {
  description = "Optional S3 bucket for load balancer access logs. The /callback request line carries a one-time authorization code, so restrict who can read this bucket."
  type        = string
  default     = null
}

# ---- Database ------------------------------------------------------------

variable "db_engine_version" {
  description = "Aurora PostgreSQL engine version."
  type        = string
  default     = "16.15"
}

variable "db_min_capacity" {
  description = "Aurora Serverless v2 minimum ACUs."
  type        = number
  default     = 0.5
}

variable "db_max_capacity" {
  description = "Aurora Serverless v2 maximum ACUs."
  type        = number
  default     = 4

  validation {
    condition     = var.db_max_capacity >= var.db_min_capacity && var.db_max_capacity <= 256
    error_message = "db_max_capacity must be at least db_min_capacity and at most 256."
  }
}

variable "db_instance_count" {
  description = "Aurora instances (1 writer plus readers in other AZs for failover)."
  type        = number
  default     = 2

  validation {
    condition     = var.db_instance_count >= 1 && var.db_instance_count <= 3
    error_message = "Use 1 to 3 instances."
  }
}

variable "db_password_version" {
  description = "Increase to generate a new Aurora password and store it in its secret in the same apply. Then force a new ECS deployment."
  type        = number
  default     = 1
}

# ---- Operations ----------------------------------------------------------

variable "create_ssm_bastion" {
  description = "Create a private EC2 instance reachable only through SSM Session Manager, for port forwarding to the database and the gateway. No inbound rules, no public IP, no SSH key."
  type        = bool
  default     = true
}

variable "deletion_protection" {
  description = "Protect the database and the load balancer from deletion. Set to false before terraform destroy."
  type        = bool
  default     = true
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention in days."
  type        = number
  default     = 365

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653], var.log_retention_days)
    error_message = "Use a CloudWatch Logs retention value, for example 30, 90, 365 or 731."
  }
}

variable "tags" {
  description = "Extra tags for every resource."
  type        = map(string)
  default     = {}
}

variable "gateway_extra_environment" {
  description = "Extra environment variables for LiteLLM, for example SSO settings for the Admin UI (GENERIC_CLIENT_ID, GENERIC_AUTHORIZATION_ENDPOINT, ...). Never set LITELLM_LOG=DEBUG in production: debug logs include request headers and bodies."
  type        = map(string)
  default     = {}
}

variable "gateway_extra_secrets" {
  description = "Extra secrets for LiteLLM: environment variable name to Secrets Manager ARN (optionally with :json-key::), for example GENERIC_CLIENT_SECRET. Secrets encrypted with another KMS key need their own kms:Decrypt grant on the execution role."
  type        = map(string)
  default     = {}
}
