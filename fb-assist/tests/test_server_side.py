"""Tests for fb_assist.server_side — the server-side consent-genericize reference.

Plants are all SYNTHETIC (no real credentials/PII). The fixture
``sample-feedback-conversation.json`` carries:
  * a pattern-valid FAKE Anthropic key,
  * real-SHAPED PII (email, SSN, IP) — regex-floor catchable,
  * a fake internal codename ("Project Halcyon"),
  * a filesystem path.

We assert the privacy contract end-to-end:
  * consent=genericized  -> every sentinel ABSENT (byte-level) from the attached
    artifact, two-pass re-id verify ``ok``, audit reflects redactions + anchor;
  * consent=none         -> only {type, reason} attached (no conversation text);
  * a forced residual leak -> the HARD gate fails closed (attach none + flag);
  * ``message_text`` reads both content-array and bare-text shapes;
  * the reference HTTP endpoint routes a POST end-to-end (direct + live socket).

Run:  USE_TF=0 pytest tests/test_server_side.py -q
"""

import json
import os
import sys
import threading
import urllib.request
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import server_side as S  # noqa: E402
from fb_assist.desktop_chat import message_text  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample-feedback-conversation.json"

# The conversation under feedback + its anchor message.
CONV_UUID = "conv-fb-aaaa-1111-2222-333344445555"
MSG_UUID = "msg-h1-content-array"
CODENAME = "Project Halcyon"

# Every synthetic sentinel that MUST be absent from a genericized artifact.
SENTINELS = [
    "sk-ant-api03-FAKE0000fake1111fake2222fake3333fake4444fake5555AA",  # secret
    "dana.lee@northwind-labs.example",                                  # email PII
    "987-65-4321",                                                      # SSN PII
    "10.1.2.3",                                                         # IP PII
    CODENAME,                                                           # semantic IP codename
    "/home/dana/code/halcyon-internal/auth.py",                        # filesystem path
]


def _event(**kw):
    base = dict(org_id="org-test", conversation_id=CONV_UUID, message_id=MSG_UUID,
                type="thumbs_down", reason="Submit froze.", user_id="user-test")
    base.update(kw)
    return S.FeedbackEvent(**base)


def _genericized_consent(terms=(CODENAME,)):
    return S.StaticConsentPolicy(S.ConsentDecision(
        attach=S.ATTACH_GENERICIZED, basis="test", genericize_terms=list(terms)))


# --------------------------------------------------------------------------- #
# 1. genericized: every sentinel gone, verify ok, audit + anchor correct
# --------------------------------------------------------------------------- #
def test_genericized_strips_every_sentinel_byte_level():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    result = S.handle_feedback(_event(), store=store, consent=_genericized_consent(),
                               sink=sink, use_gliner=False)

    assert result.status == S.STATUS_GENERICIZED
    art = result.artifact
    assert art.attach == S.ATTACH_GENERICIZED

    # The outbound bytes == the rendered markdown; also serialize the round-trip
    # conversation dict — NO sentinel may survive in EITHER.
    outbound = art.rendered or ""
    conv_json = json.dumps(art.conversation, ensure_ascii=False)
    for s in SENTINELS:
        assert s not in outbound, f"sentinel survived in rendered bytes: {s!r}"
        assert s not in conv_json, f"sentinel survived in round-trip conversation: {s!r}"

    # Two-pass re-identification verify passed; the deterministic floor was clean.
    assert result.audit.reid_verdict is True
    assert result.audit.floor_clean is True
    # The public audit dict exposes it under the friendlier key.
    assert result.audit.to_dict()["reid_verify_ok"] is True

    # Audit reflects the redactions (by category) and the message-UUID anchor.
    assert result.audit.redaction_count > 0
    cats = result.audit.redacted_categories
    assert any("ANTHROPIC" in c for c in cats), cats          # the secret
    assert "IP_CODENAME" in cats, cats                        # the codename literal
    assert result.audit.anchor["message_id"] == MSG_UUID
    assert result.audit.anchor["conversation_id"] == CONV_UUID

    # The sink captured exactly this artifact + audit.
    assert sink.last["feedback_id"] == result.feedback_id
    assert sink.last["artifact"].attach == S.ATTACH_GENERICIZED


