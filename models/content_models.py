"""
Pydantic data models for scraped Instagram content.
Matches the data shapes described in the PRD (Section 7 - Data Models).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class ContentType(str, Enum):
    REEL = "reel"
    IMAGE = "image"
    CAROUSEL = "carousel"
    UNKNOWN = "unknown"


class ScrapeStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"   # Caption/metadata extracted but no media files
    FAILED = "failed"
    PRIVATE = "private"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"


class ScrapedMedia(BaseModel):
    """Represents a single downloadable media file (video or image)."""

    url: str = Field(..., description="Direct URL to the media file")
    media_type: str = Field(..., description="'video' or 'image'")
    local_path: Optional[Path] = Field(None, description="Path to downloaded file")
    width: Optional[int] = None
    height: Optional[int] = None
    duration_seconds: Optional[float] = None

    model_config = {"arbitrary_types_allowed": True}


class ScrapedContent(BaseModel):
    """
    All content extracted from a single Instagram post/reel.
    This is the output of the scraper and the input to the content extractor pipeline.
    """

    # Identity
    shortcode: str = Field(..., description="Instagram post shortcode (e.g. ABC123)")
    url: str = Field(..., description="Original Instagram URL")
    content_type: ContentType = ContentType.UNKNOWN

    # Media
    media_items: list[ScrapedMedia] = Field(
        default_factory=list,
        description="All media files (1 for reels/images, N for carousels)",
    )
    thumbnail_url: Optional[str] = Field(None, description="Cover/thumbnail image URL")

    # Text content
    caption: Optional[str] = Field(None, description="Post caption text")

    # Creator metadata
    username: Optional[str] = None
    full_name: Optional[str] = None
    is_verified: Optional[bool] = None

    # Engagement metrics
    like_count: Optional[int] = None
    view_count: Optional[int] = None
    comment_count: Optional[int] = None

    # Timestamps
    posted_at: Optional[datetime] = None
    scraped_at: datetime = Field(default_factory=datetime.utcnow)

    # Scraper metadata
    scraper_method: Optional[str] = Field(
        None, description="Which scraper was used: 'ytdlp' or 'rapidapi'"
    )

    @property
    def is_video(self) -> bool:
        return self.content_type == ContentType.REEL

    @property
    def media_count(self) -> int:
        return len(self.media_items)

    @property
    def has_caption(self) -> bool:
        return bool(self.caption and self.caption.strip())


class ScrapeResult(BaseModel):
    """Wraps ScrapedContent with status information for error handling."""

    status: ScrapeStatus
    content: Optional[ScrapedContent] = None
    error_message: Optional[str] = None
    url: str

    @classmethod
    def success(cls, content: ScrapedContent) -> "ScrapeResult":
        return cls(status=ScrapeStatus.SUCCESS, content=content, url=content.url)

    @classmethod
    def partial(cls, content: ScrapedContent, message: str) -> "ScrapeResult":
        """Caption/metadata was retrieved but no media could be downloaded."""
        return cls(
            status=ScrapeStatus.PARTIAL,
            content=content,
            url=content.url,
            error_message=message,
        )

    @classmethod
    def failed(cls, url: str, message: str) -> "ScrapeResult":
        return cls(status=ScrapeStatus.FAILED, url=url, error_message=message)

    @classmethod
    def private(cls, url: str) -> "ScrapeResult":
        return cls(
            status=ScrapeStatus.PRIVATE,
            url=url,
            error_message="This content is private or requires authentication.",
        )

    @classmethod
    def not_found(cls, url: str) -> "ScrapeResult":
        return cls(
            status=ScrapeStatus.NOT_FOUND,
            url=url,
            error_message="Content not found. It may have been deleted.",
        )
