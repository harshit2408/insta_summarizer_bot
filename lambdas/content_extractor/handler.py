"""
Content Extractor Lambda — Stage 2 of the processing pipeline.

Triggered by:  SQS extraction queue (batch_size=1)
Runtime:       Python 3.11 Docker container (includes ffmpeg, Whisper, EasyOCR)

Processing flow per message:
  1. Parse SQS message (chat_id, shortcode, url)
  2. Scrape Instagram URL → download media to /tmp
  3. Video reels:   extract audio → transcribe with Faster-Whisper
  4. Image/carousel: run EasyOCR on each image
  5. Video reels with on-screen text: also run OCR on key frames
  6. Save extracted JSON to S3
  7. Publish message to SQS analysis queue (for AI Analyzer — Phase 2)

Error handling:
  - Any unhandled exception propagates, causing SQS to retry (up to maxReceiveCount=3)
  - After 3 failures the message goes to the DLQ for manual inspection
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from audio import transcribe_video
from ocr import extract_text_from_images, extract_text_from_video_frames

# Shared modules are COPY-ed into the container image (see Dockerfile)
from scraper.instagram_scraper import InstagramScraper
from models.content_models import ScrapeStatus, ContentType

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── AWS clients ───────────────────────────────────────────────────────────────
_region = os.environ.get("AWS_REGION", "ap-south-1")
_s3 = boto3.client("s3", region_name=_region)
_sqs = boto3.client("sqs", region_name=_region)

# ── Environment variables ─────────────────────────────────────────────────────
S3_BUCKET = os.environ["S3_BUCKET_NAME"]
SQS_ANALYSIS_QUEUE_URL = os.environ["SQS_ANALYSIS_QUEUE_URL"]
WHISPER_MODEL_SIZE = os.environ.get("WHISPER_MODEL_SIZE", "base")


# ─────────────────────────────────────────────────────────────────────────────
# Lambda entry point
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event: dict, context) -> dict:
    """
    SQS trigger handler.

    SQS sends a list of records; we process one at a time (batch_size=1).
    Returning batchItemFailures allows partial-batch failures — each failed
    message will be retried independently.
    """
    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")
        try:
            body = json.loads(record["body"])
            _process_message(body)
            logger.info("Successfully processed messageId=%s", message_id)
        except Exception:
            logger.exception("Failed to process messageId=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ─────────────────────────────────────────────────────────────────────────────
# Core processing logic
# ─────────────────────────────────────────────────────────────────────────────

def _process_message(message: dict) -> None:
    chat_id: str = message["chat_id"]
    shortcode: str = message["shortcode"]
    url: str = message["url"]

    logger.info("Processing shortcode=%s for user=%s", shortcode, chat_id)

    with tempfile.TemporaryDirectory(dir="/tmp", prefix=f"insta_{shortcode}_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        # ── Step 1: Scrape and download media ─────────────────────────────────
        scraper = InstagramScraper(download_dir=tmp_path, download_media=True)
        result = scraper.scrape(url)

        if result.status not in (ScrapeStatus.SUCCESS, ScrapeStatus.PARTIAL):
            raise RuntimeError(
                f"Scraping failed for {shortcode}: [{result.status.value}] {result.error_message}"
            )

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

        # ── Step 2: Audio transcription (for video reels) ─────────────────────
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
                # Sample up to MAX_OCR_FRAMES=4 frames at wider 10s interval to stay within budget.
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

        # ── Step 3: Image OCR (for photos and carousels) ──────────────────────
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

        # ── Step 4: Save extracted content to S3 ─────────────────────────────
        s3_key = f"users/{chat_id}/extracted/{shortcode}/extracted.json"
        _upload_to_s3(s3_key, extracted)
        logger.info("Saved extracted content to s3://%s/%s", S3_BUCKET, s3_key)

        # ── Step 5: Publish to analysis queue ────────────────────────────────
        analysis_payload = {
            **message,  # pass through original message fields
            "extracted_content": extracted,
            "s3_extracted_key": s3_key,
        }
        _sqs.send_message(
            QueueUrl=SQS_ANALYSIS_QUEUE_URL,
            MessageBody=json.dumps(analysis_payload),
        )
        logger.info("Published to analysis queue: shortcode=%s", shortcode)


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
