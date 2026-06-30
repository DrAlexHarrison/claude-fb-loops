"""Tests for fb_assist.claude_repro — the `claude-repro` API SDK surface.

Mirrors test_redact.py / test_package.py style: plant pattern-valid FAKE secrets +
real-shaped PII into a Messages API request/response, then assert byte-absence of
every sentinel from the sanitized bundle, the request-id anchor extraction
(incl. the Bedrock deterministic fallback), the preview built from the
redaction map, Message.text leniency on both shapes, and the draft embedding.

USE_TF=0 is forced before any import so transformers' TF path can't break under
Keras 3 (no tf-keras) — matches the rest of the suite.
"""
import os

os.environ["USE_TF"] = "0"
os.environ["USE_FLAX"] = "0"

import json
import urllib.parse
from collections import deque

import pytest

from fb_assist import claude_repro as CR


# --------------------------------------------------------------------------- #
# Planted sentinels — pattern-valid FAKE secrets + real-shaped PII
# --------------------------------------------------------------------------- #
SK_ANT = "sk-ant-api03-" + "Zz9" * 14 + "qQ"          # ANTHROPIC_KEY shape
GH_TOKEN = "ghp_" + "a1B2c3" * 6 + "ZZZZ"             # GITHUB_TOKEN shape (>=36)
AKIA = "AKIAIOSFODNN7EXAMPLE"                          # AWS_ACCESS_KEY shape
EMAIL = "jane.customer@example.com"
SSN = "078-05-1120"                                    # real-shaped SSN
SYS_PATH = "/home/devuser/proprietary/agent_config.yaml"  # filesystem path in system

ALL_SENTINELS = {
    "SK_ANT": SK_ANT, "GH_TOKEN": GH_TOKEN, "AKIA": AKIA,
    "EMAIL": EMAIL, "SSN": SSN, "SYS_PATH": SYS_PATH,
}


def make_request():
    """A realistic Messages API request with secrets/PII/path planted across a
    system prompt, a user message that pastes a key, a tool_use input, and a
    tool_result block burying a token."""
    return {
        "model": "claude-sonnet-4-5",
        "max_tokens": 1024,
        "system": f"You are Acme Corp's assistant. Load config from {SYS_PATH}. Codename FALCON.",
        "tools": [{
            "name": "run_sql",
            "description": "Run a SQL query against the production analytics warehouse.",
            "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}},
        }],
        "messages": [
            {"role": "user",
             "content": f"My API key is {SK_ANT} and you can reach me at {EMAIL}. Why is the JSON wrong?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me query the database."},
                {"type": "tool_use", "id": "toolu_1", "name": "run_sql",
                 "input": {"sql": f"SELECT * FROM secrets WHERE aws_key='{AKIA}'"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": [{"type": "text", "text": f"1 row: ssn={SSN}, gh_token={GH_TOKEN}"}]},
            ]},
            {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": "iVBORw0KGgo" + "A" * 300}},
            ]},
        ],
    }


def make_response():
    """A Message response that echoes sensitive input (model output can leak too)."""
    return {
        "id": "msg_01ABC", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 120, "output_tokens": 30},
        "content": [
            {"type": "text",
             "text": f"I see the key {SK_ANT} and SSN {SSN} in your data; that's why it failed."},
        ],
    }


# --------------------------------------------------------------------------- #
# Redaction recall + byte-absence (the core privacy guarantee)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def artifact():
    return CR.redact_pair(make_request(), make_response(), "req_011CtEST",
                          description="the model ignored my JSON schema and returned prose")


def test_every_sentinel_absent_from_sanitized_bundle(artifact):
    upload = artifact.upload_text()
    for name, value in ALL_SENTINELS.items():
        assert value not in upload, f"{name} leaked into the sanitized bundle"


def test_hard_gate_deterministic_secret_floor_is_empty(artifact):
    # The deterministic floor over the ACTUAL upload bytes must be empty.
    from fb_assist import redact as R
    assert artifact.hard_gate_pass is True
    assert R.scan_secrets(artifact.upload_text()) == []


def test_caller_objects_are_never_mutated():
    req, resp = make_request(), make_response()
    req_before = json.dumps(req, sort_keys=True)
    resp_before = json.dumps(resp, sort_keys=True)
    CR.redact_pair(req, resp, "req_x")
    assert json.dumps(req, sort_keys=True) == req_before, "request was mutated"
    assert json.dumps(resp, sort_keys=True) == resp_before, "response was mutated"


