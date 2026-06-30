"""Tests for fb_assist.reference_intake — the reference /v1/feedback intake endpoint.

Mirrors test_claude_repro.py style. We plant pattern-valid FAKE
secrets + real-shaped PII into a Messages-API pair, run it through the REAL
claude_repro SDK to produce a privacy-clean artifact, then assert the intake's
contract end-to-end:

  * a clean artifact is ACCEPTED, the stored report carries no sentinel, the
    request-id anchor is verifiable;
  * an under-redacted artifact (a planted raw secret/PII) trips the fail-closed
    deterministic floor -> REJECTED, nothing stored, no raw value in the receipt;
  * a malformed / bad-anchor submission is rejected;
  * the optional reputation token is verified (valid -> credited; lifted / tampered /
    revoked -> rejected; hmac-without-secret -> accepted uncredited);
  * the reference HTTP endpoint routes a POST end-to-end (direct + live socket);
  * the FeedbackSink port is satisfied by the in-memory adapter.

Run:  USE_TF=0 pytest tests/test_reference_intake.py -q
"""

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from fb_assist import claude_repro as CR  # noqa: E402
from fb_assist import reference_intake as RI  # noqa: E402
from fb_assist import reputation as REP  # noqa: E402

# --------------------------------------------------------------------------- #
# Planted sentinels — pattern-valid FAKE secrets + real-shaped PII
# --------------------------------------------------------------------------- #
SK_ANT = "sk-ant-api03-" + "Zz9" * 14 + "qQ"
EMAIL = "jane.customer@example.com"
SSN = "078-05-1120"
SYS_PATH = "/home/user/proprietary/agent_config.yaml"
ALL_SENTINELS = [SK_ANT, EMAIL, SSN, SYS_PATH]

TS = 1000.0  # fixed clock for hermetic reputation tests


