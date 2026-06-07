"""
Content Extractor Lambda — Stage 2 of the processing pipeline.

PRD Sections: REQUIREMENT 1 (serialised execution), REQUIREMENT 3 (mimicry),
              REQUIREMENT 4 (429 protocol), REQUIREMENT 5 (AWS survival).

Triggered by:  SQS extraction queue (batchSize=1, maxConcurrency=1)
Runtime:       Python 3.11 Docker container (includes ffmpeg, Whisper, EasyOCR)

────────────────────────────────────────────────────────────────────────────────
SERIALISED EXECUTION CONTRACT (Requirement 1)
────────────────────────────────────────────────────────────────────────────────
The SQS event source mapping MUST be configured with:
  - batchSize       = 1   (one message per Lambda invocation)
  - maxConcurrency  = 1   (at most one concurrent Lambda execution)

These two settings together guarantee that Instagram sees at most one
outbound request in flight at any time, regardless of queue depth. The
Lambda runtime itself is the mutex.

Why batchSize=1 is not enough alone:
  Without maxConcurrency=1, AWS can spin up multiple Lambda instances in
  parallel (one per message). batchSize=1 only limits messages PER instance.

All messages must also be sent with MessageGroupId = "global-scraper"
(a single constant string). SQS FIFO delivers only one message from a group
at a time, providing a second layer of enforcement at the queue level.

────────────────────────────────────────────────────────────────────────────────
# COLD START IP NOTE
────────────────────────────────────────────────────────────────────────────────
AWS Lambda cold starts in a new execution environment typically receive a
DIFFERENT outbound IP address from the VPC NAT pool. This is useful when
our current IP is flagged by Instagram:

  Force a cold start (all existing execution environments are recycled):
    aws lambda update-function-configuration \\
      --function-name content-extractor \\
      --environment Variables={FORCE_COLD_START=$(date +%s)}

  This touches the function configuration, causing Lambda to spin up fresh
  execution environments with new IPs on the next invocations.

  IMPORTANT: Do this only after the cooldown period has elapsed, otherwise
  the new IP will also get flagged immediately.

When to consider forcing a cold start:
  - 5+ consecutive 403s are detected (see consecutive_403_count in DynamoDB)
  - A CRITICAL log line appears: "CRITICAL: Possible IP flagging detected"
────────────────────────────────────────────────────────────────────────────────

Processing flow per message:
  1. Check global cooldown — push message back and return if active
  2. Check for easing-in sleep (if previous run ended in cooldown)
  3. Update job status: IN_QUEUE → AGENT_BROWSING
  4. Rotate User-Agent headers
  5. Attempt yt-dlp scrape with rotated headers
  6. On 429/sustained-403: activate global cooldown, notify user, re-raise
  7. Update status: EXTRACTING_CONTENT
  8. Transcribe audio / run OCR
  9. Update status: FINALIZING
 10. Upload extracted JSON to S3
 11. Publish to analysis SQS queue
 12. Post-scrape jitter sleep
 13. Update status: (handled downstream by AI Analyzer / Google Docs Writer)
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
import random
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from audio import transcribe_video
from ocr import extract_text_from_images, extract_text_from_video_frames

# Shared modules are COPY-ed into the container image (see Dockerfile)
from scraper.instagram_scraper import InstagramScraper
from models.content_models import ScrapeStatus, ContentType

# ── New helper modules (Requirements 1, 3, 4, 5, 6) ──────────────────────────
from cooldown import cooldown_manager, COOLDOWN_TRIGGER_MSG
from human_mimicry import get_rotated_headers, get_last_used_ua_index, post_scrape_sleep
from job_status import update_job_status

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── AWS clients ───────────────────────────────────────────────────────────────
_region = os.environ.get("AWS_REGION", "ap-south-1")
_s3 = boto3.client("s3", region_name=_region)
_sqs = boto3.client("sqs", region_name=_region)
_dynamodb = boto3.resource("dynamodb", region_name=_region)

# ── Environment variables ─────────────────────────────────────────────────────
S3_BUCKET = os.environ["S3_BUCKET_NAME"]
SQS_ANALYSIS_QUEUE_URL = os.environ["SQS_ANALYSIS_QUEUE_URL"]
SQS_EXTRACTION_QUEUE_URL = os.environ.get("SQS_EXTRACTION_QUEUE_URL", "")
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")
DYNAMODB_USERS_TABLE = os.environ.get("DYNAMODB_USERS_TABLE", "users")

# ── 403 streak tracking ───────────────────────────────────────────────────────
# Consecutive 403 threshold before we treat it as a soft 429 (IP flagging).
_403_SOFT_429_THRESHOLD = 3
_403_SOFT_429_WINDOW_MINUTES = 10
_CONSECUTIVE_403_DDB_KEY = "system:403_streak"
_IP_FLAG_THRESHOLD = 5   # CRITICAL log + CLI hint after this many consecutive 403s


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """SQS trigger handler.

    Configured with batchSize=1 and maxConcurrency=1 (see module docstring).
    We still iterate event["Records"] defensively, but in practice there is
    always exactly one record.

    try/finally guarantees SQS always gets a definitive answer — the message
    is either deleted (success path, implicit when we return without failure)
    or stays visible (failure path via batchItemFailures). No ghost messages.
    """
    batch_item_failures: list[dict] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        receipt_handle = record.get("receiptHandle", "")

        try:
            body = json.loads(record["body"])
            _process_message(body, receipt_handle)
            logger.info("Successfully processed messageId=%s", message_id)
        except _CooldownActive:
            # Cooldown is active — message visibility was already extended
            # inside _process_message(). Do NOT add to batchItemFailures so
            # Lambda does not delete the message; it will re-appear after the
            # visibility timeout expires.
            logger.info(
                "Cooldown active — message %s pushed back to queue", message_id
            )
        except Exception:
            logger.exception("Failed to process messageId=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ─────────────────────────────────────────────────────────────────────────────
# Internal sentinel exception
# ─────────────────────────────────────────────────────────────────────────────

class _CooldownActive(Exception):
    """Raised when global cooldown is detected at handler startup.

    Caught in lambda_handler without adding to batchItemFailures, so the
    SQS message is NOT deleted — it stays in the queue until lockdown ends.
    """


# ─────────────────────────────────────────────────────────────────────────────
# Core processing logic
# ─────────────────────────────────────────────────────────────────────────────

def _process_message(message: dict, receipt_handle: str) -> None:
    chat_id: str = message["chat_id"]
    shortcode: str = message["shortcode"]
    url: str = message["url"]

    # ── Step 1: Global cooldown gate ──────────────────────────────────────────
    # Check at the very start — before spending ANY time on this job.
    if cooldown_manager.is_active():
        logger.info(
            "Global cooldown active — pushing message back: shortcode=%s", shortcode
        )
        # Extend SQS visibility timeout to 3600s so the message re-appears after
        # the cooldown has likely expired, not in the next few seconds.
        _extend_visibility(receipt_handle, timeout_seconds=3600)
        raise _CooldownActive("cooldown active")

    # ── Step 2: Easing-in sleep after recovering from cooldown ────────────────
    # If the PREVIOUS invocation in this execution environment activated the
    # cooldown, sleep conservatively before making any outbound requests.
    if cooldown_manager.entered_cooldown_this_invocation():
        ease_in = random.uniform(60.0, 120.0)
        logger.info("Post-cooldown easing-in sleep: %.1fs", ease_in)
        time.sleep(ease_in)

    logger.info("Processing shortcode=%s for user=%s", shortcode, chat_id)

    # ── Step 3: Advance to IN_QUEUE ───────────────────────────────────────────
    update_job_status(chat_id, shortcode, "IN_QUEUE")

    with tempfile.TemporaryDirectory(dir="/tmp", prefix=f"insta_{shortcode}_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # ── Step 4: Rotate headers ────────────────────────────────────────────
        headers = get_rotated_headers()
        ua_index = get_last_used_ua_index()

        # ── Step 5: Scrape ────────────────────────────────────────────────────
        update_job_status(chat_id, shortcode, "AGENT_BROWSING")

        scrape_start = time.monotonic()
        scrape_method = "yt-dlp"
        http_status: int | None = None

        try:
            scraper = InstagramScraper(
                download_dir=tmp_path,
                download_media=True,
                http_headers=headers,
            )
            result = scraper.scrape(url)
            scrape_duration_ms = int((time.monotonic() - scrape_start) * 1000)
            http_status = 200 if result.status == ScrapeStatus.SUCCESS else None

        except Exception as exc:
            scrape_duration_ms = int((time.monotonic() - scrape_start) * 1000)
            http_status = _detect_http_status_from_exception(exc)

            _log_scrape_attempt(
                shortcode=shortcode,
                chat_id=chat_id,
                method=scrape_method,
                http_status=http_status,
                duration_ms=scrape_duration_ms,
                ua_index=ua_index,
                cooldown_active=False,
            )

            # ── Step 6: 429 / sustained-403 emergency protocol ────────────────
            if _is_rate_limit_error(exc, http_status):
                _handle_rate_limit_event(
                    chat_id=chat_id,
                    shortcode=shortcode,
                    http_status=http_status,
                )
                # Re-raise so the message goes back to the queue (batchItemFailures)
                raise

            if http_status == 403:
                streak = _increment_403_streak()
                logger.warning(
                    "HTTP 403 received: shortcode=%s streak=%d", shortcode, streak
                )
                if streak >= _IP_FLAG_THRESHOLD:
                    logger.critical(
                        "CRITICAL: Possible IP flagging detected. "
                        "Consider forcing a Lambda cold start by updating an "
                        "environment variable via AWS CLI: "
                        "aws lambda update-function-configuration "
                        "--function-name content-extractor "
                        "--environment Variables={FORCE_COLD_START=$(date +%%s)}"
                    )
                if streak >= _403_SOFT_429_THRESHOLD:
                    logger.warning(
                        "403 streak=%d exceeds soft-429 threshold — activating cooldown",
                        streak,
                    )
                    _handle_rate_limit_event(
                        chat_id=chat_id,
                        shortcode=shortcode,
                        http_status=403,
                        reason="sustained_403",
                    )
                    raise

            # Non-rate-limit scrape failure
            update_job_status(chat_id, shortcode, "FAILED")
            raise RuntimeError(
                f"Scraping failed for {shortcode}: {exc}"
            ) from exc

        _log_scrape_attempt(
            shortcode=shortcode,
            chat_id=chat_id,
            method=scrape_method,
            http_status=http_status,
            duration_ms=scrape_duration_ms,
            ua_index=ua_index,
            cooldown_active=False,
        )

        if result.status not in (ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL):
            update_job_status(chat_id, shortcode, "FAILED")
            raise RuntimeError(
                f"Scraping failed for {shortcode}: [{result.status.value}] {result.error_message}"
            )

        # Successful scrape — reset the 403 streak counter.
        _reset_403_streak()

        content = result.content
        logger.info(
            "Scraped %s: content_type=%s media_count=%d scraper=%s",
            shortcode,
            content.content_type.value,
            content.media_count,
            content.scraper_method,
        )

        extracted: dict = {
            "shortcode": shortcode,
            "chat_id": chat_id,
            "url": url,
            "content_type": content.content_type.value,
            "caption": content.caption,
            "username": content.username,
            "full_name": content.full_name,
            "like_count": content.like_count,
            "view_count": content.view_count,
            "scraper_method": content.scraper_method,
            "transcript": None,
            "ocr_text": None,
            "has_audio": False,
            "has_visual_text": False,
            "extracted_at": datetime.now(timezone.utc).isoformat(),
        }

        # ── Step 7: Advance to EXTRACTING_CONTENT ─────────────────────────────
        update_job_status(chat_id, shortcode, "EXTRACTING_CONTENT")

        # ── Step 8a: Audio transcription (for video reels) ────────────────────
        if content.is_video:
            video_media = next(
                (m for m in content.media_items if m.local_path and m.local_path.exists()),
                None,
            )
            if video_media:
                logger.info("Transcribing audio for %s", shortcode)
                transcript = transcribe_video(video_media.local_path, WHISPER_MODEL_SIZE)
                extracted["transcript"] = transcript
                extracted["has_audio"] = bool(transcript)

                # Frame OCR is expensive (~7s/frame on Lambda CPU). Only run it when
                # we have NO usable transcript — for silent videos with on-screen text.
                # Threshold: 200 chars ≈ 30 words ≈ enough context for AI analysis.
                if not transcript or len(transcript) < 200:
                    logger.info(
                        "Transcript empty or short (%d chars) — running frame OCR fallback for %s",
                        len(transcript or ""), shortcode,
                    )
                    ocr_from_frames = extract_text_from_video_frames(
                        video_media.local_path,
                        interval_seconds=5.0,
                        max_frames=15,
                    )
                    if ocr_from_frames:
                        extracted["ocr_text"] = ocr_from_frames
                        extracted["has_visual_text"] = True
                else:
                    logger.info(
                        "Transcript has %d chars — skipping frame OCR (saves ~100s)",
                        len(transcript),
                    )

        # ── Step 8b: Image OCR (for photos and carousels) ─────────────────────
        elif content.content_type in (ContentType.IMAGE, ContentType.CAROUSEL):
            image_paths = [
                m.local_path
                for m in content.media_items
                if m.local_path and m.local_path.exists()
            ]
            if image_paths:
                logger.info("Running OCR on %d image(s) for %s", len(image_paths), shortcode)
                ocr_text = extract_text_from_images(image_paths)
                extracted["ocr_text"] = ocr_text
                extracted["has_visual_text"] = bool(ocr_text)

        # ── Step 9: Advance to FINALIZING ─────────────────────────────────────
        update_job_status(chat_id, shortcode, "FINALIZING")

        # ── Step 10: Save extracted content to S3 ─────────────────────────────
        s3_key = f"users/{chat_id}/extracted/{shortcode}/extracted.json"
        _upload_to_s3(s3_key, extracted)
        logger.info("Saved extracted content to s3://%s/%s", S3_BUCKET, s3_key)

        # ── Step 11: Publish to analysis queue ────────────────────────────────
        analysis_payload = {
            **message,
            "extracted_content": extracted,
            "s3_extracted_key": s3_key,
        }
        _sqs.send_message(
            QueueUrl=SQS_ANALYSIS_QUEUE_URL,
            MessageBody=json.dumps(analysis_payload),
        )
        logger.info("Published to analysis queue: shortcode=%s", shortcode)

        # ── Step 12: Post-scrape jitter sleep ─────────────────────────────────
        # Sleep AFTER writing to S3 and enqueuing the analysis job, so the
        # downstream pipeline is not held up by our cooling-off period.
        post_scrape_sleep(method=content.scraper_method or "yt-dlp")


# ─────────────────────────────────────────────────────────────────────────────
# 429 / Rate-limit helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_rate_limit_error(exc: Exception, http_status: int | None) -> bool:
    """Return True if the exception represents an Instagram rate-limit response."""
    if http_status == 429:
        return True
    msg = str(exc).lower()
    return any(
        phrase in msg
        for phrase in ("http error 429", "rate-limited", "too many requests", "please wait")
    )


def _detect_http_status_from_exception(exc: Exception) -> int | None:
    """Try to extract an HTTP status code from a yt-dlp exception message."""
    msg = str(exc)
    for code in (429, 403, 404, 500):
        if f"HTTP Error {code}" in msg or f"http error {code}" in msg.lower():
            return code
    return None


def _handle_rate_limit_event(
    *,
    chat_id: str,
    shortcode: str,
    http_status: int | None,
    reason: str = "429_detected",
) -> None:
    """Activate global cooldown and notify the triggering user."""
    cooldown_manager.activate(reason=reason, triggered_by_shortcode=shortcode)
    update_job_status(chat_id, shortcode, "FAILED", notify=False)
    _send_telegram_direct(chat_id, COOLDOWN_TRIGGER_MSG)
    logger.critical(
        "Rate limit event: http_status=%s shortcode=%s chat_id=%s reason=%s",
        http_status, shortcode, chat_id, reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 403 streak counter (DynamoDB atomic)
# ─────────────────────────────────────────────────────────────────────────────

def _increment_403_streak() -> int:
    """Atomically increment the consecutive-403 counter and return the new value."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        resp = table.update_item(
            Key={"chat_id": _CONSECUTIVE_403_DDB_KEY},
            UpdateExpression="ADD streak_count :one SET last_403_at = :now",
            ExpressionAttributeValues={
                ":one": 1,
                ":now": datetime.now(timezone.utc).isoformat(),
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(resp["Attributes"].get("streak_count", 1))
    except ClientError as exc:
        logger.error("Failed to increment 403 streak: %s", exc)
        return 0


def _reset_403_streak() -> None:
    """Reset the consecutive-403 counter to 0 after a successful scrape."""
    table = _dynamodb.Table(DYNAMODB_USERS_TABLE)
    try:
        table.update_item(
            Key={"chat_id": _CONSECUTIVE_403_DDB_KEY},
            UpdateExpression="SET streak_count = :zero",
            ExpressionAttributeValues={":zero": 0},
        )
        logger.debug("403 streak reset to 0")
    except ClientError as exc:
        logger.warning("Failed to reset 403 streak: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Structured logging helper (Requirement 5)
# ─────────────────────────────────────────────────────────────────────────────

def _log_scrape_attempt(
    *,
    shortcode: str,
    chat_id: str,
    method: str,
    http_status: int | None,
    duration_ms: int,
    ua_index: int,
    cooldown_active: bool,
) -> None:
    """Emit a structured INFO log suitable for CloudWatch Metric Filters.

    CloudWatch Metric Filter patterns to configure (document as Terraform
    resource comments in monitoring.tf):
      { $.http_status = 403 }  → alarm if ≥5 occurrences in 10 minutes
      { $.http_status = 429 }  → alarm if ≥5 occurrences in 10 minutes
    Both alarms should target an SNS topic for ops notifications.
    """
    logger.info(
        "scrape_attempt",
        extra={
            "event": "scrape_attempt",
            "shortcode": shortcode,
            "chat_id": chat_id,
            "method": method,
            "http_status": http_status,
            "duration_ms": duration_ms,
            "user_agent_index": ua_index,
            "cooldown_active": cooldown_active,
        },
    )


# ─────────────────────────────────────────────────────────────────────────────
# SQS visibility helper
# ─────────────────────────────────────────────────────────────────────────────

def _extend_visibility(receipt_handle: str, timeout_seconds: int) -> None:
    """Push an SQS message back by extending its visibility timeout.

    Used during cooldown: we don't delete the message (so the job survives),
    but we hide it for `timeout_seconds` so it doesn't immediately re-trigger
    another Lambda invocation that would also bounce off the cooldown gate.
    """
    if not SQS_EXTRACTION_QUEUE_URL or not receipt_handle:
        logger.warning("Cannot extend visibility — missing queue URL or receipt handle")
        return
    try:
        _sqs.change_message_visibility(
            QueueUrl=SQS_EXTRACTION_QUEUE_URL,
            ReceiptHandle=receipt_handle,
            VisibilityTimeout=timeout_seconds,
        )
        logger.info(
            "Extended SQS message visibility by %ds during cooldown", timeout_seconds
        )
    except ClientError as exc:
        logger.error("Failed to extend message visibility: %s", exc)


# ─────────────────────────────────────────────────────────────────────────────
# Telegram direct send (fallback used before job_status is available)
# ─────────────────────────────────────────────────────────────────────────────

def _send_telegram_direct(chat_id: str, text: str) -> None:
    """Thin wrapper around urllib for emergency notifications (e.g. 429 alerts)."""
    import urllib.request
    import urllib.error

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10):
            pass
    except Exception as exc:
        logger.error("Emergency Telegram send failed for chat_id=%s: %s", chat_id, exc)


# ─────────────────────────────────────────────────────────────────────────────
# S3 helper
# ─────────────────────────────────────────────────────────────────────────────

def _upload_to_s3(key: str, data: dict) -> None:
    try:
        _s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=json.dumps(data, default=str),
            ContentType="application/json",
        )
    except ClientError as exc:
        logger.error("S3 upload failed for key=%s: %s", key, exc)
        raise
