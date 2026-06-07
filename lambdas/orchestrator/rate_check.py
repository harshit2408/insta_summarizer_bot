"""
Rate Check — Admission gate called by the Orchestrator before enqueuing any job.

PRD Sections: REQUIREMENT 2 (rate limiting), REQUIREMENT 4 (cooldown gate)

This module is the single chokepoint between the Telegram user and the SQS
extraction queue. ALL of the following checks happen here, in order:

  1. Global cooldown (429 lockdown) — fastest check, system-wide
  2. Burst rate limit  (3 req / 60s per user)
  3. Daily rate limit  (25 req / 24h per user)
  4. Global rate limit (40 req / 1h across ALL users)

Short-circuit logic: the first failing check sends its Telegram rejection
message and returns False. Later checks are never evaluated, which avoids
unnecessary DynamoDB writes for counters that would be immediately rejected.

Why enforce limits in the Orchestrator, not the Extractor?
  The Extractor is the expensive, fragile stage — it makes real HTTP calls
  to Instagram. Letting rate-limited requests reach the Extractor wastes
  Lambda execution time, consumes SQS capacity, and increases Instagram
  exposure. Rejecting early at the Orchestrator (API Gateway → Lambda) is
  cheap and keeps the extraction queue clean.

Why use the Orchestrator's own DynamoDB client?
  The Orchestrator and Content Extractor are separate Lambda functions with
  separate execution environments. Sharing state through DynamoDB (not
  module-level variables) is the only cross-process-safe option.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

# rate_limiter.py and cooldown.py are bundled flat into the orchestrator zip
# by Terraform (see lambda.tf — data.archive_file.orchestrator). They import
# as plain top-level modules, the same way handler.py imports doc_template.
from rate_limiter import RateLimiter, RateLimitExceeded
from cooldown import cooldown_manager, COOLDOWN_ACTIVE_MSG

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"

# Module-level singleton — reused across warm Lambda invocations.
_rate_limiter = RateLimiter()


def admission_check(chat_id: str) -> bool:
    """Run all rate-limit and cooldown checks for ``chat_id``.

    Returns:
        True  — the job is admitted; the caller should proceed to enqueue.
        False — the job is rejected; a Telegram rejection message has already
                been sent to the user.

    Side effects:
        - If admitted: atomically increments the burst, daily, and global
          DynamoDB counters.
        - If rejected: sends one Telegram message to the user and logs the
          tier/counter details for monitoring.

    This function NEVER raises. All DynamoDB / Telegram errors are caught and
    logged; on DynamoDB failure the counters fail open (job is admitted) to
    avoid blocking users due to infrastructure hiccups.
    """
    # ── Check 1: Global cooldown (no DynamoDB counter increment needed) ───────
    if cooldown_manager.is_active():
        logger.warning(
            "Admission rejected [cooldown]: chat_id=%s", chat_id
        )
        _send_rejection(chat_id, COOLDOWN_ACTIVE_MSG)
        return False

    # ── Checks 2–4: Rate-limit tiers (burst → daily → global) ────────────────
    # Each check() call atomically increments its counter AND checks the limit.
    # If it raises, we reject and return False without checking later tiers.
    checks = [
        ("burst",  lambda: _rate_limiter.check_burst(chat_id)),
        ("daily",  lambda: _rate_limiter.check_daily(chat_id)),
        ("global", lambda: _rate_limiter.check_global()),
    ]

    for tier_name, check_fn in checks:
        try:
            check_fn()
        except RateLimitExceeded as exc:
            logger.warning(
                "Admission rejected [%s]: chat_id=%s count=%d",
                exc.tier, chat_id, exc.count,
            )
            _send_rejection(chat_id, exc.message)
            return False
        except Exception as exc:
            # Unexpected error (network, DynamoDB down, etc.) — fail open.
            logger.error(
                "Rate limit check [%s] raised unexpected error for chat_id=%s: %s — admitting",
                tier_name, chat_id, exc,
            )

    logger.info("Admission granted: chat_id=%s", chat_id)
    return True


def _send_rejection(chat_id: str, text: str) -> None:
    """Send a Telegram rejection message. Best-effort — never raises."""
    if not TELEGRAM_BOT_TOKEN:
        logger.warning(
            "TELEGRAM_BOT_TOKEN not set — cannot send rejection to chat_id=%s", chat_id
        )
        return

    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        f"{TELEGRAM_API_BASE}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                logger.warning(
                    "Telegram rejection message returned HTTP %s for chat_id=%s",
                    resp.status, chat_id,
                )
    except (urllib.error.URLError, TimeoutError) as exc:
        logger.error(
            "Failed to send rejection message to chat_id=%s: %s", chat_id, exc
        )
