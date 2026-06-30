"""Tests for the keystone seam module, fb_os.questions."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from fb_os import questions as Q
from fb_os.questions import OpenQuestion, OpenQuestionSet, QuestionMatch


def _q(id="oq_2026w26_01", cluster_id="clu_x", keywords=("alpha", "beta"),
       priority=0.8, status="open", surfaces=("cli",), expires_days=30):
    now = datetime.now(timezone.utc)
    return OpenQuestion(
        id=id, question="Is alpha the thing?", hypothesis="alpha matters",
        cluster_id=cluster_id, cluster_label="alpha/beta",
        match=QuestionMatch(keywords=list(keywords), surfaces=list(surfaces)),
        priority=priority, uncertainty=0.5, evidence_count=3, status=status,
        created_at=Q.now_iso(now), expires_at=Q.now_iso(now + timedelta(days=expires_days)),
        provenance={"artifact_ids": ["a_1", "a_2"]},
    )


def test_model_roundtrip():
    q = _q()
    assert OpenQuestion.from_dict(q.to_dict()).to_dict() == q.to_dict()
    s = OpenQuestionSet(questions=[q], generator="t")
    assert OpenQuestionSet.from_dict(s.to_dict()).to_dict() == s.to_dict()


def test_publish_load_roundtrip_and_atomic(tmp_path):
    p = tmp_path / "open-questions.json"
    s = OpenQuestionSet(questions=[_q()])
    Q.publish(s, p)
    assert p.exists()
    loaded = Q.load(p)
    assert len(loaded) == 1 and loaded.questions[0].id == "oq_2026w26_01"


def test_load_missing_is_empty(tmp_path):
    assert len(Q.load(tmp_path / "nope.json")) == 0


def test_id_minting_is_stable_and_sequenced():
    when = datetime(2026, 6, 24, tzinfo=timezone.utc)  # ISO week 26
    first = Q.next_question_id([], when=when)
    assert first == "oq_2026w26_01"
    second = Q.next_question_id([first], when=when)
    assert second == "oq_2026w26_02"


def test_merge_keeps_id_stable_for_same_cluster():
    prior = OpenQuestionSet(questions=[_q(id="oq_2026w26_01", cluster_id="clu_x")])
    incoming = [_q(id="", cluster_id="clu_x", priority=0.9)]  # same cluster, fresh gen
    merged = Q.merge(prior, incoming)
    assert len(merged) == 1
    assert merged.questions[0].id == "oq_2026w26_01"      # id preserved
    assert merged.questions[0].priority == 0.9            # content updated


def test_merge_keyword_fallback_when_cluster_renamed():
    prior = OpenQuestionSet(questions=[
        _q(id="oq_2026w26_01", cluster_id="clu_old", keywords=("voice", "latency", "confirm", "transcription"))])
    # Same theme, re-clustering renamed the cluster_id but keywords overlap strongly.
    incoming = [_q(id="", cluster_id="clu_renamed",
                   keywords=("voice", "latency", "confirm", "slow"))]
    merged = Q.merge(prior, incoming)
    assert len(merged) == 1 and merged.questions[0].id == "oq_2026w26_01"


def test_merge_new_cluster_gets_new_id():
    prior = OpenQuestionSet(questions=[_q(id="oq_2026w26_01", cluster_id="clu_x", keywords=("a",))])
    incoming = [_q(id="", cluster_id="clu_y", keywords=("totally", "different", "words", "here"))]
    merged = Q.merge(prior, incoming, now=datetime(2026, 6, 24, tzinfo=timezone.utc))
    assert len(merged) == 2
    ids = {q.id for q in merged}
    assert "oq_2026w26_01" in ids and any(i != "oq_2026w26_01" for i in ids)


def test_expire_retires_past_questions():
    s = OpenQuestionSet(questions=[_q(expires_days=-1)])  # already expired
    Q.expire(s)
    assert s.questions[0].status == "retired"


def test_mark_answered_lowers_uncertainty():
    s = OpenQuestionSet(questions=[_q()])
    assert Q.mark_answered(s, "oq_2026w26_01", uncertainty_drop=0.4)
    q = s.questions[0]
    assert q.status == "answered" and q.uncertainty == pytest.approx(0.1)
    assert Q.mark_answered(s, "nonexistent") is False


def test_rank_for_returns_single_relevant():
    s = OpenQuestionSet(questions=[
        _q(id="oq_2026w26_01", keywords=("voice", "latency"), priority=0.9),
        _q(id="oq_2026w26_02", keywords=("redaction", "strip"), priority=0.8),
    ])
    got = Q.rank_for("the voice confirm latency is bad", s, surface="cli")
    assert got is not None and got.id == "oq_2026w26_01"


def test_rank_for_returns_none_when_irrelevant():
    s = OpenQuestionSet(questions=[_q(keywords=("voice", "latency"))])
    assert Q.rank_for("dark mode colors look wrong", s, surface="cli") is None


def test_rank_for_excludes_answered_and_expired_and_other_surface():
    s = OpenQuestionSet(questions=[
        _q(id="oq_2026w26_01", keywords=("voice",), status="answered"),
        _q(id="oq_2026w26_02", keywords=("voice",), expires_days=-1),
        _q(id="oq_2026w26_03", keywords=("voice",), surfaces=("ide",)),
    ])
    assert Q.rank_for("voice issue", s, surface="cli") is None
    # but it IS selectable on the ide surface (the open, unexpired, ide one)
    assert Q.rank_for("voice issue", s, surface="ide").id == "oq_2026w26_03"


def test_schema_validation_rejects_bad_priority():
    s = OpenQuestionSet(questions=[_q()])
    data = s.to_dict()
    data["questions"][0]["priority"] = 2.0
    with pytest.raises(ValueError):
        Q.validate_question_set(data)


def test_publish_validates_before_write(tmp_path):
    bad = OpenQuestionSet(questions=[_q()])
    bad.questions[0].status = "bogus"
    with pytest.raises(ValueError):
        Q.publish(bad, tmp_path / "x.json")
    assert not (tmp_path / "x.json").exists()  # nothing written on a contract violation
