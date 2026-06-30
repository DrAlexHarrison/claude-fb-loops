"""Pytest bootstrap for the fb-assist suite.

Two jobs:

1. Make ``fb_assist`` importable straight from a source checkout (no install
   needed), and force ``USE_TF=0`` before anything imports transformers/gliner
   (Keras 3 has no ``tf-keras``; the lazy TF path would otherwise break import).

2. **Generate the synthetic fixtures on demand.** ``sample-mid.jsonl`` (>1 MB)
   and ``sample-large.jsonl`` (>50 MB) are deterministic, fully-synthetic
   transcripts — they replace the original real personal sessions, which never
   ship. They are git-ignored and (re)built here if absent, so a fresh
   ``git clone && pytest`` just works with zero manual steps and zero real data.
   See ``tests/fixtures/generate_fixtures.py``.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_TESTS = Path(__file__).resolve().parent
_PKG_ROOT = _TESTS.parent            # fb-assist/  (holds the fb_assist package)
_FIXTURES = _TESTS / "fixtures"

if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# Import the generator by path (fixtures/ is not a package).
sys.path.insert(0, str(_FIXTURES))
import generate_fixtures as _gen  # noqa: E402


def _ensure_synthetic_fixtures() -> None:
    """Build any missing synthetic fixture (idempotent; ~0.1 s mid, ~2 s large)."""
    _gen.ensure(_FIXTURES, large=True)


# Generate at collection time, before any test module's body runs.
_ensure_synthetic_fixtures()
