terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # No backend block yet — using local state to draft and validate this
  # configuration while the IAM permissions needed to actually create these
  # resources are still pending (see the Stage 1 plan file). Once granted, add:
  #
  #   backend "s3" {
  #     bucket       = "sg360-bol-tfstate-610614956027"
  #     key          = "main/terraform.tfstate"
  #     region       = "us-east-1"
  #     encrypt      = true
  #     use_lockfile = true  # native S3 locking (Terraform 1.10+) — no DynamoDB table needed
  #   }
  #
  # then run `terraform init -migrate-state` to move this local state file
  # into the shared backend.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "sg360-bol-reconciliation"
      Environment = "dev"
      ManagedBy   = "terraform"
      Owner       = "nikhilm"
      Purpose     = "app-deployment"
    }
  }
}