def make_request():
    return {
        "model": "claude-sonnet-4-5", "max_tokens": 512,
        "system": f"You are Acme Corp's assistant. Load config from {SYS_PATH}.",
        "messages": [
            {"role": "user",
             "content": f"My API key is {SK_ANT}, reach me at {EMAIL}. Why is the JSON wrong?"},
            {"role": "assistant", "content": [{"type": "text", "text": "Let me look into it."}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": f"ssn={SSN}"}]}]},
        ],
    }


def make_response():
    return {
        "id": "msg_01ABC", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
        "usage": {"input_tokens": 100, "output_tokens": 20},
        "content": [{"type": "text", "text": "The schema mismatch is on the second field."}],
    }


def clean_artifact(request_id="req_011CtEST", provider="anthropic"):
    """A real, privacy-clean claude_repro artifact (the SDK side of the loop)."""
    return CR.redact_pair(make_request(), make_response(), request_id,
                          provider=provider, use_gliner=False,
                          description="the model ignored my JSON schema")


# Reputation helpers (mirror tests/test_reputation.py).
def ident(backend=None, ts=TS):
    return REP._empty_reputation(backend or REP.BACKEND, ts)


def verify_key_for(rep):
    return None if REP._verify_key_is_public(rep["backend"]) else rep["verify_key"]


def with_token(artifact, rep, *, ts=TS):
    """Attach a reputation token bound to the artifact's effort signal (hermetic)."""
    artifact.effort_signal = REP.attach_reputation_token(artifact.effort_signal,
                                                         identity=rep, ts=ts, nonce="n-fixed")
    return artifact


# --------------------------------------------------------------------------- #
# 1. accept: a clean SDK artifact is accepted, stored privacy-clean
# --------------------------------------------------------------------------- #
def test_clean_artifact_is_accepted_and_stored_clean():
    art = clean_artifact()
    sink = RI.InMemoryFeedbackSink()
    sub = RI.submission_from_artifact(art)
    receipt = RI.intake(sub, sink=sink)

    assert receipt.accepted and receipt.reason == RI.REASON_OK
    assert receipt.floor_clean is True
    assert receipt.verifiable is True
    assert receipt.anchor["request_id"] == "req_011CtEST"

    # Exactly one record, an AcceptedReport, with NO sentinel anywhere.
    assert len(sink) == 1 and len(sink.accepted) == 1
    stored = sink.last["accepted"]
    blob = json.dumps(stored.to_dict(), ensure_ascii=False)
    for s in ALL_SENTINELS:
        assert s not in blob, f"sentinel survived into the stored report: {s!r}"
    assert stored.request_id == "req_011CtEST"


def test_whole_loop_composition_via_submission_from_artifact():
    """SDK redact+anchor -> submission_from_artifact -> intake accept (the full loop)."""
    art = clean_artifact()
    sub = RI.submission_from_artifact(art)
    assert sub.request_id == art.request_id
    assert sub.redacted_repro == art.redacted_repro
    sink = RI.InMemoryFeedbackSink()
    assert RI.intake(sub, sink=sink).accepted is True


# --------------------------------------------------------------------------- #
# 2. fail-closed deterministic floor over the inbound bytes
# --------------------------------------------------------------------------- #
def test_fail_closed_on_residual_secret():
    art = clean_artifact()
    sub = RI.submission_from_artifact(art)
    # Simulate an UNDER-redacted artifact: a raw secret survived into the repro.
    sub.redacted_repro = dict(sub.redacted_repro)
    sub.redacted_repro["_leak"] = SK_ANT

    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink)

    assert not receipt.accepted
    assert receipt.reason == RI.REASON_RESIDUAL_FLOOR_LEAK
    assert receipt.floor_clean is False
    assert receipt.floor_residual.get("by_category", {}).get("secret", 0) >= 1
    # Nothing accepted; the raw secret never reached the sink OR the receipt.
    assert len(sink.accepted) == 0
    assert sink.last["accepted"] is None
    blob = json.dumps(sink.last["receipt"].to_dict(), ensure_ascii=False)
    assert SK_ANT not in blob


def test_fail_closed_on_residual_pii():
    art = clean_artifact()
    sub = RI.submission_from_artifact(art)
    sub.effort_signal = dict(sub.effort_signal)
    sub.effort_signal["_leak"] = EMAIL  # raw PII smuggled into the effort signal
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink)
    assert not receipt.accepted and receipt.reason == RI.REASON_RESIDUAL_FLOOR_LEAK
    assert EMAIL not in json.dumps(receipt.to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 3. anchor verification
# --------------------------------------------------------------------------- #
def test_malformed_submission_rejected():
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(RI.IntakeSubmission(redacted_repro={}), sink=sink)
    assert not receipt.accepted and receipt.reason == RI.REASON_MALFORMED
    assert sink.last["accepted"] is None


def test_bad_anchor_rejected_when_request_id_malformed():
    art = clean_artifact()
    sub = RI.submission_from_artifact(art)
    sub.request_id = "not-a-req-id"   # present but not the req_ shape
    sub.anchor = None
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink)
    assert not receipt.accepted and receipt.reason == RI.REASON_BAD_ANCHOR


def test_deterministic_anchor_accepted_but_unverifiable():
    # Bedrock: no request-id -> the SDK emits a deterministic fingerprint anchor.
    art = clean_artifact(request_id=None, provider="bedrock")
    sub = RI.submission_from_artifact(art)
    assert sub.request_id is None
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink)
    assert receipt.accepted is True
    assert receipt.verifiable is False
    assert receipt.anchor["type"] == "deterministic"
    assert "anchor_unverifiable" in receipt.flags
    # request_id is NOT claimed as verifiable on the stored report.
    assert sink.last["accepted"].request_id is None


# --------------------------------------------------------------------------- #
# 4. reputation token verification (optional)
# --------------------------------------------------------------------------- #
def test_no_token_accepted_anonymous():
    art = clean_artifact()
    receipt = RI.intake(RI.submission_from_artifact(art), sink=RI.InMemoryFeedbackSink())
    assert receipt.accepted
    assert receipt.reputation["verified"] is False
    assert receipt.reputation["reason"] == "no_token"


