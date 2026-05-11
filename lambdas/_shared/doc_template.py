"""
Document template — turn an :class:`Analysis` into Google Docs ``batchUpdate`` requests.

Layout:

    My Learning Archive — Owner Name
    Last Updated: <date>

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    INDEX — Jump to Section

      ↳ [1] HIGH PRIORITY
      ↳ [2] PROGRAMMING & TECH
      …

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    [1] HIGH PRIORITY

    Entry Title
    Date | Score | Category label
    Key Takeaways:
      • …
    Summary: …
    Tags: …
    Source URL
      ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    [2] PROGRAMMING & TECH
    …

Visual rules:
  • SECTION_DIVIDER (━━━) appears ONLY above a section heading — one thick
    line signals "new section starts here."
  • ENTRY_SEPARATOR (─ ─) appears after every entry — thin so it reads as
    "next entry in the same section," not a section boundary.
  • The INDEX block lists every section number so the reader can Ctrl+F "[4]"
    to jump directly to any section.

Routing rule (per PRD §4.1 FR-5.4):
  * quality_score ≥ 8  → [1] HIGH PRIORITY
  * quality_score 5–7  → [N] REVIEW LATER  (last-1 section)
  * quality_score < 5  → [N] ARCHIVE       (last section)
  * always also written under the matching category section

Dynamic sections:
  Users can define their own category sections via Telegram. Each user's
  sections are stored in DynamoDB under the ``custom_sections`` attribute
  as a JSON list of objects: ``[{"key": str, "title": str}, ...]``
  (legacy rows may include an ``emoji`` field; it is not shown in headings).

  When the AI determines the content belongs to a category that has no
  matching section yet, a new section is auto-created and appended.

  The three special "priority" sections (HIGH PRIORITY, REVIEW LATER,
  ARCHIVE) are always present and not user-editable.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Iterable

# ── Layout primitives ─────────────────────────────────────────────────────────

SECTION_DIVIDER = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
ENTRY_SEPARATOR = "  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─"

# Printed before each TOC line under INDEX — leading indent on TOC rows means
# ``find_section_index`` never treats TOC rows as real section headings.
INDEX_LINE_PREFIX = "↳ "

DOC_DEFAULT_TITLE = "My Learning Archive"

# ── Fixed priority/quality sections ──────────────────────────────────────────
# These are always present; [1] is fixed; REVIEW_LATER / ARCHIVE get dynamic numbers.

HIGH_PRIORITY_HEADING = "[1] HIGH PRIORITY"
REVIEW_LATER_LABEL = "REVIEW LATER"
ARCHIVE_LABEL      = "ARCHIVE"

# ── Default category sections (used when a user has NO custom sections) ───────

DEFAULT_CATEGORY_SECTIONS: list[dict] = [
    {"key": "Programming",      "title": "PROGRAMMING & TECH"},
    {"key": "AI",               "title": "AI & MACHINE LEARNING"},
    {"key": "Career",           "title": "CAREER DEVELOPMENT"},
    {"key": "Productivity",     "title": "PRODUCTIVITY & TOOLS"},
    {"key": "Finance",          "title": "FINANCE"},
    {"key": "Health",           "title": "HEALTH & WELLNESS"},
    {"key": "Education",        "title": "EDUCATION & LEARNING"},
    {"key": "Design",           "title": "DESIGN & CREATIVITY"},
    {"key": "Marketing",        "title": "MARKETING"},
    {"key": "Entrepreneurship", "title": "BUSINESS & ENTREPRENEURSHIP"},
    {"key": "Lifestyle",        "title": "LIFESTYLE"},
    {"key": "Other",            "title": "OTHER"},
]


# ─────────────────────────────────────────────────────────────────────────────
# SectionConfig — the runtime representation of a user's section list
# ─────────────────────────────────────────────────────────────────────────────

class SectionConfig:
    """Holds a user's ordered list of category sections and builds headings.

    ``raw_sections`` is the value of ``custom_sections`` from DynamoDB — a
    list of dicts: ``[{"key": str, "title": str}, ...]``. Legacy rows may
    include ``emoji``; it is ignored when building headings.

    If ``raw_sections`` is empty/None the DEFAULT_CATEGORY_SECTIONS are used.
    """

    def __init__(self, raw_sections: list[dict] | None = None):
        if raw_sections:
            self._sections: list[dict] = raw_sections
        else:
            self._sections = list(DEFAULT_CATEGORY_SECTIONS)

    # ── Heading builders ──────────────────────────────────────────────────────

    def _section_number(self, idx: int) -> int:
        """Return the bracket index for a category section (starting at [2])."""
        return idx + 2  # [1] is always HIGH PRIORITY

    def heading_for_index(self, idx: int) -> str:
        s = self._sections[idx]
        n = self._section_number(idx)
        title = s.get("title") or str(s["key"]).upper()
        return _format_heading(n, title)

    def all_category_headings(self) -> list[str]:
        return [self.heading_for_index(i) for i in range(len(self._sections))]

    def review_later_heading(self) -> str:
        n = len(self._sections) + 2  # after all category sections
        return _format_heading(n, REVIEW_LATER_LABEL)

    def archive_heading(self) -> str:
        n = len(self._sections) + 3
        return _format_heading(n, ARCHIVE_LABEL)

    def all_headings(self) -> list[str]:
        """All section headings in order: HIGH PRIORITY, categories, REVIEW LATER, ARCHIVE."""
        return [
            HIGH_PRIORITY_HEADING,
            *self.all_category_headings(),
            self.review_later_heading(),
            self.archive_heading(),
        ]

    # ── Section lookup ────────────────────────────────────────────────────────

    def category_heading_for(self, category: str) -> str:
        """Map a category key to its heading, creating a new section if needed.

        NOTE: This does NOT mutate the config. Callers that want to persist
        a newly detected section should call ``add_section`` then save.
        """
        for i, s in enumerate(self._sections):
            if s["key"].lower() == category.lower():
                return self.heading_for_index(i)
        # Fall back to "Other"
        for i, s in enumerate(self._sections):
            if s["key"].lower() == "other":
                return self.heading_for_index(i)
        # If even "Other" is missing, use the last section
        return self.heading_for_index(len(self._sections) - 1)

    def priority_heading_for(self, quality_score: int) -> str:
        if quality_score >= 8:
            return HIGH_PRIORITY_HEADING
        if quality_score >= 5:
            return self.review_later_heading()
        return self.archive_heading()

    def has_category(self, category: str) -> bool:
        return any(s["key"].lower() == category.lower() for s in self._sections)

    # ── Mutation ──────────────────────────────────────────────────────────────

    def add_section(self, key: str, title: str) -> bool:
        """Append a new section. Returns False if key already exists."""
        if self.has_category(key):
            return False
        self._sections.append({"key": key, "title": title})
        return True

    def remove_section(self, key: str) -> bool:
        """Remove a section by key. Returns False if not found."""
        before = len(self._sections)
        self._sections = [s for s in self._sections if s["key"].lower() != key.lower()]
        return len(self._sections) < before

    # ── Serialisation ─────────────────────────────────────────────────────────

    def to_list(self) -> list[dict]:
        """Return a plain list suitable for DynamoDB storage."""
        return list(self._sections)

    def to_json(self) -> str:
        return json.dumps(self._sections)

    @classmethod
    def from_json(cls, s: str) -> "SectionConfig":
        return cls(json.loads(s))

    # ── Display helpers ───────────────────────────────────────────────────────

    def format_for_telegram(self) -> str:
        """Return a human-readable list of sections for Telegram."""
        lines = ["Your current sections:\n"]
        lines.append(f"  {HIGH_PRIORITY_HEADING}  (always present)")
        for i, s in enumerate(self._sections):
            heading = self.heading_for_index(i)
            lines.append(f"  {heading}")
        lines.append(f"  {self.review_later_heading()}  (always present)")
        lines.append(f"  {self.archive_heading()}  (always present)")
        lines.append(
            "\nTo add:    /addsection <key> <title>\n"
            "Example:   /addsection Cooking COOKING & RECIPES\n\n"
            "To remove: /removesection <key>\n"
            "Example:   /removesection Cooking"
        )
        return "\n".join(lines)


def _format_heading(number: int, title: str) -> str:
    """Format a heading like '[3] AI & MACHINE LEARNING'."""
    return f"[{number}] {title}"


# ─────────────────────────────────────────────────────────────────────────────
# Convenience wrappers that accept a SectionConfig (used by google_docs_writer)
# ─────────────────────────────────────────────────────────────────────────────

def priority_heading_for(quality_score: int, cfg: SectionConfig | None = None) -> str:
    c = cfg or SectionConfig()
    return c.priority_heading_for(quality_score)


def category_heading_for(category: str, cfg: SectionConfig | None = None) -> str:
    c = cfg or SectionConfig()
    return c.category_heading_for(category)


# ─────────────────────────────────────────────────────────────────────────────
# Entry rendering
# ─────────────────────────────────────────────────────────────────────────────

def render_entry_text(
    *,
    title: str,
    category: str,
    subcategory: str | None,
    quality_score: int,
    summary: str,
    key_takeaways: list[str],
    tags: list[str],
    source_url: str,
    processed_at: str | None = None,
) -> str:
    """Render a single analysis as the plain-text block inserted into a section.

    Ends with ENTRY_SEPARATOR (thin dashed line) so entries within the same
    section are visually separated without being confused for section breaks.
    """
    date_str = _short_date(processed_at)
    cat_label = f"{category} › {subcategory}" if subcategory else category

    bullets = "\n".join(f"  • {t.strip()}" for t in key_takeaways if t.strip())
    tags_line = " ".join(f"#{t}" for t in tags if t) if tags else ""

    parts: list[str] = [
        title,
        f"Date: {date_str}  |  Score: {quality_score}/10  |  Category: {cat_label}",
        "",
        "Key Takeaways:",
        bullets or "  • (no takeaways extracted)",
        "",
        f"Summary: {summary}",
    ]
    if tags_line:
        parts.append(f"Tags: {tags_line}")
    parts.append(f"Source: {source_url}")
    parts.append(ENTRY_SEPARATOR)
    parts.append("")

    return "\n".join(parts) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# Skeleton generation (first-time setup of a doc)
# ─────────────────────────────────────────────────────────────────────────────

def render_skeleton(
    *, owner_name: str | None = None, cfg: SectionConfig | None = None
) -> str:
    """Return the full plaintext skeleton for a brand-new document.

    Uses ``cfg`` to determine which category sections to include. When
    ``cfg`` is None the DEFAULT_CATEGORY_SECTIONS are used.
    """
    c = cfg or SectionConfig()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    title_suffix = f" — {owner_name}" if owner_name else ""

    lines: list[str] = [
        f"{DOC_DEFAULT_TITLE}{title_suffix}",
        f"Last Updated: {today}",
        "",
    ]

    # ── Index block ───────────────────────────────────────────────────────────
    lines += [
        SECTION_DIVIDER,
        "INDEX - Jump to Section",
        "",
    ]
    for heading in c.all_headings():
        lines.append(f"{INDEX_LINE_PREFIX}{heading}")
    lines.append("")

    # ── [1] High Priority ─────────────────────────────────────────────────────
    lines += [SECTION_DIVIDER, HIGH_PRIORITY_HEADING, "", ""]

    # ── Category sections ─────────────────────────────────────────────────────
    for heading in c.all_category_headings():
        lines += [SECTION_DIVIDER, heading, "", ""]

    # ── Review Later + Archive ────────────────────────────────────────────────
    lines += [SECTION_DIVIDER, c.review_later_heading(), "", ""]
    lines += [SECTION_DIVIDER, c.archive_heading(), "", ""]

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
# batchUpdate request builders
# ─────────────────────────────────────────────────────────────────────────────

def build_insert_text_request(*, text: str, index: int) -> dict:
    """Wrap text + index into a Google Docs insertText request dict."""
    return {
        "insertText": {
            "location": {"index": index},
            "text": text,
        }
    }


def build_skeleton_requests(
    *, owner_name: str | None = None, cfg: SectionConfig | None = None
) -> list[dict]:
    """One-shot batch that seeds an empty doc with the full template skeleton."""
    return [
        build_insert_text_request(
            text=render_skeleton(owner_name=owner_name, cfg=cfg),
            index=1,
        )
    ]


def build_append_section_requests(
    *,
    cfg: SectionConfig,
    review_later_index: int | None,
    end_index: int,
    new_section_key: str,
) -> list[dict]:
    """Build requests to append a new section heading to an existing document.

    This is called when the AI detects a category that doesn't exist yet.
    The new heading is inserted before REVIEW LATER so it slots in among the
    category sections rather than after the priority sections.

    ``review_later_index`` and ``end_index`` are pre-computed by the caller
    using ``find_section_index`` / ``find_end_index`` from ``google_docs``.

    Returns an empty list when no new section is needed.
    """
    insert_at = review_later_index if review_later_index is not None else end_index

    new_heading = cfg.category_heading_for(new_section_key)
    section_text = f"{SECTION_DIVIDER}\n{new_heading}\n\n\n"

    return [build_insert_text_request(text=section_text, index=insert_at)]


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _short_date(iso_or_none: str | None) -> str:
    if not iso_or_none:
        return datetime.now(timezone.utc).strftime("%b %d, %Y")
    try:
        s = iso_or_none.replace("Z", "+00:00")
        return datetime.fromisoformat(s).strftime("%b %d, %Y")
    except ValueError:
        return datetime.now(timezone.utc).strftime("%b %d, %Y")


def heading_present(headings: Iterable[str], target: str) -> bool:
    """Case-insensitive presence check for a heading string."""
    target_norm = target.strip().lower()
    return any(h.strip().lower() == target_norm for h in headings)


def parse_section_arg(arg: str) -> tuple[str, str] | None:
    """Parse '/addsection <key> <title...>' argument string.

    Returns (key, title) or None on parse failure.

    Example inputs:
        "Cooking COOKING & RECIPES"
        "python Python Tips"
    """
    parts = arg.strip().split(None, 1)
    if len(parts) < 2:
        return None
    key, title = parts[0], parts[1].strip()
    if not title:
        return None
    if not re.match(r"^[A-Za-z0-9_\-]+$", key):
        return None
    return key, title
