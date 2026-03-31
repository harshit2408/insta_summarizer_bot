# ─────────────────────────────────────────────────────────────────────────────
# S3 — media bucket
#
# Folder layout (set by Lambda code, not enforced here):
#   users/{chat_id}/temp/{shortcode}/     ← raw downloads (auto-deleted)
#   users/{chat_id}/extracted/{shortcode}/  ← JSON with transcription/OCR
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "media" {
  # Bucket names must be globally unique — the account id suffix ensures that.
  bucket        = "${var.project_name}-${var.environment}-media-${data.aws_caller_identity.current.account_id}"
  force_destroy = var.environment != "prod"   # safe delete in dev/staging

  tags = {
    Name = "${var.project_name}-${var.environment}-media"
  }
}

data "aws_caller_identity" "current" {}

# Block ALL public access — media is only accessed by Lambda via signed URLs
resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enable versioning (supports point-in-time recovery for extracted JSON)
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption with AWS-managed keys (no extra cost)
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Lifecycle rules
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  # Rule 1: delete raw temporary media after N days (free up space)
  rule {
    id     = "delete-temp-media"
    status = "Enabled"

    filter {
      prefix = "users/"   # applies to all user uploads
    }

    # Delete non-current versions quickly (versioning is on)
    noncurrent_version_expiration {
      noncurrent_days = 1
    }

    expiration {
      days = var.media_retention_days
    }
  }

  # Rule 2: transition extracted JSON to Glacier after 365 days
  rule {
    id     = "archive-extracted-json"
    status = "Enabled"

    filter {
      prefix = "users/"
    }

    transition {
      days          = 365
      storage_class = "GLACIER"
    }
  }
}
