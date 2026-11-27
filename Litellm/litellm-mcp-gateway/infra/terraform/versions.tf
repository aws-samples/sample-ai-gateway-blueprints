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

  default_tags {
    tags = merge({ Project = var.name, ManagedBy = "terraform" }, var.tags)
  }
}
