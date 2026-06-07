"""
Human Mimicry — Header rotation and post-scrape jitter sleep.

PRD Section: REQUIREMENT 3 — Human-Mimicry & Jitter

Instagram's anti-bot systems flag traffic patterns, not just IP addresses.
Two signals we must mimic:

  1. HEADER ROTATION
     Real browsers send consistent User-Agent / sec-ch-ua triplets.
     We maintain a pool of 5 current UA strings and rotate through them,
     never reusing the same one twice in a row.

  2. POST-SCRAPE SLEEP (jitter)
     After a successful download, we pause before picking up the next job.
     This simulates the human time between viewing one video and clicking
     the next link. Without jitter, Lambda's fast cold-start cycle would
     hammer Instagram at machine speed.

Why track last_used_index at module level?
  Lambda warm invocations share the same Python process. Module-level state
  persists between invocations in the SAME execution environment, so we can
  guarantee consecutive requests don't reuse the same UA. On cold starts
  the index resets to -1 (any UA is acceptable).
"""

from __future__ import annotations

import logging
import random
import time

logger = logging.getLogger(__name__)

# ── User-Agent pool ───────────────────────────────────────────────────────────
# Five realistic browser fingerprints current as of 2026.
# Each dict is passed directly to yt-dlp's http_headers option.
# The sec-ch-ua headers MUST match the User-Agent string — mismatches are a
# strong bot signal because real browsers set them consistently.
USER_AGENT_POOL: list[dict[str, str]] = [
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Google Chrome";v="123", "Not:A-Brand";v="8", "Chromium";v="123"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Linux"',
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36 Edg/121.0.0.0"
        ),
        "sec-ch-ua": '"Not A(Brand";v="99", "Microsoft Edge";v="121", "Chromium";v="121"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    },
    {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.4.1 Safari/605.1.15"
        ),
        "sec-ch-ua": '"Safari";v="17"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    },
]

# Module-level state — persists across warm Lambda invocations.
# -1 means "no prior selection" (cold start or first call).
_last_used_index: int = -1


def get_rotated_headers() -> dict[str, str]:
    """Return a browser header dict, never repeating the previous invocation's UA.

    The selection strategy:
      - Build a candidate list that excludes the last-used index.
      - Pick uniformly at random from the remaining candidates.
      - Update the module-level tracker.

    This ensures consecutive requests within the same execution environment
    always use different User-Agent strings, which reduces the fingerprint
    correlation that Instagram's rate-limiter looks for.

    Returns:
        A dict suitable for passing directly to yt-dlp's ``http_headers`` option.
    """
    global _last_used_index  # noqa: PLW0603

    pool_size = len(USER_AGENT_POOL)
    candidates = [i for i in range(pool_size) if i != _last_used_index]

    chosen_index = random.choice(candidates)
    _last_used_index = chosen_index

    headers = dict(USER_AGENT_POOL[chosen_index])
    logger.debug("Header rotation: selected UA index=%d", chosen_index)
    return headers


def get_last_used_ua_index() -> int:
    """Return the index of the most recently selected User-Agent.

    Used by the handler to include ``user_agent_index`` in structured log output
    (PRD §5 scrape_attempt event).
    """
    return _last_used_index


def post_scrape_sleep(method: str) -> None:
    """Sleep after a successful scrape to mimic human browsing cadence.

    Args:
        method: "yt-dlp" or "rapidapi". yt-dlp hits Instagram's servers
                directly (longer sleep needed). RapidAPI routes through their
                proxy layer (shorter sleep acceptable since they manage IP risk).

    The sleep duration is randomised (uniform distribution) to avoid a
    predictable interval pattern that bot detectors could fingerprint.
    """
    if method == "yt-dlp":
        duration = random.uniform(30.0, 60.0)
    else:
        # RapidAPI manages its own IP pool — we still sleep, but shorter.
        duration = random.uniform(15.0, 30.0)

    logger.info("Post-scrape jitter sleep: %.1fs (method=%s)", duration, method)
    time.sleep(duration)
