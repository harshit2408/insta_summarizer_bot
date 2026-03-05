"""
GraphQL-based Instagram scraper — 2nd fallback (after yt-dlp, before RapidAPI).

Uses Instagram's internal GraphQL endpoint that powers the public web viewer.
No authentication or API keys required — works on all public content.

Technique credit: seotanvirbd
  Article : https://medium.com/@seotanvirbd/how-i-built-a-python-tool-that-extracts-instagram-reel-data-without-authentication-api-keys-or-0fcb35cba7b7
  GitHub  : https://github.com/seotanvirbd/Instagram-Reel-Scraper

Response path:
  data
    .xdt_api__v1__media__shortcode__web_info
      .items[0]
        .video_versions[]          ← reel / video
        .image_versions2.candidates[]  ← thumbnail / image post
        .carousel_media[]          ← multi-image / carousel
        .caption.text
        .user.username / .full_name / .is_verified
        .like_count / .comment_count / .view_count
        .taken_at                  ← Unix timestamp
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlretrieve

import requests

from models.content_models import (
    ContentType,
    ScrapedContent,
    ScrapedMedia,
    ScrapeResult,
    ScrapeStatus,
)
from utils.helpers import extract_shortcode, normalize_instagram_url

logger = logging.getLogger(__name__)

_GRAPHQL_URL = "https://www.instagram.com/graphql/query"

# doc_id for PolarisPostRootQuery — the public post viewer query.
# This value is embedded in Instagram's compiled JS bundle and changes
# occasionally with Instagram deployments.
_DOC_ID = "24368985919464652"

# Instagram media_type constants
_MEDIA_VIDEO = 2
_MEDIA_IMAGE = 1
_MEDIA_CAROUSEL = 8


def _parse_timestamp(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


class GraphQLScraper:
    """
    Scrapes public Instagram posts/reels via Instagram's internal GraphQL API.

    No API key needed. Works for:
      - Reels / videos
      - Single image posts
      - Carousel / multi-photo posts

    Parameters
    ----------
    download_dir:
        Where to save downloaded media.
    download_media:
        Set False to fetch metadata only (no files written to disk).
    timeout:
        HTTP request timeout in seconds.
    max_retries:
        Number of retries on transient network errors.
    """

    # Headers that mimic a real Chrome browser visiting instagram.com
    _BASE_HEADERS: dict[str, str] = {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/",
        "sec-ch-ua": '"Chromium";v="141", "Not/A)Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/141.0.0.0 Safari/537.36"
        ),
        "x-ig-app-id": "936619743392459",
        # A static CSRF token works for unauthenticated GraphQL queries.
        # If Instagram rotates this, replace with a fresh value from DevTools.
        "x-csrftoken": "YuvV-QRvpR2Ggzgk0cTg1T",
        "Cookie": "csrftoken=YuvV-QRvpR2Ggzgk0cTg1T; mid=aOia4gALAAHSq3em2E34YEIFkMCC",
    }

    def __init__(
        self,
        download_dir: Path | str = "./downloads",
        download_media: bool = True,
        timeout: int = 15,
        max_retries: int = 2,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.download_media = download_media
        self.timeout = timeout
        self.max_retries = max_retries

    # ──────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────

    def scrape(self, url: str) -> ScrapeResult:
        """
        Fetch metadata (and optionally download media) for *url*.

        Returns a :class:`ScrapeResult`; never raises.
        """
        try:
            normalized_url = normalize_instagram_url(url)
        except ValueError as exc:
            return ScrapeResult.failed(url, str(exc))

        shortcode = extract_shortcode(normalized_url)
        if not shortcode:
            return ScrapeResult.failed(url, "Could not extract shortcode from URL.")

        logger.info("[graphql] Scraping %s (shortcode=%s)", normalized_url, shortcode)

        raw = self._fetch(shortcode)
        if raw is None:
            return ScrapeResult.failed(url, "GraphQL request failed or timed out.")

        # Map error codes to result types
        if raw.get("_rate_limited"):
            return ScrapeResult.failed(url, "Instagram rate-limited this request (429).")
        if raw.get("_not_found"):
            return ScrapeResult.not_found(url)
        if raw.get("_private"):
            return ScrapeResult.private(url)

        item = self._extract_item(raw)
        if item is None:
            return ScrapeResult.failed(
                url, "GraphQL response contained no post data (post may be private)."
            )

        content = self._build_content(item, normalized_url, shortcode)

        if self.download_media and content.media_count > 0:
            self._download_all(content)

        logger.info(
            "[graphql] type=%s media_count=%d caption=%s",
            content.content_type,
            content.media_count,
            "yes" if content.has_caption else "no",
        )

        if content.media_count > 0 or content.has_caption:
            return ScrapeResult.success(content)

        return ScrapeResult.failed(url, "GraphQL returned empty content.")

    # ──────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────

    def _build_payload(self, shortcode: str) -> str:
        """Build the URL-encoded GraphQL POST body."""
        import json as _json

        variables = _json.dumps({"shortcode": shortcode})
        encoded_vars = quote(variables)

        # Static Comet/FB platform parameters extracted from the web bundle.
        # These rarely change and are needed to pass Instagram's request
        # validation even for unauthenticated queries.
        return (
            f"av=0&__d=www&__user=0&__a=1&__req=u"
            f"&__hs=20371.HYP%3Ainstagram_web_pkg.2.1...0"
            f"&dpr=1&__ccg=GOOD&__rev=1028249517"
            f"&__s=ywybjm%3Aq4co81%3Adplvd8"
            f"&__hsi=7559456450740095677"
            f"&__dyn=7xeUjG1mxu1syUbFp41twpUnwgU7SbzEdF8aUco2qwJw5ux609vCwjE1EE2Cw8G11wBz81s8hwGxu786a3a1YwBgao6C0Mo2swtUd8-U2zxe2GewGw9a361qw8Xxm16wa-0raazo7u3C2u2J0bS1LwTwKG0WE8oC1Iwqo5p0OwUQp1yU426V89F8uwm8jwhUaE4e1tyVrx60gm5oswFwtF85i5E"
            f"&__csr=geIAaiFliZllsBav4trBuTJ-KJ5WhnQyAnxeEWpBCC-hJADG9AgG4qpQ8zat5BypWy9eaRgBaJ2Xx2p6WgymmGDzQjJo8JJ4iKi8xObCjx50FzLF4-8DiwxDyGqoydV-ESQ9DLAB_GdDzFEsyUSeG8xmF9oymWyqyVFF84q5ooHohwuE5a0CU01kUUb81CE12E5V08m0WFA0ei80n2bLwjp42TOw2J-0rq04tUKp06PwEhy1u1ig4Dgy9wdW0D8n80rl0UxGtw53hEx2E1yPUy7U1J9Q0JFvc0cXwpyG4B6B2US01IAw2Bo0K215w0YEwj8"
            f"&__comet_req=7"
            f"&lsd=AdGtgRvhyjc"
            f"&jazoest=21085"
            f"&__spin_r=1028249517"
            f"&__spin_b=trunk"
            f"&__spin_t=1760073111"
            f"&__crn=comet.igweb.PolarisLoggedOutDesktopPostRouteNext"
            f"&fb_api_caller_class=RelayModern"
            f"&fb_api_req_friendly_name=PolarisPostRootQuery"
            f"&server_timestamps=true"
            f"&variables={encoded_vars}"
            f"&doc_id={_DOC_ID}"
        )

    def _fetch(self, shortcode: str) -> dict | None:
        """POST to the GraphQL endpoint with retry logic."""
        payload = self._build_payload(shortcode)

        for attempt in range(1, self.max_retries + 2):
            try:
                resp = requests.post(
                    _GRAPHQL_URL,
                    headers=self._BASE_HEADERS,
                    data=payload,
                    timeout=self.timeout,
                )

                if resp.status_code == 429:
                    logger.warning("[graphql] Rate limited (429). Attempt %d.", attempt)
                    if attempt <= self.max_retries:
                        time.sleep(2 ** attempt)
                        continue
                    return {"_rate_limited": True}

                if resp.status_code == 404:
                    return {"_not_found": True}

                if resp.status_code != 200:
                    logger.error("[graphql] HTTP %d for %s", resp.status_code, shortcode)
                    if attempt <= self.max_retries:
                        time.sleep(2)
                        continue
                    return None

                return resp.json()

            except requests.exceptions.Timeout:
                logger.warning("[graphql] Timeout on attempt %d.", attempt)
                if attempt <= self.max_retries:
                    time.sleep(2)
                    continue
                return None
            except requests.exceptions.RequestException as exc:
                logger.error("[graphql] Network error: %s", exc)
                return None
            except Exception as exc:  # noqa: BLE001
                logger.error("[graphql] Unexpected error: %s", exc, exc_info=True)
                return None

        return None

    def _extract_item(self, raw: dict) -> dict | None:
        """
        Navigate the nested GraphQL response to find the post item dict.

        Expected path:
          data → xdt_api__v1__media__shortcode__web_info → items[0]
        """
        try:
            items = (
                raw.get("data", {})
                .get("xdt_api__v1__media__shortcode__web_info", {})
                .get("items", [])
            )
            return items[0] if items else None
        except (AttributeError, IndexError, TypeError):
            return None

    def _build_content(
        self,
        item: dict[str, Any],
        url: str,
        shortcode: str,
    ) -> ScrapedContent:
        """Map a GraphQL item dict → ScrapedContent."""

        media_type_raw = int(item.get("media_type") or 0)

        content_type_map = {
            _MEDIA_IMAGE: ContentType.IMAGE,
            _MEDIA_VIDEO: ContentType.REEL,
            _MEDIA_CAROUSEL: ContentType.CAROUSEL,
        }
        content_type = content_type_map.get(media_type_raw, ContentType.UNKNOWN)

        # ── Media items ───────────────────────────────────────────────
        media_items: list[ScrapedMedia] = []

        carousel = item.get("carousel_media") or []
        if carousel:
            # Each slide has its own media_type, video_versions, image_versions2
            for slide in carousel:
                m = self._extract_media_item(slide)
                if m:
                    media_items.append(m)
        else:
            m = self._extract_media_item(item)
            if m:
                media_items.append(m)

        # ── User / owner ──────────────────────────────────────────────
        user = item.get("user") or item.get("owner") or {}
        username = user.get("username")
        full_name = user.get("full_name")
        is_verified = user.get("is_verified")

        # ── Caption ───────────────────────────────────────────────────
        caption_obj = item.get("caption") or {}
        caption = caption_obj.get("text") if isinstance(caption_obj, dict) else None

        # ── Metrics ───────────────────────────────────────────────────
        like_count = item.get("like_count")
        comment_count = item.get("comment_count")
        view_count = item.get("view_count") or item.get("play_count")

        # ── Thumbnail — best quality candidate ────────────────────────
        candidates = (item.get("image_versions2") or {}).get("candidates") or []
        thumbnail_url = candidates[0].get("url") if candidates else item.get("display_uri")

        return ScrapedContent(
            shortcode=shortcode,
            url=url,
            content_type=content_type,
            media_items=media_items,
            thumbnail_url=thumbnail_url,
            caption=caption,
            username=username,
            full_name=full_name,
            is_verified=is_verified,
            like_count=like_count,
            view_count=view_count,
            comment_count=comment_count,
            posted_at=_parse_timestamp(item.get("taken_at")),
            scraper_method="graphql",
        )

    def _extract_media_item(self, item: dict[str, Any]) -> ScrapedMedia | None:
        """Extract a ScrapedMedia from a post item or carousel slide dict."""
        if not item:
            return None

        media_type_raw = int(item.get("media_type") or 0)
        is_video = media_type_raw == _MEDIA_VIDEO

        media_url = ""
        width: int | None = None
        height: int | None = None
        duration: float | None = None

        if is_video:
            versions = item.get("video_versions") or []
            if versions:
                # version[0] is the highest quality progressive mp4
                best = versions[0]
                media_url = best.get("url", "")
                width = best.get("width")
                height = best.get("height")
            duration = item.get("video_duration")
        else:
            candidates = (item.get("image_versions2") or {}).get("candidates") or []
            if candidates:
                best = candidates[0]  # first = highest resolution
                media_url = best.get("url", "")
                width = best.get("width")
                height = best.get("height")

        if not media_url:
            return None

        return ScrapedMedia(
            url=media_url,
            media_type="video" if is_video else "image",
            width=width,
            height=height,
            duration_seconds=duration,
        )

    def _download_all(self, content: ScrapedContent) -> None:
        """Download all media items to self.download_dir."""
        for idx, media in enumerate(content.media_items):
            if media.local_path and media.local_path.exists():
                continue

            ext = "mp4" if media.media_type == "video" else "jpg"
            fname = f"{content.shortcode}_{idx + 1:02d}.{ext}"
            dest = self.download_dir / fname

            try:
                logger.info(
                    "[graphql] Downloading %s → %s", media.url[:60], fname
                )
                urlretrieve(media.url, dest)  # noqa: S310
                media.local_path = dest
                logger.info(
                    "[graphql] Saved %s (%.1f MB)",
                    fname,
                    dest.stat().st_size / 1e6,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "[graphql] Failed to download %s: %s", media.url[:60], exc
                )
