"""pps_pipeline.chunk — segment a session into time windows.

Two modes:

* ``fixed`` — fixed N-second windows (simple; the fallback when there is no
  ``session.jsonl`` to align to).
* ``event_boundary`` — a new chunk begins at each **tool-call event** in the
  Claude Code session, because a tool call is the natural unit of work for a
  CC session (read the file, edit, run tests, …). This is the recommended mode
  whenever a ``ccode_session`` stream is present.

A chunk is a half-open window ``[t_start, t_end)``. Chunks are used (a) as the
keyframe-sampling unit for captioning (1 frame / chunk) and (b) as metadata on
the package. The packager still emits one flat timeline; chunks are an overlay.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from typing import Iterable, Sequence

from .bundle import RawEvent


@dataclass
class Chunk:
    index: int
    t_start: float
    t_end: float

    def contains(self, t: float) -> bool:
        return self.t_start <= t < self.t_end


def _dedup_sorted(values: Iterable[float]) -> list[float]:
    out: list[float] = []
    for v in sorted(values):
        if not out or v > out[-1] + 1e-9:
            out.append(float(v))
    return out


def chunk_fixed(duration_s: float, window_s: float = 30.0) -> list[Chunk]:
    """Fixed ``window_s``-second windows spanning ``[0, duration_s)``."""
    if duration_s <= 0:
        return [Chunk(0, 0.0, 0.0)]
    if window_s <= 0:
        window_s = 30.0
    chunks: list[Chunk] = []
    i = 0
    t = 0.0
    while t < duration_s - 1e-9:
        end = min(t + window_s, duration_s)
        chunks.append(Chunk(i, t, end))
        t = end
        i += 1
    return chunks


def chunk_event_boundary(events: Sequence[RawEvent], duration_s: float) -> list[Chunk]:
    """A new chunk at each tool-call event time.

    Boundaries are the sorted, de-duplicated timestamps of ``tool_call`` events.
    Chunk 0 spans ``[0, first_boundary)``; each subsequent chunk starts exactly
    at a tool-call event time (the invariant ``interleave`` / the tests check).
    """
    boundaries = _dedup_sorted(e.t for e in events if e.kind == "tool_call")
    starts = _dedup_sorted([0.0] + boundaries)
    chunks: list[Chunk] = []
    for i, st in enumerate(starts):
        end = starts[i + 1] if i + 1 < len(starts) else max(duration_s, st)
        chunks.append(Chunk(i, st, end))
    return chunks


def make_chunks(events: Sequence[RawEvent], duration_s: float,
                mode: str = "event_boundary", window_s: float = 30.0) -> list[Chunk]:
    """Build chunks. Falls back to ``fixed`` when ``event_boundary`` is asked for
    but there are no tool-call events to align to."""
    if mode == "fixed":
        return chunk_fixed(duration_s, window_s)
    if mode == "event_boundary":
        if any(e.kind == "tool_call" for e in events):
            return chunk_event_boundary(events, duration_s)
        return chunk_fixed(duration_s, window_s)
    raise ValueError(f"unknown chunk mode: {mode!r}")


def assign_chunk(chunks: Sequence[Chunk], t: float) -> int:
    """Index of the chunk whose window contains ``t`` (last chunk for the
    boundary at ``duration``)."""
    if not chunks:
        return 0
    starts = [c.t_start for c in chunks]
    i = bisect.bisect_right(starts, t) - 1
    if i < 0:
        i = 0
    if i >= len(chunks):
        i = len(chunks) - 1
    return chunks[i].index
