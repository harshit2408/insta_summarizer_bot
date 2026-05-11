# ─────────────────────────────────────────────────────────────────────────────
# IAM — Lambda execution roles (one per Lambda function)
#
# Each role follows the principle of least privilege:
# only the exact permissions needed are granted.
# ─────────────────────────────────────────────────────────────────────────────

# Shared assume-role policy — all Lambdas use the same trust policy
data "aws_iam_policy_document" "lambda_assume_role" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# ── Lambda basic execution policy (CloudWatch Logs) ───────────────────────────
# Attach this to every Lambda role.

resource "aws_iam_policy" "lambda_basic_logs" {
  name        = "${var.project_name}-${var.environment}-lambda-basic-logs"
  description = "Allow Lambda to write logs to CloudWatch"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents"
        ]
        Resource = "arn:aws:logs:*:*:*"
      }
    ]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Role: Orchestrator Lambda
# Needs: DynamoDB Users (read/write), SQS send, SSM/Secrets read
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "orchestrator" {
  name               = "${var.project_name}-${var.environment}-orchestrator-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "orchestrator_logs" {
  role       = aws_iam_role.orchestrator.name
  policy_arn = aws_iam_policy.lambda_basic_logs.arn
}

resource "aws_iam_role_policy" "orchestrator_inline" {
  name = "orchestrator-inline"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBUsers"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        Resource = aws_dynamodb_table.users.arn
      },
      {
        Sid    = "DynamoDBReelsRead"
        Effect = "Allow"
        Action = ["dynamodb:GetItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.processed_reels.arn,
          "${aws_dynamodb_table.processed_reels.arn}/index/*"
        ]
      },
      {
        Sid    = "SQSSend"
        Effect = "Allow"
        Action = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.extraction.arn
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:${var.project_name}/*"
      }
    ]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Role: Content Extractor Lambda
# Needs: S3 read/write (temp prefix), SQS receive/delete/send, DynamoDB reels write
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "content_extractor" {
  name               = "${var.project_name}-${var.environment}-extractor-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "extractor_logs" {
  role       = aws_iam_role.content_extractor.name
  policy_arn = aws_iam_policy.lambda_basic_logs.arn
}

resource "aws_iam_role_policy" "extractor_inline" {
  name = "extractor-inline"
  role = aws_iam_role.content_extractor.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "S3ReadWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
          "s3:ListBucket"
        ]
        Resource = [
          aws_s3_bucket.media.arn,
          "${aws_s3_bucket.media.arn}/*"
        ]
      },
      {
        Sid    = "SQSExtraction"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
          "sqs:SendMessage"
        ]
        Resource = [
          aws_sqs_queue.extraction.arn,
          aws_sqs_queue.analysis.arn
        ]
      },
      {
        Sid    = "SecretsRead"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:${var.project_name}/*"
      }
    ]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Role: AI Analyzer Lambda
# Needs:
#   - SQS analysis (receive/delete) and writer (send)
#   - DynamoDB ProcessedReels write
#   - S3 read on extracted JSON, write on analysis JSON (audit dump)
#   - Secrets Manager read (Groq key — when migrated off env vars)
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "ai_analyzer" {
  count              = local.deploy_ai_analyzer ? 1 : 0
  name               = "${var.project_name}-${var.environment}-ai-analyzer-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "ai_analyzer_logs" {
  count      = local.deploy_ai_analyzer ? 1 : 0
  role       = aws_iam_role.ai_analyzer[0].name
  policy_arn = aws_iam_policy.lambda_basic_logs.arn
}

resource "aws_iam_role_policy" "ai_analyzer_inline" {
  count = local.deploy_ai_analyzer ? 1 : 0
  name  = "ai-analyzer-inline"
  role  = aws_iam_role.ai_analyzer[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSAnalysisReceive"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.analysis.arn
      },
      {
        Sid      = "SQSWriterSend"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage"]
        Resource = aws_sqs_queue.writer.arn
      },
      {
        Sid    = "DynamoDBReelsWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:GetItem"
        ]
        Resource = [
          aws_dynamodb_table.processed_reels.arn,
          "${aws_dynamodb_table.processed_reels.arn}/index/*"
        ]
      },
      {
        Sid    = "DynamoDBUsersRead"
        Effect = "Allow"
        Action = ["dynamodb:GetItem"]
        Resource = aws_dynamodb_table.users.arn
      },
      {
        Sid    = "S3ExtractedReadAnalysisWrite"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject"
        ]
        Resource = "${aws_s3_bucket.media.arn}/users/*/extracted/*"
      },
      {
        Sid      = "SecretsRead"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${var.aws_region}:*:secret:${var.project_name}/*"
      }
    ]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Role: OAuth Handler Lambda  (Phase 2 Week 4)
# Needs:
#   - DynamoDB Users (write encrypted token + onboarding flag)
#   - KMS Encrypt on the google_tokens key
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "oauth_handler" {
  count              = local.deploy_google_docs ? 1 : 0
  name               = "${var.project_name}-${var.environment}-oauth-handler-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "oauth_handler_logs" {
  count      = local.deploy_google_docs ? 1 : 0
  role       = aws_iam_role.oauth_handler[0].name
  policy_arn = aws_iam_policy.lambda_basic_logs.arn
}

resource "aws_iam_role_policy" "oauth_handler_inline" {
  count = local.deploy_google_docs ? 1 : 0
  name  = "oauth-handler-inline"
  role  = aws_iam_role.oauth_handler[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBUsersWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.users.arn
      },
      {
        Sid      = "KMSEncrypt"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:GenerateDataKey", "kms:DescribeKey"]
        Resource = aws_kms_key.google_tokens.arn
      }
    ]
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Role: Google Docs Writer Lambda  (Phase 2 Week 4)
# Needs:
#   - SQS writer (receive/delete) — DLQ permissions handled by SQS service role
#   - DynamoDB Users (read encrypted token, write google_docs_id + counters)
#   - DynamoDB ProcessedReels (UpdateItem to mark status=completed)
#   - KMS Decrypt on the google_tokens key
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_iam_role" "google_docs_writer" {
  count              = local.deploy_google_docs ? 1 : 0
  name               = "${var.project_name}-${var.environment}-docs-writer-role"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume_role.json
}

resource "aws_iam_role_policy_attachment" "google_docs_writer_logs" {
  count      = local.deploy_google_docs ? 1 : 0
  role       = aws_iam_role.google_docs_writer[0].name
  policy_arn = aws_iam_policy.lambda_basic_logs.arn
}

resource "aws_iam_role_policy" "google_docs_writer_inline" {
  count = local.deploy_google_docs ? 1 : 0
  name  = "google-docs-writer-inline"
  role  = aws_iam_role.google_docs_writer[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SQSWriterReceive"
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.writer.arn
      },
      {
        Sid    = "DynamoDBUsersRW"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:UpdateItem"
        ]
        Resource = aws_dynamodb_table.users.arn
      },
      {
        Sid    = "DynamoDBReelsUpdate"
        Effect = "Allow"
        Action = [
          "dynamodb:UpdateItem",
          "dynamodb:GetItem"
        ]
        Resource = aws_dynamodb_table.processed_reels.arn
      },
      {
        Sid      = "KMSDecrypt"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:DescribeKey"]
        Resource = aws_kms_key.google_tokens.arn
      }
    ]
  })
}
