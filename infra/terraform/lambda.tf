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

  # Shared doc_template (needed for SectionConfig + parse_section_arg)
  source {
    content  = file("${path.module}/../../lambdas/_shared/doc_template.py")
    filename = "doc_template.py"
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
      # Empty until Phase 2 Week 4 (Google Docs) is enabled. Orchestrator
      # gracefully degrades when it's blank.
      GOOGLE_OAUTH_START_URL   = local.deploy_google_docs ? local.google_oauth_start_url : ""
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

# ─────────────────────────────────────────────────────────────────────────────
# AI Analyzer Lambda
# ─────────────────────────────────────────────────────────────────────────────
# Triggered by the SQS analysis queue. Calls Groq's Chat-Completions API,
# validates the JSON response, persists to DynamoDB, and forwards a writer
# job downstream.
#
# Packaged as a plain zip (stdlib only — no Groq SDK, no pydantic) so cold
# starts stay under 1 second and we don't need a Lambda Layer.

data "archive_file" "ai_analyzer" {
  type        = "zip"
  output_path = "${path.module}/../../lambdas/ai_analyzer/lambda.zip"

  source {
    content  = file("${path.module}/../../lambdas/ai_analyzer/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/ai_analyzer/schema.py")
    filename = "schema.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/ai_analyzer/prompts.py")
    filename = "prompts.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/ai_analyzer/groq_client.py")
    filename = "groq_client.py"
  }
}

# We only deploy the analyser when a Groq API key has been provided. This
# lets `terraform apply` succeed in fresh environments before the operator
# has set up Groq, mirroring the conditional-deploy pattern used for the
# Content Extractor (which waits on extractor_image_uri).
locals {
  deploy_ai_analyzer = var.groq_api_key != ""
}

resource "aws_lambda_function" "ai_analyzer" {
  count = local.deploy_ai_analyzer ? 1 : 0

  function_name    = "${var.project_name}-${var.environment}-ai-analyzer"
  role             = aws_iam_role.ai_analyzer[0].arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.ai_analyzer.output_path
  source_code_hash = data.archive_file.ai_analyzer.output_base64sha256

  memory_size = 512
  timeout     = 120  # 2 minutes — Groq calls usually return in <5s, leave headroom for retries

  environment {
    variables = {
      GROQ_API_KEY         = var.groq_api_key
      GROQ_MODEL           = var.groq_model
      PROMPT_VARIANT       = var.prompt_variant
      DYNAMODB_REELS_TABLE = aws_dynamodb_table.processed_reels.name
      DYNAMODB_USERS_TABLE = aws_dynamodb_table.users.name
      SQS_WRITER_QUEUE_URL = aws_sqs_queue.writer.url
      S3_BUCKET_NAME       = aws_s3_bucket.media.bucket
      LOG_LEVEL            = "INFO"
    }
  }

  tags = { Name = "${var.project_name}-${var.environment}-ai-analyzer" }

  depends_on = [aws_iam_role_policy_attachment.ai_analyzer_logs]
}

resource "aws_cloudwatch_log_group" "ai_analyzer" {
  count             = local.deploy_ai_analyzer ? 1 : 0
  name              = "/aws/lambda/${aws_lambda_function.ai_analyzer[0].function_name}"
  retention_in_days = 30
}

# SQS event source mapping — analysis queue → AI Analyzer
resource "aws_lambda_event_source_mapping" "analysis_to_analyzer" {
  count = local.deploy_ai_analyzer ? 1 : 0

  event_source_arn                   = aws_sqs_queue.analysis.arn
  function_name                      = aws_lambda_function.ai_analyzer[0].arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  function_response_types = ["ReportBatchItemFailures"]
}

# ─────────────────────────────────────────────────────────────────────────────
# OAuth Handler + Google Docs Writer (Phase 2 Week 4)
# ─────────────────────────────────────────────────────────────────────────────
# Both Lambdas are conditionally deployed: only when `google_client_id` is set,
# mirroring the AI Analyzer's `groq_api_key` gate. This lets the rest of the
# infrastructure deploy cleanly while the operator is still configuring their
# Google Cloud project.
#
# State signing secret is generated once by Terraform and persisted via
# random_password — never exported, never logged.

locals {
  # Client id/secret are sensitive; booleans derived from “non-empty?” are fine to expose
  # so downstream locals/outputs are not wrongly marked sensitive.
  deploy_google_docs = nonsensitive(
    length(trimspace(var.google_client_id)) > 0 &&
    length(trimspace(var.google_client_secret)) > 0
  )

  # Default redirect URI — points at API Gateway. Operators can override
  # in tfvars (useful when keeping a stable URL across recreates).
  google_oauth_callback_url = (
    var.google_oauth_redirect_uri_override != ""
    ? var.google_oauth_redirect_uri_override
    : "${trimsuffix(aws_apigatewayv2_stage.webhook.invoke_url, "/")}/oauth/callback"
  )

  google_oauth_start_url = "${trimsuffix(aws_apigatewayv2_stage.webhook.invoke_url, "/")}/oauth/start"
}

resource "random_password" "oauth_state_secret" {
  count   = local.deploy_google_docs ? 1 : 0
  length  = 48
  special = false  # base64-friendly chars; HMAC takes any bytes
}

# ── OAuth Handler Lambda zip ──────────────────────────────────────────────────

data "archive_file" "oauth_handler" {
  count       = local.deploy_google_docs ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/../../lambdas/oauth_handler/lambda.zip"

  source {
    content  = file("${path.module}/../../lambdas/oauth_handler/handler.py")
    filename = "handler.py"
  }
  # Shared utilities — bundled flat at the zip root so they import as
  # top-level modules in the Lambda runtime.
  source {
    content  = file("${path.module}/../../lambdas/_shared/google_oauth.py")
    filename = "google_oauth.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/kms_helper.py")
    filename = "kms_helper.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/telegram.py")
    filename = "telegram.py"
  }
}

