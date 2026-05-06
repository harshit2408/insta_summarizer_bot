"""Tests for models/content_models.py"""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from models.content_models import (
    ContentType,
    ScrapedContent,
    ScrapedMedia,
    ScrapeResult,
    ScrapeStatus,
)


# ──────────────────────────────────────────────────────────────────────────────
# ScrapedMedia
# ──────────────────────────────────────────────────────────────────────────────

class TestScrapedMedia:
    def test_video_media(self):
        m = ScrapedMedia(url="https://cdn.example.com/v.mp4", media_type="video")
        assert m.media_type == "video"
        assert m.local_path is None
        assert m.duration_seconds is None

    def test_image_media(self):
        m = ScrapedMedia(url="https://cdn.example.com/img.jpg", media_type="image")
        assert m.media_type == "image"

    def test_with_dimensions(self):
        m = ScrapedMedia(
            url="https://cdn.example.com/v.mp4",
            media_type="video",
            width=1080,
            height=1920,
            duration_seconds=30.5,
        )
        assert m.width == 1080
        assert m.height == 1920
        assert m.duration_seconds == 30.5

    def test_local_path_as_path_object(self):
        p = Path("/tmp/test.mp4")
        m = ScrapedMedia(url="https://cdn.example.com/v.mp4", media_type="video", local_path=p)
        assert m.local_path == p

    def test_missing_url_raises(self):
        with pytest.raises(Exception):
            ScrapedMedia(media_type="video")  # url is required


# ──────────────────────────────────────────────────────────────────────────────
# ScrapedContent
# ──────────────────────────────────────────────────────────────────────────────

class TestScrapedContent:
    def _make_content(self, **kwargs) -> ScrapedContent:
        defaults = dict(
            shortcode="ABC123",
            url="https://www.instagram.com/p/ABC123/",
            content_type=ContentType.IMAGE,
        )
        defaults.update(kwargs)
        return ScrapedContent(**defaults)

    def test_basic_creation(self):
        c = self._make_content()
        assert c.shortcode == "ABC123"
        assert c.content_type == ContentType.IMAGE
        assert c.media_items == []

    def test_media_count_property(self):
        media = [
            ScrapedMedia(url="https://cdn.example.com/1.jpg", media_type="image"),
            ScrapedMedia(url="https://cdn.example.com/2.jpg", media_type="image"),
        ]
        c = self._make_content(media_items=media)
        assert c.media_count == 2

    def test_has_caption_true(self):
        c = self._make_content(caption="Hello World")
        assert c.has_caption is True

    def test_has_caption_false_when_none(self):
        c = self._make_content(caption=None)
        assert c.has_caption is False

    def test_has_caption_false_when_whitespace(self):
        c = self._make_content(caption="   ")
        assert c.has_caption is False

    def test_is_video_true_for_reel(self):
        c = self._make_content(content_type=ContentType.REEL)
        assert c.is_video is True

    def test_is_video_false_for_image(self):
        c = self._make_content(content_type=ContentType.IMAGE)
        assert c.is_video is False

    def test_is_video_false_for_carousel(self):
        c = self._make_content(content_type=ContentType.CAROUSEL)
        assert c.is_video is False

    def test_scraped_at_auto_set(self):
        c = self._make_content()
        assert isinstance(c.scraped_at, datetime)

    def test_all_optional_fields(self):
        c = self._make_content(
            username="testuser",
            full_name="Test User",
            is_verified=True,
            like_count=1000,
            view_count=5000,
            comment_count=42,
            posted_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            scraper_method="ytdlp",
        )
        assert c.username == "testuser"
        assert c.like_count == 1000
        assert c.is_verified is True

    def test_content_type_enum_values(self):
        assert ContentType.REEL == "reel"
        assert ContentType.IMAGE == "image"
        assert ContentType.CAROUSEL == "carousel"
        assert ContentType.UNKNOWN == "unknown"


# ──────────────────────────────────────────────────────────────────────────────
# ScrapeResult factory methods
# ──────────────────────────────────────────────────────────────────────────────

class TestScrapeResult:
    def _make_content(self) -> ScrapedContent:
        return ScrapedContent(
            shortcode="ABC123",
            url="https://www.instagram.com/p/ABC123/",
            content_type=ContentType.REEL,
        )

    def test_success(self):
        content = self._make_content()
        result = ScrapeResult.success(content)
        assert result.status == ScrapeStatus.SUCCESS
        assert result.content is content
        assert result.error_message is None
        assert result.url == content.url

    def test_failed(self):
        result = ScrapeResult.failed("https://www.instagram.com/p/ABC123/", "Oops")
        assert result.status == ScrapeStatus.FAILED
        assert result.content is None
        assert result.error_message == "Oops"

    def test_private(self):
        result = ScrapeResult.private("https://www.instagram.com/p/ABC123/")
        assert result.status == ScrapeStatus.PRIVATE
        assert result.content is None

    def test_not_found(self):
        result = ScrapeResult.not_found("https://www.instagram.com/p/ABC123/")
        assert result.status == ScrapeStatus.NOT_FOUND

    def test_partial(self):
        content = self._make_content()
        result = ScrapeResult.partial(content, "carousel: hand off to graphql")
        assert result.status == ScrapeStatus.PARTIAL
        assert result.content is content
        assert result.error_message == "carousel: hand off to graphql"

    def test_status_enum_values(self):
        assert ScrapeStatus.SUCCESS == "success"
        assert ScrapeStatus.PARTIAL == "partial"
        assert ScrapeStatus.FAILED == "failed"
        assert ScrapeStatus.PRIVATE == "private"
        assert ScrapeStatus.NOT_FOUND == "not_found"
        assert ScrapeStatus.RATE_LIMITED == "rate_limited"
