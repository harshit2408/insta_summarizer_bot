"""
Prompt templates for the AI Analyzer Lambda.

We expose two prompt **variants** so the Lambda can run an A/B test in
production simply by flipping the ``PROMPT_VARIANT`` environment variable
between ``"v1"`` and ``"v2"``. Both variants ask the model for the same
JSON schema (validated by :mod:`schema`) but differ in tone and structure.

Variant guide:
  * ``v1`` — concise, instruction-heavy, low temperature (default).
  * ``v2`` — adds chain-of-thought style "think step by step" prefix and
              richer examples, useful for low-quality / sparse content.

Dynamic sections:
  When a user has custom sections, the category list in the prompt is
  replaced with the user's section keys. If the content doesn't match any
  existing section the model may respond with ``"new_section": true`` and a
  suggested ``"suggested_section"`` dict. The writer lambda will create the
  section automatically.

When iterating on prompts, prefer adding a new variant over editing an
existing one — that way old logged outputs remain reproducible.
"""

from __future__ import annotations

from typing import TypedDict

from schema import ALLOWED_CATEGORIES  # bundled as a sibling module in the Lambda zip


class PromptMessages(TypedDict):
    system: str
    user: str


# ─────────────────────────────────────────────────────────────────────────────
# System prompt templates
# ─────────────────────────────────────────────────────────────────────────────

_SYSTEM_V1_TEMPLATE = """\
You are an expert learning curator. Your job is to analyse Instagram content
(reels, posts, carousels) and extract a COMPLETE, DEEP knowledge transfer so the
reader never needs to go back to the original post. Every insight, technique,
number, warning, and tool mentioned must be captured.

Always respond with a SINGLE valid JSON object. No prose, no markdown fences,
no commentary — just the JSON. Every field is required.

The user's current sections (categories) are:
{category_list}

Schema:
{{
  "title":          string,   // <= 60 chars, descriptive, no clickbait
  "category":       string,   // MUST be exactly one of the section keys listed above
  "subcategory":    string,   // narrower topic (e.g. "CI/CD", "Resume Writing")
  "quality_score":  integer,  // 1-10 (see rubric)
  "is_valuable":    boolean,
  "is_actionable":  boolean,
  "core_argument":  string,   // ONE sentence: the creator's central claim or thesis
  "without_this":   string,   // What specific knowledge would you LACK without reading this?
  "chapters": [               // Mirror the source structure. One object per section/slide/chapter.
    {{
      "heading":  string,     // The section title as the creator framed it
      "points": [string, ...] // DETAILED, self-contained points. Each must transfer full understanding.
                              // BAD: "Slow pipelines are a problem"
                              // GOOD: "Pipelines >30 min get ignored — devs stop running them locally.
                              //        Fix: cache dependencies aggressively at the build layer."
    }}
  ],
  "actionable_steps": [string, ...], // Concrete things the reader can do RIGHT NOW. Specific, not vague.
                                      // BAD: "Use a staging environment"
                                      // GOOD: "Never deploy to prod without a staging env that mirrors it —
                                      //        seed it with prod-like data. It's the cheapest insurance."
  "tags":           [string, ...],   // 3-5 lowercase tags, no '#'
  "reasoning":      string,          // 1-2 sentences explaining the quality_score
  "new_section":    boolean,         // true ONLY if content clearly belongs to a NEW category not in the list
  "suggested_section": {{            // REQUIRED when new_section=true, omit otherwise
    "key":   string,
    "emoji": string,
    "title": string
  }}
}}

Quality rubric:
  9-10 — Original, deep, well-explained insights with specific examples, numbers, or code.
  7-8  — Solid, practical advice; clearly conveyed with concrete details.
  5-6  — Surface-level; useful reminders but nothing new or specific.
  3-4  — Vague, low effort, mostly hype or generic advice.
  1-2  — Promotional, misleading, or empty.

Mark is_valuable=false for pure ads, motivational fluff with no substance,
or content where the audio/text is too sparse to extract any insight.

CRITICAL EXTRACTION RULES:
- Do NOT summarise away specifics. If the source mentions a number (e.g. "4 min avg deploy"),
  a tool name (e.g. "Buildkite"), or a named concept (e.g. "DORA metrics"), it MUST appear
  in your output.
- Each "point" in chapters must be a complete thought a reader can understand WITHOUT the
  original post. No orphan bullets like "Tests that always pass" — explain WHY it's a problem
  and WHAT to do about it.
- "actionable_steps" must be distinct from "chapters". Chapters = what you learn.
  Actions = what you do with that learning.
- Mirror the source's chapter/slide structure in your chapters array. Do not collapse
  multiple sections into one heading.

IMPORTANT: Only set new_section=true if the content clearly does NOT fit any
existing section. When in doubt, use "Other".
"""

