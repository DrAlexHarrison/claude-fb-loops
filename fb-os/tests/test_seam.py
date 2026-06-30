"""The seam test — the point of the whole build.

Build 1 publishes open-questions.json; the shared selector (the same one Build 3
imports) consumes it and returns exactly one applicable probe for a CLI report and
None when nothing is relevant. Plus schema round-trip in BOTH directions.
"""

import json
from pathlib import Path

import fb_os
from fb_os import fixtures, questions as Q
from fb_os.cli import do_cluster, do_ingest, do_triage
from fb_os.store import Store

REPLAY = Path(fb_os.__file__).resolve().parent.parent / "fixtures" / "triager-replay.json"


def _run_loop_to_publish(tmp_path):
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    do_ingest(store, str(inbox))
    do_cluster(store)
    qpath = tmp_path / "open-questions.json"
    do_triage(store, str(qpath), mock=str(REPLAY))
    return qpath


def test_published_file_is_schema_valid(tmp_path):
    qpath = _run_loop_to_publish(tmp_path)
    data = json.loads(qpath.read_text())
    Q.validate_question_set(data)  # raises on a contract violation
    assert data["schema_version"] == "1.0"
    assert len(data["questions"]) >= 1


def test_seam_rank_for_selects_one_for_relevant_report(tmp_path):
    qpath = _run_loop_to_publish(tmp_path)
    qs = Q.load(qpath)
    report = {"text": "the redaction is stripping harmless output, too aggressive", "surface": "cli"}
    got = Q.rank_for(report, qs, surface="cli")
    assert got is not None
    assert "redaction" in got.match.keywords
    # it returns ONE, never a survey
    assert isinstance(got, Q.OpenQuestion)


def test_seam_rank_for_returns_none_for_irrelevant_report(tmp_path):
    qpath = _run_loop_to_publish(tmp_path)
    qs = Q.load(qpath)
    got = Q.rank_for("my git push is rejected with a non-fast-forward error", qs, surface="cli")
    assert got is None


def test_seam_loads_from_default_path_via_env(tmp_path, monkeypatch):
    # Build 3 reads $FB_ASSIST_OPEN_QUESTIONS (-> the canonical path). Publishing with
    # no explicit path must land where rank_for() with no path reads from.
    target = tmp_path / "cfg" / "open-questions.json"
    monkeypatch.setenv("FB_ASSIST_OPEN_QUESTIONS", str(target))
    s = Q.OpenQuestionSet(questions=[Q.OpenQuestion(
        id="oq_2026w26_01", question="voice latency?",
        match=Q.QuestionMatch(keywords=["voice", "latency"], surfaces=["cli"]),
        priority=0.9, status="open")])
    Q.publish(s)  # no path -> default_publish_path() honors the env override
    assert target.exists()
    got = Q.rank_for("voice latency is bad", surface="cli")  # no path -> same default
    assert got is not None and got.id == "oq_2026w26_01"


def test_inbound_artifact_manifest_validates(tmp_path):
    # The other direction of the seam: a synthetic bundle's artifact.json validates
    # against artifact.schema.json (a_attach_02 is written with a manifest).
    fixtures.generate_inbox(tmp_path)
    manifest = json.loads((tmp_path / "a_attach_02" / "artifact.json").read_text())
    Q.validate_artifact_manifest(manifest)  # raises on a contract violation
    assert manifest["surface"] == "ide"


def test_full_loop_closes_via_pipeline(tmp_path):
    # End-to-end: publish, answer the top question, re-run, it flips to answered.
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    do_ingest(store, str(inbox))
    do_cluster(store)
    qpath = tmp_path / "open-questions.json"
    r1 = do_triage(store, str(qpath), mock=str(REPLAY))
    top = max(r1["questions"].open_questions(), key=lambda q: q.priority)

    fixtures.write_answer_bundle(inbox, top.id)
    do_ingest(store, str(inbox))
    do_cluster(store)
    r2 = do_triage(store, str(qpath), mock=str(REPLAY))

    published = Q.load(qpath)
    answered = published.by_id(top.id)
    assert answered is not None and answered.status == "answered"
