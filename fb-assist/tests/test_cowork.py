"""Tests for fb_assist.cowork — the Claude Cowork / local-agent (audit.jsonl) edge.

ALL plants are SYNTHETIC (no real credentials/PII). The fixture
``cowork-audit.jsonl`` models the PINNED snake_case audit shape
(``{_audit_timestamp, message:{content,role}, parent_tool_use_id, session_id,
type, uuid}``, NO ``toolUseResult`` mirror) and carries, across narrative text +
tool_use inputs + tool_result blocks:
  * a pattern-valid FAKE Anthropic key + a FAKE AWS key + a FAKE db password,
  * regex-floor PII (email, SSN, internal IP),
  * a FAKE codename ("Project Nimbus") — semantic IP, killed by the deny list,
  * a filesystem path.

We assert the Cowork contract:
  * LOCATOR discovers local-agent-mode-sessions/**/audit.jsonl (+ window filter);
  * ADAPTER resolves the snake_case envelope so the existing extractors fire;
  * the STRUCTURAL MAP locates the tool output the stock extractors MISS;
  * FIX 7: strip_blocks removes Cowork tool output INCLUDING unrecognized/MCP
    tool_results that redact.strip_categories leaves behind (the proven gap);
  * redact_cowork makes EVERY sentinel byte-absent + the egress floor is clean;
  * begin_cowork_swap/finish_swap round-trips audit.jsonl byte-exact;
  * the H6 reference intake genericizes / fails-closed / honors consent.

Run:  USE_TF=0 pytest tests/test_cowork.py -q
"""

import copy
import json
import os
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import cowork as C  # noqa: E402
from fb_assist import package as P  # noqa: E402
from fb_assist import redact as R  # noqa: E402
from fb_assist import transcripts as T  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cowork-audit.jsonl"

CODENAME = "Project Nimbus"
SENTINELS = [
    "sk-ant-api03-FAKEcowork0000aaaa1111bbbb2222cccc3333dddd4444EE",  # secret
    "dana.cowork@northwind-labs.example",                            # email
    "321-54-9876",                                                   # SSN
    "AKIAFAKECOWORK1234XY",                                          # AWS key (tool output)
    "hunter2-cowork-FAKE-pw",                                        # db pw (tool input + output)
    "10.4.5.6",                                                      # IP (bash output)
    CODENAME,                                                        # semantic IP codename
    "/Users/dana/Claude/nimbus/secrets.env",                        # filesystem path
]


def _raws():
    return list(C.parse_audit(FIXTURE))


def _blob(raws):
    return P.serialize_records(raws).decode("utf-8", "replace")


# --------------------------------------------------------------------------- #
# Fixture sanity                                                               #
# --------------------------------------------------------------------------- #
def test_fixture_is_snakecase_audit_shape():
    raws = _raws()
    assert len(raws) >= 8
    r0 = raws[0]
    # snake_case envelope, NO toolUseResult mirror anywhere.
    assert {"_audit_timestamp", "session_id", "parent_tool_use_id", "type", "uuid", "message"} <= set(r0)
    assert isinstance(r0["message"], dict) and "role" in r0["message"] and "content" in r0["message"]
    assert all("toolUseResult" not in r for r in raws), "Cowork has no toolUseResult mirror"
    # every sentinel is actually present in the raw fixture (so removal is meaningful).
    blob = _blob(raws)
    for s in SENTINELS:
        assert s in blob, f"planted sentinel missing from fixture: {s}"


# --------------------------------------------------------------------------- #
# LOCATOR                                                                      #
# --------------------------------------------------------------------------- #
def _make_session_tree(cfg: Path, sid="local_abc123", *, memory=True, skills=True):
    base = cfg / "local-agent-mode-sessions"
    sess = base / "acct" / "org" / sid
    sess.mkdir(parents=True)
    (sess / "audit.jsonl").write_text(FIXTURE.read_text(), encoding="utf-8")
    if memory:
        (sess / "agent" / "memory").mkdir(parents=True)
    if skills:
        (base / "skills-plugin").mkdir(parents=True)
    return sess / "audit.jsonl"


def test_find_cowork_sessions_discovers_audit(tmp_path):
    cfg = tmp_path / "Claude"
    audit = _make_session_tree(cfg)
    rows = C.find_cowork_sessions(config_dirs=[cfg])
    assert len(rows) == 1
    row = rows[0]
    assert row["path"] == str(audit)
    assert row["session_id"] == "local_abc123"
    assert row["has_memory"] is True
    assert row["has_skills_plugin"] is True
    assert row["config_root"] == str(cfg)