def test_response_secrets_also_redacted(artifact):
    resp_text = json.dumps(artifact.redacted_repro["response"], ensure_ascii=False)
    assert SK_ANT not in resp_text and SSN not in resp_text


def test_bug_description_meaning_survives(artifact):
    # The narrative is MASKED, not deleted — the question frame survives even though
    # NER over-redacts some tokens (GLiNER eats "JSON"->‹PERSON›; that's a safe
    # over-redaction the allowlist/genericize layer resolves, never a leak).
    req_text = json.dumps(artifact.redacted_repro["request"], ensure_ascii=False)
    assert "Why is the" in req_text and "wrong?" in req_text
    # the assistant's own narrative ("Let me query the database.") survives intact
    assert "Let me query the database." in req_text


# --------------------------------------------------------------------------- #
# strip_blocks — the NEW Messages-API structural stripper
# --------------------------------------------------------------------------- #
def test_strip_blocks_replaces_image_tool_result_and_leaves_narrative():
    pair = CR.to_pair(make_request(), make_response())
    events = CR.strip_blocks(pair, CR.DEFAULT_STRIP)
    cats = {e["api_category"] for e in events}
    assert CR.IMAGE_DATA in cats and CR.TOOL_RESULT in cats
    # image base64 blob is gone, replaced by a dimensional marker
    img = pair.request["messages"][3]["content"][0]["source"]
    assert "stripped" in img["data"] and "A" * 300 not in img["data"]
    # tool_result content replaced
    tr = pair.request["messages"][2]["content"][0]["content"]
    assert all("stripped" in sub.get("text", "") for sub in tr)
    # narrative left intact for the masker (not yet touched)
    assert pair.request["messages"][0]["content"].startswith("My API key")


def test_strip_blocks_tool_definitions_opt_in():
    pair = CR.to_pair(make_request())
    # default strip leaves tool defs alone
    CR.strip_blocks(pair, CR.DEFAULT_STRIP)
    assert pair.request["tools"][0]["description"].startswith("Run a SQL")
    # opt-in strips description + schema, keeps the name
    pair2 = CR.to_pair(make_request())
    CR.strip_blocks(pair2, list(CR.DEFAULT_STRIP) + [CR.TOOL_DEFINITIONS])
    assert pair2.request["tools"][0]["name"] == "run_sql"
    assert "stripped" in pair2.request["tools"][0]["description"]
    assert "__stripped__" in pair2.request["tools"][0]["input_schema"]


def test_narrative_spans_locate_all_categories():
    pair = CR.to_pair(make_request(), make_response())
    cats = {sp.category for sp in CR.narrative_spans(pair)}
    assert {CR.SYSTEM_PROMPT, CR.USER_TEXT, CR.ASSISTANT_TEXT, CR.TOOL_USE_INPUT} <= cats


# --------------------------------------------------------------------------- #
# The verifiable request-id anchor
# --------------------------------------------------------------------------- #
class _FakeMessage:
    """Mimics an SDK Message: the public per-response `_request_id` attribute that
    is ABSENT from Message.model_fields on anthropic 0.76.0."""
    def __init__(self, request_id=None, content=None, mid="msg_fake", usage=None, model="claude-x"):
        self._request_id = request_id
        self.content = content or [{"type": "text", "text": "ok"}]
        self.id = mid
        self.usage = usage or {"input_tokens": 1, "output_tokens": 1}
        self.model = model

    def model_dump(self, mode="python"):
        return {"id": self.id, "type": "message", "role": "assistant",
                "model": self.model, "content": self.content, "usage": self.usage,
                "stop_reason": "end_turn"}


def test_extract_request_id_from_object_attribute():
    assert CR.extract_request_id(_FakeMessage(request_id="req_LIVE9")) == "req_LIVE9"


def test_extract_request_id_from_dict():
    assert CR.extract_request_id({"_request_id": "req_D1"}) == "req_D1"
    assert CR.extract_request_id({"request_id": "req_D2"}) == "req_D2"


def test_extract_request_id_from_stream_object():
    class _FakeStream:  # streaming exposes .request_id (the response header)
        request_id = "req_STREAMHDR"
    assert CR.extract_request_id(_FakeStream()) == "req_STREAMHDR"


def test_extract_request_id_absent_returns_none():
    assert CR.extract_request_id(_FakeMessage(request_id=None)) is None
    assert CR.extract_request_id(None) is None


