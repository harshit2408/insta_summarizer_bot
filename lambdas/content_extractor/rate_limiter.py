"""
Rate Limiter — Three-tier DynamoDB-backed admission control.

PRD Section: REQUIREMENT 2 — Multi-Tier Rate Limiting

This module is enforced by the Orchestrator Lambda BEFORE a job is enqueued
into SQS, not inside the Content Extractor. Keeping the gate at the
Orchestrator means rejected requests never touch the extraction queue at all.

Tier hierarchy (checked in order — short-circuits on first failure):
  1. Burst   — 3 requests / 60-second rolling window per user
  2. Daily   — 25 requests / calendar day UTC per user
  3. Global  — 40 requests / 1-hour rolling window across ALL users

All counters live in the Users table as **synthetic partition keys** (string
``chat_id`` values) — one small item per bucket, same pattern for burst,
daily, and global. This is not a relational “wide table with one row per user
and a column per date”; each day/hour is a **separate item**, and TTL deletes
stale items so storage stays bounded.

``chat_id = "SYSTEM"`` is reserved for the **cooldown** document only
(``cooldown.py``). Global rate limits use ``chat_id = "rate_limit:global:<UTC-hour>"``
so we never accumulate unbounded attribute names on a single item.

Why DynamoDB ADD (not read-then-write)?
  DynamoDB's UpdateItem with ADD is a server-side atomic operation — the
  increment and read happen in a single request with no race window. A
  read-then-write pattern under concurrent Lambda invocations would allow
  multiple users to read the same counter value, both decide they are under
  limit, and both increment — effectively allowing 2× the intended quota.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)

DYNAMODB_USERS_TABLE = os.environ.get("DYNAMODB_USERS_TABLE", "users")

# ── Tier thresholds ───────────────────────────────────────────────────────────
BURST_LIMIT = 3          # requests per 60-second window
BURST_WINDOW_SEC = 60
BURST_TTL_SEC = 90       # slightly longer so the item outlives the window

DAILY_LIMIT = 25         # requests per UTC calendar day
DAILY_TTL_SEC = 48 * 3600

GLOBAL_LIMIT = 40        # requests per rolling hour across ALL users
GLOBAL_WINDOW_SEC = 3600
GLOBAL_TTL_SEC = 2 * 3600

# ── Telegram rejection messages (used by orchestrator/rate_check.py) ─────────
BURST_REJECTION_MSG = (
    "⏳ You're sending links too quickly. Please wait a moment before sending another."
)
DAILY_REJECTION_MSG = (
    "📊 You've reached today's limit of 25 reels. "
    "Your limit resets at midnight UTC. Come back tomorrow!"
)
GLOBAL_REJECTION_MSG = (
    "🌐 The system is handling high traffic right now. "
    "Your reel has been queued and will be processed soon. We'll notify you when it starts."
)


class RateLimitExceeded(Exception):
    """Raised when a rate-limit tier is breached.

    Attributes:
        tier:    Human-readable tier name ("burst" | "daily" | "global").
        message: Ready-to-send Telegram rejection string.
        count:   The counter value that triggered the rejection.
    """

    def __init__(self, tier: str, message: str, count: int) -> None:
        super().__init__(f"Rate limit exceeded [{tier}]: count={count}")
        self.tier = tier
        self.message = message
        self.count = count


class RateLimiter:
    """Three-tier DynamoDB-backed rate limiter.

    Usage (in Orchestrator admission gate):
        rl = RateLimiter()
        rl.check_burst(chat_id)   # raises RateLimitExceeded on breach
        rl.check_daily(chat_id)
        rl.check_global()
    """

    def __init__(self, table_name: str = DYNAMODB_USERS_TABLE) -> None:
        self._table = _dynamodb.Table(table_name)

    # ── Tier 1: Burst ─────────────────────────────────────────────────────────

    def check_burst(self, chat_id: str) -> None:
        """Atomic increment burst counter; raise RateLimitExceeded if over limit."""
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        # Align to the 60-second rolling window so each window gets its own item.
        # Without a time component the key is a single persistent item whose cnt
        # accumulates across sessions; DynamoDB TTL is lazy (up to 48 h delay),
        # so a stale counter from a previous session can still trigger rejections.
        window_start = (now_epoch // BURST_WINDOW_SEC) * BURST_WINDOW_SEC
        ttl = now_epoch + BURST_TTL_SEC
        pk = f"rate_limit:burst:{chat_id}:{window_start}"

        count = self._atomic_increment(pk, ttl)

        if count > BURST_LIMIT:
            logger.warning(
                "Rate limit hit [burst]: chat_id=%s count=%d limit=%d",
                chat_id, count, BURST_LIMIT,
            )
            raise RateLimitExceeded("burst", BURST_REJECTION_MSG, count)

        logger.debug("Burst check passed: chat_id=%s count=%d", chat_id, count)

    # ── Tier 2: Daily ─────────────────────────────────────────────────────────

    def check_daily(self, chat_id: str) -> None:
        """Atomic increment daily counter; raise RateLimitExceeded if over limit."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        ttl = now_epoch + DAILY_TTL_SEC
        pk = f"rate_limit:daily:{chat_id}:{today}"

        count = self._atomic_increment(pk, ttl)

        if count > DAILY_LIMIT:
            logger.warning(
                "Rate limit hit [daily]: chat_id=%s date=%s count=%d limit=%d",
                chat_id, today, count, DAILY_LIMIT,
            )
            raise RateLimitExceeded("daily", DAILY_REJECTION_MSG, count)

        logger.debug("Daily check passed: chat_id=%s date=%s count=%d", chat_id, today, count)

    # ── Tier 3: Global system ceiling ─────────────────────────────────────────

    def check_global(self) -> None:
        """Atomic increment the hourly global counter; raise RateLimitExceeded if over limit.

        One DynamoDB **item** per UTC hour bucket (partition key
        ``rate_limit:global:YYYY-MM-DD-HH``), with attributes ``cnt`` + ``ttl`` —
        same shape as burst/daily. Avoids storing many ``cnt_*`` attributes on
        ``chat_id=SYSTEM`` (which must stay reserved for cooldown + would hit
        the 400 KB item size limit over time).
        """
        hour_key = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H")
        now_epoch = int(datetime.now(timezone.utc).timestamp())
        ttl = now_epoch + GLOBAL_TTL_SEC
        pk = f"rate_limit:global:{hour_key}"

        count = self._atomic_increment(pk, ttl)

        if count > GLOBAL_LIMIT:
            logger.warning(
                "Rate limit hit [global]: hour=%s count=%d limit=%d",
                hour_key, count, GLOBAL_LIMIT,
            )
            raise RateLimitExceeded("global", GLOBAL_REJECTION_MSG, count)

        logger.debug("Global check passed: hour=%s count=%d", hour_key, count)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _atomic_increment(self, sk: str, ttl: int) -> int:
        """Atomically increment a counter stored in DynamoDB and return the new value.

        The Users table partition key is ``chat_id`` (String) only. Every rate
        bucket is its own item: ``chat_id`` equals the logical key string
        (e.g. ``rate_limit:daily:123:2026-05-13``). There are no extra “columns
        per date” on the user profile row — each date is a **new item** with
        its own ``cnt`` and ``ttl``.

        Why ADD instead of read-modify-write:
          DynamoDB ADD is atomic on the server — no lost updates under concurrent
          orchestrator invocations.
        """
        key: dict = {"chat_id": sk}
        update_expr = "ADD #cnt :one SET #ttl = :ttl"
        expr_names = {"#cnt": "cnt", "#ttl": "ttl"}

        try:
            resp = self._table.update_item(
                Key=key,
                UpdateExpression=update_expr,
                ExpressionAttributeNames=expr_names,
                ExpressionAttributeValues={":one": 1, ":ttl": ttl},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            logger.error("DynamoDB rate-limit increment failed for sk=%s: %s", sk, exc)
            # Fail open — do NOT block the user if DynamoDB is temporarily down.
            # Logging the error is enough for ops to investigate.
            return 0

        return int(resp["Attributes"].get("cnt", 1))
