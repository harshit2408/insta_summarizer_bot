"""
Audio transcription using Faster-Whisper.

Faster-Whisper is a reimplementation of OpenAI Whisper using CTranslate2,
giving 4x speedup on CPU with the same accuracy.

Model sizes and tradeoffs (CPU inference):
  tiny   ~75 MB   ~15–20s for a 60s video  (lower accuracy)
  base   ~145 MB  ~30–40s for a 60s video  (good balance — used by default)
  small  ~470 MB  ~90s for a 60s video     (higher accuracy, may exceed Lambda timeout)
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level model cache — reused across Lambda warm invocations
_whisper_model = None


def get_model(model_size: str = "base"):
    """Load Whisper model once and cache it for the lifetime of the Lambda container."""
    global _whisper_model
    if _whisper_model is None:
        from faster_whisper import WhisperModel

        # Use the path where the model was baked into the container image.
        # Passing a local directory path bypasses all HuggingFace Hub cache
        # logic, so Lambda's read-only filesystem (/home, /root) is never touched.
        baked_path = f"/opt/whisper_models/{model_size}"
        if os.path.isdir(baked_path):
            model_source = baked_path
            logger.info("Loading Whisper model from baked path: %s", baked_path)
        else:
            # Fallback for local dev — download from HuggingFace
            model_source = model_size
            logger.warning(
                "Baked model not found at %s — downloading from HuggingFace (cold start will be slow)",
                baked_path,
            )

        _whisper_model = WhisperModel(
            model_source,
            device="cpu",
            compute_type="int8",
        )
        logger.info("Whisper model ready")
    return _whisper_model


def transcribe_video(video_path: Path, model_size: str = "base") -> str | None:
    """
    Extract audio from a video file and transcribe it to text.

    Parameters
    ----------
    video_path : Path
        Local path to the downloaded video file (.mp4, .mov, etc.)
    model_size : str
        Whisper model size — see module docstring for options.

    Returns
    -------
    str | None
        Transcript text, or None if extraction/transcription failed.
    """
    if not video_path.exists():
        logger.warning("Video file not found: %s", video_path)
        return None

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        audio_path = Path(tmp.name)

    try:
        _extract_audio(video_path, audio_path)
        if not audio_path.exists() or audio_path.stat().st_size == 0:
            logger.warning("Audio extraction produced empty file for %s", video_path)
            return None

        transcript = _run_whisper(audio_path, model_size)
        logger.info(
            "Transcribed %s → %d chars",
            video_path.name,
            len(transcript) if transcript else 0,
        )
        return transcript

    except Exception:
        logger.exception("Transcription failed for %s", video_path)
        return None

    finally:
        # Always clean up temp audio file
        if audio_path.exists():
            audio_path.unlink(missing_ok=True)


def _extract_audio(video_path: Path, audio_path: Path) -> None:
    """Use ffmpeg to extract mono 16kHz WAV — optimal for Whisper."""
    cmd = [
        "ffmpeg",
        "-i", str(video_path),
        "-ar", "16000",   # 16 kHz sample rate (Whisper's native rate)
        "-ac", "1",       # mono
        "-c:a", "pcm_s16le",  # 16-bit PCM WAV
        str(audio_path),
        "-y",             # overwrite output
        "-loglevel", "error",
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg failed (exit {result.returncode}): {result.stderr.decode()}"
        )


def _run_whisper(audio_path: Path, model_size: str) -> str | None:
    """Run Faster-Whisper transcription and concatenate all segments."""
    model = get_model(model_size)
    segments, info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,          # skip silent segments (Voice Activity Detection)
        vad_parameters={"min_silence_duration_ms": 500},
    )
    logger.info(
        "Detected language: %s (probability: %.2f)",
        info.language,
        info.language_probability,
    )

    text_parts = [seg.text.strip() for seg in segments if seg.text.strip()]
    return " ".join(text_parts) if text_parts else None
