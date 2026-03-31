terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Once you create the S3 bucket below, uncomment this block to store
  # Terraform state remotely (recommended for team/production use).
  # Run `terraform init -migrate-state` after enabling.
  #
  # backend "s3" {
  #   bucket = "insta-agent-tf-state-<your-account-id>"
  #   key    = "phase1/terraform.tfstate"
  #   region = "us-east-1"
  # }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "instagram-learning-agent"
      Environment = var.environment
      ManagedBy   = "terraform"
    }
  }
}