def test_valid_reputation_token_is_credited():
    rep = ident(REP.BACKEND)
    art = with_token(clean_artifact(), rep)
    sub = RI.submission_from_artifact(art)
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink, public_key=verify_key_for(rep), now=TS)
    assert receipt.accepted
    assert receipt.reputation["verified"] is True
    assert receipt.reputation["pseudonymous_id"] == rep["pseudonymous_id"]
    assert "reputation_verified" in receipt.flags
    # The credited pid rides on the stored report too.
    assert sink.last["accepted"].reputation["pseudonymous_id"] == rep["pseudonymous_id"]


def test_lifted_token_rejected_effort_signal_mismatch():
    """A token minted for signal A, stapled onto a different submission -> rejected."""
    rep = ident(REP.BACKEND)
    art = clean_artifact()
    other_signal = dict(art.effort_signal)
    other_signal["surface"] = "some-other-surface"   # bind the token to a DIFFERENT signal
    tok = REP.serialize_token(REP.mint_token(other_signal, identity=rep, ts=TS, nonce="n"))
    sub = RI.submission_from_artifact(art, reputation_token=tok)  # submitted with art's signal
    sink = RI.InMemoryFeedbackSink()
    receipt = RI.intake(sub, sink=sink, public_key=verify_key_for(rep), now=TS)
    assert not receipt.accepted and receipt.reason == RI.REASON_BAD_REPUTATION_TOKEN
    assert receipt.reputation["reason"] == "effort_signal_mismatch"
    assert sink.last["accepted"] is None


def test_tampered_token_rejected_bad_signature():
    rep = ident(REP.BACKEND)
    art = clean_artifact()
    tokdict = dict(REP.mint_token(art.effort_signal, identity=rep, ts=TS, nonce="n"))
    tokdict["reputation_score"] = 999.0   # tamper the payload, signature no longer valid
    tok = REP.serialize_token(tokdict)
    sub = RI.submission_from_artifact(art, reputation_token=tok)
    receipt = RI.intake(sub, sink=RI.InMemoryFeedbackSink(),
                        public_key=verify_key_for(rep), now=TS)
    assert not receipt.accepted and receipt.reason == RI.REASON_BAD_REPUTATION_TOKEN
    assert receipt.reputation["reason"] == "bad_signature"


def test_revoked_token_rejected():
    rep = ident(REP.BACKEND)
    art = with_token(clean_artifact(), rep)
    sub = RI.submission_from_artifact(art)
    receipt = RI.intake(sub, sink=RI.InMemoryFeedbackSink(),
                        public_key=verify_key_for(rep), now=TS,
                        revocation_list=[rep["pseudonymous_id"]])
    assert not receipt.accepted and receipt.reason == RI.REASON_BAD_REPUTATION_TOKEN
    assert receipt.reputation["reason"] == "revoked"


def test_hmac_token_without_shared_secret_accepted_uncredited():
    """hmac token + no verifier secret -> unverifiable by design: accept, don't credit."""
    rep = ident(REP.BACKEND_HMAC)
    art = with_token(clean_artifact(), rep)
    sub = RI.submission_from_artifact(art)
    receipt = RI.intake(sub, sink=RI.InMemoryFeedbackSink(), public_key=None, now=TS)
    assert receipt.accepted is True
    assert receipt.reputation["verified"] is False
    assert receipt.reputation["reason"] == "missing_verification_key"
    assert "reputation_unverified" in receipt.flags


def test_verify_reputation_can_be_disabled():
    rep = ident(REP.BACKEND)
    art = with_token(clean_artifact(), rep)
    receipt = RI.intake(RI.submission_from_artifact(art), sink=RI.InMemoryFeedbackSink(),
                        verify_reputation=False)
    assert receipt.accepted and receipt.reputation["reason"] == "skipped"


