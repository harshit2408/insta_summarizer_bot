# ─────────────────────────────────────────────────────────────────────────────
# Lambda — Orchestrator + Content Extractor
#
# Deployment model:
#   Orchestrator      → zip archive (no heavy deps, only stdlib + boto3)
#   Content Extractor → Docker container image (Whisper + EasyOCR + ffmpeg)
#
# Run `scripts/deploy.ps1` to build, package, and apply this configuration.
# ─────────────────────────────────────────────────────────────────────────────

# ── Orchestrator Lambda ───────────────────────────────────────────────────────
# Triggered by API Gateway (Telegram webhook).
# Validates URL, checks duplicates, publishes to SQS extraction queue.

data "archive_file" "orchestrator" {
  type        = "zip"
  output_path = "${path.module}/../../lambdas/orchestrator/lambda.zip"

  # Main handler
  source {
    content  = file("${path.module}/../../lambdas/orchestrator/handler.py")
    filename = "handler.py"
  }

  # Shared utilities (no external deps — only stdlib)
  source {
    content  = file("${path.module}/../../utils/__init__.py")
    filename = "utils/__init__.py"
  }
  source {
    content  = file("${path.module}/../../utils/helpers.py")
    filename = "utils/helpers.py"
  }
}

resource "aws_lambda_function" "orchestrator" {
  function_name    = "${var.project_name}-${var.environment}-orchestrator"
  role             = aws_iam_role.orchestrator.arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.orchestrator.output_path
  source_code_hash = data.archive_file.orchestrator.output_base64sha256

  memory_size = 256
  timeout     = 10

  environment {
    variables = {
      TELEGRAM_BOT_TOKEN       = var.telegram_bot_token
      DYNAMODB_USERS_TABLE     = aws_dynamodb_table.users.name
      DYNAMODB_REELS_TABLE     = aws_dynamodb_table.processed_reels.name
      SQS_EXTRACTION_QUEUE_URL = aws_sqs_queue.extraction.url
      LOG_LEVEL                = "INFO"
    }
  }

  tags = { Name = "${var.project_name}-${var.environment}-orchestrator" }

  depends_on = [aws_iam_role_policy_attachment.orchestrator_logs]
}

# CloudWatch Log Group for orchestrator (explicit so we control retention)
resource "aws_cloudwatch_log_group" "orchestrator" {
  name              = "/aws/lambda/${aws_lambda_function.orchestrator.function_name}"
  retention_in_days = 30
}

# ── API Gateway → Orchestrator (Telegram webhook) ────────────────────────────

resource "aws_apigatewayv2_api" "webhook" {
  name          = "${var.project_name}-${var.environment}-webhook"
  protocol_type = "HTTP"
  description   = "Receives Telegram webhook POST requests"
}

resource "aws_apigatewayv2_stage" "webhook" {
  api_id      = aws_apigatewayv2_api.webhook.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "orchestrator" {
  api_id             = aws_apigatewayv2_api.webhook.id
  integration_type   = "AWS_PROXY"
  integration_uri    = aws_lambda_function.orchestrator.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "webhook_post" {
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "POST /webhook"
  target    = "integrations/${aws_apigatewayv2_integration.orchestrator.id}"
}

resource "aws_lambda_permission" "apigw_orchestrator" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.orchestrator.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}

# ── Content Extractor Lambda ──────────────────────────────────────────────────
# Triggered by SQS extraction queue.
# Downloads media, transcribes audio (Whisper), extracts text (EasyOCR), saves to S3.
#
# NOTE: This resource is only created when extractor_image_uri is set.
# Run deploy.ps1 which builds the Docker image, pushes it to ECR, then calls
# terraform apply — the variable is auto-populated in terraform.tfvars.

locals {
  deploy_extractor = var.extractor_image_uri != ""
}

resource "aws_lambda_function" "content_extractor" {
  count = local.deploy_extractor ? 1 : 0

  function_name = "${var.project_name}-${var.environment}-content-extractor"
  role          = aws_iam_role.content_extractor.arn
  package_type  = "Image"
  image_uri     = var.extractor_image_uri

  memory_size = 3008  # 3 GB — Whisper + EasyOCR need significant RAM
  timeout     = 300   # 5 minutes

  ephemeral_storage {
    size = 2048 # 2 GB /tmp for downloaded media
  }

  environment {
    variables = {
      S3_BUCKET_NAME         = aws_s3_bucket.media.bucket
      SQS_ANALYSIS_QUEUE_URL = aws_sqs_queue.analysis.url
      WHISPER_MODEL_SIZE     = "base"
      LOG_LEVEL              = "INFO"
      # HuggingFace Hub writes commit hashes and lock files to HF_HOME.
      # /tmp is the only writable directory in Lambda — redirect cache there.
      HF_HOME                = "/tmp/hf_cache"
    }
  }

  tags = { Name = "${var.project_name}-${var.environment}-content-extractor" }

  depends_on = [aws_iam_role_policy_attachment.extractor_logs]

  lifecycle {
    # image_uri is managed by the deploy script; prevent drift from manual pushes
    ignore_changes = [image_uri]
  }
}

resource "aws_cloudwatch_log_group" "content_extractor" {
  count             = local.deploy_extractor ? 1 : 0
  name              = "/aws/lambda/${aws_lambda_function.content_extractor[0].function_name}"
  retention_in_days = 30
}

# SQS event source mapping — Lambda polls the extraction queue automatically
resource "aws_lambda_event_source_mapping" "extraction_to_extractor" {
  count = local.deploy_extractor ? 1 : 0

  event_source_arn                   = aws_sqs_queue.extraction.arn
  function_name                      = aws_lambda_function.content_extractor[0].arn
  batch_size                         = 1  # one URL per invocation (heavy processing)
  maximum_batching_window_in_seconds = 0

  function_response_types = ["ReportBatchItemFailures"]
}
