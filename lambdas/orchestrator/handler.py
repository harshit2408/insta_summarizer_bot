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
import re
import urllib.request
import urllib.parse
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

# Shared utils are bundled into the zip by Terraform archive_file
from utils.helpers import is_valid_instagram_url, extract_shortcode, normalize_instagram_url

# doc_template is bundled into the orchestrator zip
from doc_template import SectionConfig, parse_section_arg

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

# Optional — only set once Phase 2 Week 4 (Google Docs) is deployed. When
# unset, /connect responds with a "not yet enabled" message.
GOOGLE_OAUTH_START_URL = os.environ.get("GOOGLE_OAUTH_START_URL", "")

TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

HELP_TEXT = (
    "Send me any Instagram reel or post link and I'll:\n"
    "• Transcribe the audio (Whisper AI)\n"
    "• Extract text from images (OCR)\n"
    "• Generate a summary with key takeaways\n"
    "• Save everything to your Google Docs\n\n"
    "Commands:\n"
    "/start                     — Welcome message\n"
    "/connect                   — Link your Google account\n"
    "/setdoc <url>              — Use an existing Google Doc\n"
    "/sections                  — List your current sections\n"
    "/addsection <key> <title> — Add a new section\n"
    "/removesection <key>       — Remove a section\n"
    "/help                      — Show this help"
)

