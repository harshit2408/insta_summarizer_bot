"""Tests for scraper/instagram_scraper.py — fallback orchestration logic."""

from unittest.mock import MagicMock, patch

import pytest

from models.content_models import (
    ContentType,
    ScrapedContent,
    ScrapeResult,
    ScrapeStatus,
)
from scraper.instagram_scraper import InstagramScraper


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

REEL_URL = "https://www.instagram.com/reel/ABC123/"
POST_URL = "https://www.instagram.com/p/CAR123/"


def _make_content(shortcode="ABC123", url=REEL_URL, content_type=ContentType.REEL):
    return ScrapedContent(
        shortcode=shortcode, url=url, content_type=content_type, caption="test"
    )


def _success(url=REEL_URL):
    return ScrapeResult.success(_make_content(url=url))


def _partial(url=REEL_URL, msg="carousel"):
    return ScrapeResult.partial(_make_content(url=url, content_type=ContentType.CAROUSEL), msg)


def _failed(url=REEL_URL):
    return ScrapeResult.failed(url, "fail")


def _private(url=REEL_URL):
    return ScrapeResult.private(url)


def _not_found(url=REEL_URL):
    return ScrapeResult.not_found(url)


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────

class TestInstagramScraperFallback:
    def setup_method(self):
        self.scraper = InstagramScraper(download_dir="/tmp/test_dl", download_media=False)

    # ── Tier 1 success ────────────────────────────────────────────────

    def test_ytdlp_success_returns_immediately(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_success())
        self.scraper._graphql.scrape = MagicMock(return_value=_success())

        result = self.scraper.scrape(REEL_URL)

        assert result.status == ScrapeStatus.SUCCESS
        self.scraper._graphql.scrape.assert_not_called()

    # ── Hard stops ────────────────────────────────────────────────────

    def test_ytdlp_private_no_fallback(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_private())
        self.scraper._graphql.scrape = MagicMock()

        result = self.scraper.scrape(REEL_URL)

        assert result.status == ScrapeStatus.PRIVATE
        self.scraper._graphql.scrape.assert_not_called()

    def test_ytdlp_not_found_no_fallback(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_not_found())
        self.scraper._graphql.scrape = MagicMock()

        result = self.scraper.scrape(REEL_URL)

        assert result.status == ScrapeStatus.NOT_FOUND
        self.scraper._graphql.scrape.assert_not_called()

    # ── Tier 1 partial → Tier 2 ───────────────────────────────────────

    def test_ytdlp_partial_tries_graphql(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_partial())
        self.scraper._graphql.scrape = MagicMock(return_value=_success())

        result = self.scraper.scrape(POST_URL)

        assert result.status == ScrapeStatus.SUCCESS
        self.scraper._graphql.scrape.assert_called_once()

    def test_ytdlp_failed_tries_graphql(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_failed())
        self.scraper._graphql.scrape = MagicMock(return_value=_success())

        result = self.scraper.scrape(REEL_URL)

        assert result.status == ScrapeStatus.SUCCESS

    # ── Tier 2 results ────────────────────────────────────────────────

    def test_graphql_success(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_partial())
        self.scraper._graphql.scrape = MagicMock(return_value=_success())

        result = self.scraper.scrape(POST_URL)
        assert result.status == ScrapeStatus.SUCCESS

    def test_graphql_private_hard_stop(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_failed())
        self.scraper._graphql.scrape = MagicMock(return_value=_private())

        result = self.scraper.scrape(REEL_URL)
        assert result.status == ScrapeStatus.PRIVATE

    def test_graphql_not_found_hard_stop(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_failed())
        self.scraper._graphql.scrape = MagicMock(return_value=_not_found())

        result = self.scraper.scrape(REEL_URL)
        assert result.status == ScrapeStatus.NOT_FOUND

    # ── Both tiers exhausted ──────────────────────────────────────────

    def test_both_fail_returns_failed(self):
        self.scraper._ytdlp.scrape = MagicMock(return_value=_failed())
        self.scraper._graphql.scrape = MagicMock(return_value=_failed())

        result = self.scraper.scrape(REEL_URL)
        assert result.status == ScrapeStatus.FAILED

    def test_ytdlp_partial_graphql_fail_returns_partial(self):
        """When yt-dlp got a partial result but graphql also fails,
        the partial (caption-only) result is returned rather than FAILED."""
        self.scraper._ytdlp.scrape = MagicMock(return_value=_partial())
        self.scraper._graphql.scrape = MagicMock(return_value=_failed())

        result = self.scraper.scrape(POST_URL)
        assert result.status == ScrapeStatus.PARTIAL

    # ── Invalid URL ───────────────────────────────────────────────────

    def test_invalid_url_rejected_before_scrapers(self):
        self.scraper._ytdlp.scrape = MagicMock()
        self.scraper._graphql.scrape = MagicMock()

        result = self.scraper.scrape("https://www.youtube.com/watch?v=123")

        assert result.status == ScrapeStatus.FAILED
        self.scraper._ytdlp.scrape.assert_not_called()
        self.scraper._graphql.scrape.assert_not_called()

    # ── scrape_metadata_only ──────────────────────────────────────────

    def test_scrape_metadata_only_disables_download_temporarily(self):
        self.scraper.download_media = True
        self.scraper._ytdlp.download_media = True
        self.scraper._graphql.download_media = True

        self.scraper._ytdlp.scrape = MagicMock(return_value=_success())

        result = self.scraper.scrape_metadata_only(REEL_URL)

        assert result.status == ScrapeStatus.SUCCESS
        # Flags must be restored
        assert self.scraper._ytdlp.download_media is True
        assert self.scraper._graphql.download_media is True

    def test_scrape_metadata_only_restores_on_exception(self):
        self.scraper.download_media = True
        self.scraper._ytdlp.scrape = MagicMock(side_effect=RuntimeError("oops"))

        with pytest.raises(RuntimeError):
            self.scraper.scrape_metadata_only(REEL_URL)

        # Flags still restored via finally block
        assert self.scraper._ytdlp.download_media is True
        assert self.scraper._graphql.download_media is True

    # ── Constructor ───────────────────────────────────────────────────

    def test_default_construction(self):
        s = InstagramScraper()
        assert s._ytdlp is not None
        assert s._graphql is not None
        assert s.download_media is True

    def test_construction_with_custom_params(self):
        s = InstagramScraper(download_dir="/tmp/custom", download_media=False, ytdlp_max_retries=5)
        assert s.download_media is False
        assert s._ytdlp.max_retries == 5
