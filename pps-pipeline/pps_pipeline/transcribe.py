"""pps_pipeline.transcribe — ASR (the swappable edge; faster-whisper reuse).

Only runs when a bundle **lacks** a ``transcript`` stream. It reuses the existing
``fb-assist/voice/transcribe.py`` faster-whisper wrapper — same model selection
(env ``FBW_MODEL``, default ``base.en``), same CPU/int8 settings — and adds the
per-segment timestamps the packager needs (the voice script returns joined text;
work-observation needs ``{start, end, text}`` so speech can be placed on the
timeline).

faster-whisper downloads a model on first use (~145 MB for ``base.en``), so this
is an **edge** dependency and is never invoked by the tests / ``make demo`` — the
fixture ships a transcript. Use ``mock`` for deterministic tests.
"""

from __future__ import annotations

import os
from typing import Optional

# Where the reused voice wrapper lives (documents the reuse seam).
_VOICE_WRAPPER = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..",
    "fb-assist", "voice", "transcribe.py"))

FBW_MODEL = os.environ.get("FBW_MODEL", "base.en")


def voice_wrapper_path() -> str:
    """Absolute path to the fb-assist voice faster-whisper wrapper (reuse seam)."""
    return _VOICE_WRAPPER


def transcribe_segments(wav_path: str, model: Optional[str] = None) -> list[dict]:
    """Transcribe ``wav_path`` -> ``[{start, end, text}, …]`` via faster-whisper.

    Mirrors ``fb-assist/voice/transcribe.py`` (same model/params) but keeps the
    per-segment timing. EDGE: loads/downloads a model — never called in tests.
    """
    from faster_whisper import WhisperModel  # pragma: no cover - edge dep

    m = WhisperModel(model or FBW_MODEL, device="cpu", compute_type="int8",
                     cpu_threads=4)  # pragma: no cover
    segments, _info = m.transcribe(wav_path, vad_filter=True, language="en",
                                   beam_size=1)  # pragma: no cover
    return [{"start": float(s.start), "end": float(s.end),
             "text": s.text.strip()} for s in segments]  # pragma: no cover


def transcribe_text(wav_path: str, model: Optional[str] = None) -> str:
    """Joined transcript text (parity with the voice wrapper's output)."""
    return " ".join(s["text"] for s in transcribe_segments(wav_path, model)).strip()  # pragma: no cover


def mock_segments(lines: list[tuple[float, float, str]]) -> list[dict]:
    """Build deterministic segments from ``(start, end, text)`` tuples — the test
    substitute for a real ASR run."""
    return [{"start": float(a), "end": float(b), "text": t} for a, b, t in lines]
