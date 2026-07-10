terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Intentionally NOT configuring a backend block here — this bootstrap
  # config uses local state because it creates the very S3 bucket +
  # DynamoDB table the main terraform/ project will use as ITS backend.
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "sg360-bol-reconciliation"
      Environment = "dev"
      ManagedBy   = "terraform"
      Owner       = "nikhilm"
      Purpose     = "terraform-state-backend"
    }
  }
}
