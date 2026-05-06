"""
Minimal Groq Chat-Completions client built on :mod:`urllib`.

Why not the official ``groq`` SDK?
  * The SDK pulls in ``httpx`` + ``pydantic`` + ``anyio`` (~10 MB extra).
  * The Groq REST endpoint is OpenAI-compatible — a flat HTTPS POST.
  * Keeping the analyser zip dependency-free means a sub-1-second cold start
    and no Lambda-Layer machinery in the Terraform pipeline.

Feature set:
  * Configurable model + temperature + max_tokens
  * Exponential back-off on 429 / 5xx / network errors
  * Surfaces request id for log correlation
  * Returns the raw assistant message string — JSON parsing lives in :mod:`schema`
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"

# Sensible defaults; can be overridden per-call from the handler.
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_TEMPERATURE = 0.3
DEFAULT_MAX_TOKENS = 1024
DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_RETRIES = 3


class GroqAPIError(RuntimeError):
    """Raised when Groq returns a non-2xx response we cannot recover from."""

    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None):
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class GroqClient:
    """Thin urllib wrapper for the Groq Chat-Completions endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        if not api_key:
            raise ValueError("GROQ_API_KEY is required")
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._max_retries = max_retries

    # ── Public API ───────────────────────────────────────────────────────────

    def complete(
        self,
        *,
        system: str,
        user: str,
        temperature: float = DEFAULT_TEMPERATURE,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        response_format_json: bool = True,
    ) -> "GroqResponse":
        """Send a single chat completion and return the assistant message.

        ``response_format_json=True`` enables Groq's JSON-mode (when supported
        by the chosen model) which forces the model to emit syntactically
        valid JSON. We still validate the structure ourselves in
        :func:`schema.parse_analysis`.
        """
        payload: dict = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format_json:
            payload["response_format"] = {"type": "json_object"}

        body = json.dumps(payload).encode("utf-8")
        return self._post_with_retry(body)

    # ── Internals ────────────────────────────────────────────────────────────

    def _post_with_retry(self, body: bytes) -> "GroqResponse":
        last_exc: Exception | None = None

        for attempt in range(1, self._max_retries + 1):
            try:
                return self._post_once(body)
            except GroqAPIError as exc:
                # 4xx other than 429 are non-retryable (auth, malformed, etc.)
                if exc.status is not None and exc.status not in (408, 429) and exc.status < 500:
                    raise
                last_exc = exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_exc = exc

            if attempt < self._max_retries:
                # Exponential backoff with jitter: 1s, 2s, 4s …
                delay = (2 ** (attempt - 1)) + random.uniform(0, 0.5)
                logger.warning(
                    "Groq call failed (attempt %d/%d): %s — retrying in %.1fs",
                    attempt, self._max_retries, last_exc, delay,
                )
                time.sleep(delay)

        # All retries exhausted
        raise GroqAPIError(
            f"Groq API failed after {self._max_retries} attempts: {last_exc}"
        ) from last_exc

    def _post_once(self, body: bytes) -> "GroqResponse":
        req = urllib.request.Request(
            GROQ_API_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "insta-agent-ai-analyzer/1.0",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read().decode("utf-8")
                request_id = resp.headers.get("x-request-id")
        except urllib.error.HTTPError as exc:
            err_body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
            request_id = exc.headers.get("x-request-id") if exc.headers else None
            raise GroqAPIError(
                f"Groq returned HTTP {exc.code}: {err_body[:500]}",
                status=exc.code,
                request_id=request_id,
            ) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GroqAPIError(f"Groq response was not JSON: {raw[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise GroqAPIError(f"Groq response had no choices: {raw[:200]}")

        message = choices[0].get("message", {}).get("content", "")
        usage = data.get("usage", {}) or {}

        return GroqResponse(
            content=message,
            model=data.get("model", self._model),
            request_id=request_id,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Response container
# ─────────────────────────────────────────────────────────────────────────────

class GroqResponse:
    """Lightweight value-object for a successful Groq response."""

    __slots__ = (
        "content",
        "model",
        "request_id",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    )

    def __init__(
        self,
        *,
        content: str,
        model: str,
        request_id: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
    ) -> None:
        self.content = content
        self.model = model
        self.request_id = request_id
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens

    def __repr__(self) -> str:  # pragma: no cover - debug only
        return (
            f"GroqResponse(model={self.model!r}, tokens={self.total_tokens}, "
            f"request_id={self.request_id!r})"
        )