# Matches a Google Doc URL and captures the document ID
# e.g. https://docs.google.com/document/d/1abc...xyz/edit
_GDOC_URL_RE = re.compile(
    r"(?:https?://docs\.google\.com/document/d/)?([A-Za-z0-9_\-]{20,})"
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
        first_name = user_info.get("first_name", "there")
        message = f"Hey {first_name}!\n\n{HELP_TEXT}"
        if GOOGLE_OAUTH_START_URL and not _user_has_google_link(chat_id):
            message += (
                "\n\nStep 1: Connect your Google account so I can save "
                "summaries. Tap /connect."
            )
        send_message(chat_id, message)
        return

    if text.startswith("/help"):
        send_message(chat_id, HELP_TEXT)
        return

    if text.startswith("/connect"):
        _send_connect_link(chat_id)
        return

    if text.startswith("/setdoc"):
        _handle_setdoc(chat_id, text)
        return

    if text.startswith("/sections"):
        _handle_sections(chat_id)
        return

    if text.startswith("/addsection"):
        _handle_addsection(chat_id, text)
        return

    if text.startswith("/removesection"):
        _handle_removesection(chat_id, text)
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

    ack = (
        "Got it! Processing your Instagram content...\n"
        "This takes about 30–60 seconds. I'll message you when it's ready."
    )
    if GOOGLE_OAUTH_START_URL and not _user_has_google_link(chat_id):
        ack += (
            "\n\nHeads up: I'll need access to your Google account to save "
            "the summary. Tap /connect to set that up while I work."
        )
    send_message(chat_id, ack)


def _send_connect_link(chat_id: str) -> None:
    if not GOOGLE_OAUTH_START_URL:
        send_message(
            chat_id,
            "Google Docs integration isn't enabled on this deployment yet. "
            "Ask the operator to set GOOGLE_OAUTH_START_URL.",
        )
        return

    if _user_has_google_link(chat_id):
        send_message(
            chat_id,
            "Your Google account is already connected.\n"
            "Send me an Instagram link and I'll save the summary.",
        )
        return

    send_message(
        chat_id,
        "Click the link below to connect your Google account:\n"
        f"{GOOGLE_OAUTH_START_URL}?chat_id={chat_id}\n\n"
        "I only request access to create and edit Google Docs — I never see "
        "your Gmail, Drive files, or other data.",
    )


def _handle_setdoc(chat_id: str, text: str) -> None:
    """Parse a Google Doc URL/ID from the command and save it to the user record.

    Supports:
      /setdoc https://docs.google.com/document/d/1abc...xyz/edit
      /setdoc 1abc...xyz   (bare document ID)
    """
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        send_message(
            chat_id,
            "Usage: /setdoc <Google Doc URL or document ID>\n\n"
            "Example:\n"
            "/setdoc https://docs.google.com/document/d/1abc...xyz/edit\n\n"
            "Make sure I have edit access to the doc before using this command.",
        )
        return

    m = _GDOC_URL_RE.search(arg)
    if not m:
        send_message(
            chat_id,
            "That doesn't look like a Google Doc URL or ID. "
            "Please share the full link from your browser address bar.",
        )
        return

    doc_id = m.group(1)

    if not _user_has_google_link(chat_id):
        if GOOGLE_OAUTH_START_URL:
            send_message(
                chat_id,
                "You need to connect your Google account first.\n"
                f"Tap /connect or visit:\n{GOOGLE_OAUTH_START_URL}?chat_id={chat_id}",
            )
        else:
            send_message(chat_id, "Google integration is not enabled on this deployment.")
        return

    _save_doc_id(chat_id, doc_id)
    send_message(
        chat_id,
        "Done! Summaries will now be saved to your Google Doc.\n"
        f"Doc ID: {doc_id}\n\n"
        "Make sure I have edit access — share the doc with your Google account "
        "or make it accessible to anyone with the link.",
    )


def _handle_sections(chat_id: str) -> None:
    """List the user's current sections."""
    cfg = _load_section_config(chat_id)
    send_message(chat_id, cfg.format_for_telegram())


def _handle_addsection(chat_id: str, text: str) -> None:
    """Parse /addsection <key> <title> and persist."""
    parts = text.split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    if not arg:
        send_message(
            chat_id,
            "Usage: /addsection <key> <title>\n\n"
            "Example:\n"
            "/addsection Cooking COOKING & RECIPES\n\n"
            "• key   — one word, no spaces (e.g. Cooking, Sports)\n"
            "• title — short description (often ALL-CAPS), can include spaces",
        )
        return

    parsed = parse_section_arg(arg)
    if not parsed:
        send_message(
            chat_id,
            "Could not parse that. Format is:\n"
            "/addsection <key> <title>\n\n"
            "The key must be a single word with no spaces.",
        )
        return

    key, title = parsed
    cfg = _load_section_config(chat_id)

    if cfg.has_category(key):
        send_message(chat_id, f"A section with key '{key}' already exists.")
        return

    cfg.add_section(key, title)
    _save_section_config(chat_id, cfg)

    send_message(
        chat_id,
        f"Added section: {title} (key: {key})\n\n" + cfg.format_for_telegram(),
    )


def _handle_removesection(chat_id: str, text: str) -> None:
    """Parse /removesection <key> and persist."""
    parts = text.split(maxsplit=1)
    key = parts[1].strip() if len(parts) > 1 else ""

    if not key:
        send_message(
            chat_id,
            "Usage: /removesection <key>\n\n"
            "Example: /removesection Cooking\n\n"
            "Use /sections to see all current section keys.",
        )
        return

    cfg = _load_section_config(chat_id)
    removed = cfg.remove_section(key)

    if not removed:
        send_message(
            chat_id,
            f"No section with key '{key}' found.\n\n" + cfg.format_for_telegram(),
        )
        return

    _save_section_config(chat_id, cfg)
    send_message(
        chat_id,
        f"Removed section '{key}'.\n\n" + cfg.format_for_telegram(),
    )


def _save_doc_id(chat_id: str, doc_id: str) -> None:
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET google_docs_id = :doc, last_active = :now",
            ExpressionAttributeValues={
                ":doc": doc_id,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
        logger.info("Saved google_docs_id=%s for chat_id=%s", doc_id, chat_id)
    except ClientError as exc:
        logger.error("DynamoDB save_doc_id failed for chat_id=%s: %s", chat_id, exc)
        send_message(chat_id, "Something went wrong saving your doc ID. Please try again.")


def _load_section_config(chat_id: str) -> SectionConfig:
    """Load a user's custom sections from DynamoDB, returning defaults if absent."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        resp = table.get_item(
            Key={"chat_id": chat_id},
            ProjectionExpression="custom_sections",
        )
    except ClientError as exc:
        logger.warning("DynamoDB get custom_sections failed for chat_id=%s: %s", chat_id, exc)
        return SectionConfig()

    item = resp.get("Item") or {}
    raw = item.get("custom_sections")
    if not raw:
        return SectionConfig()

    try:
        sections = json.loads(raw) if isinstance(raw, str) else raw
        return SectionConfig(sections)
    except Exception as exc:
        logger.warning("Could not parse custom_sections for chat_id=%s: %s", chat_id, exc)
        return SectionConfig()


def _save_section_config(chat_id: str, cfg: SectionConfig) -> None:
    """Persist a user's section config back to DynamoDB."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET custom_sections = :sections, last_active = :now",
            ExpressionAttributeValues={
                ":sections": json.dumps(cfg.to_list()),
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as exc:
        logger.error("DynamoDB save_section_config failed for chat_id=%s: %s", chat_id, exc)
        raise


def _user_has_google_link(chat_id: str) -> bool:
    """Return True if the user has already completed the OAuth flow."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        response = table.get_item(
            Key={"chat_id": chat_id},
            ProjectionExpression="google_refresh_token_encrypted",
        )
    except ClientError as exc:
        logger.warning("DynamoDB get_item failed for chat_id=%s: %s", chat_id, exc)
        return False
    item = response.get("Item") or {}
    return bool(item.get("google_refresh_token_encrypted"))


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
