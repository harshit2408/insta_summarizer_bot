"""
Analysis schema and validators for the AI Analyzer Lambda.

The Groq LLM is instructed to return JSON in a strict format. Even with a
low temperature this is best-effort: models occasionally emit extra prose,
trailing commas, missing fields, or out-of-range values. This module:

  * Cleans the raw model output (strips markdown fences, prose preambles)
  * Parses it as JSON
  * Validates and coerces fields into a normalised :class:`Analysis`
  * Raises :class:`AnalysisValidationError` with a precise reason on failure

Pure stdlib only — keeps the Lambda zip small and cold start fast.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any


# Allowed top-level categories. The LLM is instructed to pick one of these
# (or "Other"). Keep this list aligned with the prompt template in prompts.py.
ALLOWED_CATEGORIES: tuple[str, ...] = (
    "Programming",
    "Career",
    "Productivity",
    "Finance",
    "Health",
    "Education",
    "Design",
    "Marketing",
    "AI",
    "Entrepreneurship",
    "Lifestyle",
    "Other",
)

MAX_TITLE_LEN = 80
MAX_TAKEAWAYS = 5
MIN_TAKEAWAYS = 1
MAX_TAGS = 5
MAX_SUMMARY_LEN = 600

# Max lengths for suggested section fields
MAX_SECTION_KEY_LEN = 40
MAX_SECTION_TITLE_LEN = 80


class AnalysisValidationError(ValueError):
    """Raised when the LLM response cannot be parsed into a valid Analysis."""


@dataclass
class SuggestedSection:
    """A new section the AI suggests when content doesn't fit existing ones."""
    key: str    # CamelCase identifier, e.g. "Cooking"
    emoji: str  # single emoji, e.g. "🍳"
    title: str  # ALL-CAPS title, e.g. "COOKING & RECIPES"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass
