# ─────────────────────────────────────────────────────────────────────────────
# SQS — three-stage processing pipeline
#
# Stage 1: extraction  → Content Extractor Lambda (scrape + Whisper/OCR)
# Stage 2: analysis    → AI Analyzer Lambda       (Groq summarization)
# Stage 3: writer      → Google Docs Writer Lambda (append to doc)
#
# Each queue has a Dead Letter Queue (DLQ) for failed messages.
# ─────────────────────────────────────────────────────────────────────────────

# ── Dead-Letter Queues ────────────────────────────────────────────────────────

resource "aws_sqs_queue" "extraction_dlq" {
  name                      = "${var.project_name}-${var.environment}-extraction-dlq"
  message_retention_seconds = 1209600 # 14 days — give enough time to debug failures

  tags = { Name = "${var.project_name}-${var.environment}-extraction-dlq" }
}

resource "aws_sqs_queue" "analysis_dlq" {
  name                      = "${var.project_name}-${var.environment}-analysis-dlq"
  message_retention_seconds = 1209600

  tags = { Name = "${var.project_name}-${var.environment}-analysis-dlq" }
}

resource "aws_sqs_queue" "writer_dlq" {
  name                      = "${var.project_name}-${var.environment}-writer-dlq"
  message_retention_seconds = 1209600

  tags = { Name = "${var.project_name}-${var.environment}-writer-dlq" }
}

# ── Main Queues ───────────────────────────────────────────────────────────────

# Queue 1: Extraction — Orchestrator publishes here, Content Extractor consumes
# visibility_timeout must be >= Lambda timeout (300s for extractor)
resource "aws_sqs_queue" "extraction" {
  name                       = "${var.project_name}-${var.environment}-extraction"
  visibility_timeout_seconds = 360  # 6 min (extractor Lambda timeout = 5 min + buffer)
  message_retention_seconds  = 86400 # 1 day

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.extraction_dlq.arn
    # Only 2 attempts: Instagram aggressively rate-limits Lambda IPs after a
    # failed scrape, so retrying same URL too many times almost always fails
    # with "private content" anyway. Send to DLQ for manual review faster.
    maxReceiveCount = 2
  })

  tags = { Name = "${var.project_name}-${var.environment}-extraction" }
}

# Queue 2: Analysis — Content Extractor publishes here, AI Analyzer consumes
resource "aws_sqs_queue" "analysis" {
  name                       = "${var.project_name}-${var.environment}-analysis"
  visibility_timeout_seconds = 150  # 2.5 min (AI analyzer Lambda timeout = 2 min)
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.analysis_dlq.arn
    maxReceiveCount     = 3
  })

  tags = { Name = "${var.project_name}-${var.environment}-analysis" }
}

# Queue 3: Writer — AI Analyzer publishes here, Google Docs Writer consumes
resource "aws_sqs_queue" "writer" {
  name                       = "${var.project_name}-${var.environment}-writer"
  visibility_timeout_seconds = 90   # 1.5 min (writer Lambda timeout = 1 min)
  message_retention_seconds  = 86400

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.writer_dlq.arn
    maxReceiveCount     = 3
  })

  tags = { Name = "${var.project_name}-${var.environment}-writer" }
}
