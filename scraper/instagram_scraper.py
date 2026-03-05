"""
Main Instagram scraper with 2-tier automatic fallback.

Scraping chain:
  1. yt-dlp   — free, no key, best for reels/videos
  2. GraphQL  — free, no key, Instagram's internal API (good for all types,
                especially carousels)

Usage
-----
    from scraper import InstagramScraper

    scraper = InstagramScraper(download_dir="./downloads")
    result = scraper.scrape("https://www.instagram.com/reel/ABC123/")

    if result.status == ScrapeStatus.SUCCESS:
        content = result.content
        print(content.caption)
        print(content.media_items[0].local_path)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from models.content_models import ScrapeResult, ScrapeStatus
from utils.helpers import is_valid_instagram_url
from .ytdlp_scraper import YtDlpScraper
from .graphql_scraper import GraphQLScraper

load_dotenv()

logger = logging.getLogger(__name__)


class InstagramScraper:
    """
    High-level scraper combining yt-dlp → GraphQL with automatic fallback.

    Parameters
    ----------
    download_dir:
        Where to save downloaded media files.
    download_media:
        If False, only metadata is fetched (no files written to disk).
    ytdlp_max_retries:
        Number of yt-dlp retry attempts before falling through.
    """

    def __init__(
        self,
        download_dir: Path | str = "./downloads",
        download_media: bool = True,
        ytdlp_max_retries: int | None = None,
    ) -> None:
        download_dir = Path(download_dir)

        retries = ytdlp_max_retries
        if retries is None:
            retries = int(os.getenv("YTDLP_MAX_RETRIES", "2"))

        self._ytdlp = YtDlpScraper(
            download_dir=download_dir,
            max_retries=retries,
            download_media=download_media,
        )
        self._graphql = GraphQLScraper(
            download_dir=download_dir,
            download_media=download_media,
        )
        self.download_media = download_media

    # ──────────────────────────────────────────────────────────────────
    # Public interface
    # ──────────────────────────────────────────────────────────────────

    def scrape(self, url: str) -> ScrapeResult:
        """
        Scrape an Instagram URL through the fallback chain.

        Returns
        -------
        ScrapeResult
            Always returns a result; check ``result.status`` before using
            ``result.content``.

        Status meanings
        ---------------
        SUCCESS  — Full content + media URLs (and local files if download_media=True)
        PARTIAL  — Caption extracted but no media could be downloaded
        PRIVATE  — Content is private; no further retries make sense
        NOT_FOUND — Post was deleted or URL is invalid
        FAILED   — All scrapers exhausted
        """
        url = url.strip()

        if not is_valid_instagram_url(url):
            logger.warning("Rejected non-Instagram URL: %s", url)
            return ScrapeResult.failed(url, "Not a valid Instagram URL.")

        logger.info("Starting scrape for: %s", url)

        # ── Tier 1: yt-dlp ────────────────────────────────────────────
        ytdlp_result = self._ytdlp.scrape(url)

        if ytdlp_result.status == ScrapeStatus.SUCCESS:
            logger.info("[tier1/ytdlp] Succeeded.")
            return ytdlp_result

        # Hard stops — private/deleted content won't work with any scraper
        if ytdlp_result.status in (ScrapeStatus.PRIVATE, ScrapeStatus.NOT_FOUND):
            logger.info("[tier1/ytdlp] %s — skipping fallbacks.", ytdlp_result.status.value)
            return ytdlp_result

        if ytdlp_result.status == ScrapeStatus.PARTIAL:
            logger.info(
                "[tier1/ytdlp] Partial result (%s). Trying GraphQL for complete content...",
                ytdlp_result.error_message,
            )
        else:
            logger.warning(
                "[tier1/ytdlp] Failed (%s). Trying GraphQL fallback...",
                ytdlp_result.error_message,
            )

        # ── Tier 2: GraphQL (Instagram internal API) ──────────────────
        graphql_result = self._graphql.scrape(url)

        if graphql_result.status == ScrapeStatus.SUCCESS:
            logger.info("[tier2/graphql] Succeeded.")
            return graphql_result

        if graphql_result.status in (ScrapeStatus.PRIVATE, ScrapeStatus.NOT_FOUND):
            logger.info("[tier2/graphql] %s confirmed.", graphql_result.status.value)
            return graphql_result

        # Both tiers exhausted — return the best partial we have
        if ytdlp_result.status == ScrapeStatus.PARTIAL:
            logger.info("[all tiers] Returning caption-only result from yt-dlp.")
            return ytdlp_result

        combined_msg = (
            f"Both scrapers failed. "
            f"yt-dlp: {ytdlp_result.error_message!r} | "
            f"GraphQL: {graphql_result.error_message!r}"
        )
        logger.error(combined_msg)
        return ScrapeResult.failed(url, combined_msg)

    def scrape_metadata_only(self, url: str) -> ScrapeResult:
        """
        Fetch post metadata without downloading any media files.
        Useful for duplicate checks or quick previews.
        """
        self._ytdlp.download_media = False
        self._graphql.download_media = False

        try:
            return self.scrape(url)
        finally:
            self._ytdlp.download_media = self.download_media
            self._graphql.download_media = self.download_media
