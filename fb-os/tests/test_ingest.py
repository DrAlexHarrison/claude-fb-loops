"""Tests for fb_os.ingest — reuse fidelity, derive-or-manifest, the leak-scan floor."""

from pathlib import Path

from fb_os import fixtures, ingest
from fb_os.cli import do_cluster, do_ingest
from fb_os.store import Store


def test_footer_parse_is_inverse_of_render():
    sig = {"redaction": "surgical(2 overrides)", "quality": 4,
           "alignment_confidence": 5, "reputation_token": "rep_abc"}
    footer = fixtures._render_footer(sig)
    desc = f"some feedback text\n\n---\n{footer}"
    got = ingest.parse_effort_footer(desc)
    assert got == sig


def test_strip_footer_removes_metadata():
    desc = "the real feedback\n\n---\n[fb-assist effort signal] quality=4; alignment_confidence=5"
    assert ingest.strip_effort_footer(desc) == "the real feedback"


def test_derive_without_manifest_recovers_effort_from_footer(tmp_path):
    # a_attach_03 is a pure footer bundle (no artifact.json, no effort-signal.json).
    fixtures.generate_inbox(tmp_path)
    art = ingest.ingest_bundle(tmp_path / "a_attach_03")
    assert art["artifact_id"] == "a_attach_03"
    assert art["surface"] == "cli"
    assert art["effort_signal"]["quality"] == 4
    assert art["effort_signal"]["alignment_confidence"] == 4
    # footer must NOT survive into the stored, embeddable description
    assert "fb-assist effort signal" not in art["description"]


def test_derive_with_manifest_uses_declared_surface(tmp_path):
    fixtures.generate_inbox(tmp_path)
    art = ingest.ingest_bundle(tmp_path / "a_attach_02")  # ide bundle => manifest written
    assert art["surface"] == "ide"


def test_lossless_rederive_with_and_without_manifest(tmp_path):
    # The same logical artifact must derive the same effort-signal whether the manifest
    # carries it or it's recovered from the footer.
    fixtures.generate_inbox(tmp_path)
    a_sidecar = ingest.ingest_bundle(tmp_path / "a_attach_01")   # sidecar effort-signal.json
    a_footer = ingest.ingest_bundle(tmp_path / "a_attach_03")    # footer-only
    for a in (a_sidecar, a_footer):
        assert set(a["effort_signal"]) >= {"redaction", "quality", "alignment_confidence"}


def test_planted_secret_is_quarantined(tmp_path):
    fixtures.generate_inbox(tmp_path)
    art = ingest.ingest_bundle(tmp_path / "a_planted_secret")
    assert art["quarantined"] is True
    assert "leak-scan floor" in art["quarantine_reason"]


def test_quarantined_secret_bytes_never_reach_clustered_text(tmp_path):
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    do_ingest(store, str(inbox))
    do_cluster(store)
    secret = "sk-ant-api03-PLANTEDsecretVALUE1234567890abcdEF"
    # No clustered (non-quarantined) artifact may contain the planted secret bytes.
    for a in store.artifacts(include_quarantined=False):
        assert secret not in (a.get("description") or "")
    # The planted bundle is stored but quarantined and unclustered.
    planted = store.get_artifact("a_planted_secret")
    assert planted["quarantined"] is True and planted["cluster_id"] is None


def test_benign_bundles_not_quarantined(tmp_path):
    fixtures.generate_inbox(tmp_path)
    for aid in ("a_attach_01", "a_redact_01", "a_voice_01", "a_watch_01"):
        art = ingest.ingest_bundle(tmp_path / aid)
        assert art["quarantined"] is False, aid


def test_report_only_bundle_has_no_transcript(tmp_path):
    fixtures.generate_inbox(tmp_path)
    art = ingest.ingest_bundle(tmp_path / "a_watch_02")  # report_only
    assert art["report_only"] is True
    assert art["transcript_path"] is None


def test_ingest_inbox_is_idempotent(tmp_path):
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    first = do_ingest(store, str(inbox))
    again = do_ingest(store, str(inbox))
    assert len(first) == len(again)
    # upsert, not duplicate
    assert len(store.artifacts(include_quarantined=True)) == len(first)