def test_find_cowork_sessions_window_filter(tmp_path):
    cfg = tmp_path / "Claude"
    audit = _make_session_tree(cfg, memory=False, skills=False)
    old = 1_600_000_000  # 2020 — far outside any window
    os.utime(audit, (old, old))
    assert C.find_cowork_sessions(config_dirs=[cfg], window_hours=24) == []
    assert len(C.find_cowork_sessions(config_dirs=[cfg])) == 1  # no window => found


def test_find_cowork_sessions_default_dirs_are_mac_and_linux():
    dirs = [str(d) for d in C.COWORK_CONFIG_DIRS]
    assert any("Library/Application Support/Claude" in d for d in dirs)
    assert any(".config/Claude" in d for d in dirs)


# --------------------------------------------------------------------------- #
# ADAPTER                                                                      #
# --------------------------------------------------------------------------- #
def test_cowork_record_resolves_snakecase_envelope():
    raws = _raws()
    rec = C.cowork_record(raws[0])
    # The stock transcripts.Record accessors read camelCase and MISS the raw shape:
    assert raws[0].get("sessionId") is None and T.Record(0, raws[0], "user").session_id is None
    # …the adapter makes them resolve.
    assert rec.session_id == "local_ditto_0a1b2c3d4e5f6071"
    assert rec.uuid == "u0"
    assert rec.timestamp == "2026-06-30T18:00:00.000Z"
    assert rec.type == "user"


def test_cowork_record_normalizes_type_from_role():
    raw = {"session_id": "s", "uuid": "x", "type": "message",
           "message": {"role": "assistant", "content": "hi"}}
    rec = C.cowork_record(raw)
    assert rec.type == "assistant"  # type normalized from message.role


def test_extractors_fire_via_adapter():
    raws = _raws()
    rmap = C.cowork_redaction_map(raws)
    s = rmap["summary"]
    assert s["human_prompts"]["count"] >= 2
    assert s["assistant_text"]["count"] >= 3
    assert s["tool_calls"]["count"] >= 3
    assert s["tool_results"]["count"] >= 3


# --------------------------------------------------------------------------- #
# STRUCTURAL MAP — the blindness fix                                           #
# --------------------------------------------------------------------------- #
def test_structured_extractors_are_blind_but_structural_map_sees():
    raws = _raws()
    recs = [C.cowork_record(r) for r in raws]
    # The stock structured extractors read the absent toolUseResult -> ZERO.
    assert list(T.extract(recs, "file_contents")) == []
    assert list(T.extract(recs, "bash_output")) == []
    assert list(T.extract(recs, "websearch")) == []
    # The Cowork structural map locates them in the tool_result blocks instead.
    located = C.cowork_structural_map(raws)["located"]
    assert located.get("file_contents", 0) >= 1
    assert located.get("bash_output", 0) >= 1
    assert located.get("websearch", 0) >= 1


# --------------------------------------------------------------------------- #
# FIX 7 — strip_blocks                                                         #
# --------------------------------------------------------------------------- #
def test_strip_blocks_removes_bulk_tool_output():
    raws = _raws()
    out = C.strip_blocks(raws, C.COWORK_DEFAULT_STRIP)
    blob = _blob(out)
    # tool-output / path / thinking sentinels removed structurally…
    for s in ("AKIAFAKECOWORK1234XY", "hunter2-cowork-FAKE-pw", "10.4.5.6",
              "/Users/dana/Claude/nimbus/secrets.env"):
        assert s not in blob, f"structural strip left {s}"
    # …narrative-only sentinels survive the STRUCTURAL strip (the mask handles them).
    assert "dana.cowork@northwind-labs.example" in blob


def test_strip_blocks_closes_unrecognized_tool_gap():
    """The proven fix-7 gap: redact.strip_categories(['file_contents']) leaves an
    MCP/unrecognized tool_result's output; cowork.strip_blocks removes it."""
    call = {"type": "assistant", "uuid": "a", "message": {"role": "assistant", "content": [
        {"type": "tool_use", "id": "toolu_mcp", "name": "mcp__github__get_file_contents",
         "input": {"path": "x"}}]}}
    res = {"type": "user", "uuid": "u", "parent_tool_use_id": "toolu_mcp",
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "toolu_mcp",
                "content": [{"type": "text", "text": "leak AKIAFAKECOWORK1234XY here"}]}]}}
    # Baseline: the CC strip leaves it.
    cc = _blob(R.strip_categories([call, res], ["file_contents"]))
    assert "AKIAFAKECOWORK1234XY" in cc
    # Cowork strip_blocks removes it.
    cw = _blob(C.strip_blocks([call, res], ["file_contents"]))
    assert "AKIAFAKECOWORK1234XY" not in cw


