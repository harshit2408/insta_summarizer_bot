output "s3_media_bucket_name" {
  description = "Name of the S3 media bucket (set this in your Lambda env vars)"
  value       = aws_s3_bucket.media.bucket
}

output "s3_media_bucket_arn" {
  value = aws_s3_bucket.media.arn
}

output "dynamodb_users_table_name" {
  description = "DynamoDB Users table name (DYNAMODB_USERS_TABLE env var)"
  value       = aws_dynamodb_table.users.name
}

output "dynamodb_reels_table_name" {
  description = "DynamoDB ProcessedReels table name (DYNAMODB_REELS_TABLE env var)"
  value       = aws_dynamodb_table.processed_reels.name
}

output "orchestrator_role_arn" {
  description = "IAM role ARN for the Orchestrator Lambda"
  value       = aws_iam_role.orchestrator.arn
}

output "content_extractor_role_arn" {
  description = "IAM role ARN for the Content Extractor Lambda"
  value       = aws_iam_role.content_extractor.arn
}

output "alerts_sns_topic_arn" {
  description = "SNS topic ARN for billing/error alerts"
  value       = aws_sns_topic.alerts.arn
}

# ── SQS outputs ───────────────────────────────────────────────────────────────

output "sqs_extraction_queue_url" {
  description = "SQS extraction queue URL (SQS_EXTRACTION_QUEUE_URL env var)"
  value       = aws_sqs_queue.extraction.url
}

output "sqs_analysis_queue_url" {
  description = "SQS analysis queue URL (SQS_ANALYSIS_QUEUE_URL env var)"
  value       = aws_sqs_queue.analysis.url
}

output "sqs_writer_queue_url" {
  description = "SQS writer queue URL (SQS_WRITER_QUEUE_URL env var)"
  value       = aws_sqs_queue.writer.url
}

output "sqs_extraction_dlq_url" {
  description = "Extraction Dead Letter Queue URL"
  value       = aws_sqs_queue.extraction_dlq.url
}

# ── ECR outputs ───────────────────────────────────────────────────────────────

output "ecr_content_extractor_repository_url" {
  description = "ECR repository URL for the Content Extractor image (use in deploy.ps1)"
  value       = aws_ecr_repository.content_extractor.repository_url
}

# ── Lambda / API Gateway outputs ─────────────────────────────────────────────

output "orchestrator_function_name" {
  description = "Orchestrator Lambda function name"
  value       = aws_lambda_function.orchestrator.function_name
}

output "content_extractor_function_name" {
  description = "Content Extractor Lambda function name (empty until first docker push + apply)"
  value       = local.deploy_extractor ? aws_lambda_function.content_extractor[0].function_name : ""
}

output "telegram_webhook_url" {
  description = "POST this URL to Telegram setWebhook API to register the bot webhook"
  value       = "${trimsuffix(aws_apigatewayv2_stage.webhook.invoke_url, "/")}/webhook"
}

# ── AI Analyzer outputs ─────────────────────────────────────

output "ai_analyzer_function_name" {
  description = "AI Analyzer Lambda function name (empty until groq_api_key is set)"
  value       = local.deploy_ai_analyzer ? aws_lambda_function.ai_analyzer[0].function_name : ""
  sensitive   = true
}

output "ai_analyzer_role_arn" {
  description = "IAM role ARN for the AI Analyzer Lambda"
  value       = local.deploy_ai_analyzer ? aws_iam_role.ai_analyzer[0].arn : ""
  sensitive   = true
}

# ── Google Docs / OAuth (Phase 2 Week 4) ─────────────────────────────────────

output "kms_google_tokens_key_arn" {
  description = "KMS key ARN used to encrypt Google OAuth refresh tokens"
  value       = aws_kms_key.google_tokens.arn
}

output "kms_google_tokens_alias" {
  description = "KMS alias for the Google tokens key"
  value       = aws_kms_alias.google_tokens.name
}

output "oauth_handler_function_name" {
  description = "OAuth Handler Lambda function name (empty until google_client_id is set)"
  value       = local.deploy_google_docs ? aws_lambda_function.oauth_handler[0].function_name : ""
  sensitive   = true # Lambda carries sensitive OAuth env vars; provider propagates sensitivity
}

output "google_docs_writer_function_name" {
  description = "Google Docs Writer Lambda function name (empty until google_client_id is set)"
  value       = local.deploy_google_docs ? aws_lambda_function.google_docs_writer[0].function_name : ""
  sensitive   = true
}

output "google_oauth_start_url" {
  description = "Public URL the bot uses to send users to Google consent — paste this in BotFather welcome message if you like"
  value       = local.deploy_google_docs ? local.google_oauth_start_url : ""
}

output "google_oauth_callback_url" {
  description = "Add this URL to your Google Cloud OAuth Client → Authorized redirect URIs"
  value       = local.deploy_google_docs ? local.google_oauth_callback_url : ""
}