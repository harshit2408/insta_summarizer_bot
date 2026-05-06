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