def test_strip_blocks_strips_image_source_in_tool_result():
    """Computer-use screenshots ride as image sub-blocks in tool_result content."""
    rec = {"type": "user", "uuid": "u", "parent_tool_use_id": "t",
           "message": {"role": "user", "content": [
               {"type": "tool_result", "tool_use_id": "t",
                "content": [{"type": "image",
                             "source": {"type": "base64", "media_type": "image/png",
                                        "data": "SEKRETBASE64DATA"}}]}]}}
    out = C.strip_blocks([rec], ["tool_calls"])
    blob = _blob(out)
    assert "SEKRETBASE64DATA" not in blob
    assert out[0]["message"]["content"][0]["content"][0]["source"]["type"] == "stripped"


def test_strip_blocks_keeps_tool_name_signal():
    raws = _raws()
    out = C.strip_blocks(raws, ["tool_calls"])
    names = {b.get("name") for r in out for b in (r["message"]["content"] if isinstance(r["message"]["content"], list) else [])
             if isinstance(b, dict) and b.get("type") == "tool_use"}
    assert {"Read", "Bash", "WebSearch"} <= names  # names preserved (signal), inputs gone
    # inputs scrubbed
    for r in out:
        c = r["message"]["content"]
        if isinstance(c, list):
            for b in c:
                if isinstance(b, dict) and b.get("type") == "tool_use":
                    assert b["input"] == {"__stripped__": b["input"].get("__stripped__")}
                    assert "__stripped__" in b["input"]


def test_strip_blocks_rejects_unknown_category():
    import pytest
    with pytest.raises(ValueError):
        C.strip_blocks(_raws(), ["not_a_category"])


def test_strip_blocks_does_not_mutate_input():
    raws = _raws()
    before = copy.deepcopy(raws)
    C.strip_blocks(raws, C.COWORK_DEFAULT_STRIP)
    assert raws == before


# --------------------------------------------------------------------------- #
# redact_cowork — end-to-end byte absence + clean floor                        #
# --------------------------------------------------------------------------- #
def test_redact_cowork_removes_every_sentinel():
    raws = _raws()
    red = C.redact_cowork(raws, deny=[CODENAME])
    san = red["sanitized_raws"]
    blob = _blob(san)
    rendered = C._render_cowork_markdown(san)
    upload = rendered + "\n" + blob
    for s in SENTINELS:
        assert s not in blob, f"{s} survived in serialized bytes"
        assert s not in upload, f"{s} survived in outbound surface"
    assert len(red["redaction_map"]) > 0
    # the kept narrative remains coherent (markers present, not empty).
    assert "Cowork" in rendered or "freeze" in rendered.lower() or "‹" in rendered


def test_redact_cowork_floor_is_clean():
    raws = _raws()
    red = C.redact_cowork(raws, deny=[CODENAME])
    san = red["sanitized_raws"]
    rendered = C._render_cowork_markdown(san)
    upload = rendered + "\n" + _blob(san)
    gate = C.egress_gate(upload, rendered)
    assert gate["floor_clean"] is True, gate["floor"]


def test_redact_cowork_output_stays_audit_shape():
    """Sanitized records keep the snake_case envelope (so they are valid audit.jsonl
    for a swap) — only `type` is normalized, no camelCase aliases injected."""
    san = C.redact_cowork(_raws(), deny=[CODENAME])["sanitized_raws"]
    for r in san:
        assert "session_id" in r and "sessionId" not in r
        assert "_audit_timestamp" in r


