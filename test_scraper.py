"""
Quick smoke-test / demo for the Instagram scraper.

Usage
-----
    # Test with a real URL (requires network):
    python test_scraper.py https://www.instagram.com/reel/ABC123/

    # Test URL validation only:
    python test_scraper.py --validate-only https://www.instagram.com/reel/ABC123/

    # Run unit tests for helpers (no network needed):
    python test_scraper.py --unit
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

# ── Logging setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s – %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("test_scraper")

# ── Path setup (allow running from project root) ──────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from models.content_models import ScrapeStatus
from scraper import InstagramScraper
from utils.helpers import (
    extract_shortcode,
    is_valid_instagram_url,
    normalize_instagram_url,
    sanitize_filename,
)


# ─────────────────────────────────────────────────────────────────────────────
# Unit tests (no network)
# ─────────────────────────────────────────────────────────────────────────────

def run_unit_tests() -> None:
    print("\n" + "=" * 60)
    print(" UNIT TESTS (no network)")
    print("=" * 60)

    cases = [
        # (url, expected_valid, expected_shortcode, expected_normalized_prefix)
        (
            "https://www.instagram.com/reel/ABC123/",
            True, "ABC123", "https://www.instagram.com/reel/ABC123/",
        ),
        (
            "https://instagram.com/reel/XYZ789?igsh=sometoken",
            True, "XYZ789", "https://www.instagram.com/reel/XYZ789/",
        ),
        (
            "https://www.instagram.com/p/CDE456/",
            True, "CDE456", "https://www.instagram.com/p/CDE456/",
        ),
        (
            "https://www.instagram.com/tv/FGH012/",
            True, "FGH012", "https://www.instagram.com/tv/FGH012/",
        ),
        (
            "https://www.youtube.com/watch?v=abc",
            False, None, None,
        ),
        (
            "not a url at all",
            False, None, None,
        ),
    ]

    passed = 0
    failed = 0

    for url, exp_valid, exp_sc, exp_norm in cases:
        valid = is_valid_instagram_url(url)
        sc = extract_shortcode(url)
        try:
            norm = normalize_instagram_url(url) if exp_valid else None
        except ValueError:
            norm = None

        ok = (
            valid == exp_valid
            and sc == exp_sc
            and (exp_norm is None or norm == exp_norm)
        )

        status = "✅ PASS" if ok else "❌ FAIL"
        if ok:
            passed += 1
        else:
            failed += 1

        print(f"\n{status}  {url[:60]}")
        if not ok:
            print(f"       valid    : got={valid!r}  expected={exp_valid!r}")
            print(f"       shortcode: got={sc!r}  expected={exp_sc!r}")
            print(f"       normalized: got={norm!r}  expected={exp_norm!r}")

    print(f"\n{'=' * 60}")
    print(f" Results: {passed} passed, {failed} failed")
    print("=" * 60)

    # Test sanitize_filename
    print("\n── sanitize_filename ──")
    tests_sf = [
        ("Hello World!", "Hello_World"),
        ("Python: Best Practices & Tips", "Python_Best_Practices_Tips"),
        ("", "unnamed"),
        ("日本語テスト", "unnamed"),
    ]
    for inp, expected in tests_sf:
        result = sanitize_filename(inp)
        ok = result == expected
        status = "✅" if ok else "❌"
        print(f"  {status}  {inp!r:40s} → {result!r}  (expected {expected!r})")

    if failed > 0:
        sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────────
# Live scrape test
# ─────────────────────────────────────────────────────────────────────────────

def run_live_scrape(url: str, metadata_only: bool = False) -> None:
    print("\n" + "=" * 60)
    print(f" LIVE SCRAPE{'  (metadata only)' if metadata_only else ''}")
    print("=" * 60)
    print(f" URL: {url}")
    print("=" * 60)

    scraper = InstagramScraper(download_dir="./downloads", download_media=not metadata_only)

    if metadata_only:
        result = scraper.scrape_metadata_only(url)
    else:
        result = scraper.scrape(url)

    print(f"\nStatus : {result.status.value.upper()}")

    if result.status == ScrapeStatus.FAILED:
        print(f"Error  : {result.error_message}")
        sys.exit(1)

    if result.status == ScrapeStatus.PARTIAL:
        print(f"Note   : {result.error_message}")

    if result.content is None:
        print("No content returned.")
        sys.exit(1)

    c = result.content
    print(f"\n── Content ──────────────────────────────────────────────")
    print(f"  Type        : {c.content_type.value}")
    print(f"  Shortcode   : {c.shortcode}")
    print(f"  Username    : @{c.username}")
    print(f"  Full Name   : {c.full_name}")
    print(f"  Posted At   : {c.posted_at}")
    print(f"  Likes       : {c.like_count}")
    print(f"  Views       : {c.view_count}")
    print(f"  Comments    : {c.comment_count}")
    print(f"  Scraper     : {c.scraper_method}")
    print(f"  Media Items : {c.media_count}")

    for i, m in enumerate(c.media_items, 1):
        print(f"\n  Media [{i}]")
        print(f"    Type       : {m.media_type}")
        print(f"    Resolution : {m.width}×{m.height}")
        print(f"    Duration   : {m.duration_seconds}s")
        print(f"    Local Path : {m.local_path}")
        print(f"    URL        : {m.url[:80]}...")

    if c.caption:
        print(f"\n── Caption (first 300 chars) ────────────────────────────")
        print(f"  {c.caption[:300]}")

    print("\n── JSON Export ──────────────────────────────────────────")
    as_dict = c.model_dump(mode="json")
    # Truncate long URLs in the JSON for readability
    print(json.dumps(as_dict, indent=2, default=str)[:2000])
    print("\n✅ Done!\n")


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test the Instagram scraper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("url", nargs="?", help="Instagram URL to scrape")
    parser.add_argument(
        "--unit", action="store_true", help="Run unit tests (no network)"
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only check URL validity, do not scrape",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Fetch metadata without downloading media",
    )
    args = parser.parse_args()

    if args.unit:
        run_unit_tests()
        return

    if not args.url:
        parser.print_help()
        print("\n⚠  Provide a URL or use --unit for offline tests.\n")
        sys.exit(1)

    if args.validate_only:
        valid = is_valid_instagram_url(args.url)
        sc = extract_shortcode(args.url)
        print(f"\nURL      : {args.url}")
        print(f"Valid    : {valid}")
        print(f"Shortcode: {sc}")
        if valid:
            print(f"Normalized: {normalize_instagram_url(args.url)}")
        return

    run_live_scrape(args.url, metadata_only=args.metadata_only)


if __name__ == "__main__":
    main()
