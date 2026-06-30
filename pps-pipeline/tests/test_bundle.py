"""Bundle contract: schema validity, present-stream existence, and the
single-t0 offset normalization (clock-drift survival)."""

from __future__ import annotations

import json
import os

import pytest

from pps_pipeline import bundle as B
from pps_pipeline import fixture as F


def test_manifest_validates_against_schema(loaded_bundle):
    assert B.validate_manifest(loaded_bundle.manifest) == []


def test_core_fields(loaded_bundle):
    assert loaded_bundle.session_id == F.SESSION_ID
    assert loaded_bundle.t0_epoch == F.T0_EPOCH
    assert loaded_bundle.duration_s == F.DURATION_S


def test_present_streams(loaded_bundle):
    present = set(loaded_bundle.present_streams())
    assert present == {"transcript", "captions", "network", "ccode_session"}
    # video + events are present:false in the demo.
    assert loaded_bundle.stream("video") is None
    assert loaded_bundle.stream("events") is None


def test_raw_events_kinds_and_count(loaded_bundle):
    evs = loaded_bundle.raw_events()
    assert len(evs) == 21
    assert {e.kind for e in evs} == {"prompt", "caption", "speech",
                                     "tool_call", "tool_result", "net"}


def test_offset_normalization_across_time_bases(loaded_bundle):
    """transcript/captions carry offsets; HAR + the CC .jsonl carry ABSOLUTE
    timestamps. All must normalize to the same offset axis from t0."""
    evs = loaded_bundle.raw_events()
    by = {}
    for e in evs:
        by.setdefault(e.kind, []).append(e)
    # CC prompt was stamped via absolute ISO at t0+10 -> offset 10.0.
    assert any(abs(e.t - 10.0) < 1e-6 and "Refactor" in e.text
               for e in by["prompt"])
    # HAR net entry was absolute ISO at t0+22 -> offset 22.0.
    assert any(abs(e.t - 22.0) < 1e-6 for e in by["net"])
    # Caption stream is offset-native at t=13.
    assert any(abs(e.t - 13.0) < 1e-6 for e in by["caption"])
    # Every event lands within the session window.
    assert all(0.0 <= e.t <= loaded_bundle.duration_s for e in evs)


def test_missing_present_stream_raises(tmp_path):
    d = tmp_path / "broken"
    F.generate(str(d))
    os.remove(d / "transcript.jsonl")  # present:true but file gone
    with pytest.raises(B.BundleError):
        B.load_bundle(str(d))


def test_schema_violation_raises(tmp_path):
    d = tmp_path / "badmanifest"
    F.generate(str(d))
    m = json.loads((d / "manifest.json").read_text())
    del m["t0_epoch"]  # required
    (d / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(B.BundleError):
        B.load_bundle(str(d))


def test_clock_drift_event_dropped(tmp_path):
    """An event whose normalized t falls far outside [0, duration] is dropped
    (the drift guard), and reported by drift_report."""
    d = tmp_path / "drift"
    F.generate(str(d))
    with open(d / "transcript.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"start": 99999.0, "end": 99999.0,
                             "text": "way out of band"}) + "\n")
    b = B.load_bundle(str(d))
    assert all(e.text != "way out of band" for e in b.raw_events())
    assert b.drift_report()["drifted"] >= 1
