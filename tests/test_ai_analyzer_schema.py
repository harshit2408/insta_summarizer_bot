"""Tests for the AI Analyzer schema parser/validator."""

from __future__ import annotations

import json

import pytest

from schema import (  # type: ignore[import-not-found]  # added to sys.path by conftest.py
    ALLOWED_CATEGORIES,
    Analysis,
    AnalysisValidationError,
    parse_analysis,
)


# Reusable known-good payload — every test that needs valid JSON starts from this
GOOD = {
    "title": "Python list comprehensions explained",
    "category": "Programming",
    "subcategory": "Python",
    "quality_score": 8,
    "is_valuable": True,
    "is_actionable": True,
    "key_takeaways": [
        "Use [x for x in xs] for transformations",
        "Add an if to filter",
        "Nested comprehensions for 2D data",
    ],
    "summary": "Quick tutorial on Python list comprehensions for beginners.",
    "tags": ["python", "lists", "comprehensions"],
    "reasoning": "Clear examples with code.",
}


def _payload(**overrides) -> str:
    data = {**GOOD, **overrides}
    return json.dumps(data)


# ─────────────────────────────────────────────────────────────────────────────
# Happy path
# ─────────────────────────────────────────────────────────────────────────────

class TestHappyPath:
    def test_parses_clean_json(self):
        analysis = parse_analysis(_payload())
        assert isinstance(analysis, Analysis)
        assert analysis.title == GOOD["title"]
        assert analysis.category == "Programming"
        assert analysis.quality_score == 8
        assert analysis.is_valuable is True
        assert len(analysis.key_takeaways) == 3

    def test_strips_json_code_fence(self):
        wrapped = "```json\n" + _payload() + "\n```"
        analysis = parse_analysis(wrapped)
        assert analysis.title == GOOD["title"]

    def test_strips_plain_code_fence(self):
        wrapped = "```\n" + _payload() + "\n```"
        analysis = parse_analysis(wrapped)
        assert analysis.title == GOOD["title"]

    def test_extracts_json_after_prose(self):
        prefixed = "Sure! Here is the analysis:\n" + _payload() + "\nLet me know!"
        analysis = parse_analysis(prefixed)
        assert analysis.category == "Programming"

    def test_handles_braces_inside_strings(self):
        # The brace-counter must respect quoted strings or it would split early
        payload = _payload(summary='Use {x: 1} dicts to map')
        analysis = parse_analysis(payload)
        assert "{x: 1}" in analysis.summary


# ─────────────────────────────────────────────────────────────────────────────
# Field validation
# ─────────────────────────────────────────────────────────────────────────────

class TestRequiredFields:
    @pytest.mark.parametrize("missing", ["title", "category", "quality_score", "summary"])
    def test_missing_required_field_raises(self, missing: str):
        data = dict(GOOD)
        data.pop(missing)
        with pytest.raises(AnalysisValidationError, match=missing):
            parse_analysis(json.dumps(data))

    def test_empty_string_for_required_raises(self):
        with pytest.raises(AnalysisValidationError, match="title"):
            parse_analysis(_payload(title=""))


class TestQualityScore:
    @pytest.mark.parametrize("score", [0, -1, 11, 100])
    def test_out_of_range_raises(self, score):
        with pytest.raises(AnalysisValidationError, match="quality_score"):
            parse_analysis(_payload(quality_score=score))

    def test_string_score_is_coerced(self):
        analysis = parse_analysis(_payload(quality_score="7"))
        assert analysis.quality_score == 7

    def test_float_score_is_rounded(self):
        analysis = parse_analysis(_payload(quality_score=7.6))
        assert analysis.quality_score == 8

    def test_garbage_score_raises(self):
        with pytest.raises(AnalysisValidationError):
            parse_analysis(_payload(quality_score="excellent"))