def test_anchor_first_party_request_id_is_verifiable():
    a = CR.anchor_for({"model": "m"}, {"id": "msg_1"}, "req_abc", provider="anthropic")
    assert a["type"] == "request_id" and a["verifiable"] is True and a["request_id"] == "req_abc"


def test_anchor_bedrock_falls_back_to_deterministic():
    # Bedrock/Vertex branch: header absent -> deterministic, NOT verifiable.
    resp = {"id": "msg_bedrock", "model": "claude-x", "usage": {"input_tokens": 5, "output_tokens": 2}}
    a = CR.anchor_for({"model": "claude-x"}, resp, None, provider="bedrock")
    assert a["type"] == "deterministic"
    assert a["verifiable"] is False
    assert a["provider"] == "bedrock"
    assert a["provider_id"] == "msg_bedrock"
    assert len(a["fingerprint"]) == 16
    # deterministic: same inputs -> same fingerprint
    a2 = CR.anchor_for({"model": "claude-x"}, resp, None, provider="bedrock")
    assert a["fingerprint"] == a2["fingerprint"]


def test_anchor_non_req_id_falls_back_even_on_anthropic():
    a = CR.anchor_for({}, {"id": "msg_1"}, "not-a-req-id", provider="anthropic")
    assert a["type"] == "deterministic" and a["verifiable"] is False


def test_artifact_request_id_only_set_when_verifiable():
    art = CR.redact_pair(make_request(), make_response(), "req_real")
    assert art.request_id == "req_real" and art.anchor["verifiable"] is True
    art2 = CR.redact_pair(make_request(), make_response(), None, provider="bedrock")
    assert art2.request_id is None and art2.anchor["type"] == "deterministic"


# --------------------------------------------------------------------------- #
# Preview built from the redaction_map, NOT diff_preview
# --------------------------------------------------------------------------- #
def test_preview_built_from_rmap_shows_per_category_counts(artifact):
    pv = artifact.preview
    assert isinstance(pv, CR.ReproPreview)
    # structural strips AND narrative masks both represented, per API category
    assert pv.by_category.get(CR.IMAGE_DATA, 0) >= 1
    assert pv.by_category.get(CR.TOOL_RESULT, 0) >= 1
    assert pv.by_category.get(CR.SYSTEM_PROMPT, 0) >= 1
    assert pv.by_category.get(CR.USER_TEXT, 0) >= 1
    assert pv.masked_total >= 1 and pv.stripped_total >= 1
    # per-entity counts come straight off the map
    assert "ANTHROPIC_KEY" in pv.by_entity
    rendered = pv.render()
    assert "MASKED" in rendered and "STRIPPED" in rendered and "by category" in rendered


def test_preview_from_rmap_direct():
    rmap = [
        {"api_category": "user_text", "method": "mask", "entity": "EMAIL_ADDRESS",
         "replacement": "‹EMAIL_ADDRESS›", "count": 1, "location": "request.messages[0].content"},
        {"api_category": "tool_result", "method": "strip", "entity": None,
         "replacement": "‹tool_result stripped›", "count": 1, "location": "request.messages[2]"},
    ]
    pv = CR.preview_from_rmap(rmap)
    assert pv.masked_total == 1 and pv.stripped_total == 1
    assert pv.by_category == {"user_text": 1, "tool_result": 1}
    assert pv.by_method == {"mask": 1, "strip": 1}


# --------------------------------------------------------------------------- #
# Message.text leniency — array-of-blocks AND bare-text
# --------------------------------------------------------------------------- #
def test_message_text_content_array_shape():
    msg = {"content": [{"type": "text", "text": "hello "},
                       {"type": "thinking", "thinking": "(ignored-vis)"},
                       {"type": "text", "text": "world"}]}
    assert CR.message_text(msg) == "hello (ignored-vis)world"


def test_message_text_bare_text_shape():
    assert CR.message_text({"text": "just a flat string"}) == "just a flat string"


def test_message_text_plain_string_content():
    assert CR.message_text({"content": "plain content"}) == "plain content"


def test_message_text_on_object():
    msg = _FakeMessage(content=[{"type": "text", "text": "from-object"}])
    assert CR.message_text(msg) == "from-object"


def test_message_text_missing_is_empty():
    assert CR.message_text({}) == ""


# --------------------------------------------------------------------------- #
# Ring-buffer wrapper + report_last (capture path, no network)
# --------------------------------------------------------------------------- #
class _FakeInnerMessages:
    """Stands in for the real `messages` resource — no network."""
    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        rid = kwargs.pop("_fake_request_id", "req_default")
        return _FakeMessage(request_id=rid,
                            content=[{"type": "text", "text": "fake completion"}])