resource "aws_lambda_function" "oauth_handler" {
  count = local.deploy_google_docs ? 1 : 0

  function_name    = "${var.project_name}-${var.environment}-oauth-handler"
  role             = aws_iam_role.oauth_handler[0].arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.oauth_handler[0].output_path
  source_code_hash = data.archive_file.oauth_handler[0].output_base64sha256

  memory_size = 256
  timeout     = 15

  environment {
    variables = {
      GOOGLE_CLIENT_ID     = var.google_client_id
      GOOGLE_CLIENT_SECRET = var.google_client_secret
      GOOGLE_REDIRECT_URI  = local.google_oauth_callback_url
      OAUTH_STATE_SECRET   = random_password.oauth_state_secret[0].result
      DYNAMODB_USERS_TABLE = aws_dynamodb_table.users.name
      KMS_KEY_ID           = aws_kms_key.google_tokens.arn
      TELEGRAM_BOT_TOKEN   = var.telegram_bot_token
      LOG_LEVEL            = "INFO"
    }
  }

  tags = { Name = "${var.project_name}-${var.environment}-oauth-handler" }

  depends_on = [aws_iam_role_policy_attachment.oauth_handler_logs]
}

resource "aws_cloudwatch_log_group" "oauth_handler" {
  count             = local.deploy_google_docs ? 1 : 0
  name              = "/aws/lambda/${aws_lambda_function.oauth_handler[0].function_name}"
  retention_in_days = 30
}

# ── API Gateway routes for /oauth/start and /oauth/callback ─────────────────

resource "aws_apigatewayv2_integration" "oauth_handler" {
  count                  = local.deploy_google_docs ? 1 : 0
  api_id                 = aws_apigatewayv2_api.webhook.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.oauth_handler[0].invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "oauth_start" {
  count     = local.deploy_google_docs ? 1 : 0
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "GET /oauth/start"
  target    = "integrations/${aws_apigatewayv2_integration.oauth_handler[0].id}"
}

resource "aws_apigatewayv2_route" "oauth_callback" {
  count     = local.deploy_google_docs ? 1 : 0
  api_id    = aws_apigatewayv2_api.webhook.id
  route_key = "GET /oauth/callback"
  target    = "integrations/${aws_apigatewayv2_integration.oauth_handler[0].id}"
}

resource "aws_lambda_permission" "apigw_oauth_handler" {
  count         = local.deploy_google_docs ? 1 : 0
  statement_id  = "AllowAPIGatewayInvokeOAuth"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.oauth_handler[0].function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.webhook.execution_arn}/*/*"
}

# ── Google Docs Writer Lambda zip ─────────────────────────────────────────────

data "archive_file" "google_docs_writer" {
  count       = local.deploy_google_docs ? 1 : 0
  type        = "zip"
  output_path = "${path.module}/../../lambdas/google_docs_writer/lambda.zip"

  source {
    content  = file("${path.module}/../../lambdas/google_docs_writer/handler.py")
    filename = "handler.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/google_oauth.py")
    filename = "google_oauth.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/google_docs.py")
    filename = "google_docs.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/doc_template.py")
    filename = "doc_template.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/kms_helper.py")
    filename = "kms_helper.py"
  }
  source {
    content  = file("${path.module}/../../lambdas/_shared/telegram.py")
    filename = "telegram.py"
  }
}

resource "aws_lambda_function" "google_docs_writer" {
  count = local.deploy_google_docs ? 1 : 0

  function_name    = "${var.project_name}-${var.environment}-docs-writer"
  role             = aws_iam_role.google_docs_writer[0].arn
  runtime          = "python3.11"
  handler          = "handler.lambda_handler"
  filename         = data.archive_file.google_docs_writer[0].output_path
  source_code_hash = data.archive_file.google_docs_writer[0].output_base64sha256

  memory_size = 512
  timeout     = 60   # 1 minute — Google Docs API calls are usually <2s

  environment {
    variables = {
      GOOGLE_CLIENT_ID       = var.google_client_id
      GOOGLE_CLIENT_SECRET   = var.google_client_secret
      GOOGLE_OAUTH_START_URL = local.google_oauth_start_url
      DYNAMODB_USERS_TABLE   = aws_dynamodb_table.users.name
      DYNAMODB_REELS_TABLE   = aws_dynamodb_table.processed_reels.name
      KMS_KEY_ID             = aws_kms_key.google_tokens.arn
      TELEGRAM_BOT_TOKEN     = var.telegram_bot_token
      LOG_LEVEL              = "INFO"
    }
  }

  tags = { Name = "${var.project_name}-${var.environment}-docs-writer" }

  depends_on = [aws_iam_role_policy_attachment.google_docs_writer_logs]
}

resource "aws_cloudwatch_log_group" "google_docs_writer" {
  count             = local.deploy_google_docs ? 1 : 0
  name              = "/aws/lambda/${aws_lambda_function.google_docs_writer[0].function_name}"
  retention_in_days = 30
}

# SQS event source mapping — writer queue → Google Docs Writer Lambda
resource "aws_lambda_event_source_mapping" "writer_to_docs_writer" {
  count = local.deploy_google_docs ? 1 : 0

  event_source_arn                   = aws_sqs_queue.writer.arn
  function_name                      = aws_lambda_function.google_docs_writer[0].arn
  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  function_response_types = ["ReportBatchItemFailures"]
}
