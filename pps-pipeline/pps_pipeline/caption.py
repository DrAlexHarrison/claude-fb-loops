"""pps_pipeline.caption — keyframe sampling + captioning (the swappable edge).

**Sample, never stream.** Per the investigation, raw video must never reach the
LLM. ``caption.py`` samples a sparse set of keyframes (1/chunk by default) with
ffmpeg and turns each into a short *text* caption. Only the caption text flows
downstream — the frame bytes are discarded here, at the edge.

Backends (one interface, swappable):

* ``mock`` — canned captions; free + deterministic; the only backend the tests /
  ``make demo`` use. No ffmpeg, no model, no network.
* ``claude`` — Claude vision on the sampled frame (best quality, Max auth, no
  metered spend). The production default. Never invoked by tests.
* ``ollama`` — local LLaVA/BLIP via Ollama (free, fully local fallback).

In the CORE demo, captions are **pre-baked in the bundle** (the fixture ships a
``captions.jsonl``), so this module is not on the demo path — it is the real
recorder-side step you swap in when you have a ``video.mkv``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

from .bundle import RawEvent
from .chunk import Chunk


@dataclass
class CaptionBackendBase:
    name: str = "base"

    def caption_frame(self, frame_path: str, hint: str = "") -> str:  # pragma: no cover
        raise NotImplementedError


class MockCaptionBackend(CaptionBackendBase):
    """Returns canned captions (per-chunk list or a constant). Offline."""

    def __init__(self, captions: Optional[Sequence[str]] = None,
                 constant: str = "screen frame (mock caption)"):
        self.name = "mock"
        self.captions = list(captions) if captions is not None else None
        self.constant = constant
        self._i = 0

    def caption_frame(self, frame_path: str, hint: str = "") -> str:
        if self.captions is not None:
            cap = self.captions[self._i % len(self.captions)]
            self._i += 1
            return cap
        return self.constant


class ClaudeCaptionBackend(CaptionBackendBase):
    """Claude vision on a sampled frame (Max auth). Production default.

    NOT used by tests / demo. Reads the image bytes ONLY to send to the captioner
    and returns text; the bytes never enter the package.
    """

    def __init__(self, model: Optional[str] = None, timeout: int = 90):
        self.name = "claude"
        self.model = model
        self.timeout = timeout

    def caption_frame(self, frame_path: str, hint: str = "") -> str:  # pragma: no cover
        # Intentionally not wired to a live call in the core build. A real
        # implementation would pass the image to the Claude API / `claude -p`
        # with a "describe what the developer is doing on screen" instruction.
        raise NotImplementedError(
            "ClaudeCaptionBackend is documented but not wired in the core build; "
            "use backend='mock' for tests/demo, or implement the vision call here.")


class OllamaCaptionBackend(CaptionBackendBase):
    """Local LLaVA/BLIP via Ollama — the free local fallback."""

    def __init__(self, model: str = "llava", host: str = "http://localhost:11434"):
        self.name = "ollama"
        self.model = model
        self.host = host

    def caption_frame(self, frame_path: str, hint: str = "") -> str:  # pragma: no cover
        import base64
        import json
        import urllib.request
        with open(frame_path, "rb") as fh:
            img_b64 = base64.b64encode(fh.read()).decode()
        body = json.dumps({
            "model": self.model,
            "prompt": "Describe what the developer is doing on screen in one line.",
            "images": [img_b64], "stream": False,
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/generate", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode()).get("response", "").strip()


def make_caption_backend(name: str, **kw) -> CaptionBackendBase:
    name = (name or "mock").lower()
    if name == "mock":
        return MockCaptionBackend(**kw)
    if name == "claude":
        return ClaudeCaptionBackend(**kw)
    if name == "ollama":
        return OllamaCaptionBackend(**kw)
    raise ValueError(f"unknown caption backend: {name!r}")


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def sample_keyframe(video_path: str, t: float, out_path: str) -> str:  # pragma: no cover
    """Extract ONE frame at ``t`` seconds via ffmpeg. The edge step that turns
    video into a still we can caption (and then discard)."""
    if not ffmpeg_available():
        raise RuntimeError("ffmpeg not available — captioning is an edge dep")
    cmd = ["ffmpeg", "-y", "-ss", str(t), "-i", video_path, "-frames:v", "1",
           "-q:v", "3", out_path]
    subprocess.run(cmd, capture_output=True, check=True)
    return out_path


def caption_chunks(video_path: str, chunks: Sequence[Chunk],
                   backend: str | CaptionBackendBase = "mock",
                   frames_dir: Optional[str] = None) -> list[RawEvent]:
    """Caption one keyframe per chunk -> ``caption`` RawEvents (text-only).

    With ``backend='mock'`` no ffmpeg/model is invoked (frames are not sampled),
    so this is safe + deterministic in CI. With a real backend it samples the
    mid-chunk frame, captions it, and discards the image.
    """
    be = backend if isinstance(backend, CaptionBackendBase) else make_caption_backend(backend)
    events: list[RawEvent] = []
    for c in chunks:
        mid = c.t_start + (c.t_end - c.t_start) / 2.0
        if be.name == "mock":
            text = be.caption_frame("", hint=f"chunk{c.index}")
        else:  # pragma: no cover - edge path, never in tests
            fdir = frames_dir or "/tmp/pps-frames"
            os.makedirs(fdir, exist_ok=True)
            fp = os.path.join(fdir, f"frame_{c.index}.jpg")
            sample_keyframe(video_path, mid, fp)
            text = be.caption_frame(fp, hint=f"chunk{c.index}")
            try:
                os.remove(fp)  # the frame bytes never persist into the package
            except OSError:
                pass
        events.append(RawEvent(round(mid, 3), "caption", text,
                               f"frame@{int(mid)}s", {"chunk": c.index}))
    return events
