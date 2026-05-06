"""
OCR text extraction using EasyOCR.

EasyOCR supports 80+ languages and works well on:
  - Text-heavy slides / infographics (common in educational Instagram posts)
  - Overlaid text on images
  - Carousel slides with bullet points

We run OCR on:
  - Single image posts
  - Every slide in a carousel
  - Key frames extracted from video (every N seconds) — for text-on-screen reels
"""

from __future__ import annotations

import logging
from pathlib import Path

import cv2
import numpy as np

logger = logging.getLogger(__name__)

# Module-level reader cache — EasyOCR model loading takes ~10s
_ocr_reader = None


def get_reader():
    """Load EasyOCR reader once and cache for the container's lifetime."""
    global _ocr_reader
    if _ocr_reader is None:
        import easyocr
        import os

        logger.info("Loading EasyOCR reader (en)...")

        # Models are pre-baked at /opt/easyocr_models (read-only at runtime).
        # user_network MUST be writable — EasyOCR.__init__ unconditionally
        # tries to mkdir it. Point it at /tmp (the only writable dir in Lambda).
        user_network_dir = "/tmp/easyocr_user_network"
        os.makedirs(user_network_dir, exist_ok=True)

        _ocr_reader = easyocr.Reader(
            ["en"],
            gpu=False,
            model_storage_directory="/opt/easyocr_models",
            user_network_directory=user_network_dir,
            download_enabled=False,  # never try to download — fail loudly if model missing
        )
        logger.info("EasyOCR reader ready")
    return _ocr_reader


def extract_text_from_image(image_path: Path) -> str | None:
    """
    Run OCR on a single image file.

    Parameters
    ----------
    image_path : Path
        Local path to the image (.jpg, .png, .webp, etc.)

    Returns
    -------
    str | None
        Extracted text joined into a single string, or None if nothing found.
    """
    if not image_path.exists():
        logger.warning("Image not found: %s", image_path)
        return None

    try:
        reader = get_reader()
        results = reader.readtext(
            str(image_path),
            detail=0,           # return just the text strings, not bounding boxes
            paragraph=True,     # merge nearby text into paragraphs
        )
        text = " ".join(r.strip() for r in results if r.strip())
        logger.info("OCR: %s → %d chars", image_path.name, len(text))
        return text or None

    except Exception:
        logger.exception("OCR failed for %s", image_path)
        return None


def extract_text_from_images(image_paths: list[Path]) -> str | None:
    """
    Run OCR across multiple images (e.g. carousel slides) and combine results.

    Returns None if all images produce empty text.
    """
    texts = []
    for path in image_paths:
        text = extract_text_from_image(path)
        if text:
            texts.append(text)

    return "\n---\n".join(texts) if texts else None


def extract_key_frames(
    video_path: Path,
    interval_seconds: float = 5.0,
    max_frames: int | None = None,
) -> list[Path]:
    """
    Extract key frames from a video at regular intervals for OCR.

    Used for reels where important text appears on-screen (e.g. tutorial steps).

    Parameters
    ----------
    video_path : Path
        Local path to the downloaded video.
    interval_seconds : float
        How often to capture a frame (default: every 5 seconds).
    max_frames : int | None
        Hard cap on number of frames to extract. None = unlimited.
        Use this to bound OCR time on Lambda (each frame ~7s on CPU).

    Returns
    -------
    list[Path]
        Paths to saved JPEG frame images in the same directory as the video.
    """
    if not video_path.exists():
        return []

    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        logger.warning("Could not open video: %s", video_path)
        return []

    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    frame_interval = max(1, int(fps * interval_seconds))
    frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    saved_frames: list[Path] = []
    frame_idx = 0

    while frame_idx < frame_count:
        if max_frames is not None and len(saved_frames) >= max_frames:
            break

        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            break

        out_path = video_path.parent / f"frame_{frame_idx:06d}.jpg"
        cv2.imwrite(str(out_path), frame)
        saved_frames.append(out_path)

        frame_idx += frame_interval

    cap.release()
    logger.info("Extracted %d key frames from %s", len(saved_frames), video_path.name)
    return saved_frames


def extract_text_from_video_frames(
    video_path: Path,
    interval_seconds: float = 5.0,
    max_frames: int | None = None,
) -> str | None:
    """
    Extract text from video by sampling frames and running OCR on each.

    Returns combined text from all frames that contained readable text.
    """
    frames = extract_key_frames(video_path, interval_seconds, max_frames=max_frames)
    if not frames:
        return None

    try:
        return extract_text_from_images(frames)
    finally:
        # Clean up frame files
        for frame_path in frames:
            frame_path.unlink(missing_ok=True)