class Analysis:
    """Validated AI analysis of a single Instagram post.

    Field shapes match the ``analysis`` block stored in the
    ``ProcessedReels`` DynamoDB table (see PRD §7.1).
    """

    title: str
    category: str
    subcategory: str
    quality_score: int
    is_valuable: bool
    is_actionable: bool
    key_takeaways: list[str]
    summary: str
    tags: list[str]
    reasoning: str
    # Optional — only set when the AI suggests a new section
    new_section: bool = False
    suggested_section: SuggestedSection | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def parse_analysis(
    raw_text: str,
    *,
    allowed_categories: tuple[str, ...] | None = None,
) -> Analysis:
    """Parse an LLM response string into a validated :class:`Analysis`.

    Steps:
      1. Strip markdown fences and leading/trailing prose.
      2. Locate the outermost JSON object (handles models that wrap JSON in
         explanatory text despite our prompt).
      3. ``json.loads`` and validate every field.

    Raises :class:`AnalysisValidationError` if any step fails.
    """
    if not raw_text or not raw_text.strip():
        raise AnalysisValidationError("Empty response from model")

    cleaned = _strip_code_fences(raw_text)
    json_blob = _extract_json_object(cleaned)

    try:
        data = json.loads(json_blob)
    except json.JSONDecodeError as exc:
        raise AnalysisValidationError(
            f"Model output was not valid JSON: {exc.msg} at pos {exc.pos}"
        ) from exc

    if not isinstance(data, dict):
        raise AnalysisValidationError(
            f"Expected JSON object, got {type(data).__name__}"
        )

    return _validate_and_coerce(data, allowed_categories=allowed_categories)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Remove ```json ... ``` markdown wrappers some models add anyway."""
    return _FENCE_RE.sub("", text).strip()


def _extract_json_object(text: str) -> str:
    """Return the substring spanning the first balanced ``{...}`` block.

    Tolerates models that prefix the JSON with explanatory prose
    ("Here's the analysis: { ... }"). Uses a simple brace counter that
    respects strings and escapes — sufficient for well-formed JSON output.
    """
    start = text.find("{")
    if start == -1:
        raise AnalysisValidationError("No JSON object found in response")

    depth = 0
    in_string = False
    escape = False

    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue

        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    raise AnalysisValidationError("Unbalanced braces in JSON response")


def _validate_and_coerce(
    data: dict[str, Any],
    *,
    allowed_categories: tuple[str, ...] | None = None,
) -> Analysis:
    """Apply field-by-field validation, raising on any structural problem.

    ``allowed_categories`` overrides ALLOWED_CATEGORIES when the caller
    supplies the user's custom section keys for category normalisation.
    """
    cats = allowed_categories if allowed_categories is not None else ALLOWED_CATEGORIES

    title = _clean_str(data.get("title"), "title", required=True)
    if len(title) > MAX_TITLE_LEN:
        title = title[: MAX_TITLE_LEN].rstrip() + "…"

    category_raw = _clean_str(data.get("category"), "category", required=True)
    category = _normalise_category(category_raw, allowed=cats)

    subcategory = _clean_str(data.get("subcategory"), "subcategory", required=False) or ""

    quality_score = _coerce_int(
        data.get("quality_score"), "quality_score", lo=1, hi=10
    )

    is_valuable = _coerce_bool(data.get("is_valuable"), "is_valuable")
    is_actionable = _coerce_bool(data.get("is_actionable"), "is_actionable")

    key_takeaways = _coerce_str_list(
        data.get("key_takeaways"),
        "key_takeaways",
        min_len=MIN_TAKEAWAYS,
        max_len=MAX_TAKEAWAYS,
    )

    summary = _clean_str(data.get("summary"), "summary", required=True)
    if len(summary) > MAX_SUMMARY_LEN:
        summary = summary[:MAX_SUMMARY_LEN].rstrip() + "…"

    tags_raw = _coerce_str_list(
        data.get("tags"), "tags", min_len=1, max_len=MAX_TAGS, allow_empty=True
    )
    tags = [_normalise_tag(t) for t in tags_raw]

    reasoning = _clean_str(data.get("reasoning"), "reasoning", required=False) or ""

    # ── Optional new-section suggestion ──────────────────────────────────────
    new_section = bool(data.get("new_section", False))
    suggested_section: SuggestedSection | None = None

    if new_section:
        raw_ss = data.get("suggested_section")
        if isinstance(raw_ss, dict):
            ss_key = _clean_str(raw_ss.get("key"), "suggested_section.key", required=True)
            ss_key = ss_key[:MAX_SECTION_KEY_LEN]
            ss_emoji = _clean_str(raw_ss.get("emoji"), "suggested_section.emoji", required=False) or "📝"
            ss_title = _clean_str(raw_ss.get("title"), "suggested_section.title", required=False) or ss_key.upper()
            ss_title = ss_title[:MAX_SECTION_TITLE_LEN]
            suggested_section = SuggestedSection(key=ss_key, emoji=ss_emoji, title=ss_title)
        else:
            # Model said new_section=true but didn't provide the dict — treat as false
            new_section = False

    return Analysis(
        title=title,
        category=category,
        subcategory=subcategory,
        quality_score=quality_score,
        is_valuable=is_valuable,
        is_actionable=is_actionable,
        key_takeaways=key_takeaways,
        summary=summary,
        tags=tags,
        reasoning=reasoning,
        new_section=new_section,
        suggested_section=suggested_section,
    )


def _clean_str(value: Any, field_name: str, *, required: bool) -> str:
    if value is None:
        if required:
            raise AnalysisValidationError(f"Missing required field '{field_name}'")
        return ""
    if not isinstance(value, str):
        raise AnalysisValidationError(
            f"Field '{field_name}' must be a string, got {type(value).__name__}"
        )
    cleaned = value.strip()
    if required and not cleaned:
        raise AnalysisValidationError(f"Field '{field_name}' must not be empty")
    return cleaned


def _coerce_int(value: Any, field_name: str, *, lo: int, hi: int) -> int:
    # Accept ints, floats, and numeric strings — models occasionally return "8"
    try:
        if isinstance(value, bool):  # bool is subclass of int — reject
            raise TypeError
        if isinstance(value, (int, float)):
            n = int(round(value))
        elif isinstance(value, str):
            n = int(round(float(value.strip())))
        else:
            raise TypeError
    except (TypeError, ValueError) as exc:
        raise AnalysisValidationError(
            f"Field '{field_name}' must be an integer in [{lo}, {hi}], got {value!r}"
        ) from exc

    if not (lo <= n <= hi):
        raise AnalysisValidationError(
            f"Field '{field_name}' = {n} is out of range [{lo}, {hi}]"
        )
    return n


_TRUTHY = {"true", "yes", "1", "valuable", "actionable"}
_FALSY = {"false", "no", "0", "not valuable", "not actionable"}


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        v = value.strip().lower()
        if v in _TRUTHY:
            return True
        if v in _FALSY:
            return False
    raise AnalysisValidationError(
        f"Field '{field_name}' must be a boolean, got {value!r}"
    )


def _coerce_str_list(
    value: Any,
    field_name: str,
    *,
    min_len: int,
    max_len: int,
    allow_empty: bool = False,
) -> list[str]:
    if value is None:
        if allow_empty:
            return []
        raise AnalysisValidationError(f"Missing required field '{field_name}'")

    # Some models return a single string when only one item is found.
    if isinstance(value, str):
        value = [value]

    if not isinstance(value, list):
        raise AnalysisValidationError(
            f"Field '{field_name}' must be a list, got {type(value).__name__}"
        )

    items: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise AnalysisValidationError(
                f"{field_name}[{i}] must be a string, got {type(item).__name__}"
            )
        s = item.strip()
        if s:
            items.append(s)

    if not items and allow_empty:
        return []

    if len(items) < min_len:
        raise AnalysisValidationError(
            f"Field '{field_name}' must have at least {min_len} item(s), got {len(items)}"
        )

    return items[:max_len]


def _normalise_category(
    raw: str,
    *,
    allowed: tuple[str, ...] | None = None,
) -> str:
    """Snap LLM category to the closest member of the allowed list.

    Falls back to "Other" when no match is found. If "Other" is not in
    the allowed list either, returns the last element of the list.
    """
    cats = allowed if allowed is not None else ALLOWED_CATEGORIES
    cleaned = raw.strip()

    # Case-insensitive direct match
    for c in cats:
        if cleaned.lower() == c.lower():
            return c

    # Common synonyms (only relevant when default categories are active)
    synonyms: dict[str, str] = {
        "Tech": "Programming",
        "Technology": "Programming",
        "Coding": "Programming",
        "Software": "Programming",
        "Money": "Finance",
        "Investing": "Finance",
        "Wellness": "Health",
        "Fitness": "Health",
        "Learning": "Education",
        "Study": "Education",
        "Business": "Entrepreneurship",
        "Startup": "Entrepreneurship",
        "Ml": "AI",
        "Machine Learning": "AI",
    }
    title_form = cleaned.title()
    if title_form in synonyms:
        mapped = synonyms[title_form]
        # Only use the synonym if it's actually in the allowed list
        for c in cats:
            if c.lower() == mapped.lower():
                return c

    # Fall back to "Other" if present
    for c in cats:
        if c.lower() == "other":
            return c

    # Last resort: return the last entry in the list
    return cats[-1] if cats else "Other"


_TAG_RE = re.compile(r"[^a-z0-9\-]+")


def _normalise_tag(tag: str) -> str:
    """Lowercase and slug-ify tag: 'Python Tips!' → 'python-tips'."""
    s = tag.strip().lower().lstrip("#")
    s = _TAG_RE.sub("-", s).strip("-")
    return s or tag.strip().lower()
