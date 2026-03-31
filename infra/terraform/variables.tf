variable "aws_region" {
  description = "AWS region to deploy all resources into"
  type        = string
  default     = "us-east-1"
}

variable "environment" {
  description = "Deployment environment label (dev | staging | prod)"
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be dev, staging, or prod."
  }
}

variable "project_name" {
  description = "Short project identifier used in resource names"
  type        = string
  default     = "insta-agent"
}

variable "media_retention_days" {
  description = "Days before raw media files are deleted from S3"
  type        = number
  default     = 7
}

variable "dynamo_billing_mode" {
  description = "DynamoDB billing mode: PAY_PER_REQUEST (default) or PROVISIONED"
  type        = string
  default     = "PAY_PER_REQUEST"
}

variable "enable_point_in_time_recovery" {
  description = "Enable DynamoDB point-in-time recovery (recommended for prod)"
  type        = bool
  default     = false   # set true in prod
}

variable "billing_alert_threshold_usd" {
  description = "USD amount at which a CloudWatch billing alarm fires"
  type        = number
  default     = 10
}

variable "alert_email" {
  description = "Email address to receive billing / error alerts"
  type        = string
  default     = ""   # set this in terraform.tfvars
}