class TestCategoryNormalisation:
    def test_known_category_passes(self):
        assert parse_analysis(_payload(category="Programming")).category == "Programming"

    def test_case_insensitive(self):
        assert parse_analysis(_payload(category="programming")).category == "Programming"

    def test_synonym_mapped(self):
        assert parse_analysis(_payload(category="Tech")).category == "Programming"
        assert parse_analysis(_payload(category="Money")).category == "Finance"
        assert parse_analysis(_payload(category="Machine Learning")).category == "AI"

    def test_unknown_falls_back_to_other(self):
        assert parse_analysis(_payload(category="Cooking")).category == "Other"

    def test_all_allowed_categories_round_trip(self):
        for cat in ALLOWED_CATEGORIES:
            assert parse_analysis(_payload(category=cat)).category == cat


class TestBooleans:
    @pytest.mark.parametrize("value, expected", [
        (True, True),
        (False, False),
        ("true", True),
        ("False", False),
        ("yes", True),
        ("NO", False),
        (1, True),
        (0, False),
    ])
    def test_bool_coercion(self, value, expected):
        analysis = parse_analysis(_payload(is_valuable=value))
        assert analysis.is_valuable is expected

    def test_invalid_bool_raises(self):
        with pytest.raises(AnalysisValidationError, match="is_valuable"):
            parse_analysis(_payload(is_valuable="maybe"))


class TestTakeaways:
    def test_at_least_one_takeaway_required(self):
        with pytest.raises(AnalysisValidationError, match="key_takeaways"):
            parse_analysis(_payload(key_takeaways=[]))

    def test_caps_at_five(self):
        many = [f"Tip {i}" for i in range(10)]
        analysis = parse_analysis(_payload(key_takeaways=many))
        assert len(analysis.key_takeaways) == 5

    def test_string_promoted_to_list(self):
        # Some models return a string when there's only one takeaway
        analysis = parse_analysis(_payload(key_takeaways="Single tip"))
        assert analysis.key_takeaways == ["Single tip"]

    def test_blank_items_dropped(self):
        analysis = parse_analysis(_payload(key_takeaways=["Real tip", "", "  ", "Another"]))
        assert analysis.key_takeaways == ["Real tip", "Another"]

    def test_non_string_item_raises(self):
        with pytest.raises(AnalysisValidationError):
            parse_analysis(_payload(key_takeaways=["ok", 42]))


class TestTags:
    def test_tags_normalised_to_slugs(self):
        analysis = parse_analysis(_payload(tags=["Python Tips!", "#beginners", "Side-Projects"]))
        assert analysis.tags == ["python-tips", "beginners", "side-projects"]

    def test_caps_at_five_tags(self):
        many = [f"tag{i}" for i in range(10)]
        analysis = parse_analysis(_payload(tags=many))
        assert len(analysis.tags) == 5


class TestTitle:
    def test_long_title_truncated(self):
        long = "x" * 200
        analysis = parse_analysis(_payload(title=long))
        assert len(analysis.title) <= 81  # MAX_TITLE_LEN(80) + ellipsis


# ─────────────────────────────────────────────────────────────────────────────
# Malformed input
# ─────────────────────────────────────────────────────────────────────────────

class TestMalformed:
    def test_empty_string(self):
        with pytest.raises(AnalysisValidationError, match="Empty"):
            parse_analysis("")

    def test_whitespace_only(self):
        with pytest.raises(AnalysisValidationError, match="Empty"):
            parse_analysis("   \n  ")

    def test_no_json_object(self):
        with pytest.raises(AnalysisValidationError, match="No JSON"):
            parse_analysis("Sorry, I cannot help with that.")

    def test_unbalanced_braces(self):
        with pytest.raises(AnalysisValidationError, match="Unbalanced"):
            parse_analysis('{"title": "no closing brace"')

    def test_invalid_json_inside_braces(self):
        with pytest.raises(AnalysisValidationError, match="not valid JSON"):
            parse_analysis('{"title": "x", "category": ,}')

    def test_top_level_array_rejected(self):
        # _extract_json_object would still find the {} inside an array element,
        # but plain "[1,2,3]" has no { so we get "No JSON"
        with pytest.raises(AnalysisValidationError):
            parse_analysis("[1, 2, 3]")
