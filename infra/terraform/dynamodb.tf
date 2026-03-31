# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB — Users table
# Stores per-user settings: telegram_chat_id, google_refresh_token, preferences
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "users" {
  name         = "${var.project_name}-${var.environment}-users"
  billing_mode = var.dynamo_billing_mode
  hash_key     = "chat_id"

  attribute {
    name = "chat_id"
    type = "S"   # Telegram chat_id stored as string
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-users"
  }
}


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB — ProcessedReels table
# Stores every processed post with its AI analysis output
#
# Access patterns served by indexes:
#   1. Get by primary key            → chat_id + shortcode (table scan/get-item)
#   2. List all posts for a user     → GSI: chat_id (pk) + scraped_at (sk)
#   3. Query posts by category       → GSI: chat_id (pk) + category (sk)
#   4. Query posts by quality score  → GSI: chat_id (pk) + quality_score (sk)
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_dynamodb_table" "processed_reels" {
  name         = "${var.project_name}-${var.environment}-processed-reels"
  billing_mode = var.dynamo_billing_mode
  hash_key     = "chat_id"
  range_key    = "shortcode"

  attribute {
    name = "chat_id"
    type = "S"
  }

  attribute {
    name = "shortcode"
    type = "S"
  }

  attribute {
    name = "scraped_at"
    type = "S"   # ISO-8601 string — sortable lexicographically
  }

  attribute {
    name = "category"
    type = "S"
  }

  attribute {
    name = "quality_score"
    type = "N"
  }

  # GSI-1: list all posts for a user ordered by date (newest first)
  global_secondary_index {
    name            = "chat_id-scraped_at-index"
    hash_key        = "chat_id"
    range_key       = "scraped_at"
    projection_type = "ALL"
  }

  # GSI-2: filter posts for a user by category
  global_secondary_index {
    name            = "chat_id-category-index"
    hash_key        = "chat_id"
    range_key       = "category"
    projection_type = "ALL"
  }

  # GSI-3: filter posts for a user by quality score (for weekly digest)
  global_secondary_index {
    name            = "chat_id-quality-index"
    hash_key        = "chat_id"
    range_key       = "quality_score"
    projection_type = "ALL"
  }

  point_in_time_recovery {
    enabled = var.enable_point_in_time_recovery
  }

  tags = {
    Name = "${var.project_name}-${var.environment}-processed-reels"
  }
}
