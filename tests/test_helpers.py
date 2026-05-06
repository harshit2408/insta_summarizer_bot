"""Tests for utils/helpers.py"""

import pytest

from utils.helpers import (
    extract_shortcode,
    is_valid_instagram_url,
    normalize_instagram_url,
    sanitize_filename,
)


# ──────────────────────────────────────────────────────────────────────────────
# is_valid_instagram_url
# ──────────────────────────────────────────────────────────────────────────────

class TestIsValidInstagramUrl:
    def test_reel_url(self):
        assert is_valid_instagram_url("https://www.instagram.com/reel/ABC123/")

    def test_post_url(self):
        assert is_valid_instagram_url("https://www.instagram.com/p/XYZ789/")

    def test_tv_url(self):
        assert is_valid_instagram_url("https://www.instagram.com/tv/IGTV123/")

    def test_no_www(self):
        assert is_valid_instagram_url("https://instagram.com/p/ABC123/")

    def test_url_with_query_params(self):
        assert is_valid_instagram_url(
            "https://www.instagram.com/p/ABC123/?igsh=abc&utm_source=ig"
        )

    def test_url_with_trailing_content(self):
        assert is_valid_instagram_url("https://www.instagram.com/reel/ABC123/?igshid=xyz")

    def test_invalid_domain(self):
        assert not is_valid_instagram_url("https://www.twitter.com/p/ABC123/")

    def test_instagram_profile_url(self):
        assert not is_valid_instagram_url("https://www.instagram.com/username/")

    def test_empty_string(self):
        assert not is_valid_instagram_url("")

    def test_plain_text(self):
        assert not is_valid_instagram_url("not a url at all")

    def test_http_scheme(self):
        assert is_valid_instagram_url("http://www.instagram.com/p/ABC123/")

    def test_reels_plural(self):
        assert is_valid_instagram_url("https://www.instagram.com/reels/ABC123/")

    def test_leading_whitespace(self):
        assert is_valid_instagram_url("  https://www.instagram.com/p/ABC123/  ")


# ──────────────────────────────────────────────────────────────────────────────
# extract_shortcode
# ──────────────────────────────────────────────────────────────────────────────

class TestExtractShortcode:
    def test_reel_url(self):
        assert extract_shortcode("https://www.instagram.com/reel/ABC123/") == "ABC123"

    def test_post_url(self):
        assert extract_shortcode("https://www.instagram.com/p/XYZ789/") == "XYZ789"

    def test_url_with_query_params(self):
        assert extract_shortcode("https://www.instagram.com/p/ABC123/?igsh=foo") == "ABC123"

    def test_no_trailing_slash(self):
        assert extract_shortcode("https://www.instagram.com/p/ABC123") == "ABC123"

    def test_shortcode_with_dashes(self):
        assert extract_shortcode("https://www.instagram.com/p/Ab-cd_1/") == "Ab-cd_1"

    def test_invalid_url_returns_none(self):
        assert extract_shortcode("https://www.twitter.com/p/ABC123/") is None

    def test_empty_string_returns_none(self):
        assert extract_shortcode("") is None

    def test_no_www(self):
        assert extract_shortcode("https://instagram.com/p/SHORT1/") == "SHORT1"

    def test_tv_url(self):
        assert extract_shortcode("https://www.instagram.com/tv/IGTV1/") == "IGTV1"


# ──────────────────────────────────────────────────────────────────────────────
# normalize_instagram_url
# ──────────────────────────────────────────────────────────────────────────────

class TestNormalizeInstagramUrl:
    def test_reel_url_stays_reel(self):
        result = normalize_instagram_url("https://www.instagram.com/reel/ABC123/")
        assert result == "https://www.instagram.com/reel/ABC123/"

    def test_post_url_uses_p_prefix(self):
        result = normalize_instagram_url("https://www.instagram.com/p/XYZ789/")
        assert result == "https://www.instagram.com/p/XYZ789/"

    def test_query_params_stripped(self):
        result = normalize_instagram_url(
            "https://www.instagram.com/p/ABC123/?igsh=abc&utm_source=ig"
        )
        assert result == "https://www.instagram.com/p/ABC123/"
        assert "igsh" not in result

    def test_no_www_normalized(self):
        result = normalize_instagram_url("https://instagram.com/p/ABC123/")
        assert result == "https://www.instagram.com/p/ABC123/"

    def test_tv_url_preserved(self):
        result = normalize_instagram_url("https://www.instagram.com/tv/IGTV1/")
        assert result == "https://www.instagram.com/tv/IGTV1/"

    def test_trailing_slash_added(self):
        result = normalize_instagram_url("https://www.instagram.com/p/ABC123")
        assert result.endswith("/")

    def test_invalid_url_raises(self):
        with pytest.raises(ValueError):
            normalize_instagram_url("https://www.twitter.com/p/ABC123/")

    def test_idempotent(self):
        url = "https://www.instagram.com/p/ABC123/"
        assert normalize_instagram_url(normalize_instagram_url(url)) == url

    def test_reels_plural_normalizes_to_reel(self):
        result = normalize_instagram_url("https://www.instagram.com/reels/ABC123/")
        assert "/reel/" in result


# ──────────────────────────────────────────────────────────────────────────────
# sanitize_filename
# ──────────────────────────────────────────────────────────────────────────────

class TestSanitizeFilename:
    def test_plain_ascii(self):
        assert sanitize_filename("hello_world") == "hello_world"

    def test_spaces_replaced(self):
        result = sanitize_filename("hello world")
        assert " " not in result

    def test_special_chars_replaced(self):
        result = sanitize_filename("file/name:test*")
        assert "/" not in result
        assert ":" not in result
        assert "*" not in result

    def test_unicode_stripped_to_ascii(self):
        result = sanitize_filename("café")
        assert result == "cafe"

    def test_max_length_respected(self):
        long_name = "a" * 200
        assert len(sanitize_filename(long_name)) <= 100

    def test_custom_max_length(self):
        assert len(sanitize_filename("a" * 50, max_length=20)) <= 20

    def test_empty_string_returns_unnamed(self):
        assert sanitize_filename("") == "unnamed"

    def test_consecutive_underscores_collapsed(self):
        result = sanitize_filename("a  b")
        assert "__" not in result

    def test_dots_preserved(self):
        assert "." in sanitize_filename("file.mp4")
