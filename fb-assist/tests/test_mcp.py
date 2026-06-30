"""Drive the fb-assist MCP tool surface in-process and assert the SAME invariants
as test_integration.py come through the server layer (no logic drift).

The tools are thin wrappers over ``pipeline``/``package``/``locate``/``profile``/
``genericize`` and are directly callable (registered, not rewritten). We pre-seed
per-session state with a tmp transcript path so the suite is hermetic — it never
reads the real ``~/.claude`` and never invokes ``/feedback``.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pytest

from fb_assist import mcp_server as M
from fb_assist import package as P

SID = "mcp00000-1111-2222-3333-444455556666"
PLANTED = {
    "ant_key": "sk-ant-api03-MCP11111TEST2222CCCC3333DDDD4444",
    "person": "Marlene Vasquez",
    "ssn": "123-45-6789",
    "github": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789AB",
}
SENTINELS = list(PLANTED.values())


def _records() -> list[dict]:
    def env(uuid, parent, **extra):
        b = {"uuid": uuid, "parentUuid": parent, "isSidechain": False,
             "sessionId": SID, "timestamp": "2026-06-29T18:00:00.000Z",
             "cwd": "/home/x/proj", "gitBranch": "main", "version": "2.1.195",
             "userType": "external"}
        b.update(extra)
        return b
    return [
        {"type": "ai-title", "aiTitle": "submit freezes", "sessionId": SID},
        env("u1", None, type="user", promptSource="typed",
            message={"role": "user", "content": (
                f"I'm {PLANTED['person']} (SSN {PLANTED['ssn']}). Pasted key "
                f"{PLANTED['ant_key']}. The real bug: /feedback keeps FREEZING on submit.")}),
        env("a1", "u1", type="assistant",
            message={"role": "assistant", "content": [
                {"type": "text", "text": "Got it — the submit freeze is the real issue."}]}),
        env("u2", "a1", type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": f"GH={PLANTED['github']}\n"}]},
            toolUseResult={"stdout": f"GH={PLANTED['github']}\n", "stderr": "", "interrupted": False, "isImage": False}),
    ]


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    M._STATE.clear()
    path = tmp_path / f"{SID}.jsonl"
    path.write_bytes(P.serialize_records(_records()))
    # Pre-seed state with the known path so tools don't scan the real ~/.claude.
    M._STATE[SID] = M._Session(session_id=SID, path=str(path), cwd=str(tmp_path))
    # locate.resolve is only used by locate_session + submit_begin's gather-gate;
    # pin it to a no-live-session view over our tmp file.
    monkeypatch.setattr(M.L, "resolve", lambda cwd=None, session_id=None: {
        "session_id": SID, "path": str(path), "project_dir": str(tmp_path),
        "config_dir": str(tmp_path), "account": "custom", "is_live": False,
        "candidates": [{"path": str(path), "session_id": SID}],
        "cwd": str(tmp_path), "slug": "x", "live_session_id": None,
    })
    return SID, path, tmp_path


def test_detect_through_server(seeded):
    sid, _, _ = seeded
    det = M.detect(sid)
    assert det["secret_count"] >= 1
    assert "human_prompts" in det["summary"]["categories_located"]
    for d in det["narrative_findings"]:
        assert d["text"] not in SENTINELS  # masked-by-default


def test_redact_and_assemble_strip_sentinels(seeded):
    sid, path, _ = seeded
    r = M.redact_recipe(sid, {"profile_apply": False})
    assert r["redactions"] >= 1 and r["by_category"]
    a = M.assemble(sid, "The /feedback submit flow freezes every time.",
                   effort_signal={"redaction": "surgical", "quality": 4, "alignment_confidence": 5})
    assert str(path) in a["targets"]
    up = P.serialize_records(M._STATE[sid].sanitized_raws).decode("utf-8")
    for s in SENTINELS:
        assert s not in up, f"LEAK through server: {s}"


def test_preview_and_gate(seeded):
    sid, _, _ = seeded
    M.redact_recipe(sid, {"profile_apply": False})
    M.assemble(sid, "freeze on submit")
    pv = M.preview(sid)
    assert pv["modified_records"] > 0 and "STRIPPED" in pv["render"]
    gate = M.leak_scan(sid)
    assert gate["floor_clean"] is True
    assert gate["floor"]["secrets"] == [] and gate["floor"]["pii"] == []
    for c in gate["candidates"]:
        assert c["text"] not in SENTINELS


def test_submit_begin_finish_nondestructive(seeded):
    sid, path, _ = seeded
    original = path.read_bytes()
    M.redact_recipe(sid, {"profile_apply": False})
    M.assemble(sid, "freeze on submit")
    sb = M.submit_begin(sid)
    assert sb["staged"] is True and Path(sb["journal_path"]).exists()
    during = path.read_bytes()
    for s in SENTINELS:
        assert s not in during.decode("utf-8")
    sf = M.submit_finish(sid)
    assert sf["restored"] is True
    assert path.read_bytes() == original


def test_submit_begin_refuses_dirty_live_session(seeded, monkeypatch, tmp_path):
    """FIX 1: if the live session would co-upload a secret, submit_begin REFUSES
    (default) and recommends checkpoint."""
    sid, path, _ = seeded
    # A separate live session file carrying a planted key.
    live = tmp_path / "live-session-9999.jsonl"
    live.write_text(f'{{"type":"user","message":{{"role":"user","content":"oops {PLANTED["ant_key"]}"}}}}\n')
    monkeypatch.setattr(M.L, "resolve", lambda cwd=None, session_id=None: {
        "path": str(live), "is_live": True, "live_session_id": "live-session-9999",
        "candidates": [{"path": str(path)}, {"path": str(live)}],
    })
    M.redact_recipe(sid, {"profile_apply": False})
    M.assemble(sid, "freeze")
    sb = M.submit_begin(sid)  # default allow_live_gate=False
    assert sb["staged"] is False
    assert "checkpoint" in sb["recommend"]
    assert sb["gather_floor_clean"] is False
    assert path.read_bytes()  # nothing swapped


def test_recover_orphans_runs(seeded):
    out = M.recover_orphans()
    assert "journals" in out and "healed" in out


def test_profile_resolve_shape(seeded, tmp_path):
    """The profile tool returns the resolved policy shape (precedence + learn are
    exhaustively covered in test_profile.py; here we just prove the wrapper works)."""
    sid, _, _ = seeded
    res = M.profile_resolve(cwd=str(tmp_path), session_id=sid)
    assert "entities" in res and "hard_floors" in res


def test_open_questions_selection(tmp_path, monkeypatch):
    """The question-loop reader: returns the single best-matching open question for a
    report, None when nothing's relevant, and respects status/expiry/surface (§14)."""
    import json
    qfile = tmp_path / "open-questions.json"
    qfile.write_text(json.dumps({
        "generator": "test",
        "questions": [
            {"id": "oq_match", "question": "Wishing /feedback could attach one past session?",
             "match": {"keywords": ["feedback", "attach", "session"], "surfaces": ["cli"]},
             "priority": 0.9, "uncertainty": 0.5, "status": "open",
             "expires_at": "2099-01-01T00:00:00Z"},
            {"id": "oq_other", "question": "Unrelated probe",
             "match": {"keywords": ["graphql", "billing"], "surfaces": ["cli"]},
             "priority": 0.9, "uncertainty": 0.5, "status": "open",
             "expires_at": "2099-01-01T00:00:00Z"},
            {"id": "oq_closed", "question": "Already answered",
             "match": {"keywords": ["feedback"], "surfaces": ["cli"]},
             "priority": 1.0, "status": "answered", "expires_at": "2099-01-01T00:00:00Z"},
        ],
    }))
    monkeypatch.setenv("FB_ASSIST_OPEN_QUESTIONS", str(qfile))

    # report mentions feedback/attach/session -> the matching question wins
    r = M.open_questions("the /feedback flow won't attach my past session", surface="cli")
    assert r["question"] is not None and r["question"]["id"] == "oq_match"
    assert r["open_count"] == 2  # the answered one is excluded

    # report about nothing relevant -> no probe
    assert M.open_questions("how do I rename a file", surface="cli")["question"] is None

    # missing file -> graceful None
    monkeypatch.setenv("FB_ASSIST_OPEN_QUESTIONS", str(tmp_path / "nope.json"))
    assert M.open_questions("feedback attach session")["question"] is None
