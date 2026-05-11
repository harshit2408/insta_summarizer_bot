"""
Tiny Telegram Bot API helper — used by the Google Docs Writer to send
the user a completion notification, and by the OAuth callback to confirm
that account linking succeeded.

Stdlib urllib only.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    parse_mode: str | None = None,
    disable_web_page_preview: bool = True,
    timeout: float = 10.0,
) -> None:
    """Best-effort send. Logs errors but never raises so the pipeline keeps moving."""
    if not bot_token:
        logger.warning("TELEGRAM_BOT_TOKEN missing — skipping send_message")
        return

    payload: dict = {
        "chat_id": chat_id,
        "text": text,
        "disable_web_page_preview": disable_web_page_preview,
    }
    if parse_mode:
        payload["parse_mode"] = parse_mode

    body = json.dumps(payload).encode("utf-8")
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                logger.warning("Telegram sendMessage HTTP %s", resp.status)
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.error("Telegram sendMessage failed for chat_id=%s: %s", chat_id, exc)