_SYSTEM_V2_TEMPLATE = """\
You are a meticulous learning analyst. You read raw Instagram content
(transcripts, OCR text, captions) and produce structured, exhaustive summaries
that make the original post unnecessary to read.

Before answering, work through these questions silently:
  1. What is the creator's single central argument or claim?
  2. How many distinct sections/slides/chapters does this content have? List them.
  3. For each section: what are ALL the specific facts, numbers, tools, warnings,
     and techniques mentioned? Do not discard any.
  4. Which points are learnings (facts/concepts) vs actions (things to do)?
  5. Does it fit one of the user's sections, or does it need a new one?

The user's current sections (categories) are:
{category_list}

Then respond with ONE JSON object — no preamble, no markdown, no trailing text.

Required fields:
  title             (string, <= 60 chars, descriptive)
  category          (MUST be exactly one of the section keys above)
  subcategory       (string)
  quality_score     (integer 1-10)
  is_valuable       (boolean)
  is_actionable     (boolean)
  core_argument     (string — one sentence: the creator's main claim)
  without_this      (string — what specific knowledge would you lack without this?)
  chapters          (array of objects: {{ "heading": string, "points": [string, ...] }}
                     — one object per section/slide. Points must be self-contained and specific.
                     BAD point: "Secrets in YAML is bad"
                     GOOD point: "Never put secrets in plain YAML. Use the platform's secret store.
                                  If a secret leaks, rotate it immediately.")
  actionable_steps  (array of strings — concrete, specific actions. Not generic advice.)
  tags              (array of 3-5 lowercase tags, no '#')
  reasoning         (string, 1-2 sentences on quality_score)
  new_section       (boolean — true ONLY when content needs a truly new section)
  suggested_section (object with key/emoji/title — REQUIRED when new_section=true)

Calibration:
  10 - Rare, deep, specific. Numbers, named techniques, worked examples.
   8 - Useful technique with concrete detail. e.g. "Three caching strategies with trade-offs."
   6 - Reminder of common knowledge. Useful but nothing new.
   4 - Vague platitudes. e.g. "Ship often and iterate."
   2 - Pure self-promotion or product ad.

CRITICAL: If the source content has 7 slides, your chapters array must have 7 objects.
Do not collapse sections. Do not discard specifics (numbers, tool names, named metrics).
Every point must transfer complete understanding — a reader must not need to revisit
the original post.
"""


def _build_system_prompt(template: str, category_keys: list[str]) -> str:
    """Substitute the user's current section keys into a system prompt template."""
    formatted = "\n".join(f"  - {k}" for k in category_keys)
    return template.format(category_list=formatted)


# ─────────────────────────────────────────────────────────────────────────────
# User prompt template
# ─────────────────────────────────────────────────────────────────────────────

def _build_user_prompt(
    *,
    content_type: str,
    transcript: str | None,
    ocr_text: str | None,
    caption: str | None,
    username: str | None,
) -> str:
    """Format the extracted content into a user message for the LLM."""
    transcript_block = _wrap_block("TRANSCRIPT", transcript)
    ocr_block = _wrap_block("ON-SCREEN TEXT (OCR)", ocr_text)
    caption_block = _wrap_block("CAPTION", caption)

    creator_line = f"CREATOR: @{username}\n" if username else ""

    return (
        f"CONTENT TYPE: {content_type}\n"
        f"{creator_line}"
        f"{transcript_block}"
        f"{ocr_block}"
        f"{caption_block}"
        f"\n"
        f"Analyse the above and return the JSON object."
    )


def _wrap_block(label: str, text: str | None) -> str:
    if not text or not text.strip():
        return f"{label}: (none)\n"
    # Hard cap to keep token count predictable. 6000 chars ≈ 1500 tokens.
    snippet = text.strip()
    if len(snippet) > 6000:
        snippet = snippet[:6000] + "… [truncated]"
    return f"{label}:\n{snippet}\n\n"


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_VARIANT_TEMPLATES: dict[str, str] = {
    "v1": _SYSTEM_V1_TEMPLATE,
    "v2": _SYSTEM_V2_TEMPLATE,
}

DEFAULT_VARIANT = "v1"

# Fallback category keys used when no user-specific sections are available.
_DEFAULT_CATEGORY_KEYS: list[str] = list(ALLOWED_CATEGORIES)


def build_messages(
    *,
    content_type: str,
    transcript: str | None,
    ocr_text: str | None,
    caption: str | None,
    username: str | None = None,
    variant: str = DEFAULT_VARIANT,
    category_keys: list[str] | None = None,
) -> PromptMessages:
    """Return system + user messages ready for Groq's chat API.

    ``category_keys`` should be the list of section keys for this specific
    user (e.g. ``["Programming", "AI", "Cooking"]``). When omitted the
    default ALLOWED_CATEGORIES are used.

    Unknown variants fall back to the default, so a mistyped env var can
    never crash the Lambda — it just runs with v1.
    """
    template = _VARIANT_TEMPLATES.get(variant, _VARIANT_TEMPLATES[DEFAULT_VARIANT])
    keys = category_keys if category_keys else _DEFAULT_CATEGORY_KEYS
    system = _build_system_prompt(template, keys)
    user = _build_user_prompt(
        content_type=content_type,
        transcript=transcript,
        ocr_text=ocr_text,
        caption=caption,
        username=username,
    )
    return {"system": system, "user": user}


def available_variants() -> list[str]:
    return list(_VARIANT_TEMPLATES.keys())
