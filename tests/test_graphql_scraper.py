"""Tests for scraper/graphql_scraper.py"""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from models.content_models import ContentType, ScrapeStatus
from scraper.graphql_scraper import GraphQLScraper, _parse_timestamp


# ──────────────────────────────────────────────────────────────────────────────
# _parse_timestamp
# ──────────────────────────────────────────────────────────────────────────────

class TestParseTimestamp:
    def test_valid_unix(self):
        result = _parse_timestamp(1700000000)
        assert result is not None
        assert result.year == 2023

    def test_none_returns_none(self):
        assert _parse_timestamp(None) is None

    def test_invalid_returns_none(self):
        assert _parse_timestamp("bad") is None


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper._build_payload
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildPayload:
    def setup_method(self):
        self.scraper = GraphQLScraper(download_dir="/tmp/test_dl", download_media=False)

    def test_contains_shortcode(self):
        payload = self.scraper._build_payload("ABC123")
        assert "ABC123" in payload

    def test_contains_doc_id(self):
        payload = self.scraper._build_payload("ABC123")
        assert "doc_id=" in payload

    def test_is_string(self):
        assert isinstance(self.scraper._build_payload("XYZ"), str)

    def test_different_shortcodes(self):
        p1 = self.scraper._build_payload("AAA")
        p2 = self.scraper._build_payload("BBB")
        assert p1 != p2


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper._extract_item
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractItem:
    def setup_method(self):
        self.scraper = GraphQLScraper(download_dir="/tmp/test_dl", download_media=False)

    def _make_raw(self, item):
        return {
            "data": {
                "xdt_api__v1__media__shortcode__web_info": {
                    "items": [item]
                }
            }
        }

    def test_returns_first_item(self):
        item = {"id": "1", "media_type": 1}
        raw = self._make_raw(item)
        assert self.scraper._extract_item(raw) == item

    def test_empty_items_returns_none(self):
        raw = {"data": {"xdt_api__v1__media__shortcode__web_info": {"items": []}}}
        assert self.scraper._extract_item(raw) is None

    def test_missing_data_key_returns_none(self):
        assert self.scraper._extract_item({}) is None

    def test_none_input_returns_none(self):
        assert self.scraper._extract_item({"data": None}) is None

    def test_special_flags_not_items(self):
        assert self.scraper._extract_item({"_rate_limited": True}) is None


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper._extract_media_item
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractMediaItem:
    def setup_method(self):
        self.scraper = GraphQLScraper(download_dir="/tmp/test_dl", download_media=False)

    def test_video_item(self):
        item = {
            "media_type": 2,
            "video_versions": [
                {"url": "https://cdn.example.com/v.mp4", "width": 720, "height": 1280}
            ],
            "video_duration": 30.5,
        }
        media = self.scraper._extract_media_item(item)
        assert media is not None
        assert media.media_type == "video"
        assert media.url == "https://cdn.example.com/v.mp4"
        assert media.duration_seconds == 30.5

    def test_image_item(self):
        item = {
            "media_type": 1,
            "image_versions2": {
                "candidates": [
                    {"url": "https://cdn.example.com/img.jpg", "width": 1080, "height": 1080}
                ]
            },
        }
        media = self.scraper._extract_media_item(item)
        assert media is not None
        assert media.media_type == "image"
        assert media.url == "https://cdn.example.com/img.jpg"

    def test_none_returns_none(self):
        assert self.scraper._extract_media_item(None) is None

    def test_empty_dict_returns_none(self):
        assert self.scraper._extract_media_item({}) is None

    def test_video_no_versions_returns_none(self):
        item = {"media_type": 2, "video_versions": []}
        assert self.scraper._extract_media_item(item) is None

    def test_image_no_candidates_returns_none(self):
        item = {"media_type": 1, "image_versions2": {"candidates": []}}
        assert self.scraper._extract_media_item(item) is None


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper._build_content
# ──────────────────────────────────────────────────────────────────────────────

class TestBuildContent:
    def setup_method(self):
        self.scraper = GraphQLScraper(download_dir="/tmp/test_dl", download_media=False)
        self.url = "https://www.instagram.com/p/ABC123/"

    def _image_item(self, **overrides):
        base = {
            "media_type": 1,
            "image_versions2": {
                "candidates": [
                    {"url": "https://cdn.example.com/img.jpg", "width": 1080, "height": 1080}
                ]
            },
            "user": {"username": "testuser", "full_name": "Test User", "is_verified": False},
            "caption": {"text": "Test caption"},
            "like_count": 500,
            "comment_count": 20,
            "taken_at": 1700000000,
        }
        base.update(overrides)
        return base

    def _video_item(self, **overrides):
        base = {
            "media_type": 2,
            "video_versions": [
                {"url": "https://cdn.example.com/v.mp4", "width": 720, "height": 1280}
            ],
            "video_duration": 45.0,
            "user": {"username": "creator", "full_name": "Creator Name"},
            "caption": {"text": "Video caption"},
            "like_count": 1000,
            "taken_at": 1700000000,
        }
        base.update(overrides)
        return base

    def test_image_content_type(self):
        content = self.scraper._build_content(self._image_item(), self.url, "ABC123")
        assert content.content_type == ContentType.IMAGE

    def test_video_content_type(self):
        content = self.scraper._build_content(self._video_item(), self.url, "ABC123")
        assert content.content_type == ContentType.REEL

    def test_carousel_content_type(self):
        item = {
            "media_type": 8,
            "carousel_media": [
                {
                    "media_type": 1,
                    "image_versions2": {"candidates": [{"url": "https://cdn.example.com/1.jpg"}]},
                },
                {
                    "media_type": 1,
                    "image_versions2": {"candidates": [{"url": "https://cdn.example.com/2.jpg"}]},
                },
            ],
            "user": {"username": "u"},
            "caption": {"text": "carousel"},
            "taken_at": 1700000000,
        }
        content = self.scraper._build_content(item, self.url, "ABC123")
        assert content.content_type == ContentType.CAROUSEL
        assert content.media_count == 2

    def test_caption_extracted(self):
        content = self.scraper._build_content(self._image_item(), self.url, "ABC123")
        assert content.caption == "Test caption"

    def test_caption_none_when_absent(self):
        item = self._image_item()
        item["caption"] = None
        content = self.scraper._build_content(item, self.url, "ABC123")
        assert content.caption is None

    def test_username_extracted(self):
        content = self.scraper._build_content(self._image_item(), self.url, "ABC123")
        assert content.username == "testuser"

    def test_like_count_extracted(self):
        content = self.scraper._build_content(self._image_item(), self.url, "ABC123")
        assert content.like_count == 500

    def test_scraper_method_is_graphql(self):
        content = self.scraper._build_content(self._image_item(), self.url, "ABC123")
        assert content.scraper_method == "graphql"

    def test_unknown_media_type(self):
        item = self._image_item(media_type=99)
        content = self.scraper._build_content(item, self.url, "ABC123")
        assert content.content_type == ContentType.UNKNOWN


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper._fetch — HTTP layer mocked
# ──────────────────────────────────────────────────────────────────────────────

