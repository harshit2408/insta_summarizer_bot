# ─────────────────────────────────────────────────────────────────────────────
# Content Extractor Lambda — Docker container image
#
# Base: python:3.11-slim (Debian Bookworm)
# Why NOT the official Lambda AL2 base image:
#   - AL2 base has NO package manager (no yum/dnf/microdnf)
#   - ffmpeg, libgomp, etc. cannot be installed via any standard method
#   - Debian slim has apt-get with full package availability
#
# Lambda compatibility is provided by awslambdaric (AWS Lambda Runtime
# Interface Client) — the official Python package for custom Lambda runtimes.
# This is the recommended AWS pattern for containers with heavy dependencies.
#
# Build context: project root  (run `docker build` from repo root)
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# ── System dependencies ───────────────────────────────────────────────────────
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        libgomp1 \
        libglib2.0-0 \
        libsm6 \
        libxext6 \
    && rm -rf /var/lib/apt/lists/*

# ── Lambda Runtime Interface Client ──────────────────────────────────────────
# Makes any container work as a Lambda function (replaces the Lambda base image runtime)
RUN pip install --no-cache-dir awslambdaric

# ── Python dependencies ───────────────────────────────────────────────────────
COPY lambdas/content_extractor/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Pre-download Whisper model ────────────────────────────────────────────────
# Download directly to /opt/whisper_models/<size>/ using snapshot_download.
# This stores model files (config.json, model.bin, etc.) at a known fixed path
# that audio.py reads directly — bypassing HuggingFace Hub cache entirely.
# Lambda's /home and /root are read-only; /opt is readable at runtime.
ARG WHISPER_MODEL_SIZE=base
RUN python3 -c "\
from huggingface_hub import snapshot_download; \
import os; \
out = '/opt/whisper_models/${WHISPER_MODEL_SIZE}'; \
os.makedirs(out, exist_ok=True); \
snapshot_download('Systran/faster-whisper-${WHISPER_MODEL_SIZE}', local_dir=out); \
print('Whisper model saved to', out)"

# ── Pre-download EasyOCR models ───────────────────────────────────────────────
RUN python3 -c "import easyocr; easyocr.Reader(['en'], gpu=False, model_storage_directory='/opt/easyocr_models', download_enabled=True); print('EasyOCR ready')"
ENV EASYOCR_MODULE_PATH=/opt/easyocr_models

# ── Application code ──────────────────────────────────────────────────────────
ENV LAMBDA_TASK_ROOT=/var/task
WORKDIR ${LAMBDA_TASK_ROOT}

COPY lambdas/content_extractor/handler.py  ${LAMBDA_TASK_ROOT}/handler.py
COPY lambdas/content_extractor/audio.py    ${LAMBDA_TASK_ROOT}/audio.py
COPY lambdas/content_extractor/ocr.py      ${LAMBDA_TASK_ROOT}/ocr.py

COPY scraper/  ${LAMBDA_TASK_ROOT}/scraper/
COPY models/   ${LAMBDA_TASK_ROOT}/models/
COPY utils/    ${LAMBDA_TASK_ROOT}/utils/

# ── Lambda entry point ────────────────────────────────────────────────────────
# awslambdaric acts as the runtime — it polls Lambda for events and calls the handler
ENTRYPOINT ["/usr/local/bin/python3", "-m", "awslambdaric"]
CMD ["handler.lambda_handler"]
