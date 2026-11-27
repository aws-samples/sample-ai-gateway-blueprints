# AWS WAF on the internal load balancer. Created by default; pass waf_web_acl_arn to use your own.
#  - The gateway's per-server /token endpoint answers any caller that holds a refresh token and
#    returns raw Atlassian tokens. The gateway refreshes stored tokens itself and never uses it,
#    so refresh grants through it are blocked.
#  - The OAuth endpoints are rate-limited per source address.
#  - AWS managed known bad inputs rules (including Log4j patterns).
# Paths are URL-decoded, normalized and lowercased before matching, so encoded paths cannot skip a rule.
# Logs redact the query string (one-time codes) and the Authorization header.

locals {
  create_waf      = var.waf_web_acl_arn == null && var.create_waf
  waf_web_acl_arn = var.waf_web_acl_arn != null ? var.waf_web_acl_arn : (local.create_waf ? aws_wafv2_web_acl.this[0].arn : null)
}

resource "aws_wafv2_web_acl" "this" {
  count       = local.create_waf ? 1 : 0
  name        = var.name
  description = "LiteLLM MCP gateway"
  scope       = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "block-refresh-through-gateway-token-endpoint"
    priority = 10

    action {
      block {}
    }

    statement {
      and_statement {
        statement {
          byte_match_statement {
            search_string         = "/token"
            positional_constraint = "ENDS_WITH"
            field_to_match {
              uri_path {}
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "NORMALIZE_PATH"
            }
            text_transformation {
              priority = 2
              type     = "LOWERCASE"
            }
          }
        }
        statement {
          byte_match_statement {
            search_string         = "refresh_token"
            positional_constraint = "CONTAINS"
            field_to_match {
              body {
                oversize_handling = "MATCH"
              }
            }
            text_transformation {
              priority = 0
              type     = "URL_DECODE"
            }
            text_transformation {
              priority = 1
              type     = "LOWERCASE"
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-block-refresh"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "rate-limit-oauth-endpoints"
    priority = 20

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit                 = var.waf_oauth_rate_limit
        evaluation_window_sec = 300
        aggregate_key_type    = "IP"

        scope_down_statement {
          or_statement {
            statement {
              byte_match_statement {
                search_string         = "/v1/mcp/server/oauth/"
                positional_constraint = "STARTS_WITH"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
                text_transformation {
                  priority = 1
                  type     = "NORMALIZE_PATH"
                }
                text_transformation {
                  priority = 2
                  type     = "LOWERCASE"
                }
              }
            }
            statement {
              byte_match_statement {
                search_string         = "/callback"
                positional_constraint = "STARTS_WITH"
                field_to_match {
                  uri_path {}
                }
                text_transformation {
                  priority = 0
                  type     = "URL_DECODE"
                }
                text_transformation {
                  priority = 1
                  type     = "NORMALIZE_PATH"
                }
                text_transformation {
                  priority = 2
                  type     = "LOWERCASE"
                }
              }
            }
          }
        }
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-rate-oauth"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "aws-known-bad-inputs"
    priority = 30

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        vendor_name = "AWS"
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${var.name}-known-bad-inputs"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = var.name
    sampled_requests_enabled   = true
  }
}

# WAF requires the aws-waf-logs- prefix for CloudWatch Logs destinations.
resource "aws_cloudwatch_log_group" "waf" {
  count             = local.create_waf ? 1 : 0
  name              = "aws-waf-logs-${var.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
}

resource "aws_wafv2_web_acl_logging_configuration" "this" {
  count                   = local.create_waf ? 1 : 0
  resource_arn            = aws_wafv2_web_acl.this[0].arn
  log_destination_configs = [aws_cloudwatch_log_group.waf[0].arn]

  redacted_fields {
    query_string {}
  }

  redacted_fields {
    single_header {
      name = "authorization"
    }
  }
}

resource "aws_wafv2_web_acl_association" "this" {
  count        = var.waf_web_acl_arn != null || var.create_waf ? 1 : 0
  resource_arn = aws_lb.this.arn
  web_acl_arn  = local.waf_web_acl_arn
}