class TestFetch:
    def setup_method(self):
        self.scraper = GraphQLScraper(
            download_dir="/tmp/test_dl", download_media=False, max_retries=1
        )

    def _mock_response(self, status_code=200, json_data=None):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {"data": {}}
        return resp

    def test_200_returns_json(self):
        with patch("requests.post", return_value=self._mock_response(200, {"data": "ok"})):
            result = self.scraper._fetch("ABC123")
        assert result == {"data": "ok"}

    def test_429_returns_rate_limited(self):
        with patch("requests.post", return_value=self._mock_response(429)):
            result = self.scraper._fetch("ABC123")
        assert result == {"_rate_limited": True}

    def test_404_returns_not_found(self):
        with patch("requests.post", return_value=self._mock_response(404)):
            result = self.scraper._fetch("ABC123")
        assert result == {"_not_found": True}

    def test_500_all_retries_returns_none(self):
        with patch("requests.post", return_value=self._mock_response(500)), \
             patch("time.sleep"):
            result = self.scraper._fetch("ABC123")
        assert result is None

    def test_timeout_returns_none(self):
        with patch("requests.post", side_effect=requests.exceptions.Timeout()), \
             patch("time.sleep"):
            result = self.scraper._fetch("ABC123")
        assert result is None

    def test_request_exception_returns_none(self):
        with patch("requests.post", side_effect=requests.exceptions.ConnectionError("fail")):
            result = self.scraper._fetch("ABC123")
        assert result is None

    def test_unexpected_exception_returns_none(self):
        with patch("requests.post", side_effect=RuntimeError("boom")):
            result = self.scraper._fetch("ABC123")
        assert result is None


# ──────────────────────────────────────────────────────────────────────────────
# GraphQLScraper.scrape — end-to-end with mocked HTTP
# ──────────────────────────────────────────────────────────────────────────────

class TestGraphQLScraperScrape:
    URL = "https://www.instagram.com/p/ABC123/"

    def setup_method(self):
        self.scraper = GraphQLScraper(download_dir="/tmp/test_dl", download_media=False)

    def _make_raw(self, item):
        return {
            "data": {
                "xdt_api__v1__media__shortcode__web_info": {"items": [item]}
            }
        }

    def _image_item(self):
        return {
            "media_type": 1,
            "image_versions2": {
                "candidates": [{"url": "https://cdn.example.com/img.jpg", "width": 1080}]
            },
            "user": {"username": "u", "full_name": "U"},
            "caption": {"text": "cap"},
            "taken_at": 1700000000,
        }

    def test_image_success(self):
        with patch.object(self.scraper, "_fetch", return_value=self._make_raw(self._image_item())):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.SUCCESS
        assert result.content.content_type == ContentType.IMAGE

    def test_fetch_returns_none_fails(self):
        with patch.object(self.scraper, "_fetch", return_value=None):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.FAILED

    def test_rate_limited(self):
        with patch.object(self.scraper, "_fetch", return_value={"_rate_limited": True}):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.FAILED

    def test_not_found(self):
        with patch.object(self.scraper, "_fetch", return_value={"_not_found": True}):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.NOT_FOUND

    def test_private(self):
        with patch.object(self.scraper, "_fetch", return_value={"_private": True}):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.PRIVATE

    def test_empty_item_fails(self):
        with patch.object(self.scraper, "_fetch", return_value={"data": {}}):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.FAILED

    def test_invalid_url_fails(self):
        result = self.scraper.scrape("https://www.twitter.com/p/ABC/")
        assert result.status == ScrapeStatus.FAILED

    def test_no_shortcode_fails(self):
        result = self.scraper.scrape("https://www.instagram.com/")
        assert result.status == ScrapeStatus.FAILED

    def test_download_called_when_enabled(self):
        self.scraper.download_media = True
        with patch.object(self.scraper, "_fetch", return_value=self._make_raw(self._image_item())), \
             patch.object(self.scraper, "_download_all") as mock_dl:
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.SUCCESS
        mock_dl.assert_called_once()

    def test_no_media_no_caption_fails(self):
        item = {
            "media_type": 1,
            "image_versions2": {"candidates": []},
            "user": {"username": "u"},
            "caption": None,
        }
        with patch.object(self.scraper, "_fetch", return_value=self._make_raw(item)):
            result = self.scraper.scrape(self.URL)
        assert result.status == ScrapeStatus.FAILED
