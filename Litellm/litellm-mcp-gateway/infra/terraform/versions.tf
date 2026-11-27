terraform {
  # 1.11+ for write-only arguments and ephemeral resources: generated secrets never enter the state.
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 6.59, < 7.0" # 6.59+ ignores Aurora auto minor upgrades instead of planning a downgrade
    }
    random = {
      source  = "hashicorp/random"
      version = ">= 3.7, < 4.0"
    }
  }
}

provider "aws" {
  region = var.region

  # IAM tag keys are case-insensitive: a caller tag such as "project" next to the default
  # "Project" fails every IAM role with duplicate tag keys, so the caller key wins.
  default_tags {
    tags = merge(
      { for k, v in { Project = var.name, ManagedBy = "terraform" } : k => v if !contains([for t in keys(var.tags) : lower(t)], lower(k)) },
      var.tags,
    )
  }
}