def _wire_fake_client():
    pytest.importorskip("anthropic")  # ReportingClient needs the [api] extra
    client = CR.ReportingClient(api_key="test-key", report_buffer=3)
    client.messages._inner = _FakeInnerMessages()
    return client


def test_ring_buffer_records_request_id_on_create():
    client = _wire_fake_client()
    client.messages.create(model="claude-x", max_tokens=8,
                           messages=[{"role": "user", "content": "hi"}],
                           _fake_request_id="req_ring1")
    assert len(client.report_buffer) == 1
    req, resp, rid, provider = client.report_buffer[-1]
    assert rid == "req_ring1" and provider == "anthropic"
    assert req["model"] == "claude-x"  # transport keys curated out, model kept


def test_ring_buffer_respects_maxlen_and_report_last_is_most_recent():
    client = _wire_fake_client()
    for i in range(5):
        client.messages.create(model="claude-x", max_tokens=8,
                               messages=[{"role": "user", "content": f"msg {i}"}],
                               _fake_request_id=f"req_{i}")
    # maxlen=3 -> only the last three survive
    assert len(client.report_buffer) == 3
    art = client.report_last("most recent please")
    assert art.request_id == "req_4"  # newest
    art_prev = client.report_last("the one before", index=-2)
    assert art_prev.request_id == "req_3"


def test_report_last_empty_ring_raises():
    client = _wire_fake_client()
    with pytest.raises(IndexError):
        client.report_last("nothing recorded")


def test_streaming_capture_uses_stream_request_id_header():
    """Streaming path: get_final_message() snapshot may lack _request_id;
    the wrapper reads stream.request_id (the header)."""
    class _FakeStream:
        request_id = "req_streamHDR"
        def get_final_message(self):
            return _FakeMessage(request_id=None,  # snapshot lacks it on purpose
                                content=[{"type": "text", "text": "streamed"}])

    class _FakeStreamMgr:
        def __enter__(self): return _FakeStream()
        def __exit__(self, *a): return False

    class _FakeInnerWithStream(_FakeInnerMessages):
        def stream(self, **kwargs): return _FakeStreamMgr()

    pytest.importorskip("anthropic")  # ReportingClient needs the [api] extra
    client = CR.ReportingClient(api_key="test-key")
    client.messages._inner = _FakeInnerWithStream()
    with client.messages.stream(model="claude-x", max_tokens=8,
                                messages=[{"role": "user", "content": "hi"}]) as s:
        s.get_final_message()
    _, _, rid, _ = client.report_buffer[-1]
    assert rid == "req_streamHDR"


# --------------------------------------------------------------------------- #
# Effort signal — the cross-surface "one platform" schema
# --------------------------------------------------------------------------- #
def test_effort_signal_cross_surface_shape(artifact):
    eff = artifact.effort_signal
    assert eff["surface"] == "api"
    assert eff["request_id"] == "req_011CtEST"
    assert eff["provider"] == "anthropic"
    for key in ("repro_completeness", "redaction", "redaction_decisions",
                "self_rating", "reputation_token", "anchor"):
        assert key in eff
    assert eff["repro_completeness"]["has_request"] is True
    assert eff["repro_completeness"]["has_response"] is True
    assert eff["repro_completeness"]["turns"] == 4
    assert eff["repro_completeness"]["tools_included"] is True
    assert isinstance(eff["redaction_decisions"]["by_category"], dict)
    # JSON-serializable (it rides along with the report)
    json.dumps(eff)


def test_effort_self_rating_passthrough():
    art = CR.redact_pair(make_request(), make_response(), "req_z",
                         quality=4, alignment_confidence=5)
    assert art.effort_signal["self_rating"] == {"quality": 4, "alignment_confidence": 5}


# --------------------------------------------------------------------------- #
# Draft builder — embeds the anchor; never auto-sends
# --------------------------------------------------------------------------- #
def test_draft_embeds_anchor_and_targets_support(artifact):
    draft = CR.build_draft(artifact, deliver="return")
    assert draft.to == "support@anthropic.com"
    assert "req_011CtEST" in draft.subject
    assert "req_011CtEST" in draft.body
    # the redacted repro rides in the body, with no sentinels
    for value in ALL_SENTINELS.values():
        assert value not in draft.body


