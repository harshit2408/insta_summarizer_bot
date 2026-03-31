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
