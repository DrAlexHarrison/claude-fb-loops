"""Tests for fb_os.metrics — signal-quality weighting + dashboard render."""

from pathlib import Path

import fb_os
from fb_os import fixtures, metrics, questions as Q
from fb_os.cli import do_cluster, do_ingest, do_triage
from fb_os.store import Store

REPLAY = Path(fb_os.__file__).resolve().parent.parent / "fixtures" / "triager-replay.json"


def _full_pipeline(tmp_path):
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    do_ingest(store, str(inbox))
    do_cluster(store)
    do_triage(store, str(tmp_path / "oq.json"), mock=str(REPLAY))
    return store


def test_effort_weighting_raises_high_quality_priority():
    high = [{"quality": 5, "alignment_confidence": 5, "reputation_token": "r"}] * 3
    low = [{"quality": 2, "alignment_confidence": 2, "reputation_token": None}] * 3
    # Equal cluster size, different signal quality -> higher priority for high quality.
    assert metrics.cluster_priority(3, high) > metrics.cluster_priority(3, low)


def test_compute_metrics_shape(tmp_path):
    store = _full_pipeline(tmp_path)
    m = metrics.compute_metrics(store)
    assert m["artifacts"]["ingested"] >= 13
    assert m["artifacts"]["quarantined"] == 1            # the planted secret
    assert m["artifacts"]["triaged"] >= 13
    assert m["questions"]["total"] >= 1
    assert m["clusters"]["suppressed"] >= 1              # the kerning singleton
    # time-to-triage is computed (created_at -> triaged_at)
    assert m["time_to_triage_hours"]["count"] >= 1


def test_signal_quality_overall_reflects_effort():
    from fb_os.metrics import signal_quality
    hi = [{"effort_signal": {"quality": 5, "alignment_confidence": 5}}]
    lo = [{"effort_signal": {"quality": 1, "alignment_confidence": 1}}]
    assert signal_quality(hi) > signal_quality(lo)


def test_render_html_is_self_contained(tmp_path):
    store = _full_pipeline(tmp_path)
    out = metrics.write_html(store, tmp_path / "dash.html")
    html = Path(out).read_text()
    assert html.startswith("<!doctype html>")
    assert "Feedback OS" in html
    assert "http://" not in html.replace("http://localhost", "")  # no external network assets
    assert "suppressed" in html  # the privacy-floor note renders
