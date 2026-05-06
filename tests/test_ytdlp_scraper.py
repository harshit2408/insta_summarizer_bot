"""Tests for scraper/ytdlp_scraper.py"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yt_dlp

from models.content_models import ContentType, ScrapeStatus
from scraper.ytdlp_scraper import (
    YtDlpScraper,
    _detect_content_type,
    _is_video_entry,
    _parse_timestamp,
)


# ──────────────────────────────────────────────────────────────────────────────
# _is_video_entry
# ──────────────────────────────────────────────────────────────────────────────

class TestIsVideoEntry:
    def test_type_video(self):
        assert _is_video_entry({"_type": "video"}) is True

    def test_vcodec_present(self):
        assert _is_video_entry({"vcodec": "h264"}) is True

    def test_vcodec_none_string(self):
        assert _is_video_entry({"vcodec": "none"}) is False

    def test_vcodec_null(self):
        assert _is_video_entry({"vcodec": None}) is False

    def test_video_ext_mp4(self):
        assert _is_video_entry({"video_ext": "mp4"}) is True

    def test_video_ext_none_string(self):
        assert _is_video_entry({"video_ext": "none"}) is False

    def test_ext_mp4_with_duration(self):
        assert _is_video_entry({"ext": "mp4", "duration": 30.0}) is True

    def test_ext_mp4_without_duration(self):
        assert _is_video_entry({"ext": "mp4", "duration": None}) is False

    def test_mp4_in_url(self):
        assert _is_video_entry({"url": "https://cdn.example.com/video.mp4?token=abc"}) is True

    def test_empty_dict_is_not_video(self):
        assert _is_video_entry({}) is False

    def test_image_entry(self):
        assert _is_video_entry({"ext": "jpg", "url": "https://cdn.example.com/img.jpg"}) is False


# ──────────────────────────────────────────────────────────────────────────────
# _detect_content_type
# ──────────────────────────────────────────────────────────────────────────────

class TestDetectContentType:
    def test_reel_in_url(self):
        assert _detect_content_type({"webpage_url": "https://www.instagram.com/reel/ABC/"}) == ContentType.REEL

    def test_playlist_type_is_carousel(self):
        assert _detect_content_type({"_type": "playlist"}) == ContentType.CAROUSEL

    def test_multiple_entries_is_carousel(self):
        info = {
            "webpage_url": "https://www.instagram.com/p/ABC/",
            "entries": [{"id": "1"}, {"id": "2"}],
        }
        assert _detect_content_type(info) == ContentType.CAROUSEL

    def test_video_signals_returns_reel(self):
        info = {
            "webpage_url": "https://www.instagram.com/p/ABC/",
            "_type": "video",
        }
        assert _detect_content_type(info) == ContentType.REEL

    def test_vcodec_returns_reel(self):
        info = {
            "webpage_url": "https://www.instagram.com/p/ABC/",
            "vcodec": "h264",
        }
        assert _detect_content_type(info) == ContentType.REEL

    def test_no_video_signals_returns_image(self):
        info = {"webpage_url": "https://www.instagram.com/p/ABC/"}
        assert _detect_content_type(info) == ContentType.IMAGE

    def test_single_entry_no_video_is_image(self):
        info = {
            "webpage_url": "https://www.instagram.com/p/ABC/",
            "entries": [{"id": "1"}],
        }
        assert _detect_content_type(info) == ContentType.IMAGE


# ──────────────────────────────────────────────────────────────────────────────
# _parse_timestamp
# ──────────────────────────────────────────────────────────────────────────────

class TestParseTimestamp:
    def test_valid_unix_timestamp(self):
        result = _parse_timestamp(1700000000)
        assert result is not None
        assert result.year == 2023

    def test_float_timestamp(self):
        result = _parse_timestamp(1700000000.0)
        assert result is not None

    def test_none_returns_none(self):
        assert _parse_timestamp(None) is None

    def test_invalid_string_returns_none(self):
        assert _parse_timestamp("not-a-timestamp") is None

    def test_zero_timestamp(self):
        result = _parse_timestamp(0)
        assert result is not None
        assert result.year == 1970


# ──────────────────────────────────────────────────────────────────────────────
# YtDlpScraper._entry_to_media
# ──────────────────────────────────────────────────────────────────────────────

class TestEntryToMedia:
    def setup_method(self):
        self.scraper = YtDlpScraper(download_dir="/tmp/test_dl", download_media=False)

    def test_video_entry_direct_url(self):
        entry = {
            "_type": "video",
            "url": "https://cdn.fbcdn.net/video.mp4",
            "width": 720,
            "height": 1280,
            "duration": 30.0,
        }
        media = self.scraper._entry_to_media(entry)
        assert media is not None
        assert media.media_type == "video"
        assert media.url == "https://cdn.fbcdn.net/video.mp4"
        assert media.width == 720
        assert media.duration_seconds == 30.0

    def test_image_entry_from_formats(self):
        entry = {
            "formats": [
                {"url": "https://cdn.fbcdn.net/img_low.jpg", "width": 320, "height": 320},
                {"url": "https://cdn.fbcdn.net/img_high.jpg", "width": 1080, "height": 1080},
            ]
        }
        media = self.scraper._entry_to_media(entry)
        assert media is not None
        assert media.media_type == "image"
        # Should use the last format (highest quality)
        assert "img_high" in media.url

    def test_entry_from_thumbnails_fallback(self):
        entry = {
            "thumbnails": [
                {"url": "https://cdn.fbcdn.net/thumb.jpg", "width": 640, "height": 640}
            ]
        }
        media = self.scraper._entry_to_media(entry)
        assert media is not None
        assert media.media_type == "image"

    def test_none_entry_returns_none(self):
        assert self.scraper._entry_to_media(None) is None

    def test_empty_dict_returns_media_with_webpage_url(self):
        entry = {"webpage_url": "https://www.instagram.com/p/ABC/"}
        media = self.scraper._entry_to_media(entry)
        assert media is not None
        assert media.url == "https://www.instagram.com/p/ABC/"

    def test_instagram_url_in_url_field_uses_formats(self):
        entry = {
            "_type": "video",
            "url": "https://www.instagram.com/p/ABC/",   # filtered out
            "formats": [{"url": "https://cdn.fbcdn.net/video.mp4"}],
        }
        media = self.scraper._entry_to_media(entry)
        assert media is not None
        assert "fbcdn" in media.url


# ──────────────────────────────────────────────────────────────────────────────
# YtDlpScraper._build_content
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildContent:
    def setup_method(self):
        self.scraper = YtDlpScraper(download_dir="/tmp/test_dl", download_media=False)

    def _reel_info(self, **overrides):
        base = {
            "id": "ABC123",
            "webpage_url": "https://www.instagram.com/reel/ABC123/",
            "_type": "video",
            "url": "https://cdn.fbcdn.net/video.mp4",
            "description": "Test caption",
            "uploader_id": "testuser",
            "uploader": "Test User",
            "like_count": 1000,
            "comment_count": 50,
            "timestamp": 1700000000,
            "duration": 30.0,
        }
        base.update(overrides)
        return base

    def test_reel_content_type(self):
        info = self._reel_info()
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.content_type == ContentType.REEL

    def test_caption_extracted(self):
        info = self._reel_info(description="Hello World")
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.caption == "Hello World"

    def test_username_extracted(self):
        info = self._reel_info(uploader_id="myuser")
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.username == "myuser"

    def test_username_at_prefix_stripped(self):
        info = self._reel_info(uploader_id="@myuser")
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.username == "myuser"

    def test_scraper_method_is_ytdlp(self):
        info = self._reel_info()
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.scraper_method == "ytdlp"

    def test_posted_at_parsed(self):
        info = self._reel_info(timestamp=1700000000)
        content = self.scraper._build_content(info, info["webpage_url"], "ABC123")
        assert content.posted_at is not None
        assert content.posted_at.year == 2023

    def test_carousel_info_with_entries(self):
        info = {
            "id": "CAR1",
            "webpage_url": "https://www.instagram.com/p/CAR1/",
            "_type": "playlist",
            "entries": [
                {"id": "s1", "url": "https://cdn.fbcdn.net/img1.jpg"},
                {"id": "s2", "url": "https://cdn.fbcdn.net/img2.jpg"},
            ],
            "description": "carousel post",
        }
        content = self.scraper._build_content(info, info["webpage_url"], "CAR1")
        assert content.content_type == ContentType.CAROUSEL


# ──────────────────────────────────────────────────────────────────────────────
# YtDlpScraper.scrape — mocked at _extract_info_metadata_only
# ──────────────────────────────────────────────────────────────────────────────

class TestYtDlpScraperScrape:
    REEL_URL = "https://www.instagram.com/reel/ABC123/"
    POST_URL = "https://www.instagram.com/p/CAR1/"

    def setup_method(self):
        self.scraper = YtDlpScraper(download_dir="/tmp/test_dl", download_media=False)

    def _reel_info(self):
        return {
            "id": "ABC123",
            "webpage_url": self.REEL_URL,
            "_type": "video",
            "url": "https://cdn.fbcdn.net/video.mp4",
            "description": "Test caption",
            "uploader_id": "testuser",
            "uploader": "Test User",
            "like_count": 100,
            "timestamp": 1700000000,
            "duration": 30.0,
        }

    def test_reel_success_no_download(self):
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=self._reel_info()):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.SUCCESS
        assert result.content.content_type == ContentType.REEL
        assert result.content.has_caption

    def test_reel_success_with_download(self):
        self.scraper.download_media = True
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=self._reel_info()), \
             patch.object(self.scraper, "_download_media") as mock_dl:
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.SUCCESS
        mock_dl.assert_called_once()

    def test_carousel_returns_partial(self):
        carousel_info = {
            "id": "CAR1",
            "webpage_url": self.POST_URL,
            "_type": "playlist",
            "entries": [{"id": "s1"}, {"id": "s2"}],
            "description": "test",
        }
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=carousel_info):
            result = self.scraper.scrape(self.POST_URL)
        assert result.status == ScrapeStatus.PARTIAL
        assert result.content.content_type == ContentType.CAROUSEL

    def test_private_content(self):
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value={"_private": True}):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.PRIVATE

    def test_not_found_content(self):
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value={"_not_found": True}):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.NOT_FOUND

    def test_none_info_returns_failed(self):
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=None):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.FAILED

    def test_invalid_url_returns_failed(self):
        result = self.scraper.scrape("https://www.twitter.com/p/ABC123/")
        assert result.status == ScrapeStatus.FAILED

    def test_empty_id_treated_as_private(self):
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value={}):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.PRIVATE

    def test_caption_only_no_media_returns_partial(self):
        info = {
            "id": "ABC123",
            "webpage_url": self.REEL_URL,
            "description": "Some caption",
            # no url, no formats, no thumbnails
        }
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=info):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.PARTIAL
        assert result.content.has_caption

    def test_no_caption_no_media_returns_failed(self):
        info = {
            "id": "ABC123",
            "webpage_url": self.REEL_URL,
            # no description, no media
        }
        with patch.object(self.scraper, "_extract_info_metadata_only", return_value=info):
            result = self.scraper.scrape(self.REEL_URL)
        assert result.status == ScrapeStatus.FAILED


# ──────────────────────────────────────────────────────────────────────────────
# YtDlpScraper._extract_info_metadata_only — error path coverage
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractInfoMetadataOnly:
    def setup_method(self):
        self.scraper = YtDlpScraper(download_dir="/tmp/test_dl", download_media=False)

    def test_download_error_private(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl = MagicMock()
            mock_ydl_class.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl_class.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("login required")
            result = self.scraper._extract_info_metadata_only("https://www.instagram.com/p/ABC/")
        assert result == {"_private": True}

    def test_download_error_not_found(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl = MagicMock()
            mock_ydl_class.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl_class.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = yt_dlp.utils.DownloadError("not found 404")
            result = self.scraper._extract_info_metadata_only("https://www.instagram.com/p/ABC/")
        assert result == {"_not_found": True}

    def test_generic_exception_returns_none(self):
        with patch("yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl = MagicMock()
            mock_ydl_class.return_value.__enter__ = MagicMock(return_value=mock_ydl)
            mock_ydl_class.return_value.__exit__ = MagicMock(return_value=False)
            mock_ydl.extract_info.side_effect = RuntimeError("unexpected")
            result = self.scraper._extract_info_metadata_only("https://www.instagram.com/p/ABC/")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# YtDlpScraper._download_media
# ──────────────────────────────────────────────────────────────────────────────

class TestDownloadMedia:
    def setup_method(self, tmp_path=None):
        self.scraper = YtDlpScraper(download_dir="/tmp/test_dl_dm", download_media=True)

    def _make_content_with_media(self, url, media_type):
        from models.content_models import ScrapedContent, ScrapedMedia
        media = ScrapedMedia(url=url, media_type=media_type)
        return ScrapedContent(
            shortcode="SC1",
            url="https://www.instagram.com/p/SC1/",
            content_type=ContentType.IMAGE if media_type == "image" else ContentType.REEL,
            media_items=[media],
        ), media

    def test_video_saved_as_mp4(self):
        content, media = self._make_content_with_media("https://cdn.example.com/v.mp4", "video")
        with patch("scraper.ytdlp_scraper.urlretrieve") as mock_dl, \
             patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 1_000_000
            self.scraper._download_media(content)
        mock_dl.assert_called_once()
        call_args = mock_dl.call_args[0]
        assert str(call_args[1]).endswith(".mp4")

    def test_image_saved_as_jpg(self):
        content, media = self._make_content_with_media("https://cdn.example.com/img.jpg", "image")
        with patch("scraper.ytdlp_scraper.urlretrieve") as mock_dl, \
             patch("pathlib.Path.stat") as mock_stat:
            mock_stat.return_value.st_size = 500_000
            self.scraper._download_media(content)
        mock_dl.assert_called_once()
        call_args = mock_dl.call_args[0]
        assert str(call_args[1]).endswith(".jpg")

    def test_already_downloaded_skipped(self, tmp_path):
        self.scraper.download_dir = Path(str(tmp_path)) if tmp_path else Path("/tmp")
        existing = Path("/tmp/existing.mp4")
        from models.content_models import ScrapedContent, ScrapedMedia
        media = ScrapedMedia(url="https://cdn.example.com/v.mp4", media_type="video", local_path=existing)
        content = ScrapedContent(
            shortcode="SC2",
            url="https://www.instagram.com/p/SC2/",
            content_type=ContentType.REEL,
            media_items=[media],
        )
        with patch("scraper.ytdlp_scraper.urlretrieve") as mock_dl, \
             patch.object(Path, "exists", return_value=True):
            self.scraper._download_media(content)
        mock_dl.assert_not_called()

    def test_download_error_handled_gracefully(self):
        content, _ = self._make_content_with_media("https://cdn.example.com/v.mp4", "video")
        with patch("scraper.ytdlp_scraper.urlretrieve", side_effect=OSError("network fail")):
            # Should not raise
            self.scraper._download_media(content)
