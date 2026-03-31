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
        Resource = "*"   # tightened once SQS queues are created in Phase 1 Week 2
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
        Resource = "*"
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
