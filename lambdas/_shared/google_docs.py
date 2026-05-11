"""
Google Docs API client — minimal urllib wrapper.

Only the four endpoints the writer actually needs:

  * ``create_document(title)``  → ``POST /v1/documents``
        Create a brand new doc on the user's Drive when they don't bring
        one of their own.
  * ``get_document(doc_id)``    → ``GET  /v1/documents/{id}``
        Returns the full document tree. We use it to (a) verify the doc is
        accessible, and (b) locate the insertion point for a given section
        heading (read its content array).
  * ``batch_update(doc_id, requests)`` → ``POST /v1/documents/{id}:batchUpdate``
        Workhorse — every insert/format request goes through this.
  * ``find_document_by_name(name)`` → Drive ``files.list``
        Used by the "Use existing doc" onboarding step so users can paste
        a doc URL or just name and we look it up.

Stdlib urllib only — same rationale as ``groq_client.py`` (small zip,
fast cold start, no Lambda Layer).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


DOCS_API_BASE = "https://docs.googleapis.com/v1"
DRIVE_API_BASE = "https://www.googleapis.com/drive/v3"


class GoogleDocsError(RuntimeError):
    """Any non-2xx response from the Docs / Drive API."""

    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class GoogleDocsClient:
    """Stateless wrapper — pass the access token explicitly per call.

    Access tokens are short lived (~1 hour) and we don't want them stored
    on a long-lived module-level client.
    """

    def __init__(self, *, timeout: float = 20.0):
        self._timeout = timeout

    # ── Documents ────────────────────────────────────────────────────────────

    def create_document(self, *, access_token: str, title: str) -> dict:
        """Create a brand new doc on the user's Drive.

        Returns the full document resource (we only really need its ``documentId``).
        """
        body = json.dumps({"title": title}).encode("utf-8")
        return self._request(
            "POST", f"{DOCS_API_BASE}/documents",
            access_token=access_token,
            body=body,
        )

    def get_document(self, *, access_token: str, document_id: str) -> dict:
        """Fetch the full document including its content/body."""
        url = f"{DOCS_API_BASE}/documents/{urllib.parse.quote(document_id, safe='')}"
        return self._request("GET", url, access_token=access_token)

    def batch_update(
        self,
        *,
        access_token: str,
        document_id: str,
        requests: list[dict],
    ) -> dict:
        """Apply a list of mutation requests atomically.

        Google's batchUpdate is all-or-nothing: if any single request is
        rejected the whole batch fails. This is what we want — better to
        retry the entire append than to half-write a corrupt entry.
        """
        if not requests:
            return {}
        url = f"{DOCS_API_BASE}/documents/{urllib.parse.quote(document_id, safe='')}:batchUpdate"
        body = json.dumps({"requests": requests}).encode("utf-8")
        return self._request("POST", url, access_token=access_token, body=body)

    # ── Drive (used only to look up an existing doc by name) ─────────────────

    def find_document_by_name(self, *, access_token: str, name: str) -> str | None:
        """Return the documentId of the first matching Google Doc, or None."""
        params = {
            "q": (
                f"name = '{name.replace(chr(39), chr(92) + chr(39))}' "
                "and mimeType = 'application/vnd.google-apps.document' "
                "and trashed = false"
            ),
            "fields": "files(id,name)",
            "pageSize": "5",
        }
        url = f"{DRIVE_API_BASE}/files?{urllib.parse.urlencode(params)}"
        result = self._request("GET", url, access_token=access_token)
        files = result.get("files") or []
        return files[0]["id"] if files else None

    # ── Internals ────────────────────────────────────────────────────────────

    def _request(
        self,
        method: str,
        url: str,
        *,
        access_token: str,
        body: bytes | None = None,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=body, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            err = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            logger.error("Google Docs %s %s → HTTP %s: %s", method, url, exc.code, err[:300])
            raise GoogleDocsError(
                f"Google Docs API HTTP {exc.code}: {err[:200]}",
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            raise GoogleDocsError(f"Network error: {exc}") from exc

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GoogleDocsError(f"Non-JSON response: {raw[:200]}") from exc


# ─────────────────────────────────────────────────────────────────────────────
# Document structure helpers
# ─────────────────────────────────────────────────────────────────────────────

def find_end_index(document: dict) -> int:
    """Return the index just before the trailing newline of the body.

    Google Docs documents always end with a single newline at the last
    position. Inserting *at* the trailing newline raises an ``invalidArgument``,
    so we always insert one position before it.
    """
    body = document.get("body", {})
    content = body.get("content", []) or []
    if not content:
        return 1  # empty doc — index 1 is the only valid insertion point
    last = content[-1]
    end_index = last.get("endIndex", 1)
    return max(1, end_index - 1)


def find_section_index(document: dict, heading_text: str) -> int | None:
    """Return the index *after* the paragraph whose text matches ``heading_text``.

    Used to insert a new entry directly under a section heading (e.g. under
    ``PROGRAMMING & TECH``) instead of always appending at the end.
    Matching is case-insensitive and whitespace-tolerant *except* leading
    indent: TOC lines in the skeleton are indented (``[2] PROGRAMMING …`` prefixed by
    spaces) and must NOT match — otherwise inserts land inside the INDEX
    block instead of under the real section heading.
    """
    target = heading_text.strip().lower()
    body = document.get("body", {})

    for element in body.get("content", []) or []:
        para = element.get("paragraph")
        if not para:
            continue
        text = "".join(
            run.get("textRun", {}).get("content", "")
            for run in para.get("elements", []) or []
        )
        # Drop trailing newline for "starts with whitespace" checks (TOC lines)
        stem = text.rstrip("\r\n")
        if stem and stem[0].isspace():
            continue

        if stem.strip().lower() == target:
            return element.get("endIndex")
    return None
