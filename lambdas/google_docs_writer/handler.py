"""
Google Docs Writer Lambda — Stage 4 of the processing pipeline (PRD §5.3).

Triggered by:  SQS writer queue (one message per analysed reel)
Runtime:       Python 3.11 zip (stdlib + boto3 only)

Per-message flow:

  1. Parse SQS body → message produced by the AI Analyzer.
  2. Look up the user record in DynamoDB (Users table).
        - If google_refresh_token_encrypted is missing → user hasn't completed
          OAuth. Send a Telegram message with the /oauth/start link and stop.
  3. KMS-decrypt the refresh token (chat_id used as encryption context).
  4. Refresh the access token via Google's token endpoint.
  5. If the user has no google_docs_id yet, auto-create one and persist it.
  6. Fetch the doc; if it's empty, seed the section skeleton.
  7. Insert the entry into BOTH its priority section AND its category section.
     Higher index is written first so the lower index stays valid (Google Docs
     batchUpdate applies requests sequentially — inserting at a higher position
     does not shift lower indices).
  8. Mark the ProcessedReels row ``status = "completed"`` and increment the
     user's reels_processed counter.
  9. Send a Telegram completion message including the Google Doc link.

Recoverable errors raise — SQS will retry with exponential backoff. After
the redrive maxReceiveCount (=3) the message lands in the writer DLQ for
manual triage.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError

from doc_template import (
    HIGH_PRIORITY_HEADING,
    SectionConfig,
    build_append_section_requests,
    build_insert_text_request,
    build_skeleton_requests,
    render_entry_text,
)
from google_docs import (
    GoogleDocsClient,
    GoogleDocsError,
    find_end_index,
    find_section_index,
)
from google_oauth import OAuthError, refresh_access_token
from kms_helper import TokenEncryptionError, decrypt_refresh_token
from telegram import send_message

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── Environment ──────────────────────────────────────────────────────────────
GOOGLE_CLIENT_ID = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET = os.environ["GOOGLE_CLIENT_SECRET"]
GOOGLE_OAUTH_START_URL = os.environ.get("GOOGLE_OAUTH_START_URL", "")

DYNAMODB_USERS_TABLE = os.environ["DYNAMODB_USERS_TABLE"]
DYNAMODB_REELS_TABLE = os.environ["DYNAMODB_REELS_TABLE"]
KMS_KEY_ID = os.environ["KMS_KEY_ID"]
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

DEFAULT_DOC_TITLE = os.environ.get("DEFAULT_DOC_TITLE", "Instagram Learning Archive")

# ── AWS clients (warm-reused) ────────────────────────────────────────────────
_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)
_kms = boto3.client("kms", region_name=_region)

# ── Module singletons ────────────────────────────────────────────────────────
_docs = GoogleDocsClient()


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """SQS trigger handler — supports partial batch failure reporting."""
    batch_item_failures: list[dict] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])
            _process_message(body)
            logger.info("Successfully wrote messageId=%s", message_id)
        except Exception:
            logger.exception("Failed to write messageId=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ─────────────────────────────────────────────────────────────────────────────
# Core processing
# ─────────────────────────────────────────────────────────────────────────────

def _process_message(message: dict) -> None:
    chat_id = str(message["chat_id"])
    shortcode = message["shortcode"]
    url = message.get("url", "")
    analysis = message.get("analysis") or {}

    logger.info("Writing analysis to Google Docs: chat_id=%s shortcode=%s", chat_id, shortcode)

    user = _load_user(chat_id)
    if not user:
        logger.warning("No user record for chat_id=%s — aborting write", chat_id)
        return

    encrypted_token = user.get("google_refresh_token_encrypted")
    if not encrypted_token:
        _notify_oauth_required(chat_id)
        _mark_reel_status(chat_id, shortcode, "awaiting_oauth")
        return

    # ── Mint a fresh access token ────────────────────────────────────────────
    try:
        refresh_token = decrypt_refresh_token(
            _kms, chat_id=chat_id, ciphertext_b64=encrypted_token,
        )
    except TokenEncryptionError as exc:
        logger.error("KMS decrypt failed for chat_id=%s: %s", chat_id, exc)
        _clear_refresh_token(chat_id)
        _notify_oauth_required(chat_id, reason="encryption")
        return

    try:
        tokens = refresh_access_token(
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            refresh_token=refresh_token,
        )
    except OAuthError as exc:
        msg = str(exc)
        if "invalid or revoked" in msg:
            logger.warning("Refresh token revoked for chat_id=%s — clearing", chat_id)
            _clear_refresh_token(chat_id)
            _notify_oauth_required(chat_id, reason="revoked")
            return
        raise

    access_token = tokens.access_token

    # ── Load the user's section config ───────────────────────────────────────
    cfg = _load_section_config(user)

    # ── Handle AI-suggested new section ──────────────────────────────────────
    if analysis.get("new_section") and isinstance(analysis.get("suggested_section"), dict):
        ss = analysis["suggested_section"]
        key = ss.get("key", "")
        title = ss.get("title", key.upper())
        if key and not cfg.has_category(key):
            cfg.add_section(key, title)
            _save_section_config(chat_id, cfg)
            logger.info(
                "Auto-created section key=%s for chat_id=%s", key, chat_id
            )
            _send_new_section_notification(chat_id=chat_id, title=title)

    # ── Resolve / create the user's destination doc ──────────────────────────
    doc_id = user.get("google_docs_id")
    if not doc_id:
        doc_id = _create_user_doc(chat_id, access_token, user.get("full_name"))

    # ── Build + execute the insert ───────────────────────────────────────────
    doc_link = _append_entry(
        access_token=access_token,
        document_id=doc_id,
        analysis=analysis,
        url=url,
        processed_at=message.get("processed_at"),
        owner_name=user.get("full_name"),
        cfg=cfg,
    )

    # ── Bookkeeping ──────────────────────────────────────────────────────────
    _mark_reel_status(chat_id, shortcode, "completed", doc_id=doc_id)
    _increment_user_counter(chat_id)
    _send_completion_message(chat_id, analysis, doc_link, url)


# ─────────────────────────────────────────────────────────────────────────────
# Google Docs append logic
# ─────────────────────────────────────────────────────────────────────────────

def _append_entry(
    *,
    access_token: str,
    document_id: str,
    analysis: dict,
    url: str,
    processed_at: str | None,
    owner_name: str | None,
    cfg: SectionConfig,
) -> str:
    """Render the entry and insert it into BOTH its priority and category sections.

    Insertion order within the batchUpdate batch:
        Always write the HIGHER index first. Google Docs applies requests
        sequentially — inserting at a higher position does not affect any
        index below it, so the lower index we computed remains valid for the
        second request without a re-fetch.

    Returns a deep link to the document.
    """
    quality_score = int(analysis.get("quality_score") or 0)
    category = analysis.get("category") or "Other"

    entry_text = render_entry_text(
        title=analysis.get("title") or "(untitled)",
        category=category,
        subcategory=analysis.get("subcategory") or "",
        quality_score=quality_score,
        summary=analysis.get("summary") or "",
        key_takeaways=list(analysis.get("key_takeaways") or []),
        tags=list(analysis.get("tags") or []),
        source_url=url,
        processed_at=processed_at,
    )

    # Fetch the doc once — we may need to seed the skeleton.
    document = _docs.get_document(access_token=access_token, document_id=document_id)

    # Seed skeleton if this is a brand-new doc (HIGH PRIORITY heading absent).
    needs_skeleton = find_section_index(document, HIGH_PRIORITY_HEADING) is None
    if needs_skeleton:
        logger.info("Seeding skeleton in document_id=%s", document_id)
        _docs.batch_update(
            access_token=access_token,
            document_id=document_id,
            requests=build_skeleton_requests(owner_name=owner_name, cfg=cfg),
        )
        document = _docs.get_document(access_token=access_token, document_id=document_id)

    # ── If the category section doesn't exist in the doc yet, append it ──────
    category_heading = cfg.category_heading_for(category)
    if find_section_index(document, category_heading) is None:
        logger.info(
            "Category section '%s' not in doc — appending it", category_heading
        )
        append_reqs = build_append_section_requests(
            cfg=cfg,
            review_later_index=find_section_index(document, cfg.review_later_heading()),
            end_index=find_end_index(document),
            new_section_key=category,
        )
        if append_reqs:
            _docs.batch_update(
                access_token=access_token,
                document_id=document_id,
                requests=append_reqs,
            )
            document = _docs.get_document(access_token=access_token, document_id=document_id)

    # ── Resolve both insertion indices independently ──────────────────────────
    priority_heading = cfg.priority_heading_for(quality_score)

    priority_index = (
        find_section_index(document, priority_heading)
        or find_end_index(document)
    )
    category_index = (
        find_section_index(document, category_heading)
        or find_end_index(document)
    )

    # ── Insert higher index first so the lower stays valid ───────────────────
    if priority_index >= category_index:
        requests = [
            build_insert_text_request(text=entry_text, index=priority_index),
            build_insert_text_request(text=entry_text, index=category_index),
        ]
    else:
        requests = [
            build_insert_text_request(text=entry_text, index=category_index),
            build_insert_text_request(text=entry_text, index=priority_index),
        ]

    _docs.batch_update(
        access_token=access_token,
        document_id=document_id,
        requests=requests,
    )

    return f"https://docs.google.com/document/d/{document_id}/edit"


def _create_user_doc(chat_id: str, access_token: str, owner_name: str | None) -> str:
    """Create a fresh Doc for a user who didn't supply one."""
    title = DEFAULT_DOC_TITLE
    if owner_name:
        title = f"{DEFAULT_DOC_TITLE} — {owner_name}"

    try:
        doc = _docs.create_document(access_token=access_token, title=title)
    except GoogleDocsError as exc:
        logger.error("create_document failed for chat_id=%s: %s", chat_id, exc)
        raise

    doc_id = doc["documentId"]
    logger.info("Auto-created doc id=%s for chat_id=%s", doc_id, chat_id)

    # Persist immediately so we don't create a second doc on a transient retry.
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET google_docs_id = :doc, google_doc_created_at = :now",
            ExpressionAttributeValues={
                ":doc": doc_id,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as exc:
        logger.error("Failed to persist new doc_id for chat_id=%s: %s", chat_id, exc)
        raise

    return doc_id


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB helpers
# ─────────────────────────────────────────────────────────────────────────────

def _load_section_config(user: dict) -> SectionConfig:
    """Build a SectionConfig from the user's DynamoDB record."""
    raw = user.get("custom_sections")
    if not raw:
        return SectionConfig()
    try:
        sections = json.loads(raw) if isinstance(raw, str) else raw
        return SectionConfig(sections)
    except Exception as exc:
        logger.warning("Could not parse custom_sections: %s", exc)
        return SectionConfig()


def _save_section_config(chat_id: str, cfg: SectionConfig) -> None:
    """Persist updated section config to DynamoDB after auto-adding a section."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="SET custom_sections = :sections",
            ExpressionAttributeValues={
                ":sections": json.dumps(cfg.to_list()),
            },
        )
    except ClientError as exc:
        logger.error("Failed to save section config for chat_id=%s: %s", chat_id, exc)


def _load_user(chat_id: str) -> dict | None:
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        resp = table.get_item(Key={"chat_id": chat_id})
    except ClientError as exc:
        logger.error("DynamoDB get_item users failed for chat_id=%s: %s", chat_id, exc)
        raise
    return resp.get("Item")


def _mark_reel_status(
    chat_id: str,
    shortcode: str,
    status: str,
    *,
    doc_id: str | None = None,
) -> None:
    table = _dynamodb.Table(DYNAMODB_REELS_TABLE)
    update_parts = ["#st = :s", "writer_completed_at = :now"]
    expr_values: dict[str, Any] = {
        ":s": status,
        ":now": datetime.now(timezone.utc).isoformat(),
    }
    if doc_id:
        update_parts.append("google_docs_id = :doc")
        expr_values[":doc"] = doc_id

    try:
        table.update_item(
            Key={"chat_id": chat_id, "shortcode": shortcode},
            UpdateExpression="SET " + ", ".join(update_parts),
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as exc:
        logger.warning("Could not mark status for shortcode=%s: %s", shortcode, exc)


def _increment_user_counter(chat_id: str) -> None:
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression=(
                "SET reels_processed = if_not_exists(reels_processed, :zero) + :one,"
                "    last_processed_at = :now"
            ),
            ExpressionAttributeValues={
                ":zero": Decimal("0"),
                ":one": Decimal("1"),
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except ClientError as exc:
        logger.warning("Could not increment counter for chat_id=%s: %s", chat_id, exc)


def _clear_refresh_token(chat_id: str) -> None:
    """Wipe the broken token so we don't loop on it."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": chat_id},
            UpdateExpression="REMOVE google_refresh_token_encrypted",
        )
    except ClientError as exc:
        logger.warning("Could not clear refresh token for chat_id=%s: %s", chat_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram messaging
# ─────────────────────────────────────────────────────────────────────────────

_OAUTH_REASONS = {
    "revoked": (
        "Your Google access has expired or was revoked.\n"
        "Reconnect to keep saving summaries:\n{url}"
    ),
    "encryption": (
        "We can't decrypt your stored Google credentials.\n"
        "Please reconnect:\n{url}"
    ),
    None: (
        "You haven't connected Google Docs yet.\n"
        "Click here to authorize and I'll save your summary:\n{url}"
    ),
}


def _notify_oauth_required(chat_id: str, *, reason: str | None = None) -> None:
    if not GOOGLE_OAUTH_START_URL:
        logger.warning("GOOGLE_OAUTH_START_URL not configured — cannot notify chat_id=%s", chat_id)
        return

    template = _OAUTH_REASONS.get(reason, _OAUTH_REASONS[None])
    url = f"{GOOGLE_OAUTH_START_URL}?chat_id={chat_id}"
    send_message(bot_token=TELEGRAM_BOT_TOKEN, chat_id=chat_id, text=template.format(url=url))


def _send_new_section_notification(chat_id: str, *, title: str) -> None:
    """Tell the user that a new section was automatically created."""
    send_message(
        bot_token=TELEGRAM_BOT_TOKEN,
        chat_id=chat_id,
        text=(
            f"New section created: {title}\n\n"
            "This reel didn't fit your existing sections, so I automatically "
            "added a new one. Use /sections to see all your sections, or "
            "/removesection to remove it if you don't want it."
        ),
    )


def _send_completion_message(chat_id: str, analysis: dict, doc_link: str, source_url: str) -> None:
    title = analysis.get("title") or "(untitled)"
    category = analysis.get("category") or "Other"
    quality = analysis.get("quality_score") or "?"
    takeaways = list(analysis.get("key_takeaways") or [])
    bullets = "\n".join(f"  • {t}" for t in takeaways[:3]) if takeaways else "  • (no key takeaways)"

    text = (
        "Saved to your Google Docs!\n\n"
        f"{title}\n"
        f"Category: {category}  Score: {quality}/10\n\n"
        "Key Takeaways:\n"
        f"{bullets}\n\n"
        f"Doc: {doc_link}\n"
        f"Source: {source_url}"
    )
    send_message(bot_token=TELEGRAM_BOT_TOKEN, chat_id=chat_id, text=text)