# --------------------------------------------------------------------------- #
# SWAP — round-trip on audit.jsonl + open-question doc                         #
# --------------------------------------------------------------------------- #
def test_begin_cowork_swap_round_trips_byte_exact(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(FIXTURE.read_text(), encoding="utf-8")
    original = audit.read_bytes()

    san = C.redact_cowork(_raws(), deny=[CODENAME])["sanitized_raws"]
    handle = C.begin_cowork_swap(audit, san, live_session_id="some_other_session",
                                 backup_root=tmp_path / "bak")
    # sanitized bytes now on disk; no sentinel present.
    swapped = audit.read_bytes()
    assert swapped != original
    for s in SENTINELS:
        assert s.encode() not in swapped
    # restore -> byte-exact original back.
    P.finish_swap(handle.journal_path)
    assert audit.read_bytes() == original


def test_begin_cowork_swap_refuses_live_session(tmp_path):
    import pytest
    audit = tmp_path / "local_live.jsonl"
    audit.write_text(FIXTURE.read_text(), encoding="utf-8")
    san = C.redact_cowork(_raws())["sanitized_raws"]
    with pytest.raises(P.LiveTranscriptError):
        C.begin_cowork_swap(audit, san, live_session_id="local_live",
                            backup_root=tmp_path / "bak")


def test_swap_open_question_is_documented():
    assert isinstance(C.SWAP_OPEN_QUESTION, str)
    q = C.SWAP_OPEN_QUESTION.lower()
    assert "unproven" in q and "audit.jsonl" in q and "feedback" in q


def test_assemble_cowork_payload_under_budget(tmp_path):
    audit = tmp_path / "audit.jsonl"
    san = C.redact_cowork(_raws())["sanitized_raws"]
    payload = C.assemble_cowork_payload("Cowork froze on submit.", audit, san,
                                        effort_signal={"surface": "cowork", "redaction": "genericize"})
    assert payload.total_bytes <= P.FEEDBACK_BUDGET_BYTES
    assert str(audit) in payload.targets
    assert not payload.dropped


# --------------------------------------------------------------------------- #
# H6 — reference Cowork -> Anthropic intake (REFERENCE, not deployed)          #
# --------------------------------------------------------------------------- #
def _intake(consent=C.ATTACH_GENERICIZED, terms=(CODENAME,), raws=None):
    store = C.InMemoryCoworkSessionStore({"local_demo": list(raws if raws is not None else _raws())})
    policy = C.StaticCoworkConsentPolicy(C.CoworkConsentDecision(
        attach=consent, genericize_terms=list(terms)))
    sink = C.InMemoryCoworkFeedbackSink()
    ev = C.CoworkFeedbackEvent(session_id="local_demo", turn_uuid="u0",
                               type="thumbs_down", reason="Froze on submit.", user_id="user-x")
    res = C.handle_cowork_feedback(ev, store=store, consent=policy, sink=sink)
    return res, sink


def test_intake_genericized_is_clean():
    res, sink = _intake()
    assert res.status == C.STATUS_GENERICIZED
    art = res.artifact
    blob = json.dumps(art.to_dict(), ensure_ascii=False)
    for s in SENTINELS:
        assert s not in blob, f"{s} leaked into the attached artifact"
    assert res.audit.floor_clean is True
    assert res.audit.redaction_count > 0
    assert sink.last["feedback_id"] == res.feedback_id
    assert art.anchor == {"session_id": "local_demo", "turn_uuid": "u0"}


def test_intake_none_attaches_no_conversation():
    res, _ = _intake(consent=C.ATTACH_NONE)
    assert res.status == C.STATUS_NONE
    assert res.artifact.rendered is None
    assert res.artifact.sanitized_records is None
    assert res.artifact.reason == "Froze on submit."


def test_intake_raw_is_flagged():
    res, _ = _intake(consent=C.ATTACH_RAW)
    assert res.status == C.STATUS_RAW
    assert "raw_optin" in res.artifact.flags
    assert res.audit.floor_clean is False


def test_intake_session_unavailable_fails_closed():
    store = C.InMemoryCoworkSessionStore({})  # empty
    policy = C.StaticCoworkConsentPolicy(C.CoworkConsentDecision(attach=C.ATTACH_GENERICIZED))
    sink = C.InMemoryCoworkFeedbackSink()
    ev = C.CoworkFeedbackEvent(session_id="missing", type="td", reason="x")
    res = C.handle_cowork_feedback(ev, store=store, consent=policy, sink=sink)
    assert res.status == C.STATUS_FAILED_CLOSED
    assert "session_unavailable" in res.artifact.flags
    assert res.artifact.attach == C.ATTACH_NONE


def test_intake_fails_closed_on_residual(monkeypatch):
    """If the redactor ever returned leaky bytes, the HARD gate fails closed —
    attach none + flag, never the leaky artifact."""
    leaky = [{"session_id": "s", "_audit_timestamp": "t", "type": "user", "uuid": "u",
              "parent_tool_use_id": None,
              "message": {"role": "user",
                          "content": "leak sk-ant-api03-FAKEcowork0000aaaa1111bbbb2222cccc3333dddd4444EE"}}]
    monkeypatch.setattr(C, "redact_cowork",
                        lambda raws, **kw: {"sanitized_raws": leaky, "redaction_map": []})
    res, _ = _intake()
    assert res.status == C.STATUS_FAILED_CLOSED
    assert "residual_floor_leak" in res.artifact.flags
    assert res.artifact.attach == C.ATTACH_NONE
    assert res.audit.floor_clean is False


def test_consent_decision_rejects_bad_attach():
    import pytest
    with pytest.raises(ValueError):
        C.CoworkConsentDecision(attach="bogus")
