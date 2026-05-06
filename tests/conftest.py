"""
Test setup shared across all tests.

The AI Analyzer Lambda's modules (handler.py, schema.py, prompts.py,
groq_client.py) live inside ``lambdas/ai_analyzer/`` and import each other
as sibling top-level modules — exactly how AWS Lambda loads a zip-packaged
handler. To make those imports work under pytest we add the directory to
``sys.path`` so ``import schema`` resolves to ``lambdas/ai_analyzer/schema.py``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

_AI_ANALYZER_DIR = _ROOT / "lambdas" / "ai_analyzer"
if _AI_ANALYZER_DIR.is_dir():
    sys.path.insert(0, str(_AI_ANALYZER_DIR))