def test_audit_to_dict_carries_no_raw_value():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    result = S.handle_feedback(_event(), store=store, consent=_genericized_consent(), sink=sink)
    blob = json.dumps(result.audit.to_dict(), ensure_ascii=False)
    for s in SENTINELS:
        assert s not in blob, f"audit leaked a raw value: {s!r}"


# --------------------------------------------------------------------------- #
# 2. consent=none: only {type, reason}; no conversation text
# --------------------------------------------------------------------------- #
def test_consent_none_attaches_only_type_reason():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    consent = S.StaticConsentPolicy(S.ConsentDecision(attach=S.ATTACH_NONE, basis="test"))
    result = S.handle_feedback(_event(), store=store, consent=consent, sink=sink)

    assert result.status == S.STATUS_NONE
    art = result.artifact
    assert art.attach == S.ATTACH_NONE
    assert art.conversation is None
    assert art.rendered is None
    assert art.type == "thumbs_down" and art.reason == "Submit froze."
    assert result.audit.redaction_count == 0
    assert result.audit.attached == S.ATTACH_NONE
    # The anchor is still recorded (it travels in the URL, not the body).
    assert result.audit.anchor["message_id"] == MSG_UUID

    # Defensive: no sentinel anywhere in the public result (no conversation text).
    blob = json.dumps(result.to_public_dict(), ensure_ascii=False)
    for s in SENTINELS:
        assert s not in blob


# --------------------------------------------------------------------------- #
# 3. fail-closed: a forced residual leak must NOT ship
# --------------------------------------------------------------------------- #
def test_fail_closed_on_residual_floor_leak():
    """A deliberately-broken genericizer that leaves a live secret in the outbound
    bytes must trip the HARD gate -> attach none + flag, never the leaky artifact."""
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    leak = SENTINELS[0]  # the fake secret

    def broken_genericize(conv, decision):
        rendered = f"# {conv.name}\n\n### Human\nstill leaking {leak} oops\n"
        return S.GenericizeResult(
            rendered=rendered, conversation={"leak": rendered},
            redaction_map=[], counts={"redactions": 0, "by_category": {}, "by_severity": {}},
            effort_signal={}, reid_ok=True, meaning_risk_flags=[],
            leak_candidates=[], floor_clean=True,   # lies! the handler re-checks.
            floor_residual=[],
        )

    result = S.handle_feedback(_event(), store=store, consent=_genericized_consent(),
                               sink=sink, genericize=broken_genericize)

    assert result.status == S.STATUS_FAILED_CLOSED
    assert result.artifact.attach == S.ATTACH_NONE          # failed closed to safe
    assert result.artifact.conversation is None
    assert "fail_closed" in result.artifact.flags
    assert "residual_floor_leak" in result.artifact.flags
    assert result.audit.floor_clean is False
    # The leak never reached the sink's stored artifact.
    blob = json.dumps(result.to_public_dict(), ensure_ascii=False)
    assert leak not in blob


def test_fail_closed_on_reid_verify_failure():
    """If the two-pass re-id verify fails (even with a clean floor), fail closed."""
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()

    def reid_fails(conv, decision):
        return S.GenericizeResult(
            rendered="# x\n\n### Human\nclean text, no sentinels\n",
            conversation={}, redaction_map=[],
            counts={"redactions": 0, "by_category": {}, "by_severity": {}},
            effort_signal={}, reid_ok=False, meaning_risk_flags=[],
            leak_candidates=[], floor_clean=True, floor_residual=[],
        )

    result = S.handle_feedback(_event(), store=store, consent=_genericized_consent(),
                               sink=sink, genericize=reid_fails)
    assert result.status == S.STATUS_FAILED_CLOSED
    assert result.artifact.attach == S.ATTACH_NONE
    assert "reid_verify_failed" in result.artifact.flags


def test_conversation_unavailable_fails_closed():
    class EmptyStore:
        def fetch(self, *a):
            return None

    sink = S.InMemoryFeedbackSink()
    result = S.handle_feedback(_event(), store=EmptyStore(), consent=_genericized_consent(),
                               sink=sink)
    assert result.status == S.STATUS_FAILED_CLOSED
    assert result.artifact.attach == S.ATTACH_NONE
    assert "conversation_unavailable" in result.artifact.flags


