"""
AI Analyzer Lambda — Stage 3 of the processing pipeline (PRD §5.3, Lambda 3).

Triggered by:  SQS analysis queue (batch_size=1)
Runtime:       Python 3.11 zip (stdlib only — no external dependencies)

Per-message flow:
  1. Parse SQS record → ``extracted_content`` payload published by the
     Content Extractor Lambda (handler.py).
  2. Skip empty content fast — write a low-quality placeholder analysis,
     don't waste a Groq call.
  3. Build the system+user prompt for the active variant.
  4. Call Groq Chat Completions (Llama 3 family, JSON mode).
  5. Parse + validate the JSON response.
  6. Persist the analysis to:
       * S3   → users/{chat_id}/extracted/{shortcode}/analysis.json (full audit)
       * DynamoDB ProcessedReels   (the single row used by digest/search)
  7. Publish a "writer" message to the next SQS queue.

Error handling:
  * Recoverable Groq errors (429, 5xx, network) — propagate so SQS retries.
  * Validation errors after a successful API call — log + retry once with
    variant ``v2`` (more verbose prompt). If that also fails, store the raw
    output and a stub analysis so the user still gets feedback.
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from botocore.exceptions import ClientError


from groq_client import GroqClient 
from prompts import DEFAULT_VARIANT, available_variants, build_messages 
from schema import Analysis, AnalysisValidationError, parse_analysis 

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# ── AWS clients (module-level, reused across warm invocations) ────────────────
_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)
_sqs = boto3.client("sqs", region_name=_region)
_s3 = boto3.client("s3", region_name=_region)

# ── Environment variables ────────────────────────────────────────────────────
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
DYNAMODB_REELS_TABLE = os.environ["DYNAMODB_REELS_TABLE"]
SQS_WRITER_QUEUE_URL = os.environ["SQS_WRITER_QUEUE_URL"]
S3_BUCKET = os.environ.get("S3_BUCKET_NAME", "")  # optional — only used for full audit dump

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
PROMPT_VARIANT = os.environ.get("PROMPT_VARIANT", DEFAULT_VARIANT)
GROQ_TEMPERATURE = float(os.environ.get("GROQ_TEMPERATURE", "0.3"))
GROQ_MAX_TOKENS = int(os.environ.get("GROQ_MAX_TOKENS", "1024"))

# ── Module singletons ────────────────────────────────────────────────────────
_groq = GroqClient(api_key=GROQ_API_KEY, model=GROQ_MODEL)


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
            logger.info("Successfully analysed messageId=%s", message_id)
        except Exception:
            logger.exception("Failed to analyse messageId=%s", message_id)
            batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}


# ─────────────────────────────────────────────────────────────────────────────
# Core processing logic
# ─────────────────────────────────────────────────────────────────────────────

def _process_message(message: dict) -> None:
    chat_id = str(message["chat_id"])
    shortcode = message["shortcode"]
    url = message.get("url", "")

    extracted = message.get("extracted_content") or {}
    content_type = extracted.get("content_type", "unknown")

    transcript = (extracted.get("transcript") or "").strip()
    ocr_text = (extracted.get("ocr_text") or "").strip()
    caption = (extracted.get("caption") or "").strip()

    logger.info(
        "Analyzing shortcode=%s user=%s content_type=%s "
        "(transcript=%d ocr=%d caption=%d chars)",
        shortcode, chat_id, content_type,
        len(transcript), len(ocr_text), len(caption),
    )

    # Fast-path: nothing to analyse → store a placeholder so the user sees
    # something in their digest and we don't waste tokens.
    if not (transcript or ocr_text or caption):
        analysis = _empty_content_stub()
        prompt_variant_used = "skipped"
        token_usage: dict[str, int | None] = {}
        groq_request_id = None
        latency_ms = 0
    else:
        analysis, prompt_variant_used, token_usage, groq_request_id, latency_ms = (
            _analyse_with_groq(
                content_type=content_type,
                transcript=transcript,
                ocr_text=ocr_text,
                caption=caption,
                username=extracted.get("username"),
            )
        )

    now = datetime.now(timezone.utc).isoformat()

    # 1. Persist full audit to S3 (best-effort — never block the pipeline on this)
    _store_analysis_to_s3(chat_id, shortcode, analysis, prompt_variant_used, token_usage)

    # 2. Persist to DynamoDB ProcessedReels (the row used by digest/search)
    _persist_to_dynamodb(
        chat_id=chat_id,
        shortcode=shortcode,
        url=url,
        content_type=content_type,
        extracted=extracted,
        analysis=analysis,
        prompt_variant=prompt_variant_used,
        groq_request_id=groq_request_id,
        latency_ms=latency_ms,
        processed_at=now,
    )

    # 3. Publish to writer queue for Google Docs ingestion
    writer_payload = {
        **{k: v for k, v in message.items() if k != "extracted_content"},
        "analysis": analysis.to_dict(),
        "processed_at": now,
        "prompt_variant": prompt_variant_used,
    }
    _sqs.send_message(
        QueueUrl=SQS_WRITER_QUEUE_URL,
        MessageBody=json.dumps(writer_payload, default=str),
    )
    logger.info("Published to writer queue: shortcode=%s", shortcode)


# ─────────────────────────────────────────────────────────────────────────────
# Groq orchestration
# ─────────────────────────────────────────────────────────────────────────────

def _analyse_with_groq(
    *,
    content_type: str,
    transcript: str,
    ocr_text: str,
    caption: str,
    username: str | None,
) -> tuple[Analysis, str, dict[str, int | None], str | None, int]:
    """Run the Groq call with one validation-failure fallback to v2.

    Returns (analysis, variant_used, token_usage, request_id, latency_ms).
    """
    primary_variant = PROMPT_VARIANT if PROMPT_VARIANT in available_variants() else DEFAULT_VARIANT

    attempts = [primary_variant]
    if "v2" not in attempts:
        attempts.append("v2")

    last_exc: AnalysisValidationError | None = None
    last_raw: str = ""

    for variant in attempts:
        messages = build_messages(
            content_type=content_type,
            transcript=transcript,
            ocr_text=ocr_text,
            caption=caption,
            username=username,
            variant=variant,
        )

        t0 = time.monotonic()
        resp = _groq.complete(
            system=messages["system"],
            user=messages["user"],
            temperature=GROQ_TEMPERATURE,
            max_tokens=GROQ_MAX_TOKENS,
        )
        latency_ms = int((time.monotonic() - t0) * 1000)

        last_raw = resp.content
        token_usage = {
            "prompt_tokens": resp.prompt_tokens,
            "completion_tokens": resp.completion_tokens,
            "total_tokens": resp.total_tokens,
        }

        try:
            analysis = parse_analysis(resp.content)
            logger.info(
                "Groq OK variant=%s tokens=%s request_id=%s latency=%dms",
                variant, resp.total_tokens, resp.request_id, latency_ms,
            )
            return analysis, variant, token_usage, resp.request_id, latency_ms
        except AnalysisValidationError as exc:
            last_exc = exc
            logger.warning(
                "Validation failed for variant=%s: %s — raw=%r",
                variant, exc, resp.content[:300],
            )
            # try next variant

    
    # store a low-confidence stub so the user sees the post and we move on.
    logger.error("All prompt variants failed validation; storing stub. Last error: %s", last_exc)
    return _malformed_response_stub(last_raw), "stub", {}, None, 0


# ─────────────────────────────────────────────────────────────────────────────
# DynamoDB / S3 persistence
# ─────────────────────────────────────────────────────────────────────────────

def _persist_to_dynamodb(
    *,
    chat_id: str,
    shortcode: str,
    url: str,
    content_type: str,
    extracted: dict,
    analysis: Analysis,
    prompt_variant: str,
    groq_request_id: str | None,
    latency_ms: int,
    processed_at: str,
) -> None:
    """Upsert the full analysis row into the ProcessedReels table.

    Keys (chat_id, shortcode) match what the Orchestrator's duplicate-check
    looks for, so re-processing is idempotent.
    """
    table = _dynamodb.Table(DYNAMODB_REELS_TABLE)

    item: dict[str, Any] = {
        "chat_id": chat_id,
        "shortcode": shortcode,
        "url": url,
        "content_type": content_type,
        "platform": "instagram",
        "scraped_at": extracted.get("extracted_at") or processed_at,
        "processed_at": processed_at,

        "extracted_content": {
            "transcript": _truncate_for_ddb(extracted.get("transcript")),
            "ocr_text": _truncate_for_ddb(extracted.get("ocr_text")),
            "caption": _truncate_for_ddb(extracted.get("caption")),
            "has_audio": bool(extracted.get("has_audio")),
            "has_visual_text": bool(extracted.get("has_visual_text")),
        },

        "analysis": {
            "title": analysis.title,
            "category": analysis.category,
            "subcategory": analysis.subcategory,
            "quality_score": analysis.quality_score,
            "is_valuable": analysis.is_valuable,
            "is_actionable": analysis.is_actionable,
            "key_takeaways": analysis.key_takeaways,
            "summary": analysis.summary,
            "tags": analysis.tags,
            "reasoning": analysis.reasoning,
        },

        # GSI sort keys (must be top-level for chat_id-category-index etc.)
        "category": analysis.category,
        "quality_score": analysis.quality_score,

        "metadata": {
            "username": extracted.get("username"),
            "full_name": extracted.get("full_name"),
            "like_count": extracted.get("like_count"),
            "view_count": extracted.get("view_count"),
            "scraper_method": extracted.get("scraper_method"),
        },

        "ai_metadata": {
            "model": GROQ_MODEL,
            "prompt_variant": prompt_variant,
            "groq_request_id": groq_request_id,
            "latency_ms": latency_ms,
        },

        "status": "analysed",
    }

    safe_item = _to_ddb_safe(item)

    try:
        table.put_item(Item=safe_item)
    except ClientError as exc:
        logger.error("DynamoDB put_item failed for shortcode=%s: %s", shortcode, exc)
        raise


def _store_analysis_to_s3(
    chat_id: str,
    shortcode: str,
    analysis: Analysis,
    prompt_variant: str,
    token_usage: dict[str, int | None],
) -> None:
    if not S3_BUCKET:
        return  # optional sink

    key = f"users/{chat_id}/extracted/{shortcode}/analysis.json"
    body = json.dumps(
        {
            "analysis": analysis.to_dict(),
            "model": GROQ_MODEL,
            "prompt_variant": prompt_variant,
            "token_usage": token_usage,
            "stored_at": datetime.now(timezone.utc).isoformat(),
        },
        default=str,
    )
    try:
        _s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json",
        )
    except ClientError as exc:
        # Non-fatal: DynamoDB still has the canonical record
        logger.warning("S3 audit dump failed (non-fatal) key=%s: %s", key, exc)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _empty_content_stub() -> Analysis:
    """Return a placeholder analysis for posts where we extracted no text."""
    return Analysis(
        title="(no extractable content)",
        category="Other",
        subcategory="",
        quality_score=1,
        is_valuable=False,
        is_actionable=False,
        key_takeaways=["No transcript, OCR, or caption text was extracted."],
        summary="The post had no recoverable text. AI analysis was skipped.",
        tags=["empty"],
        reasoning="Skipped Groq call: extractor returned no text.",
    )


def _malformed_response_stub(raw: str) -> Analysis:
    """Stub used when every prompt variant returns invalid JSON."""
    snippet = (raw or "")[:200].replace("\n", " ")
    return Analysis(
        title="(analysis unavailable)",
        category="Other",
        subcategory="",
        quality_score=1,
        is_valuable=False,
        is_actionable=False,
        key_takeaways=["Model output could not be parsed into structured form."],
        summary="The AI model returned an unexpected response format.",
        tags=["needs-review"],
        reasoning=f"Model output snippet: {snippet}",
    )


def _truncate_for_ddb(text: str | None, max_len: int = 30_000) -> str | None:
    """DynamoDB items have a 400 KB limit; cap large fields defensively."""
    if not text:
        return None
    if len(text) <= max_len:
        return text
    return text[:max_len] + "… [truncated]"


def _to_ddb_safe(value: Any) -> Any:
    """Recursively convert floats → Decimal and drop ``None`` values."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_ddb_safe(v) for k, v in value.items() if v is not None}
    if isinstance(value, list):
        return [_to_ddb_safe(v) for v in value if v is not None]
    return value
