"""
Debug helper – dumps the raw yt-dlp info dict for an Instagram URL.
Helps understand exactly what fields yt-dlp returns so we can improve
the scraper's media URL extraction.

Usage:
    python debug_ytdlp.py https://www.instagram.com/p/DVTymEsCalL/
"""

import json
import sys
from pathlib import Path

import yt_dlp

url = sys.argv[1] if len(sys.argv) > 1 else "https://www.instagram.com/p/DVTymEsCalL/"

opts = {
    "quiet": True,
    "skip_download": True,
    "format": "best[ext=mp4]/best",
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    },
}

print(f"\nFetching info for: {url}\n")

with yt_dlp.YoutubeDL(opts) as ydl:
    info = ydl.extract_info(url, download=False)
    info = ydl.sanitize_info(info)

# Show top-level keys and their types/values
print("=" * 60)
print("TOP-LEVEL KEYS")
print("=" * 60)
for k, v in info.items():
    if isinstance(v, (str, int, float, bool, type(None))):
        print(f"  {k:30s} = {v!r}")
    elif isinstance(v, list):
        print(f"  {k:30s} = [{len(v)} items]")
    elif isinstance(v, dict):
        print(f"  {k:30s} = {{dict, {len(v)} keys}}")

# Show entries (carousel slides)
entries = info.get("entries") or []
print(f"\n{'=' * 60}")
print(f"ENTRIES (carousel slides): {len(entries)}")
print("=" * 60)
for i, entry in enumerate(entries):
    print(f"\n  Entry [{i}] keys:")
    for k, v in entry.items():
        if isinstance(v, (str, int, float, bool, type(None))):
            snippet = repr(v)[:100]
            print(f"    {k:30s} = {snippet}")
        elif isinstance(v, list):
            print(f"    {k:30s} = [{len(v)} items]")
        elif isinstance(v, dict):
            print(f"    {k:30s} = {{dict}}")

# Show formats list
formats = info.get("formats") or []
print(f"\n{'=' * 60}")
print(f"FORMATS: {len(formats)}")
print("=" * 60)
for i, fmt in enumerate(formats[-5:]):  # last 5 (best quality)
    print(f"\n  Format [{i}]:")
    for k in ("format_id", "ext", "url", "width", "height", "vcodec", "acodec", "filesize"):
        v = fmt.get(k)
        if v is not None:
            snippet = str(v)[:120]
            print(f"    {k:20s} = {snippet}")

# Show thumbnails
thumbnails = info.get("thumbnails") or []
print(f"\n{'=' * 60}")
print(f"THUMBNAILS: {len(thumbnails)}")
print("=" * 60)
for t in thumbnails[-3:]:
    print(f"  {t.get('id','?'):5s} {t.get('width','?')}x{t.get('height','?')}  {str(t.get('url',''))[:100]}")
