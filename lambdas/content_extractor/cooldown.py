"""
Cooldown Manager — Global 429 / IP-flagging emergency lockdown.

PRD Section: REQUIREMENT 4 — The "429" Emergency Protocol

When Instagram returns a 429 or sustained 403s, the entire scraping system
must enter a timed lockdown to let our IP range cool off. This module owns
that state: writing, reading, and clearing the lockdown record in DynamoDB.

Lockdown record location:
  Table:          Users table (DYNAMODB_USERS_TABLE)
  Partition key:  chat_id = "SYSTEM"
  The "SYSTEM" sentinel avoids provisioning a separate control table.

Singleton pattern:
  A single module-level `cooldown_manager` instance is created at import time.
  Lambda warm invocations reuse it, avoiding repeated env-var lookups.

Belt-and-suspenders expiry:
  The record carries BOTH:
    - `until` (ISO timestamp) — checked at runtime by is_active()
    - `ttl`   (Unix epoch)    — used by DynamoDB TTL auto-deletion
  If DynamoDB TTL is lagging (it can be up to 48h behind), the `until` check
  still stops the lockdown on time. If we forget to call deactivate(), the TTL
  eventually removes the record anyway.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

_region = os.environ.get("AWS_REGION", "ap-south-1")
_dynamodb = boto3.resource("dynamodb", region_name=_region)

DYNAMODB_USERS_TABLE = os.environ.get("DYNAMODB_USERS_TABLE", "users")

# How long a 429 lockdown lasts (minutes).
COOLDOWN_DURATION_MINUTES = 45
# TTL for the DynamoDB item — longer than the lockdown so TTL doesn't race.
COOLDOWN_TTL_MINUTES = 90

COOLDOWN_SK = "global_cooldown"
SYSTEM_PK = "SYSTEM"

# Telegram message sent to the user who TRIGGERED the 429.
COOLDOWN_TRIGGER_MSG = (
    "⚠️ Instagram briefly blocked our request. I've paused the system for 45 minutes "
    "to recover. Your reel and any others in the queue will resume automatically."
)

# Message for users who hit the gate WHILE cooldown is active (used in rate_check.py).
COOLDOWN_ACTIVE_MSG = (
    "🛑 Our scraping service is briefly resting to avoid overloading Instagram's servers. "
    "Your link has been saved and will be processed automatically in ~45 minutes. "
    "No action needed."
)


class CooldownManager:
    """Manages the global scraping cooldown state in DynamoDB.

    Methods:
        is_active()            → bool   — True if lockdown is currently in effect.
        activate(reason, triggered_by) — Write lockdown record.
        deactivate()                    — Remove lockdown record (called on recovery).
        was_previous_run_cooling_down() → bool — Startup easing check.
    """

    def __init__(self, table_name: str = DYNAMODB_USERS_TABLE) -> None:
        self._table = _dynamodb.Table(table_name)
        # Track across the lifetime of this execution environment so the handler
        # can apply an easing-in sleep after a cooldown ends.
        self._entered_cooldown_this_invocation: bool = False

    # ── Public API ────────────────────────────────────────────────────────────

    def is_active(self) -> bool:
        """Return True if the global cooldown is currently in effect.

        Reads from DynamoDB on every call — callers should cache the result
        within a single Lambda invocation rather than calling this repeatedly.
        """
        try:
            resp = self._table.get_item(
                Key={"chat_id": SYSTEM_PK},
                ProjectionExpression="sk_cooldown, #u",
                ExpressionAttributeNames={"#u": "until"},
            )
        except ClientError as exc:
            logger.error("DynamoDB get cooldown failed: %s — assuming not active", exc)
            return False

        item = resp.get("Item")
        if not item:
            return False

        # Check the sk_cooldown sentinel attribute exists (confirms this is the
        # cooldown record, not some other SYSTEM item).
        if not item.get("sk_cooldown"):
            return False

        until_str: str | None = item.get("until")
        if not until_str:
            return False

        try:
            until_dt = datetime.fromisoformat(until_str)
        except ValueError:
            logger.warning("Malformed cooldown.until value: %r — treating as expired", until_str)
            return False

        now = datetime.now(timezone.utc)
        active = now < until_dt
        if not active:
            logger.info("Cooldown record exists but has expired (until=%s)", until_str)
        return active

    def activate(self, *, reason: str, triggered_by_shortcode: str) -> None:
        """Write a lockdown record to DynamoDB.

        Args:
            reason:                  Why cooldown was triggered (e.g. "429_detected").
            triggered_by_shortcode:  The shortcode whose job hit the 429.
        """
        now = datetime.now(timezone.utc)
        until = now + timedelta(minutes=COOLDOWN_DURATION_MINUTES)
        ttl_epoch = int((now + timedelta(minutes=COOLDOWN_TTL_MINUTES)).timestamp())

        try:
            self._table.update_item(
                Key={"chat_id": SYSTEM_PK},
                UpdateExpression=(
                    "SET sk_cooldown = :sk, #u = :until, reason = :reason, "
                    "triggered_by_shortcode = :sc, activated_at = :now, #ttl = :ttl"
                ),
                ExpressionAttributeNames={"#u": "until", "#ttl": "ttl"},
                ExpressionAttributeValues={
                    ":sk": COOLDOWN_SK,
                    ":until": until.isoformat(),
                    ":reason": reason,
                    ":sc": triggered_by_shortcode,
                    ":now": now.isoformat(),
                    ":ttl": ttl_epoch,
                },
            )
            self._entered_cooldown_this_invocation = True
            logger.critical(
                "GLOBAL COOLDOWN ACTIVATED: reason=%s shortcode=%s until=%s",
                reason, triggered_by_shortcode, until.isoformat(),
            )
        except ClientError as exc:
            logger.error("Failed to write cooldown record: %s", exc)
            raise

    def deactivate(self) -> None:
        """Remove the cooldown record (called after successful recovery or manual override)."""
        try:
            self._table.update_item(
                Key={"chat_id": SYSTEM_PK},
                UpdateExpression="REMOVE sk_cooldown, #u, reason, triggered_by_shortcode, activated_at",
                ExpressionAttributeNames={"#u": "until"},
            )
            logger.info("Global cooldown deactivated")
        except ClientError as exc:
            logger.error("Failed to deactivate cooldown: %s", exc)

    def entered_cooldown_this_invocation(self) -> bool:
        """True if activate() was called during the CURRENT Lambda invocation.

        The handler uses this to apply an easing-in sleep after recovery:
        if the previous run triggered cooldown, the NEXT cold-start invocation
        should sleep 60–120s before making any outbound requests.
        """
        return self._entered_cooldown_this_invocation


# ── Module-level singleton ────────────────────────────────────────────────────
# One instance per execution environment. Lambda warm invocations reuse it.
cooldown_manager = CooldownManager()
