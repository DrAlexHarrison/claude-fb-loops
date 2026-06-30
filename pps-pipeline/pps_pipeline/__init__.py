"""pps_pipeline — PPS work-observation interview pipeline (Build 2).

Turns a recorded work-observation ``SessionBundle`` (video + audio + ASR
transcript + network HAR + the candidate's Claude Code ``.jsonl`` + an events
stream) into an interleaved, timestamp-aligned, **text-only** multimodal package,
then into a structured, evidence-cited Claude assessment of the candidate's work.

Two load-bearing, *structurally enforced* constraints from the investigation:

1. **Raw video/image bytes never enter the package or reach the LLM.** The
   ``InterleavedPackage`` is text only — frames are sampled + captioned upstream
   and only the caption *text* is interleaved (see :mod:`pps_pipeline.interleave`).
2. **The packager is the original work.** :func:`pps_pipeline.interleave.interleave`
   gets the timestamp-merge + strict-ordering + every-event-exactly-once
   invariants right; everything else (capture, captioning, ASR) is a swappable
   edge or a reuse of ``fb_assist``.

Maximal reuse: the candidate's ``session.jsonl`` is parsed with
``fb_assist.transcripts``; every text surface is redacted with ``fb_assist.redact``
(the proven detector floor + leak-scan egress gate); ASR reuses the
``fb-assist/voice`` faster-whisper wrapper.
"""

from __future__ import annotations

import os as _os
import sys as _sys

# fb_assist's redaction stack forces the torch-only path; mirror it defensively
# so importing fb_assist from here never trips transformers' TF/Keras-3 import.
_os.environ.setdefault("USE_TF", "0")
_os.environ.setdefault("USE_FLAX", "0")
_os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
_os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")


def _ensure_fb_assist_importable() -> None:
    """Make ``import fb_assist`` resolve without a pip install.

    We depend on the sibling ``fb-assist/`` package but deliberately do NOT
    install it (its heavy deps — presidio/gliner/spacy — must not be re-resolved
    over a metered link). If ``fb_assist`` isn't already importable, add the
    sibling ``fb-assist/`` source dir to ``sys.path`` so the package directory
    resolves directly.
    """
    try:
        import fb_assist  # noqa: F401
        return
    except Exception:
        pass
    here = _os.path.dirname(_os.path.abspath(__file__))
    repo_root = _os.path.dirname(_os.path.dirname(here))  # …/anthropic-feedback-loops
    fb = _os.path.join(repo_root, "fb-assist")
    if _os.path.isdir(_os.path.join(fb, "fb_assist")) and fb not in _sys.path:
        _sys.path.insert(0, fb)


_ensure_fb_assist_importable()

__version__ = "0.1.0"

__all__ = ["__version__"]
