"""Re-assert the 7 integration invariants through the reusable pipeline API.

``test_integration.py`` proves the validated call-sequence inline; this proves the
SAME guarantees come through ``fb_assist.pipeline`` (the functions the MCP server
wraps), so the runtime and the proof can't drift. Plants a pattern-valid fake
secret + real-shaped PII and asserts byte-absence from the outbound surface, the
two-layer gate, and a non-destructive begin/finish swap straddle.
"""

from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pytest

from fb_assist import pipeline as PL
from fb_assist import package as P

SID = "aaaabbbb-cccc-dddd-eeee-ffff00001111"
PLANTED = {
    "ant_key": "sk-ant-api03-PIPE1111TEST2222CCCC3333DDDD4444",
    "aws_file": "AKIAZZ44QQ55WW66EE77",
    "person": "Marlene Vasquez",
    "ssn": "123-45-6789",
    "github": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789AB",
}
SENTINELS = list(PLANTED.values())


def _planted_records() -> list[dict]:
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
                f"I'm {PLANTED['person']} (SSN {PLANTED['ssn']}). I pasted key "
                f"{PLANTED['ant_key']} by accident. The real bug: /feedback keeps FREEZING on submit.")}),
        env("a1", "u1", type="assistant",
            message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                {"type": "thinking", "thinking": "note key", "signature": "ZZ=="},
                {"type": "text", "text": "Got it — the submit freeze is the real issue."}]}),
        env("a2", "a1", type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/home/x/proj/c.py"}}]}),
        env("u2", "a2", type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": f'AWS_SECRET="{PLANTED["aws_file"]}"\n'}]},
            toolUseResult={"stdout": f'AWS_SECRET="{PLANTED["aws_file"]}"\n', "stderr": "", "interrupted": False, "isImage": False}),
        env("a3", "u2", type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Bash", "input": {"command": "cat .env"}}]}),
        env("u3", "a3", type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": f"GH={PLANTED['github']}\n"}]},
            toolUseResult={"stdout": f"GH={PLANTED['github']}\n", "stderr": "", "interrupted": False, "isImage": False}),
    ]


@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    d = tmp_path_factory.mktemp("pl")
    path = d / f"{SID}.jsonl"
    path.write_bytes(P.serialize_records(_planted_records()))
    art = PL.run_flow(str(path),
                      "The /feedback submit flow freezes every time.",
                      effort_signal={"redaction": "surgical", "quality": 4, "alignment_confidence": 5})
    return art, path, d


def test_parse_keeps_both_views(flow):
    art, path, _ = flow
    parsed = art["parsed"]
    assert len(parsed.records) == len(parsed.raws) > 0
    assert parsed.session_id == SID


def test_detect_locates_and_finds(flow):
    art, _, _ = flow
    det = art["detection"]
    assert "human_prompts" in det["summary"]["categories_located"]
    assert det["secret_count"] >= 1 and det["narrative_findings"]
    # masked-by-default: no raw secret echoed into the detection dict
    for d in det["narrative_findings"]:
        assert d["text"] not in SENTINELS


def test_planted_secrets_gone_from_upload(flow):
    art, _, _ = flow
    up = PL.upload_text(art["payload"])
    for s in SENTINELS:
        assert s not in up, f"LEAK: sentinel survived in upload: {s}"


def test_meaning_preserved(flow):
    art, _, _ = flow
    up = PL.upload_text(art["payload"])
    assert "FREEZING" in up and "/feedback" in up
    assert "‹ANTHROPIC_KEY›" in up


def test_two_layer_gate(flow):
    art, _, _ = flow
    gate = art["gate"]
    # HARD floor over actual upload bytes must be clean.
    assert gate["floor_clean"] is True
    assert gate["floor"]["secrets"] == [] and gate["floor"]["pii"] == []
    # NER candidates may exist but must contain NO real planted value.
    for c in gate["candidates"]:
        assert c["text"] not in SENTINELS


def test_preview_shows_redactions(flow):
    art, _, _ = flow
    pv = art["preview"]
    # NOTE: byte-shrink only holds for realistic sessions with large tool blobs
    # (the integration test asserts it); on this tiny fixture the replace-markers +
    # 3-byte ‹guillemets› can net slightly larger. The real invariants are below.
    assert pv.modified_records > 0
    assert pv.stripped_by_category  # the bridge surfaced per-category redactions
    assert any(c in pv.stripped_by_category for c in ("ANTHROPIC_KEY", "US_SSN", "PERSON"))
    text = pv.render()
    assert "INCLUDED" in text and "STRIPPED" in text


def test_begin_finish_swap_straddle_nondestructive(flow):
    """The runtime's submit_begin/submit_finish: stage the sanitized bytes for a
    later /feedback turn, then restore byte-exact — proven on the pipeline output."""
    art, path, d = flow
    original = path.read_bytes()
    payload = art["payload"]
    handle = P.begin_swap(payload.targets, backup_root=d / "bk")
    during = path.read_bytes()
    for s in SENTINELS:
        assert s not in during.decode("utf-8")
    assert during == payload.targets[str(path)]
    P.finish_swap(handle.journal_path)
    assert path.read_bytes() == original
