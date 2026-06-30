"""Chunking: event-boundary alignment to tool calls, fixed windows, assignment,
and the fallback when there is nothing to align to."""

from __future__ import annotations

from pps_pipeline.bundle import RawEvent
from pps_pipeline.chunk import (Chunk, assign_chunk, chunk_event_boundary,
                                chunk_fixed, make_chunks)


def _evs(loaded_bundle):
    return loaded_bundle.raw_events()


def test_event_boundary_aligns_to_tool_calls(loaded_bundle):
    evs = _evs(loaded_bundle)
    chunks = chunk_event_boundary(evs, loaded_bundle.duration_s)
    tool_times = sorted({round(e.t, 6) for e in evs if e.kind == "tool_call"})
    # 5 distinct tool calls in the fixture -> 6 chunks (leading [0, first)).
    assert len(tool_times) == 5
    assert len(chunks) == 6
    # Every chunk start (except chunk 0) is exactly a tool-call event time.
    starts = [round(c.t_start, 6) for c in chunks]
    assert starts[0] == 0.0
    assert starts[1:] == tool_times


def test_chunks_are_contiguous_and_cover_duration(loaded_bundle):
    evs = _evs(loaded_bundle)
    chunks = chunk_event_boundary(evs, loaded_bundle.duration_s)
    for a, b in zip(chunks, chunks[1:]):
        assert a.t_end == b.t_start          # contiguous, no gaps/overlaps
    assert chunks[-1].t_end >= loaded_bundle.duration_s


def test_fixed_windows():
    chunks = chunk_fixed(100.0, window_s=30.0)
    assert [(c.t_start, c.t_end) for c in chunks] == [
        (0.0, 30.0), (30.0, 60.0), (60.0, 90.0), (90.0, 100.0)]


def test_assign_chunk():
    chunks = [Chunk(0, 0.0, 12.0), Chunk(1, 12.0, 35.0), Chunk(2, 35.0, 200.0)]
    assert assign_chunk(chunks, 0.0) == 0
    assert assign_chunk(chunks, 11.9) == 0
    assert assign_chunk(chunks, 12.0) == 1
    assert assign_chunk(chunks, 34.9) == 1
    assert assign_chunk(chunks, 35.0) == 2
    assert assign_chunk(chunks, 199.0) == 2


def test_event_boundary_falls_back_to_fixed_without_tool_calls():
    evs = [RawEvent(5.0, "speech", "hi", "s0"),
           RawEvent(40.0, "caption", "screen", "c0")]
    chunks = make_chunks(evs, 60.0, mode="event_boundary", window_s=30.0)
    # No tool_call events -> fixed windows.
    assert [(c.t_start, c.t_end) for c in chunks] == [(0.0, 30.0), (30.0, 60.0)]
