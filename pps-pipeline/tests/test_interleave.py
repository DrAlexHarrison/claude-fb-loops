"""The packager (the CORE original work). Pins the three load-bearing
invariants: monotonic time order, every event exactly once, text-only."""

from __future__ import annotations

import pytest

from pps_pipeline import _schema_util as su
from pps_pipeline.bundle import RawEvent
from pps_pipeline.chunk import make_chunks
from pps_pipeline.interleave import (PackagingError, assert_text_only,
                                     interleave, package_text)


def _mk(events, duration=200.0, mode="event_boundary"):
    chunks = make_chunks(events, duration, mode=mode)
    return interleave(events, chunks, "sid", mode=mode)


# --- Invariant 1: strict (monotonic non-decreasing) time order ------------- #
def test_timeline_is_time_ordered(package):
    ts = [e["t"] for e in package["timeline"]]
    assert ts == sorted(ts)
    for a, b in zip(ts, ts[1:]):
        assert a <= b


def test_out_of_order_input_is_sorted():
    evs = [RawEvent(40.0, "speech", "later", "s1"),
           RawEvent(10.0, "prompt", "first", "p1"),
           RawEvent(25.0, "net", "mid", "n1")]
    pkg = _mk(evs, mode="fixed")
    assert [e["t"] for e in pkg["timeline"]] == [10.0, 25.0, 40.0]


def test_tie_break_is_deterministic():
    # Same timestamp across kinds -> fixed kind-priority order, reproducible.
    evs = [RawEvent(5.0, "net", "n", "n"),
           RawEvent(5.0, "prompt", "p", "p"),
           RawEvent(5.0, "caption", "c", "c"),
           RawEvent(5.0, "tool_call", "tc", "tc")]
    k1 = [e["kind"] for e in _mk(evs, mode="fixed")["timeline"]]
    k2 = [e["kind"] for e in _mk(list(reversed(evs)), mode="fixed")["timeline"]]
    assert k1 == k2 == ["prompt", "caption", "tool_call", "net"]


# --- Invariant 2: every event exactly once --------------------------------- #
def test_every_event_exactly_once(loaded_bundle, package):
    n_in = len(loaded_bundle.raw_events())
    assert len(package["timeline"]) == n_in
    sources = [e["source"] for e in package["timeline"]]
    assert len(set(sources)) == len(sources)  # none dropped or duplicated


def test_count_mismatch_is_impossible_by_construction():
    evs = [RawEvent(float(i), "speech", f"s{i}", f"src{i}") for i in range(7)]
    pkg = _mk(evs, mode="fixed")
    assert len(pkg["timeline"]) == 7


# --- Invariant 3: text only, no raw bytes ---------------------------------- #
def test_text_only_no_bytes(package):
    assert_text_only(package["timeline"])  # does not raise
    for e in package["timeline"]:
        assert isinstance(e["text"], str)
        for v in e.values():
            assert not isinstance(v, (bytes, bytearray))


def test_assert_text_only_rejects_bytes():
    bad = [{"t": 1.0, "kind": "caption", "text": "ok", "source": "x"},
           {"t": 2.0, "kind": "caption", "text": "raw", "source": "y",
            "frame": b"\x89PNG..."}]
    with pytest.raises(PackagingError):
        assert_text_only(bad)


def test_interleave_rejects_nonstring_text():
    evs = [RawEvent(1.0, "caption", "ok", "a")]
    # Force a non-str text post-hoc to prove the structural guard fires.
    evs.append(RawEvent(2.0, "caption", None, "b"))  # type: ignore[arg-type]
    with pytest.raises(PackagingError):
        _mk(evs, mode="fixed")


# --- package shape + chunk alignment --------------------------------------- #
def test_package_is_schema_valid(package):
    assert su.validation_errors("package.schema.json", package) == []


def test_chunking_metadata(package):
    assert package["chunking"]["mode"] == "event_boundary"
    assert package["chunking"]["chunk_count"] == 6


def test_timeline_chunk_indices_align_to_tool_calls(loaded_bundle, package):
    # Each tool_call entry should open (be the first event of) its chunk window.
    tool_starts = sorted({round(e.t, 6) for e in loaded_bundle.raw_events()
                          if e.kind == "tool_call"})
    chunk_starts = {e["chunk"]: e["t"] for e in reversed(package["timeline"])}
    # the t of the earliest event in each chunk:
    earliest = {}
    for e in package["timeline"]:
        earliest.setdefault(e["chunk"], e["t"])
    # chunks 1..5 each begin exactly at a tool-call time.
    for idx in range(1, 6):
        assert round(earliest[idx], 6) in tool_starts


def test_package_text_is_pure_text(package):
    txt = package_text(package)
    assert isinstance(txt, str) and "tool_call" in txt
