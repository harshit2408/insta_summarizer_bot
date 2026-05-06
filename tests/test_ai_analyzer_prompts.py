"""Tests for the AI Analyzer prompt builder."""

from __future__ import annotations

from prompts import (  # type: ignore[import-not-found]
    DEFAULT_VARIANT,
    available_variants,
    build_messages,
)


class TestVariants:
    def test_v1_and_v2_available(self):
        variants = available_variants()
        assert "v1" in variants
        assert "v2" in variants

    def test_default_is_v1(self):
        assert DEFAULT_VARIANT == "v1"

    def test_unknown_variant_falls_back_to_default(self):
        msgs = build_messages(
            content_type="reel",
            transcript="hello",
            ocr_text=None,
            caption=None,
            variant="does-not-exist",
        )
        # Should match v1 system prompt
        v1 = build_messages(
            content_type="reel", transcript="hello", ocr_text=None, caption=None, variant="v1"
        )
        assert msgs["system"] == v1["system"]


class TestUserPrompt:
    def test_includes_content_type(self):
        msgs = build_messages(
            content_type="carousel",
            transcript=None,
            ocr_text="some ocr",
            caption=None,
        )
        assert "CONTENT TYPE: carousel" in msgs["user"]

    def test_marks_missing_blocks_explicitly(self):
        # Don't silently omit empty fields — the LLM should know they're absent
        msgs = build_messages(
            content_type="reel",
            transcript=None,
            ocr_text=None,
            caption="A short caption",
        )
        assert "TRANSCRIPT: (none)" in msgs["user"]
        assert "ON-SCREEN TEXT (OCR): (none)" in msgs["user"]
        assert "A short caption" in msgs["user"]

    def test_includes_creator_when_present(self):
        msgs = build_messages(
            content_type="reel",
            transcript="hi",
            ocr_text=None,
            caption=None,
            username="pythontips",
        )
        assert "@pythontips" in msgs["user"]

    def test_omits_creator_line_when_missing(self):
        msgs = build_messages(
            content_type="reel",
            transcript="hi",
            ocr_text=None,
            caption=None,
            username=None,
        )
        assert "CREATOR:" not in msgs["user"]

    def test_truncates_huge_transcript(self):
        huge = "abc " * 5000  # ~20k chars
        msgs = build_messages(
            content_type="reel",
            transcript=huge,
            ocr_text=None,
            caption=None,
        )
        # Transcript block must have been capped at 6000 chars + truncation marker
        assert "[truncated]" in msgs["user"]
        assert len(msgs["user"]) < 8_000


class TestSystemPrompt:
    def test_lists_categories(self):
        msgs = build_messages(
            content_type="reel", transcript="x", ocr_text=None, caption=None, variant="v1"
        )
        # The category list is interpolated from ALLOWED_CATEGORIES
        assert "Programming" in msgs["system"]
        assert "Other" in msgs["system"]

    def test_v2_mentions_step_by_step(self):
        msgs = build_messages(
            content_type="reel", transcript="x", ocr_text=None, caption=None, variant="v2"
        )
        # v2 uses chain-of-thought language; v1 doesn't
        assert "think" in msgs["system"].lower() or "before" in msgs["system"].lower()
