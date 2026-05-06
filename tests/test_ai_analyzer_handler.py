"""
Tests for the AI Analyzer Lambda handler.

These tests stub out boto3 (DynamoDB / SQS / S3) and the GroqClient so the
handler runs entirely in-process without network or AWS access.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from unittest.mock import MagicMock

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Module reload helpers
# ─────────────────────────────────────────────────────────────────────────────
# The handler reads env vars and instantiates AWS clients at import time, so
# every test (or class) needs a clean import after env vars and mocks are in
# place. ``_reload_handler`` does that.

REQUIRED_ENV = {
    "GROQ_API_KEY": "test-key",
    "DYNAMODB_REELS_TABLE": "test-reels-table",
    "SQS_WRITER_QUEUE_URL": "https://sqs.test/writer",
    "S3_BUCKET_NAME": "test-bucket",
    "AWS_REGION": "us-east-1",
}


def _reload_handler(monkeypatch, *, groq_response: str = "", groq_raises: Exception | None = None):
    """Reload the handler module with patched env vars and a stubbed Groq client."""
    for k, v in REQUIRED_ENV.items():
        monkeypatch.setenv(k, v)

    # Forget any previously imported version to pick up env changes
    for mod in ("handler", "groq_client", "schema", "prompts"):
        sys.modules.pop(mod, None)

    # Stub boto3 BEFORE importing handler — the handler creates clients at
    # module load time. We patch the boto3 module itself so any client/resource
    # call returns a MagicMock we can inspect.
    import boto3

    fake_table = MagicMock(name="DDBTable")
    fake_resource = MagicMock(name="DDBResource")
    fake_resource.Table.return_value = fake_table
    fake_sqs = MagicMock(name="SQSClient")
    fake_s3 = MagicMock(name="S3Client")

    def fake_resource_factory(service_name, **_kw):
        if service_name == "dynamodb":
            return fake_resource
        return MagicMock()

    def fake_client_factory(service_name, **_kw):
        return {"sqs": fake_sqs, "s3": fake_s3}.get(service_name, MagicMock())

    monkeypatch.setattr(boto3, "resource", fake_resource_factory)
    monkeypatch.setattr(boto3, "client", fake_client_factory)

    handler = importlib.import_module("handler")
    importlib.reload(handler)  # ensure fresh state

    # Replace the GroqClient instance with one that returns canned text
    fake_response = MagicMock()
    fake_response.content = groq_response
    fake_response.model = "llama-test"
    fake_response.request_id = "req-123"
    fake_response.prompt_tokens = 100
    fake_response.completion_tokens = 50
    fake_response.total_tokens = 150

    fake_groq = MagicMock()
    if groq_raises is not None:
        fake_groq.complete.side_effect = groq_raises
    else:
        fake_groq.complete.return_value = fake_response
    handler._groq = fake_groq

    return handler, fake_table, fake_sqs, fake_s3, fake_groq


# ─────────────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────────────

VALID_ANALYSIS_JSON = json.dumps({
    "title": "Test Title",
    "category": "Programming",
    "subcategory": "Python",
    "quality_score": 8,
    "is_valuable": True,
    "is_actionable": True,
    "key_takeaways": ["one", "two", "three"],
    "summary": "A short summary.",
    "tags": ["python", "tips"],
    "reasoning": "Clear examples.",
})


def _sqs_event(body: dict) -> dict:
    return {
        "Records": [
            {
                "messageId": "msg-1",
                "body": json.dumps(body),
            }
        ]
    }


def _extracted_message(*, transcript: str = "Hello world", caption: str = "") -> dict:
    return {
        "chat_id": "12345",
        "shortcode": "ABC123",
        "url": "https://instagram.com/reel/ABC123/",
        "extracted_content": {
            "content_type": "reel",
            "transcript": transcript,
            "ocr_text": None,
            "caption": caption,
            "username": "creator",
            "has_audio": True,
            "has_visual_text": False,
            "extracted_at": "2026-05-01T10:00:00Z",
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_processes_valid_message(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_response=VALID_ANALYSIS_JSON
        )

        result = handler.lambda_handler(_sqs_event(_extracted_message()), None)

        assert result == {"batchItemFailures": []}
        # Groq was called exactly once (no fallback needed)
        assert groq.complete.call_count == 1
        # DynamoDB row written
        ddb.put_item.assert_called_once()
        # Writer queue message published
        sqs.send_message.assert_called_once()
        # Audit dump to S3
        s3.put_object.assert_called_once()

    def test_dynamodb_row_has_required_fields(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_response=VALID_ANALYSIS_JSON
        )
        handler.lambda_handler(_sqs_event(_extracted_message()), None)

        item = ddb.put_item.call_args.kwargs["Item"]
        # Primary key
        assert item["chat_id"] == "12345"
        assert item["shortcode"] == "ABC123"
        # GSI sort keys promoted to top level
        assert item["category"] == "Programming"
        assert item["quality_score"] == 8
        # Nested analysis preserved
        assert item["analysis"]["title"] == "Test Title"
        assert item["analysis"]["is_valuable"] is True
        # AI metadata captured for observability
        assert item["ai_metadata"]["prompt_variant"] == "v1"
        assert item["ai_metadata"]["groq_request_id"] == "req-123"

    def test_writer_payload_strips_extracted_content(self, monkeypatch):
        """Writer Lambda only needs the analysis — extracted text is large."""
        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_response=VALID_ANALYSIS_JSON
        )
        handler.lambda_handler(_sqs_event(_extracted_message()), None)

        sent_body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert "analysis" in sent_body
        assert "extracted_content" not in sent_body
        assert sent_body["chat_id"] == "12345"
        assert sent_body["shortcode"] == "ABC123"


# ─────────────────────────────────────────────────────────────────────────────
# Empty content fast-path
# ─────────────────────────────────────────────────────────────────────────────

class TestEmptyContent:
    def test_skips_groq_for_empty_content(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_response=VALID_ANALYSIS_JSON
        )
        msg = _extracted_message(transcript="", caption="")
        msg["extracted_content"]["ocr_text"] = ""

        handler.lambda_handler(_sqs_event(msg), None)

        # No Groq call — token usage saved
        groq.complete.assert_not_called()
        # Still wrote a stub to DynamoDB so the orchestrator's duplicate check works
        ddb.put_item.assert_called_once()
        item = ddb.put_item.call_args.kwargs["Item"]
        assert item["analysis"]["is_valuable"] is False
        assert item["analysis"]["quality_score"] == 1
        assert item["ai_metadata"]["prompt_variant"] == "skipped"


# ─────────────────────────────────────────────────────────────────────────────
# Validation fallback
# ─────────────────────────────────────────────────────────────────────────────

class TestValidationFallback:
    def test_retries_with_v2_on_validation_failure(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(monkeypatch)

        # First call returns garbage, second returns valid JSON
        bad_response = MagicMock(
            content="not json at all",
            model="llama-test",
            request_id="req-bad",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        )
        good_response = MagicMock(
            content=VALID_ANALYSIS_JSON,
            model="llama-test",
            request_id="req-good",
            prompt_tokens=100, completion_tokens=50, total_tokens=150,
        )
        groq.complete.side_effect = [bad_response, good_response]

        result = handler.lambda_handler(_sqs_event(_extracted_message()), None)

        assert result == {"batchItemFailures": []}
        assert groq.complete.call_count == 2
        ddb.put_item.assert_called_once()
        item = ddb.put_item.call_args.kwargs["Item"]
        assert item["ai_metadata"]["prompt_variant"] == "v2"

    def test_stub_when_all_variants_fail(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(monkeypatch)

        bad_response = MagicMock(
            content="totally invalid",
            model="llama-test", request_id=None,
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
        )
        groq.complete.return_value = bad_response

        result = handler.lambda_handler(_sqs_event(_extracted_message()), None)

        # Pipeline does NOT fail — it stores a stub instead so SQS doesn't retry forever
        assert result == {"batchItemFailures": []}
        ddb.put_item.assert_called_once()
        item = ddb.put_item.call_args.kwargs["Item"]
        assert item["ai_metadata"]["prompt_variant"] == "stub"
        assert "needs-review" in item["analysis"]["tags"]


# ─────────────────────────────────────────────────────────────────────────────
# Error propagation
# ─────────────────────────────────────────────────────────────────────────────

class TestErrors:
    def test_groq_api_error_reports_batch_item_failure(self, monkeypatch):
        from groq_client import GroqAPIError  # noqa: WPS433 — local import after path setup

        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_raises=GroqAPIError("503 boom", status=503)
        )

        result = handler.lambda_handler(_sqs_event(_extracted_message()), None)

        # SQS should retry — the failed messageId is in batchItemFailures
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}
        ddb.put_item.assert_not_called()

    def test_malformed_record_does_not_take_down_other_records(self, monkeypatch):
        handler, ddb, sqs, s3, groq = _reload_handler(
            monkeypatch, groq_response=VALID_ANALYSIS_JSON
        )

        event = {
            "Records": [
                {"messageId": "bad", "body": "not-json"},
                {"messageId": "good", "body": json.dumps(_extracted_message())},
            ]
        }
        result = handler.lambda_handler(event, None)

        assert {"itemIdentifier": "bad"} in result["batchItemFailures"]
        assert {"itemIdentifier": "good"} not in result["batchItemFailures"]
        # The good record was processed
        ddb.put_item.assert_called_once()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_truncate_for_ddb(self, monkeypatch):
        handler, *_ = _reload_handler(monkeypatch, groq_response=VALID_ANALYSIS_JSON)
        assert handler._truncate_for_ddb(None) is None
        assert handler._truncate_for_ddb("") is None
        assert handler._truncate_for_ddb("short") == "short"
        long = "x" * 50_000
        out = handler._truncate_for_ddb(long, max_len=100)
        assert out is not None
        assert len(out) < len(long)
        assert out.endswith("[truncated]")

    def test_to_ddb_safe_converts_floats_and_drops_nones(self, monkeypatch):
        from decimal import Decimal

        handler, *_ = _reload_handler(monkeypatch, groq_response=VALID_ANALYSIS_JSON)

        out = handler._to_ddb_safe({
            "a": 1.5,
            "b": None,
            "c": [1.0, None, "ok"],
            "d": {"x": 2.5, "y": None},
        })
        assert out == {
            "a": Decimal("1.5"),
            "c": [Decimal("1.0"), "ok"],
            "d": {"x": Decimal("2.5")},
        }