# --------------------------------------------------------------------------- #
# 5. purity + single-write contract
# --------------------------------------------------------------------------- #
def test_submission_never_mutated():
    art = clean_artifact()
    sub = RI.submission_from_artifact(art)
    before = json.dumps(sub.redacted_repro, sort_keys=True)
    RI.intake(sub, sink=RI.InMemoryFeedbackSink())
    assert json.dumps(sub.redacted_repro, sort_keys=True) == before


def test_sink_store_called_exactly_once_on_accept_and_reject():
    art = clean_artifact()
    sink = RI.InMemoryFeedbackSink()
    RI.intake(RI.submission_from_artifact(art), sink=sink)
    assert len(sink) == 1
    bad = RI.submission_from_artifact(art)
    bad.request_id, bad.anchor = "nope", None
    RI.intake(bad, sink=sink)
    assert len(sink) == 2  # one more, even though rejected


# --------------------------------------------------------------------------- #
# 6. the reference HTTP endpoint routes a POST end-to-end
# --------------------------------------------------------------------------- #
def _post_body(art, **extra):
    body = {"request_id": art.request_id, "redacted_repro": art.redacted_repro,
            "effort_signal": art.effort_signal, "anchor": art.anchor}
    body.update(extra)
    return json.dumps(body)


def test_route_function_direct():
    art = clean_artifact()
    sink = RI.InMemoryFeedbackSink()
    status, payload = RI.route("POST", "/v1/feedback", _post_body(art), sink=sink)
    assert status == RI.HTTP_ACCEPTED
    assert payload["status"] == RI.STATUS_ACCEPTED
    assert payload["anchor"]["request_id"] == "req_011CtEST"
    assert "redacted_repro" not in payload  # the receipt carries no repro/value
    for s in ALL_SENTINELS:
        assert s not in json.dumps(payload, ensure_ascii=False)
    # off-path 404; non-POST 405
    assert RI.route("POST", "/nope", "{}", sink=sink)[0] == 404
    assert RI.route("GET", "/v1/feedback", "", sink=sink)[0] == 405


def test_route_rejects_leak_with_422():
    art = clean_artifact()
    repro = dict(art.redacted_repro)
    repro["_leak"] = SK_ANT
    body = json.dumps({"request_id": art.request_id, "redacted_repro": repro,
                       "effort_signal": art.effort_signal})
    status, payload = RI.route("POST", "/v1/feedback", body, sink=RI.InMemoryFeedbackSink())
    assert status == RI.HTTP_REJECTED
    assert payload["status"] == RI.STATUS_REJECTED
    assert payload["reason"] == RI.REASON_RESIDUAL_FLOOR_LEAK
    assert SK_ANT not in json.dumps(payload, ensure_ascii=False)


def test_reference_http_server_live_roundtrip():
    art = clean_artifact()
    sink = RI.InMemoryFeedbackSink()
    server = RI.make_reference_app(sink, host="127.0.0.1", port=0)
    host, port = server.server_address
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()
    try:
        url = f"http://{host}:{port}/v1/feedback"
        req = urllib.request.Request(url, data=_post_body(art).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            assert resp.status == RI.HTTP_ACCEPTED
            payload = json.loads(resp.read().decode())
    finally:
        t.join(timeout=10)
        server.server_close()
    assert payload["status"] == RI.STATUS_ACCEPTED
    assert sink.last is not None and sink.last["accepted"] is not None
    assert SK_ANT not in json.dumps(sink.last["accepted"].to_dict(), ensure_ascii=False)


# --------------------------------------------------------------------------- #
# 7. the port is satisfied by the reference adapter
# --------------------------------------------------------------------------- #
def test_reference_adapter_satisfies_port():
    assert isinstance(RI.InMemoryFeedbackSink(), RI.FeedbackSink)


# --------------------------------------------------------------------------- #
# Standalone runner
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v", "-s"]))
