# ─────────────────────────────────────────────────────────────────────────────
# KMS — Customer-managed key for encrypting Google OAuth refresh tokens
#
# Used by:
#   * OAuth Callback Lambda  → kms:Encrypt (store on user record)
#   * Google Docs Writer     → kms:Decrypt (refresh access token before each call)
#
# A per-user EncryptionContext ({"chat_id": "<id>"}) binds each ciphertext to
# the user it was created for, so a leaked DynamoDB row cannot be replayed
# against a different user even with kms:Decrypt access.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_kms_key" "google_tokens" {
  description             = "Encrypts Google OAuth refresh tokens for ${var.project_name}-${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  tags = {
    Name = "${var.project_name}-${var.environment}-google-tokens"
  }
}

resource "aws_kms_alias" "google_tokens" {
  name          = "alias/${var.project_name}-${var.environment}-google-tokens"
  target_key_id = aws_kms_key.google_tokens.key_id
}
