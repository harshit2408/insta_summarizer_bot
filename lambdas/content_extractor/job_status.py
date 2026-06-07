"""
Job Status — DynamoDB state machine + Telegram user feedback loop.

PRD Section: REQUIREMENT 1 (state transitions) + REQUIREMENT 6 (user feedback)

Every time the Content Extractor advances a job through the pipeline, it must
do two things atomically in the same function call:
  1. Write the new status (+ timestamp) to DynamoDB.
  2. Send the corresponding Telegram notification to the user.

The `update_job_status()` function encapsulates both so a caller can never
accidentally update one without the other.

Status lifecycle (in order):
  PENDING → IN_QUEUE → AGENT_BROWSING → EXTRACTING_CONTENT → FINALIZING → COMPLETED
                                                                          ↘ FAILED

The Orchestrator writes PENDING when it first creates the job record.
The Extractor drives the remaining transitions.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Literal

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)
_sqs = boto3.client("sqs", region_name=_region)

DYNAMODB_REELS_TABLE = os.environ.get("DYNAMODB_REELS_TABLE", "processed_reels")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
SQS_EXTRACTION_QUEUE_URL = os.environ.get("SQS_EXTRACTION_QUEUE_URL", "")

# ── Valid status values ───────────────────────────────────────────────────────
JobStatus = Literal[
    "PENDING",
    "IN_QUEUE",
    "AGENT_BROWSING",
    "EXTRACTING_CONTENT",
    "FINALIZING",
    "COMPLETED",
    "FAILED",
]

# ── Telegram message templates ────────────────────────────────────────────────
# COMPLETED is handled by the existing _send_completion_message() in the
# Google Docs Writer lambda; we do not send it here.
STATUS_MESSAGES: dict[str, str | None] = {
    "PENDING": None,  # Orchestrator handles this with its own ack message
    "IN_QUEUE": "📋 Your reel is in the queue! I'll start working on it shortly.",
    "AGENT_BROWSING": "🌐 Agent is browsing Instagram to fetch your content...",
    "EXTRACTING_CONTENT": (
        "🎙️ Extracting audio and visuals from the reel. This is the slow part (~30s)."
    ),
    "FINALIZING": (
        "✨ Almost done! Running AI analysis and saving to your Google Doc..."
    ),
    "COMPLETED": None,  # Handled downstream by google_docs_writer
    "FAILED": None,     # Formatted dynamically with shortcode — see update_job_status()
}


def update_job_status(
    chat_id: str,
    shortcode: str,
    status: JobStatus,
    *,
    notify: bool = True,
    queue_position: int | None = None,
) -> None:
    """Advance a job to `status` in DynamoDB and optionally notify the user.

    This is the ONLY function that should write job status — centralising
    here ensures the DynamoDB write and Telegram send are always in sync.

    Args:
        chat_id:        Telegram chat ID (also the DynamoDB partition key).
        shortcode:      Instagram shortcode (DynamoDB sort key).
        status:         Target status string.
        notify:         If False, skip the Telegram send (use for transitions
                        that happen too fast to be meaningful to the user).
        queue_position: When status is IN_QUEUE, pass the SQS approximate
                        message count so we can include a queue-position hint.
                        If None, we query SQS automatically.
    """
    now = datetime.now(timezone.utc).isoformat()

    # ── 1. Write to DynamoDB ──────────────────────────────────────────────────
    try:
        table = _dynamodb.Table(DYNAMODB_REELS_TABLE)
        table.update_item(
            Key={"chat_id": chat_id, "shortcode": shortcode},
            UpdateExpression="SET #s = :status, status_updated_at = :now",
            ExpressionAttributeNames={"#s": "status"},
            ExpressionAttributeValues={":status": status, ":now": now},
        )
        logger.info(
            "Job status updated: shortcode=%s chat_id=%s status=%s",
            shortcode, chat_id, status,
        )
    except ClientError as exc:
        # Log but do NOT raise — a failed DynamoDB write should not block the
        # Telegram notification; user UX matters more than perfect state consistency.
        logger.error(
            "DynamoDB status update failed for shortcode=%s status=%s: %s",
            shortcode, status, exc,
        )

    # ── 2. Send Telegram notification ─────────────────────────────────────────
    if not notify:
        return

    message = _build_message(status, shortcode, chat_id, queue_position)
    if message:
        _send_telegram(chat_id, message)


def _build_message(
    status: str,
    shortcode: str,
    chat_id: str,
    queue_position: int | None,
) -> str | None:
    """Build the Telegram message string for the given status.

    Returns None if no notification should be sent for this status.
    """
    if status == "FAILED":
        return (
            f"❌ Something went wrong processing your reel ({shortcode}). "
            "It's been logged and I'll retry automatically. No action needed."
        )

    if status == "IN_QUEUE":
        base = STATUS_MESSAGES["IN_QUEUE"]
        pos = queue_position if queue_position is not None else _get_queue_position()
        if pos > 1:
            return f"{base}\nPosition in queue: ~{pos}"
        return f"{base}\nYou're next!"

    return STATUS_MESSAGES.get(status)


def _get_queue_position() -> int:
    """Query the SQS extraction queue for approximate number of messages.

    Returns 1 on any error (conservative — "you're next!" is better than
    showing a wrong queue depth).
    """
    if not SQS_EXTRACTION_QUEUE_URL:
        return 1
    try:
        resp = _sqs.get_queue_attributes(
            QueueUrl=SQS_EXTRACTION_QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages"],
        )
        return max(1, int(resp["Attributes"].get("ApproximateNumberOfMessages", 1)))
    except Exception as exc:
        logger.warning("Could not fetch SQS queue depth: %s", exc)
        return 1


def _send_telegram(chat_id: str, text: str) -> None:
    """Best-effort Telegram send. Never raises — a notification failure must not
    abort the scraping pipeline.
    """
    if not TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN not set — skipping notification to %s", chat_id)
        return

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Telegram sendMessage returned HTTP %s for chat_id=%s", resp.status, chat_id
                )
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.error("Telegram sendMessage failed for chat_id=%s: %s", chat_id, exc)
