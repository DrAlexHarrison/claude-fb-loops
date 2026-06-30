"""Smoke tests for the swappable edge (captioning + ASR). The real backends
(ffmpeg/Claude-vision/Ollama/faster-whisper) are NEVER invoked in CI — only the
mock paths + the reuse seams are pinned."""

from __future__ import annotations

import os

from pps_pipeline import transcribe as TR
from pps_pipeline.caption import (MockCaptionBackend, caption_chunks,
                                  make_caption_backend)
from pps_pipeline.chunk import chunk_fixed


def test_caption_mock_backend_no_ffmpeg():
    chunks = chunk_fixed(60.0, window_s=30.0)  # 2 chunks
    caps = ["editor on auth.ts", "terminal green"]
    evs = caption_chunks("/nonexistent/video.mkv",
                         chunks, backend=MockCaptionBackend(captions=caps))
    assert len(evs) == 2
    assert all(e.kind == "caption" and isinstance(e.text, str) for e in evs)
    assert evs[0].text == "editor on auth.ts"
    # caption events are text-only and timestamped (mid-chunk)
    assert evs[0].t == 15.0 and evs[1].t == 45.0


def test_caption_backend_factory():
    assert make_caption_backend("mock").name == "mock"
    assert make_caption_backend("claude").name == "claude"
    assert make_caption_backend("ollama").name == "ollama"


def test_transcribe_mock_segments_and_reuse_seam():
    segs = TR.mock_segments([(0.0, 2.0, "hello"), (2.0, 4.0, "world")])
    assert segs == [{"start": 0.0, "end": 2.0, "text": "hello"},
                    {"start": 2.0, "end": 4.0, "text": "world"}]
    # the reuse seam points at the fb-assist voice faster-whisper wrapper
    assert TR.voice_wrapper_path().endswith(os.path.join("voice", "transcribe.py"))
    assert os.path.exists(TR.voice_wrapper_path())
