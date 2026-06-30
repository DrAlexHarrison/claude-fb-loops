"""Tests for fb_os.triager — the keystone producer.

Free + deterministic: the LLM backend is the record/replay mock (or a tiny inline
stub), never a live Claude call.
"""

from datetime import datetime, timezone
from pathlib import Path

import fb_os
from fb_os import fixtures, questions as Q
from fb_os.cli import do_cluster, do_ingest
from fb_os.store import Store
from fb_os.triager import (CATEGORIES, ROUTES, MockTriagerBackend, Triager,
                           TriagerBackend, cluster_priority, cluster_signature,
                           effort_weight)

REPLAY = Path(fb_os.__file__).resolve().parent.parent / "fixtures" / "triager-replay.json"


def _populated_store(tmp_path):
    inbox = tmp_path / "inbox"
    fixtures.generate_inbox(inbox)
    store = Store(":memory:")
    do_ingest(store, str(inbox))
    do_cluster(store)
    return store


def test_golden_replay_run(tmp_path):
    store = _populated_store(tmp_path)
    triager = Triager(store, MockTriagerBackend(REPLAY))
    result = triager.run(Q.OpenQuestionSet())

    # fixed-label-set honored across every triage record (never-invent)
    for rec in result["triage"]:
        assert rec["category"] in CATEGORIES
        assert rec["route"] in ROUTES
        assert 0.0 <= rec["priority"] <= 1.0

    # the attach theme yields a question with stable id + correct cluster_id/provenance
    qs = result["questions"]
    attach = next((q for q in qs if q.cluster_id == "clu_session_feedback_one"), None)
    assert attach is not None
    assert attach.id.startswith("oq_") and "w" in attach.id
    members = [m["artifact_id"] for m in store.cluster_members("clu_session_feedback_one")]
    assert attach.provenance["artifact_ids"] == sorted(members)
    # replayed content (not the heuristic) — the hand-authored question text
    assert "attach a single past session" in attach.question
    # published set is schema-valid
    Q.validate_question_set(qs.to_dict())


def test_suppressed_clusters_never_triaged(tmp_path):
    store = _populated_store(tmp_path)
    Triager(store, MockTriagerBackend(REPLAY)).run(Q.OpenQuestionSet())
    # the suppressed singleton (kerning) is never given a triage record or a question
    triaged_ids = {r["artifact_id"] for r in store.triage_records()}
    assert "a_singleton_kerning" not in triaged_ids


class _BogusLabelBackend(TriagerBackend):
    """Returns labels OUTSIDE the fixed sets — the triager must coerce them."""

    def triage_cluster(self, cluster, members, open_questions):
        return {
            "theme": {"summary": "x"},
            "artifact_triage": {"category": "frobnicate", "route": "banana", "priority": 5},
            "per_artifact": {},
            "question": {"question": "q?", "hypothesis": "h", "keywords": ["k"],
                         "surfaces": ["cli"], "uncertainty": 0.5},
        }


def test_never_invent_labels_coerced(tmp_path):
    store = _populated_store(tmp_path)
    Triager(store, _BogusLabelBackend()).run(Q.OpenQuestionSet())
    for rec in store.triage_records():
        assert rec["category"] == "other"   # frobnicate -> other
        assert rec["route"] == "none"       # banana -> none
        assert rec["priority"] == 1.0       # 5 -> clamped to 1.0


def test_answered_flip_closes_loop(tmp_path):
    store = _populated_store(tmp_path)
    triager = Triager(store, MockTriagerBackend(REPLAY))
    first = triager.run(Q.OpenQuestionSet())
    target = max(first["questions"].open_questions(), key=lambda q: q.priority)

    # a user answers the top question
    inbox2 = tmp_path / "inbox"
    fixtures.write_answer_bundle(inbox2, target.id)
    do_ingest(store, str(inbox2))
    do_cluster(store)
    second = triager.run(first["questions"])

    answered = [q for q in second["questions"] if q.status == "answered"]
    assert any(q.id == target.id for q in answered)
    # no duplicate of the answered theme was spawned
    open_ids = [q.id for q in second["questions"].open_questions()]
    assert len(open_ids) == len(set(open_ids))


def test_effort_weight_orders_equal_size_clusters():
    high = [{"quality": 5, "alignment_confidence": 5, "reputation_token": "r"}] * 3
    low = [{"quality": 1, "alignment_confidence": 1, "reputation_token": None}] * 3
    assert effort_weight(high) > effort_weight(low)
    # equal evidence count, different quality -> different priority
    assert cluster_priority(3, high) > cluster_priority(3, low)


def test_cluster_signature_is_stable():
    c = {"keywords": ["voice", "latency", "confirm", "transcription", "extra"]}
    assert cluster_signature(c) == "confirm|latency|transcription|voice"