# --------------------------------------------------------------------------- #
# 4. raw opt-in
# --------------------------------------------------------------------------- #
def test_raw_optin_attaches_raw_with_flag():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    consent = S.StaticConsentPolicy(S.ConsentDecision(attach=S.ATTACH_RAW, basis="power-user"))
    result = S.handle_feedback(_event(), store=store, consent=consent, sink=sink)

    assert result.status == S.STATUS_RAW
    assert result.artifact.attach == S.ATTACH_RAW
    assert "raw_optin" in result.artifact.flags
    # Raw means raw: the original (unredacted) values ARE present, by design.
    assert SENTINELS[0] in json.dumps(result.artifact.conversation, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 5. message_text handles both shapes
# --------------------------------------------------------------------------- #
def test_message_text_both_shapes():
    # content[] array shape
    arr = {"content": [{"type": "text", "text": "from content array"}], "text": ""}
    assert message_text(arr) == "from content array"
    # bare top-level text shape
    bare = {"content": [], "text": "from bare text"}
    assert message_text(bare) == "from bare text"
    # both present -> prefer the content[] join
    both = {"content": [{"type": "text", "text": "array wins"}], "text": "bare loses"}
    assert message_text(both) == "array wins"
    # malformed -> "" (lenient)
    assert message_text({"weird": 1}) == ""


# --------------------------------------------------------------------------- #
# 6. the reference HTTP endpoint routes a POST end-to-end
# --------------------------------------------------------------------------- #
def _path(org, conv, msg):
    return (f"/api/organizations/{org}/chat_conversations/{conv}"
            f"/chat_messages/{msg}/chat_feedback")


def test_route_function_direct():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    body = json.dumps({"type": "thumbs_down", "reason": "froze"})
    status, payload = S.route("POST", _path("org-x", CONV_UUID, MSG_UUID), body,
                              store=store, consent=_genericized_consent(), sink=sink,
                              user_id="user-x")
    assert status == 201
    assert payload["status"] == S.STATUS_GENERICIZED
    assert payload["anchor"]["conversation_id"] == CONV_UUID
    # The 201 body carries NO conversation text — only status + anchor + audit.
    assert "conversation" not in payload
    blob = json.dumps(payload, ensure_ascii=False)
    for s in SENTINELS:
        assert s not in blob
    # A non-matching path 404s; a GET 405s.
    assert S.route("POST", "/nope", "{}", store=store, consent=_genericized_consent(),
                   sink=sink)[0] == 404
    assert S.route("GET", _path("o", CONV_UUID, MSG_UUID), "", store=store,
                   consent=_genericized_consent(), sink=sink)[0] == 405


def test_reference_http_server_live_roundtrip():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    sink = S.InMemoryFeedbackSink()
    server = S.make_reference_app(store, _genericized_consent(), sink, host="127.0.0.1", port=0)
    host, port = server.server_address
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        url = f"http://{host}:{port}" + _path("org-live", CONV_UUID, MSG_UUID)
        data = json.dumps({"type": "thumbs_down", "reason": "froze"}).encode()
        req = urllib.request.Request(url, data=data, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "X-User-Id": "user-live"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == 201
            payload = json.loads(resp.read().decode())
    finally:
        t.join(timeout=10)
        server.server_close()

    assert payload["status"] == S.STATUS_GENERICIZED
    assert payload["attach"] == S.ATTACH_GENERICIZED
    # The server actually attached to the sink, privacy-clean.
    assert sink.last is not None
    assert SENTINELS[0] not in (sink.last["artifact"].rendered or "")


# --------------------------------------------------------------------------- #
# 7. ports are satisfied by the reference adapters (Protocol structural check)
# --------------------------------------------------------------------------- #
def test_reference_adapters_satisfy_ports():
    store = S.InMemoryConversationStore.from_export(FIXTURE)
    assert isinstance(store, S.ConversationStore)
    assert isinstance(_genericized_consent(), S.ConsentPolicy)
    assert isinstance(S.InMemoryFeedbackSink(), S.FeedbackSink)


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    import pytest
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
