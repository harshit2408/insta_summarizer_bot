"""
Primary Instagram scraper using yt-dlp.

yt-dlp natively supports Instagram and can handle:
  - Reels (video)
  - Single image posts
  - IGTV

Carousels are intentionally handed off to GraphQL (tier 2) because yt-dlp
only extracts video slides from mixed carousels and silently drops image
slides.

Design: yt-dlp always runs in metadata-only mode first. Only after we
confirm the content type is a single reel/image do we download the file
directly. This prevents downloading carousel video slides twice.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlretrieve

import yt_dlp

from models.content_models import (
    ContentType,
    ScrapedContent,
    ScrapedMedia,
    ScrapeResult,
)
from utils.helpers import extract_shortcode, normalize_instagram_url

logger = logging.getLogger(__name__)


def _is_video_entry(entry: dict[str, Any]) -> bool:
    """
    Return True when a yt-dlp info/entry dict represents a video.

    yt-dlp is inconsistent about which field carries this signal depending on
    the Instagram post type and whether ffmpeg is available, so we check every
    relevant field in priority order.
    """
    # Explicit type field set by yt-dlp
    if entry.get("_type") == "video":
        return True

    # vcodec present and not 'none' / None
    vcodec = entry.get("vcodec") or "none"
    if vcodec != "none":
        return True

    # video_ext present and not 'none' / None
    video_ext = entry.get("video_ext") or "none"
    if video_ext not in ("none", ""):
        return True

    # ext is mp4 AND the entry has a real duration (images have no duration)
    if entry.get("ext") == "mp4" and entry.get("duration"):
        return True

    # URL points to an mp4 file
    url = entry.get("url") or ""
    if ".mp4" in url:
        return True

    return False


def _detect_content_type(info: dict[str, Any]) -> ContentType:
    """Infer ContentType from the yt-dlp info dict."""
    url: str = info.get("webpage_url", "") or info.get("original_url", "")
    lower = url.lower()

    if "/reel" in lower:
        return ContentType.REEL

    # yt-dlp marks carousels (and image-only playlists) as _type=playlist
    if info.get("_type") == "playlist":
        return ContentType.CAROUSEL

    entries = info.get("entries") or []
    if len(entries) > 1:
        return ContentType.CAROUSEL

    if _is_video_entry(info):
        return ContentType.REEL

    return ContentType.IMAGE


def _parse_timestamp(ts: Any) -> datetime | None:
    """Convert a Unix timestamp (int/float/None) to a UTC datetime."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    except (ValueError, TypeError, OSError):
        return None


