"""pps_pipeline.interleave — THE packager (the core original work).

There is no turnkey OSS for "merge a captioned screen recording + ASR transcript
+ tool calls + network into one thing an LLM can reason over". This module is
that thing. It takes the redacted, per-stream events and merges them into a
single :class:`InterleavedPackage`: one **strictly time-ordered, text-only**
timeline, every source event present exactly once, each entry source-referenced
so the assessor can cite evidence.

The three invariants this module *guarantees* (and the tests pin):

1. **Monotonic time order.** ``timeline[i].t <= timeline[i+1].t`` for all i.
   Ties (same ``t`` across streams) are broken deterministically by a fixed
   ``kind`` priority then by source, so the merge is reproducible.
2. **Every event exactly once.** ``len(timeline) == len(input events)`` and no
   source ref is dropped or duplicated.
3. **Text only — no raw bytes.** Every entry is ``{t, kind, text, source}`` with
   ``text`` a ``str``; the structural check :func:`assert_text_only` proves no
   ``bytes`` / image / video object can be present. The "never feed raw video to
   the LLM" constraint is enforced *structurally*, not by convention.
"""

from __future__ import annotations

import json
from typing import Sequence

from . import _schema_util as _su
from .bundle import RawEvent
from .chunk import Chunk, assign_chunk

SCHEMA_VERSION = "1.0"

# Deterministic tie-break order when two events share the same timestamp. Prompt
# (intent) precedes the caption of the screen at that instant, which precedes the
# words spoken, the tool call, its result, then network/other.
_KIND_PRIORITY = {
    "prompt": 0,
    "caption": 1,
    "speech": 2,
    "tool_call": 3,
    "tool_result": 4,
    "net": 5,
    "event": 6,
}


class PackagingError(ValueError):
    """Raised when an interleave invariant would be violated."""


def _sort_key(e: RawEvent):
    return (round(e.t, 6), _KIND_PRIORITY.get(e.kind, 99), e.source)


def assert_text_only(timeline: list[dict]) -> None:
    """Prove the package carries no raw image/video bytes.

    Every entry must be a plain dict of JSON scalars with a ``str`` ``text`` and
    no ``bytes`` anywhere. This is the structural enforcement of "raw video
    never enters the package / reaches the LLM".
    """
    for i, entry in enumerate(timeline):
        if not isinstance(entry, dict):
            raise PackagingError(f"timeline[{i}] is not a dict")
        if not isinstance(entry.get("text"), str):
            raise PackagingError(f"timeline[{i}].text is not str")
        for k, v in entry.items():
            if isinstance(v, (bytes, bytearray, memoryview)):
                raise PackagingError(f"timeline[{i}].{k} carries raw bytes")
    # Final belt-and-suspenders: the whole timeline must be JSON-serializable
    # with no binary. json.dumps raises TypeError on bytes/objects.
    try:
        json.dumps(timeline)
    except TypeError as exc:  # pragma: no cover - defensive
        raise PackagingError(f"timeline is not pure-text JSON: {exc}") from exc


def interleave(events: Sequence[RawEvent], chunks: Sequence[Chunk],
               session_id: str, mode: str = "event_boundary",
               floor_clean: bool = True, redaction_applied: bool = True,
               validate: bool = True) -> dict:
    """Merge ``events`` into one ``InterleavedPackage`` dict.

    ``events`` should already be redacted (see :mod:`pps_pipeline.redact_pass`);
    interleave does not redact. It sorts, enforces the invariants, attaches the
    owning chunk index, and stamps the redaction gate state onto the package.
    """
    ordered = sorted(events, key=_sort_key)

    timeline: list[dict] = []
    for e in ordered:
        timeline.append({
            "t": round(float(e.t), 3),
            "kind": e.kind,
            "text": e.text,
            "source": e.source,
            "chunk": assign_chunk(chunks, e.t),
        })

    # Invariant 1: monotonic non-decreasing time.
    for i in range(1, len(timeline)):
        if timeline[i]["t"] < timeline[i - 1]["t"]:
            raise PackagingError(
                f"non-monotonic timeline at {i}: "
                f"{timeline[i - 1]['t']} -> {timeline[i]['t']}")

    # Invariant 2: every event exactly once (count + unique source preserved).
    if len(timeline) != len(events):
        raise PackagingError(
            f"event count changed: {len(events)} in, {len(timeline)} out")

    # Invariant 3: text only, no raw bytes.
    assert_text_only(timeline)

    package = {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "chunking": {"mode": mode, "chunk_count": len(chunks)},
        "redaction": {"applied": bool(redaction_applied),
                      "floor_clean": bool(floor_clean)},
        "timeline": timeline,
    }

    if validate:
        errs = _su.validation_errors("package.schema.json", package)
        if errs:
            raise PackagingError("package schema errors: " + "; ".join(errs))
    return package


def package_text(package: dict) -> str:
    """The concatenated timeline text — what an assessor (or the leak-scan gate)
    actually reads. Pure text, by construction."""
    return "\n".join(f'[{e["t"]:.1f}s {e["kind"]}] {e["text"]}'
                     for e in package.get("timeline", []))
