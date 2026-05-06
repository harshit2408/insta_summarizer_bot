"""
Orchestrator Lambda — entry point for the Telegram bot webhook.

Responsibilities:
  1. Parse incoming Telegram update (message / callback_query)
  2. Register new users in DynamoDB on first contact
  3. Validate Instagram URL
  4. Check for duplicate processing (DynamoDB GetItem)
  5. Publish job to SQS extraction queue
  6. Send acknowledgment back to user via Telegram Bot API

Triggered by:  API Gateway HTTP POST /webhook
Runtime:       Python 3.11 (zip — no heavy dependencies)
Dependencies:  boto3 (built-in to Lambda), urllib (stdlib)
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
import urllib.parse
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# Shared utils are bundled into the zip by Terraform archive_file
from utils.helpers import is_valid_instagram_url, extract_shortcode, normalize_instagram_url

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── AWS clients (module-level for connection reuse across warm invocations) ───
_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)
_sqs = boto3.client("sqs", region_name=_region)

# ── Environment variables ─────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DYNAMODB_USERS_TABLE = os.environ["DYNAMODB_USERS_TABLE"]
DYNAMODB_REELS_TABLE = os.environ["DYNAMODB_REELS_TABLE"]
SQS_EXTRACTION_QUEUE_URL = os.environ["SQS_EXTRACTION_QUEUE_URL"]

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HELP_TEXT = (
    "Send me any Instagram reel or post link and I'll:\n"
    "• Transcribe the audio (Whisper AI)\n"
    "• Extract text from images (OCR)\n"
    "• Generate a summary with key takeaways\n"
    "• Save everything to your Google Docs\n\n"
    "Commands:\n"
    "/start — Welcome message\n"
    "/help  — Show this help"
)


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """API Gateway HTTP → Lambda proxy integration (payload format 2.0)."""
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        logger.warning("Received non-JSON body")
        return _ok()

    update_id = body.get("update_id")
    logger.info("Processing Telegram update_id=%s", update_id)

    message = body.get("message") or body.get("edited_message")
    if not message:
        # Ignore non-message updates (polls, reactions, etc.)
        return _ok()

    chat_id = str(message["chat"]["id"])
    text = (message.get("text") or "").strip()
    user_info = message.get("from", {})

    try:
        _handle_message(chat_id, text, user_info)
    except Exception:
        logger.exception("Unhandled error for chat_id=%s text=%r", chat_id, text)
        send_message(chat_id, "Something went wrong on our end. Please try again in a moment.")

    # Always return 200 to Telegram — otherwise it retries indefinitely
    return _ok()


# ─────────────────────────────────────────────────────────────────────────────
# Core message routing
# ─────────────────────────────────────────────────────────────────────────────

def _handle_message(chat_id: str, text: str, user_info: dict) -> None:
    # Ensure user record exists / update last_active
    _upsert_user(chat_id, user_info)

    if text.startswith("/start"):
        send_message(
            chat_id,
            f"Hey {user_info.get('first_name', 'there')}! 👋\n\n{HELP_TEXT}",
        )
        return

    if text.startswith("/help"):
        send_message(chat_id, HELP_TEXT)
        return

    if not text:
        send_message(chat_id, "Please send an Instagram URL.")
        return

    if not is_valid_instagram_url(text):
        send_message(
            chat_id,
            "That doesn't look like an Instagram URL.\n"
            "Please send a link to a reel or post (e.g. https://www.instagram.com/reel/ABC123/).",
        )
        return

    # Normalise URL for consistent DynamoDB keys
    shortcode = extract_shortcode(text)
    normalized_url = normalize_instagram_url(text)

    # Duplicate check
    if _is_duplicate(chat_id, shortcode):
        send_message(
            chat_id,
            "You've already processed this post! "
            "Check your Google Docs for the existing summary.",
        )
        return

    # Enqueue for processing
    _enqueue_extraction(chat_id, shortcode, normalized_url)

    send_message(
        chat_id,
        "⏳ Got it! Processing your Instagram content...\n"
        "This takes about 30–60 seconds. I'll message you when it's ready.",
    )


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _upsert_user(chat_id: str, user_info: dict) -> None:
    """Create user on first visit; update last_active on every message."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression=(
                "SET last_active = :now,"
                "    telegram_username = if_not_exists(telegram_username, :uname),"
                "    full_name         = if_not_exists(full_name, :fname),"
                "    created_at        = if_not_exists(created_at, :now),"
                "    reels_processed   = if_not_exists(reels_processed, :zero),"
                "    onboarding_completed = if_not_exists(onboarding_completed, :false)"
            ),
            ExpressionAttributeValues={
                ":now": now,
                ":uname": user_info.get("username", ""),
                ":fname": (
                    f"{user_info.get('first_name', '')} {user_info.get('last_name', '')}".strip()
                ),
                ":zero": 0,
                ":false": False,
            },
        )
    except ClientError as exc:
        logger.error("DynamoDB upsert_user failed: %s", exc)
        raise


def _is_duplicate(chat_id: str, shortcode: str) -> bool:
    """Return True if this user already processed this post."""
    table = _dynamodb.Table(DYNAMODB_REELS_TABLE)
    try:
        response = table.get_item(
            Key={"chat_id": chat_id, "shortcode": shortcode},
            ProjectionExpression="shortcode",  # fetch minimal data
        )
        return "Item" in response
    except ClientError as exc:
        logger.warning("DynamoDB duplicate check failed: %s — treating as not duplicate", exc)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# SQS helper
# ─────────────────────────────────────────────────────────────────────────────

def _enqueue_extraction(chat_id: str, shortcode: str, url: str) -> None:
    """Publish a job message to the extraction SQS queue."""
    payload = {
        "chat_id": chat_id,
        "shortcode": shortcode,
        "url": url,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    _sqs.send_message(
        QueueUrl=SQS_EXTRACTION_QUEUE_URL,
        MessageBody=json.dumps(payload),
    )
    logger.info("Enqueued extraction job: shortcode=%s user=%s", shortcode, chat_id)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram API helpers
# ─────────────────────────────────────────────────────────────────────────────

def send_message(chat_id: str, text: str, parse_mode: str = "") -> None:
    """Send a plain-text or MarkdownV2 message to the user."""
    payload: dict = {"chat_id": chat_id, "text": text}
    if parse_mode:
        payload["parse_mode"] = parse_mode

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning("Telegram sendMessage returned HTTP %s", resp.status)
    except Exception as exc:
        logger.error("Failed to send Telegram message to %s: %s", chat_id, exc)


def _ok() -> dict:
    return {"statusCode": 200, "body": "ok"}
