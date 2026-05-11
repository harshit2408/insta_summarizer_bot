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

variable "telegram_bot_token" {
  description = "Telegram Bot API token (from BotFather) — set in terraform.tfvars, never commit"
  type        = string
  sensitive   = true
  default     = ""
}

variable "extractor_image_uri" {
  description = "ECR image URI for the Content Extractor Lambda (set by deploy.ps1 after docker push)"
  type        = string
  default     = ""
}

# ── AI Analyzer ──────────────────────────────────────────────

variable "groq_api_key" {
  description = "Groq Cloud API key — leave empty to skip deploying the AI Analyzer Lambda."
  type        = string
  sensitive   = true
  default     = ""
}

variable "groq_model" {
  description = "Groq model id used for analysis (e.g. llama-3.3-70b-versatile)."
  type        = string
  default     = "llama-3.3-70b-versatile"
}

variable "prompt_variant" {
  description = "Active prompt variant for the AI Analyzer (v1 = concise, v2 = chain-of-thought)."
  type        = string
  default     = "v1"

  validation {
    condition     = contains(["v1", "v2"], var.prompt_variant)
    error_message = "prompt_variant must be 'v1' or 'v2'."
  }
}

# ── Google Docs / OAuth ──────────────────────────────────────────────────────

variable "google_client_id" {
  description = "Google Cloud OAuth 2.0 Client ID (Web application). Leave empty to skip deploying the Google Docs Writer + OAuth Lambdas."
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_client_secret" {
  description = "Google Cloud OAuth 2.0 Client secret matching google_client_id."
  type        = string
  sensitive   = true
  default     = ""
}

variable "google_oauth_redirect_uri_override" {
  description = "Optional override for the Google OAuth redirect URI. When empty the API Gateway-generated /oauth/callback URL is used. Set this to keep a stable redirect across redeploys."
  type        = string
  default     = ""
}