class YtDlpScraper:
    """
    Wraps yt-dlp to extract metadata and optionally download media from
    public Instagram posts/reels.

    Always runs yt-dlp in metadata-only mode first, then downloads files
    directly only when the content is confirmed to be a single reel/image.
    Carousels are returned as PARTIAL so the GraphQL scraper handles them.
    """

    def __init__(
        self,
        download_dir: Path | str = "./downloads",
        max_retries: int = 2,
        download_media: bool = True,
    ) -> None:
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(parents=True, exist_ok=True)
        self.max_retries = max_retries
        self.download_media = download_media

    # ──────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────

    def scrape(self, url: str) -> ScrapeResult:
        """
        Fetch metadata and download media for *url*.

        Returns a :class:`ScrapeResult` – always succeeds or wraps the
        error; never raises.
        """
        try:
            normalized_url = normalize_instagram_url(url)
        except ValueError as exc:
            return ScrapeResult.failed(url, str(exc))

        shortcode = extract_shortcode(normalized_url)
        logger.info("[ytdlp] Scraping %s (shortcode=%s)", normalized_url, shortcode)

        # Step 1: metadata only — no files written to disk yet
        info = self._extract_info_metadata_only(normalized_url)
        if info is None:
            return ScrapeResult.failed(url, "yt-dlp returned no info for this URL.")

        if info.get("_private"):
            return ScrapeResult.private(url)
        if info.get("_not_found"):
            return ScrapeResult.not_found(url)

        # Private content can also surface as an empty info dict
        if not info.get("id") and not info.get("entries"):
            return ScrapeResult.private(url)

        content = self._build_content(info, normalized_url, shortcode)
        logger.info(
            "[ytdlp] type=%s media_count=%d caption=%s",
            content.content_type,
            content.media_count,
            "yes" if content.has_caption else "no",
        )

        # ── Carousels: hand off to GraphQL ────────────────────────────
        # yt-dlp only extracts video slides; image slides are invisible to
        # it. GraphQL reliably returns every slide, so we always defer.
        # No file has been downloaded yet at this point, so there's nothing
        # to clean up.
        if content.content_type == ContentType.CAROUSEL:
            logger.info(
                "[ytdlp] Carousel detected — handing off to GraphQL for all slides."
            )
            return ScrapeResult.partial(
                content,
                "Carousel: yt-dlp cannot fetch image slides. GraphQL will handle.",
            )

        # ── Non-carousel: download the single media file ──────────────
        if content.media_count > 0:
            if self.download_media:
                self._download_media(content)
            return ScrapeResult.success(content)

        # No media but caption present
        if content.has_caption:
            logger.info("[ytdlp] No media URL found. Caption extracted successfully.")
            return ScrapeResult.partial(
                content,
                "Caption extracted but no media URL could be resolved.",
            )

        return ScrapeResult.failed(normalized_url, "yt-dlp returned no usable content.")

    # ──────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────

    def _ydl_opts(self, shortcode: str) -> dict[str, Any]:
        outtmpl = str(self.download_dir / f"{shortcode}_%(autonumber)s.%(ext)s")
        return {
            "outtmpl": outtmpl,
            # Prefer a single pre-merged file (avoids ffmpeg requirement)
            "format": "best[ext=mp4]/best",
            "merge_output_format": "mp4",
            # Always skip download — we download manually after content-type check
            "skip_download": True,
            "quiet": True,
            "no_warnings": False,
            "retries": self.max_retries,
            "fragment_retries": self.max_retries,
            "writeinfojson": False,
            "writethumbnail": False,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            },
        }

    def _extract_info_metadata_only(self, url: str) -> dict[str, Any] | None:
        """Run yt-dlp in metadata-only mode (no files written to disk)."""
        shortcode = extract_shortcode(url) or "unknown"
        try:
            with yt_dlp.YoutubeDL(self._ydl_opts(shortcode)) as ydl:
                info = ydl.extract_info(url, download=False)
                return ydl.sanitize_info(info)
        except yt_dlp.utils.DownloadError as exc:
            msg = str(exc).lower()
            if "private" in msg or "login" in msg or "authentication" in msg:
                logger.warning("[ytdlp] Private content: %s", exc)
                return {"_private": True}
            if "not found" in msg or "404" in msg or "does not exist" in msg:
                logger.warning("[ytdlp] Content not found: %s", exc)
                return {"_not_found": True}
            logger.error("[ytdlp] DownloadError: %s", exc)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("[ytdlp] Unexpected error: %s", exc, exc_info=True)
            return None

    def _download_media(self, content: ScrapedContent) -> None:
        """
        Download all media items in *content* directly using the URLs
        extracted by yt-dlp. Skips items that are already on disk.
        """
        for idx, media in enumerate(content.media_items):
            if media.local_path and media.local_path.exists():
                continue

            ext = "mp4" if media.media_type == "video" else "jpg"
            fname = f"{content.shortcode}_{idx + 1:02d}.{ext}"
            dest = self.download_dir / fname

            try:
                logger.info("[ytdlp] Downloading %s → %s", media.url[:60], fname)
                urlretrieve(media.url, dest)  # noqa: S310
                media.local_path = dest
                logger.info(
                    "[ytdlp] Saved %s (%.1f MB)", fname, dest.stat().st_size / 1e6
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("[ytdlp] Failed to download %s: %s", media.url[:60], exc)

    def _build_content(
        self,
        info: dict[str, Any],
        url: str,
        shortcode: str | None,
    ) -> ScrapedContent:
        """Convert a yt-dlp info dict → ScrapedContent."""

        # Handle playlist (carousel) vs single item
        raw_entries = info.get("entries") or []
        entries: list[dict] = [e for e in raw_entries if e]

        media_items: list[ScrapedMedia] = []
        if entries:
            for entry in entries:
                media = self._entry_to_media(entry)
                if media and not media.url.startswith("https://www.instagram.com/"):
                    media_items.append(media)
        else:
            media = self._entry_to_media(info)
            if media and not media.url.startswith("https://www.instagram.com/"):
                media_items.append(media)

        content_type = _detect_content_type(info)
        first = entries[0] if entries else info

        username = (
            info.get("uploader_id")
            or info.get("uploader")
            or first.get("uploader_id")
            or first.get("uploader")
        )
        if username and username.startswith("@"):
            username = username[1:]

        return ScrapedContent(
            shortcode=shortcode or info.get("id", "unknown"),
            url=url,
            content_type=content_type,
            media_items=media_items,
            thumbnail_url=info.get("thumbnail") or first.get("thumbnail"),
            caption=info.get("description") or first.get("description"),
            username=username,
            full_name=info.get("uploader") or first.get("uploader"),
            like_count=info.get("like_count") or first.get("like_count"),
            view_count=info.get("view_count") or first.get("view_count"),
            comment_count=info.get("comment_count") or first.get("comment_count"),
            posted_at=_parse_timestamp(
                info.get("timestamp") or first.get("timestamp")
            ),
            scraper_method="ytdlp",
        )

    def _entry_to_media(self, entry: dict[str, Any]) -> ScrapedMedia | None:
        """Extract a ScrapedMedia record from a single yt-dlp entry."""
        if not entry:
            return None

        is_video = _is_video_entry(entry)

        media_url = ""
        width: int | None = entry.get("width")
        height: int | None = entry.get("height")

        # (a) Direct url field
        direct = entry.get("url", "")
        if direct and not direct.startswith("https://www.instagram.com/"):
            media_url = direct

        # (b) requested_formats — yt-dlp's selected format set
        if not media_url:
            for fmt in entry.get("requested_formats") or []:
                u = fmt.get("url", "")
                if u and not u.startswith("https://www.instagram.com/"):
                    media_url = u
                    width = width or fmt.get("width")
                    height = height or fmt.get("height")
                    break

        # (c) formats list — walk best-last
        if not media_url:
            for fmt in reversed(entry.get("formats") or []):
                u = fmt.get("url", "")
                if u and not u.startswith("https://www.instagram.com/"):
                    media_url = u
                    width = width or fmt.get("width")
                    height = height or fmt.get("height")
                    break

        # (d) thumbnails as last resort for image posts
        if not media_url:
            for thumb in reversed(entry.get("thumbnails") or []):
                u = thumb.get("url", "")
                if u:
                    media_url = u
                    width = width or thumb.get("width")
                    height = height or thumb.get("height")
                    is_video = False
                    break

        # (e) webpage_url fallback (indicates metadata-only result)
        if not media_url:
            media_url = entry.get("webpage_url") or entry.get("original_url") or ""

        return ScrapedMedia(
            url=media_url,
            media_type="video" if is_video else "image",
            local_path=None,  # set later by _download_media if needed
            width=width,
            height=height,
            duration_seconds=entry.get("duration"),
        )
