"""
Utility helpers for URL validation and file management.
"""

import re
import unicodedata
from urllib.parse import urlparse, urlunparse

# Matches all known Instagram content URL patterns:
# /p/    → single image or carousel
# /reel/ → reel
# /tv/   → IGTV (legacy)
_INSTAGRAM_URL_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"(?:p|reel|tv|reels)/"
    r"([A-Za-z0-9_\-]+)"
    r"/?",
    re.IGNORECASE,
)


def is_valid_instagram_url(url: str) -> bool:
    """Return True if *url* is a recognisable Instagram post/reel URL."""
    return bool(_INSTAGRAM_URL_RE.search(url.strip()))


def extract_shortcode(url: str) -> str | None:
    """
    Pull the shortcode out of an Instagram URL.

    Examples
    --------
    >>> extract_shortcode("https://www.instagram.com/reel/ABC123/")
    'ABC123'
    >>> extract_shortcode("https://instagram.com/p/XYZ789?igsh=abc")
    'XYZ789'
    """
    match = _INSTAGRAM_URL_RE.search(url.strip())
    return match.group(1) if match else None


def normalize_instagram_url(url: str) -> str:
    """
    Canonicalize an Instagram URL:
    - Strip query parameters and fragments
    - Ensure trailing slash
    - Use https://www.instagram.com/...

    This makes duplicate-detection in DynamoDB reliable.
    """
    shortcode = extract_shortcode(url)
    if not shortcode:
        raise ValueError(f"Not a valid Instagram URL: {url!r}")

    # Determine content path prefix from the original URL
    lower = url.lower()
    if "/reel/" in lower or "/reels/" in lower:
        prefix = "reel"
    elif "/tv/" in lower:
        prefix = "tv"
    else:
        prefix = "p"

    return f"https://www.instagram.com/{prefix}/{shortcode}/"


def sanitize_filename(name: str, max_length: int = 100) -> str:
    """
    Convert an arbitrary string into a safe filename.
    Strips accents, replaces spaces/special chars with underscores.
    """
    # Normalize unicode (NFKD → ASCII)
    normalized = unicodedata.normalize("NFKD", name)
    ascii_bytes = normalized.encode("ascii", "ignore")
    safe = ascii_bytes.decode("ascii")

    # Replace anything that's not alphanumeric, dash, or dot
    safe = re.sub(r"[^\w\-.]", "_", safe)

    # Collapse consecutive underscores
    safe = re.sub(r"_+", "_", safe).strip("_")

    return safe[:max_length] or "unnamed"