def test_draft_mailto_url_is_well_formed(artifact):
    draft = CR.build_draft(artifact, deliver="mailto")
    assert draft.mailto_url.startswith("mailto:support%40anthropic.com?")
    parsed = urllib.parse.urlparse(draft.mailto_url)
    qs = urllib.parse.parse_qs(parsed.query)
    assert "subject" in qs and "body" in qs
    assert "req_011CtEST" in qs["subject"][0]


def test_draft_file_delivery_writes_and_never_sends(tmp_path, artifact):
    draft = CR.build_draft(artifact, deliver="file", out_dir=str(tmp_path))
    assert len(draft.files) == 2
    md = [f for f in draft.files if f.endswith(".md")][0]
    content = open(md).read()
    assert "support@anthropic.com" in content and "req_011CtEST" in content
    for value in ALL_SENTINELS.values():
        assert value not in content


def test_deterministic_anchor_draft_subject_uses_fingerprint():
    art = CR.redact_pair(make_request(), make_response(), None, provider="bedrock")
    draft = CR.build_draft(art, deliver="return")
    assert art.anchor["fingerprint"] in draft.subject


# --------------------------------------------------------------------------- #
# Ingest paths — Langfuse / Helicone -> ReproPair (schemas pinned from docs)
# --------------------------------------------------------------------------- #
def test_from_helicone_reads_raw_bodies():
    obj = {
        "request_body": make_request(),
        "response_body": make_response(),
        "request_id": "req_helicone",
    }
    pair = CR.from_helicone(obj)
    assert pair.source == "helicone"
    assert pair.request_id == "req_helicone"
    assert pair.request["model"] == "claude-sonnet-4-5"
    # redaction still works end-to-end off a Helicone-sourced pair
    art = CR.redact_pair(pair.request, pair.response, pair.request_id)
    for value in ALL_SENTINELS.values():
        assert value not in art.upload_text()


def test_from_helicone_decodes_json_string_bodies():
    obj = {"request_body": json.dumps({"model": "m", "messages": []}),
           "response_body": json.dumps(make_response())}
    pair = CR.from_helicone(obj)
    assert pair.request["model"] == "m"
    assert pair.response["id"] == "msg_01ABC"


def test_from_langfuse_maps_input_output():
    gen = {
        "id": "obs_1",
        "model": "claude-sonnet-4-5",
        "input": {"system": "be helpful", "messages": [{"role": "user", "content": "hi"}]},
        "output": {"content": [{"type": "text", "text": "hello"}]},
        "usage": {"input": 5, "output": 2},
        "metadata": {"request_id": "req_langfuse"},
    }
    pair = CR.from_langfuse(gen)
    assert pair.source == "langfuse"
    assert pair.request_id == "req_langfuse"
    assert pair.request["messages"][0]["content"] == "hi"
    assert pair.response["content"][0]["text"] == "hello"


def test_from_langfuse_text_only_output():
    gen = {"model": "m", "input": [{"role": "user", "content": "q"}], "output": "an answer"}
    pair = CR.from_langfuse(gen)
    assert pair.request["messages"] == [{"role": "user", "content": "q"}]
    assert pair.response["content"][0]["text"] == "an answer"


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_cli_redact_subcommand(tmp_path, capsys):
    rp = tmp_path / "req.json"
    sp = tmp_path / "resp.json"
    rp.write_text(json.dumps(make_request()))
    sp.write_text(json.dumps(make_response()))
    rc = CR.main(["redact", str(rp), str(sp)])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hard_gate_pass"] is True
    blob = json.dumps(out)
    for value in ALL_SENTINELS.values():
        assert value not in blob


def test_floor_sweep_catches_pii_in_nonnarrative_fields():
    """The hard-gate floor must catch email / IPv4 / US-SSN that live OUTSIDE
    narrative spans (a metadata leaf, a structured response field) — not just secrets."""
    from fb_assist import redact as R
    root = {
        "request": {"metadata": {"contact": "leak.me@corp.example", "ip": "203.0.113.7"}},
        "response": {"trace": "row dump ssn=123-45-6789 in a structured field"},
    }
    rmap: list = []
    removed = CR._enforce_secret_floor(root, rmap)
    blob = json.dumps(root)
    assert "leak.me@corp.example" not in blob
    assert "203.0.113.7" not in blob
    assert "123-45-6789" not in blob
    assert removed >= 3
    assert R.scan_secrets(blob) == [] and R._scan_pii_regex(blob) == []
