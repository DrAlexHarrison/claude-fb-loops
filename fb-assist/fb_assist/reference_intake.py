"""fb_assist.reference_intake — a DROP-IN reference of an Anthropic ``/v1/feedback``
intake endpoint for ``claude_repro`` artifacts (the API-surface counterpart to
:mod:`fb_assist.server_side`).

WHAT THIS IS
------------
:mod:`fb_assist.claude_repro` lets a developer turn an Anthropic **Messages API**
request/response into a privacy-clean, request-id-anchored repro — but today there
is **nowhere to send it**: Anthropic ships *no* feedback-intake endpoint, so the SDK
can only draft an email to ``support@``. This module is the missing other half: a
runnable, framework-agnostic, stdlib-only **reference** of what an Anthropic
``POST /v1/feedback`` intake **would** look like — the same port/adapter shape as
:mod:`fb_assist.server_side`, so an Anthropic engineer implements ONE small adapter
(the :class:`FeedbackSink`) against their real store and the privacy-bearing core —
the fail-closed deterministic floor, the ungameable request-id anchor check, and the
optional reputation-token verification — is already done, tested, and reusable.

Together the two modules show the WHOLE loop:

    claude_repro.redact_pair(...)        # SDK: redact locally + anchor on request-id
        -> Artifact {request_id, redacted_repro, effort_signal, reputation_token?}
    reference_intake.intake(submission)  # this module: validate + accept (or reject)
        -> IntakeReceipt {accepted | rejected, anchor, reputation, floor verdict}

⚠️ THE PRINCIPLED BOUNDARY (load-bearing) ⚠️
--------------------------------------------
This file is **reference-not-deployed**. It is built **only** against the PUBLIC
Anthropic Messages-API shape — the ``request-id`` (``req_…``) returned on every
response header, and the request/response JSON a developer already holds. It makes
**ZERO Anthropic-internal assumptions**: nothing about how Anthropic stores feedback,
correlates request-ids, or scores reputation. Everything it cannot know becomes the
single adapter **PORT** below.

THE PORT (the one seam an Anthropic engineer implements)
--------------------------------------------------------
  * :class:`FeedbackSink` — ``store(report_id, receipt, accepted) -> None``. The seam
    to wherever accepted feedback is durably written. The :class:`AcceptedReport` is
    already privacy-clean by the time it reaches here; the sink only persists it.

THE INTAKE CONTRACT (what the endpoint enforces)
------------------------------------------------
:func:`intake` is the whole step. Over an inbound ``claude_repro`` artifact it:

  1. **shape-validates** the submission (``redacted_repro`` present + dict);
  2. runs the **fail-closed deterministic floor** over the *inbound bytes* — the
     exact :mod:`fb_assist.redact` gate (``scan_secrets`` + ``_scan_pii_regex``) —
     and **rejects anything that still carries a secret/PII** (an under-redacted or
     tampered artifact never enters the store);
  3. **verifies the anchor** — the ``request_id`` must match the Anthropic
     ``req_…`` shape (the ungameable anchor that ties the report to a real metered
     call); a non-Anthropic provider may instead present the deterministic-fingerprint
     fallback (accepted, flagged ``verifiable: False``);
  4. **optionally verifies the reputation token** via
     :func:`fb_assist.reputation.verify_token`, bound to the submission's
     ``effort_signal`` — a forged / lifted / stale / revoked token is rejected; a
     valid one credits its pseudonymous id; an unverifiable-by-design (hmac, no shared
     secret) token is accepted uncredited;
  5. routes the result to the :class:`FeedbackSink` — storing the privacy-clean
     :class:`AcceptedReport` on accept, or only the (value-free) :class:`IntakeReceipt`
     on reject.

Reference adapters (:class:`InMemoryFeedbackSink`) + :func:`make_reference_app` make
the whole thing RUN in-repo over a stdlib ``http.server``. Run the demo:

    python -m fb_assist.reference_intake            # SDK -> intake, end to end
    python -m fb_assist.reference_intake --leak     # show the floor reject a leak
    python -m fb_assist.reference_intake --serve     # the illustrative HTTP endpoint

LOCAL ONLY. No network egress (the optional GLiNER PII pass is off by default). Pure
validation: the inbound submission is never mutated.
"""

from __future__ import annotations

import os

# Mirror redact.py / server_side.py: force the torch-only path so importing the
# sibling redactor (which lazy-loads NER) never explodes on a TF import under Keras 3.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import argparse
import json
import re
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Iterable, Optional, Protocol, runtime_checkable

from . import reputation as REP
from .redact import (
    Finding,
    _scan_pii_regex,
    scan_secrets,
    summarize_findings,
)

__all__ = [
    # the one port an Anthropic engineer implements
    "FeedbackSink",
    # data model
    "IntakeSubmission",
    "AcceptedReport",
    "IntakeReceipt",
    # the core handler
    "intake",
    "submission_from_artifact",
    # reference adapter (so the whole thing runs in-repo)
    "InMemoryFeedbackSink",
    # reference HTTP endpoint (illustrative)
    "FEEDBACK_PATH_RE",
    "route",
    "make_reference_handler",
    "make_reference_app",
    # status / reason constants
    "STATUS_ACCEPTED",
    "STATUS_REJECTED",
    "REASON_OK",
    "REASON_MALFORMED",
    "REASON_RESIDUAL_FLOOR_LEAK",
    "REASON_BAD_ANCHOR",
    "REASON_BAD_REPUTATION_TOKEN",
    "main",
]

# The Anthropic request-id shape (the ungameable anchor). Verified against the public
# Messages-API response header (``request-id: req_…``); see claude_repro.anchor_for.
REQUEST_ID_RE = re.compile(r"^req_[A-Za-z0-9]{6,}$")

# Result statuses.
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"

# Reject reasons (also the receipt ``reason`` on accept == REASON_OK).
REASON_OK = "ok"
REASON_MALFORMED = "malformed_submission"
REASON_RESIDUAL_FLOOR_LEAK = "residual_floor_leak"
REASON_BAD_ANCHOR = "bad_anchor"
REASON_BAD_REPUTATION_TOKEN = "bad_reputation_token"

# Reputation-token verdicts that mean "present but cannot be checked here" (the hmac
# fallback when the verifier holds no shared secret) — accepted UNCREDITED, not
# rejected. Every other invalid reason is an ACTIVE failure (tamper / lift / stale /
# revoked) and fails closed.
_UNVERIFIABLE_TOKEN_REASONS = frozenset({"missing_verification_key"})


# ===========================================================================  #
# PORT — the one adapter seam.  Implement it against your real store.           #
# ===========================================================================  #
@runtime_checkable
class FeedbackSink(Protocol):
    """PORT — your accepted-feedback record.

    ``store`` persists the outcome of one intake. On **accept** ``accepted`` is the
    privacy-clean :class:`AcceptedReport` (the redacted repro + anchor + effort/
    reputation summary) and you durably write it. On **reject** ``accepted`` is
    ``None`` and only the value-free :class:`IntakeReceipt` is handed over (so even
    rejections leave an auditable trail, but never a raw value). Implement it against
    wherever feedback actually lives; this reference makes NO assumption about that
    store."""

    def store(self, report_id: str, receipt: "IntakeReceipt",
              accepted: "Optional[AcceptedReport]") -> None:
        ...


# ===========================================================================  #
# DATA MODEL                                                                    #
# ===========================================================================  #
@dataclass
class IntakeSubmission:
    """One inbound ``claude_repro`` artifact, in the OBSERVABLE contract shape.

    ``request_id`` is the ungameable anchor (the Messages-API ``req_…``).
    ``redacted_repro`` is ``{"request": …, "response": …}`` AFTER the SDK's local
    redaction. ``effort_signal`` is the cross-surface signal the SDK emits.
    ``reputation_token`` is the OPTIONAL serialized token (also mirrored inside
    ``effort_signal['reputation_token']``). ``anchor`` is the SDK's anchor dict (the
    deterministic-fingerprint fallback lives here for Bedrock/Vertex)."""

    redacted_repro: dict
    request_id: Optional[str] = None
    effort_signal: dict = field(default_factory=dict)
    reputation_token: Optional[str] = None
    anchor: Optional[dict] = None
    provider: str = "anthropic"

    @classmethod
    def from_request(cls, body: Any) -> "IntakeSubmission":
        """Build from the parsed JSON request body (the ``Artifact.to_dict()`` shape,
        or the trimmed ``{request_id, redacted_repro, effort_signal, reputation_token}``
        a thin client would POST). Never raises on a missing/extra field."""
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        if isinstance(body, str):
            try:
                body = json.loads(body or "{}")
            except (json.JSONDecodeError, ValueError):
                body = {}
        if not isinstance(body, dict):
            body = {}
        effort = body.get("effort_signal")
        effort = effort if isinstance(effort, dict) else {}
        anchor = body.get("anchor")
        anchor = anchor if isinstance(anchor, dict) else None
        # request_id: top-level wins, else the effort signal, else the anchor.
        rid = body.get("request_id") or effort.get("request_id")
        if not rid and isinstance(anchor, dict) and anchor.get("type") == "request_id":
            rid = anchor.get("request_id")
        # reputation_token: top-level wins, else inside the effort signal.
        tok = body.get("reputation_token") or effort.get("reputation_token")
        provider = (body.get("provider")
                    or effort.get("provider")
                    or (anchor or {}).get("provider")
                    or "anthropic")
        repro = body.get("redacted_repro")
        return cls(
            redacted_repro=repro if isinstance(repro, dict) else {},
            request_id=rid if isinstance(rid, str) else None,
            effort_signal=effort,
            reputation_token=tok if isinstance(tok, str) else None,
            anchor=anchor,
            provider=str(provider),
        )

    def inbound_bytes(self) -> str:
        """The exact inbound payload the deterministic floor scans — the redacted
        repro AND the effort signal serialized together, so the fail-closed guarantee
        covers the whole submission, not just the repro."""
        return json.dumps(
            {"redacted_repro": self.redacted_repro, "effort_signal": self.effort_signal},
            ensure_ascii=False,
        )


@dataclass
class AcceptedReport:
    """What the sink persists on accept — privacy-clean by construction.

    Carries the redacted repro, the verified anchor, the effort signal, and the
    reputation summary (pseudonymous id + claimed score, or ``None`` for anonymous).
    NO raw value ever reaches here — the floor proved the inbound bytes clean first."""

    report_id: str
    request_id: Optional[str]
    anchor: dict
    verifiable: bool
    redacted_repro: dict
    effort_signal: dict
    reputation: dict       # {verified: bool, pseudonymous_id, reputation_score, reason}

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "request_id": self.request_id,
            "anchor": self.anchor,
            "verifiable": self.verifiable,
            "redacted_repro": self.redacted_repro,
            "effort_signal": self.effort_signal,
            "reputation": self.reputation,
        }


@dataclass
class IntakeReceipt:
    """The outcome of :func:`intake` — value-free, safe to return AND to log.

    On a ``201/202`` this is the body a real endpoint would return: status, the
    anchor, the floor verdict, and the reputation summary. It NEVER contains a raw
    sensitive value (the floor residual is summarized by category only)."""

    report_id: str
    status: str                 # accepted | rejected
    reason: str                 # ok | residual_floor_leak | bad_anchor | ...
    anchor: dict
    verifiable: bool
    floor_clean: bool
    floor_residual: dict        # category summary only (no raw values)
    reputation: dict            # {verified, pseudonymous_id, reputation_score, reason}
    flags: list = field(default_factory=list)

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_ACCEPTED

    def to_dict(self) -> dict:
        return {
            "report_id": self.report_id,
            "status": self.status,
            "reason": self.reason,
            "anchor": self.anchor,
            "verifiable": self.verifiable,
            "floor_clean": self.floor_clean,
            "floor_residual": self.floor_residual,
            "reputation": self.reputation,
            "flags": list(self.flags),
        }


# ===========================================================================  #
# THE CORE HANDLER                                                              #
# ===========================================================================  #
def _new_report_id() -> str:
    return "rep-" + _uuid.uuid4().hex


def _floor_residual_summary(findings: list) -> dict:
    """Roll the floor findings up to a category summary — counts only, no raw value
    (uses :func:`redact.summarize_findings`, then strips any sample text)."""
    s = summarize_findings(findings)
    return {
        "total": s.get("total", len(findings)),
        "by_category": s.get("by_category", {}),
        "by_severity": s.get("by_severity", {}),
        "by_detector": s.get("by_detector", {}),
    }


def _resolve_anchor(sub: IntakeSubmission) -> tuple[dict, bool, Optional[str]]:
    """Resolve (anchor_dict, verifiable, reject_reason).

    A well-formed Anthropic ``req_…`` is the verifiable anchor. A provider that
    presents the deterministic-fingerprint fallback (Bedrock/Vertex) is accepted but
    flagged ``verifiable=False``. Anything else is a bad anchor (reject)."""
    rid = sub.request_id
    if isinstance(rid, str) and REQUEST_ID_RE.match(rid):
        return ({"type": "request_id", "request_id": rid, "provider": sub.provider,
                 "verifiable": True}, True, None)
    # No usable request-id: accept a deterministic fingerprint anchor if one is present.
    anc = sub.anchor or {}
    if isinstance(anc, dict) and anc.get("type") == "deterministic" and anc.get("fingerprint"):
        out = dict(anc)
        out["verifiable"] = False
        return (out, False, None)
    # A present-but-malformed request-id, or no anchor at all -> reject.
    return ({"type": "none", "request_id": rid, "provider": sub.provider,
             "verifiable": False}, False, REASON_BAD_ANCHOR)


def _verify_reputation(sub: IntakeSubmission, *, public_key: Optional[str],
                       revocation_list: Optional[Iterable[str]],
                       now: Optional[float]) -> tuple[dict, Optional[str]]:
    """Verify the OPTIONAL reputation token, bound to this submission's effort signal.

    Returns (reputation_summary, reject_reason). ``reject_reason`` is set only for an
    ACTIVE failure (tamper / lift / stale / revoked). A missing token -> anonymous
    (accepted). An unverifiable-by-design token (hmac, no shared secret) -> accepted
    UNCREDITED with the reason recorded."""
    tok = sub.reputation_token
    if not tok:
        return ({"verified": False, "pseudonymous_id": None, "reputation_score": None,
                 "reason": "no_token"}, None)
    res = REP.verify_token(tok, public_key=public_key, revocation_list=revocation_list,
                           effort_signal=sub.effort_signal, now=now)
    summary = {
        "verified": bool(res.get("valid")),
        "pseudonymous_id": res.get("pseudonymous_id"),
        "reputation_score": res.get("reputation_score") if res.get("valid") else None,
        "reason": res.get("reason"),
    }
    if res.get("valid"):
        return (summary, None)
    if res.get("reason") in _UNVERIFIABLE_TOKEN_REASONS:
        # Present but uncheckable here — accept, do not credit. (A real deployment that
        # holds the shared secret out-of-band would pass it as ``public_key``.)
        return (summary, None)
    # Active failure: a forged / lifted / stale / revoked credential -> fail closed.
    return (summary, REASON_BAD_REPUTATION_TOKEN)


def intake(
    submission: IntakeSubmission,
    *,
    sink: FeedbackSink,
    public_key: Optional[str] = None,
    revocation_list: Optional[Iterable[str]] = None,
    verify_reputation: bool = True,
    use_gliner: bool = False,
    report_id: Optional[str] = None,
    now: Optional[float] = None,
) -> IntakeReceipt:
    """The reference ``/v1/feedback`` intake step, end-to-end (see the module docstring).

    Validates the inbound ``claude_repro`` artifact and routes the outcome to ``sink``
    exactly once. FAIL-CLOSED on EVERY check: a malformed submission, a residual
    secret/PII in the inbound bytes, a bad anchor, or a forged reputation token all
    produce a ``rejected`` receipt and store NO redacted repro. Pure: ``submission``
    is never mutated."""
    rid = report_id or _new_report_id()

    def _reject(reason: str, anchor: dict, verifiable: bool, *, floor_clean: bool = True,
                floor_residual: Optional[dict] = None, reputation: Optional[dict] = None,
                flags: Optional[list] = None) -> IntakeReceipt:
        receipt = IntakeReceipt(
            report_id=rid, status=STATUS_REJECTED, reason=reason, anchor=anchor,
            verifiable=verifiable, floor_clean=floor_clean,
            floor_residual=floor_residual or {}, reputation=reputation or {},
            flags=flags or [reason],
        )
        sink.store(rid, receipt, None)   # reject: no AcceptedReport ever stored
        return receipt

    # (1) shape validation.
    if not isinstance(submission.redacted_repro, dict) or not submission.redacted_repro:
        return _reject(REASON_MALFORMED, {"type": "none", "verifiable": False}, False)

    # (2) FAIL-CLOSED deterministic floor over the inbound bytes — the same gate the
    #     SDK and server_side enforce. ANY residual secret/PII -> reject (never store).
    blob = submission.inbound_bytes()
    floor = scan_secrets(blob) + _scan_pii_regex(blob)
    if floor:
        return _reject(REASON_RESIDUAL_FLOOR_LEAK, {"type": "none", "verifiable": False},
                       False, floor_clean=False, floor_residual=_floor_residual_summary(floor))

    # (3) anchor verification — the ungameable request-id (or the deterministic fallback).
    anchor, verifiable, anchor_reason = _resolve_anchor(submission)
    if anchor_reason:
        return _reject(anchor_reason, anchor, verifiable)

    # (4) OPTIONAL reputation-token verification, bound to the effort signal.
    if verify_reputation:
        reputation, rep_reason = _verify_reputation(
            submission, public_key=public_key, revocation_list=revocation_list, now=now)
    else:
        reputation, rep_reason = ({"verified": False, "pseudonymous_id": None,
                                   "reputation_score": None, "reason": "skipped"}, None)
    if rep_reason:
        return _reject(rep_reason, anchor, verifiable, reputation=reputation)

    # (5) ACCEPT — build the privacy-clean report and route it to the sink.
    flags = []
    if not verifiable:
        flags.append("anchor_unverifiable")
    if reputation.get("verified"):
        flags.append("reputation_verified")
    elif submission.reputation_token:
        flags.append("reputation_unverified")

    accepted = AcceptedReport(
        report_id=rid, request_id=submission.request_id if verifiable else None,
        anchor=anchor, verifiable=verifiable,
        redacted_repro=submission.redacted_repro, effort_signal=submission.effort_signal,
        reputation=reputation,
    )
    receipt = IntakeReceipt(
        report_id=rid, status=STATUS_ACCEPTED, reason=REASON_OK, anchor=anchor,
        verifiable=verifiable, floor_clean=True, floor_residual={},
        reputation=reputation, flags=flags,
    )
    sink.store(rid, receipt, accepted)
    return receipt


def submission_from_artifact(artifact: Any, *, reputation_token: Optional[str] = None,
                             provider: Optional[str] = None) -> IntakeSubmission:
    """Build an :class:`IntakeSubmission` from a :class:`claude_repro.Artifact` (or any
    object/dict exposing ``redacted_repro`` / ``request_id`` / ``anchor`` /
    ``effort_signal``). This is the explicit composition seam between the SDK and the
    intake: ``intake(submission_from_artifact(art), sink=...)`` runs the whole loop.

    ``reputation_token`` overrides the token; otherwise it is read from the artifact's
    effort signal (where :func:`reputation.attach_reputation_token` puts it)."""
    def _read(name, default=None):
        if isinstance(artifact, dict):
            return artifact.get(name, default)
        return getattr(artifact, name, default)

    effort = _read("effort_signal") or {}
    if not isinstance(effort, dict):
        effort = {}
    tok = reputation_token or effort.get("reputation_token")
    anchor = _read("anchor")
    prov = provider or _read("provider") or (anchor or {}).get("provider") or "anthropic"
    return IntakeSubmission(
        redacted_repro=_read("redacted_repro") or {},
        request_id=_read("request_id"),
        effort_signal=effort,
        reputation_token=tok if isinstance(tok, str) else None,
        anchor=anchor if isinstance(anchor, dict) else None,
        provider=str(prov),
    )


# ===========================================================================  #
# REFERENCE ADAPTER — concrete, so the whole pipeline RUNS in-repo.             #
# (Swap it for a real implementation of the FeedbackSink PORT.)                 #
# ===========================================================================  #
class InMemoryFeedbackSink:
    """Reference :class:`FeedbackSink` that captures what WOULD be persisted — standing
    in for your real feedback store. Inspect via :attr:`records` / :attr:`last` /
    :attr:`accepted` / :meth:`get`."""

    def __init__(self):
        self.records: list[dict] = []

    def store(self, report_id: str, receipt: "IntakeReceipt",
              accepted: "Optional[AcceptedReport]") -> None:
        self.records.append({"report_id": report_id, "receipt": receipt, "accepted": accepted})

    @property
    def last(self) -> Optional[dict]:
        return self.records[-1] if self.records else None

    @property
    def accepted(self) -> list:
        """Only the records that were ACCEPTED (carry an AcceptedReport)."""
        return [r for r in self.records if r["accepted"] is not None]

    def get(self, report_id: str) -> Optional[dict]:
        for r in self.records:
            if r["report_id"] == report_id:
                return r
        return None

    def __len__(self) -> int:
        return len(self.records)


# ===========================================================================  #
# REFERENCE HTTP ENDPOINT — illustrative stdlib http.server handler.            #
# Shows the EXACT seam: a POST /v1/feedback -> intake.                          #
# This is NOT production HTTP plumbing — it is the wiring diagram an engineer    #
# reads to see where the step plugs in. Real deployments use their own router.  #
# ===========================================================================  #
FEEDBACK_PATH_RE = re.compile(r"^/v1/feedback/?$")

# HTTP statuses the reference returns (accepted -> 202; rejected validation -> 422).
HTTP_ACCEPTED = 202
HTTP_REJECTED = 422


def route(
    method: str,
    path: str,
    body: Any,
    *,
    sink: FeedbackSink,
    **intake_kw,
) -> tuple[int, dict]:
    """Pure routing of one request -> ``(status_code, json_body)``.

    Matches ``POST /v1/feedback``, parses the artifact body, and runs :func:`intake`.
    Returns ``202`` with the receipt on accept, ``422`` with the receipt on a
    validation reject (the body NEVER carries a raw value), ``404`` off-path, ``405``
    on a non-POST. Call this directly in a test, or let :func:`make_reference_handler`
    call it over a socket."""
    if method != "POST":
        return 405, {"error": "method not allowed"}
    if not FEEDBACK_PATH_RE.match(path):
        return 404, {"error": "not found"}
    submission = IntakeSubmission.from_request(body)
    receipt = intake(submission, sink=sink, **intake_kw)
    code = HTTP_ACCEPTED if receipt.accepted else HTTP_REJECTED
    return code, receipt.to_dict()


def make_reference_handler(sink: FeedbackSink, **intake_kw):
    """Build a :class:`BaseHTTPRequestHandler` subclass bound to the sink. ILLUSTRATIVE
    — it shows where the step plugs into an HTTP layer, nothing more."""

    class _IntakeHTTPRequestHandler(BaseHTTPRequestHandler):
        server_version = "fb-assist-reference-intake/0.1"

        def do_POST(self):  # noqa: N802 (stdlib naming)
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            status, payload = route(self.command, self.path, body, sink=sink, **intake_kw)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # keep the demo quiet
            pass

    return _IntakeHTTPRequestHandler


def make_reference_app(sink: FeedbackSink, *, host: str = "127.0.0.1", port: int = 0,
                       **intake_kw) -> HTTPServer:
    """An :class:`HTTPServer` wired to the sink (ephemeral port by default).
    ILLUSTRATIVE. Use ``server.server_address`` to discover the bound port, then
    ``server.serve_forever()`` (or ``handle_request()`` in a test)."""
    return HTTPServer((host, port), make_reference_handler(sink, **intake_kw))


# ===========================================================================  #
# Rendering for the demo (human-readable receipt)                              #
# ===========================================================================  #
def render_receipt(receipt: IntakeReceipt) -> str:
    lines = [
        f"report_id : {receipt.report_id}",
        f"status    : {receipt.status}  ({receipt.reason})",
        f"anchor    : type={receipt.anchor.get('type')}  verifiable={receipt.verifiable}"
        + (f"  request_id={receipt.anchor.get('request_id')}" if receipt.anchor.get('request_id') else ""),
        f"floor     : {'CLEAN ✅' if receipt.floor_clean else 'RESIDUAL ❌ ' + json.dumps(receipt.floor_residual.get('by_category', {}))}",
    ]
    rep = receipt.reputation or {}
    if rep.get("verified"):
        lines.append(f"reputation: VERIFIED  pid={rep.get('pseudonymous_id')}  score={rep.get('reputation_score')}")
    elif rep.get("reason") in (None, "no_token"):
        lines.append("reputation: anonymous (no token)")
    else:
        lines.append(f"reputation: unverified ({rep.get('reason')})")
    if receipt.flags:
        lines.append(f"flags     : {', '.join(receipt.flags)}")
    return "\n".join(lines)


# ===========================================================================  #
# CLI — python -m fb_assist.reference_intake [--leak] [--serve]                  #
# Demonstrates the WHOLE loop: claude_repro redacts+anchors -> this intake       #
# validates+accepts.                                                            #
# ===========================================================================  #
def _demo_pair() -> tuple[dict, dict]:
    """A synthetic Messages-API request/response with planted FAKE sentinels (no real
    data) — the same flavor the claude_repro tests use."""
    sk = "sk-ant-api03-" + "Zz9" * 14 + "qQ"
    request = {
        "model": "claude-sonnet-4-5", "max_tokens": 512,
        "system": "You are Acme Corp's assistant. Codename FALCON guards the vault.",
        "messages": [
            {"role": "user",
             "content": f"My key is {sk} and I'm at jane.doe@example.com. Why did the JSON fail?"},
            {"role": "assistant", "content": [{"type": "text", "text": "Let me look into it."}]},
        ],
    }
    response = {
        "id": "msg_01DEMO", "type": "message", "role": "assistant",
        "model": "claude-sonnet-4-5", "stop_reason": "end_turn",
        "usage": {"input_tokens": 42, "output_tokens": 12},
        "content": [{"type": "text", "text": "The schema mismatch is on the second field."}],
    }
    return request, response


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fb_assist.reference_intake",
        description="Reference /v1/feedback intake for claude_repro artifacts. Builds a "
                    "real claude_repro artifact from a SYNTHETIC pair, then validates it "
                    "through the intake — showing the whole loop. Reference-not-deployed; "
                    "zero Anthropic-internal assumptions.",
    )
    ap.add_argument("--leak", action="store_true",
                    help="inject a raw secret into the submission to show the floor reject it")
    ap.add_argument("--no-reputation", action="store_true",
                    help="skip minting/attaching a reputation token")
    ap.add_argument("--serve", action="store_true",
                    help="start the illustrative HTTP endpoint instead of a one-shot")
    ap.add_argument("--port", type=int, default=0, help="port for --serve (default: ephemeral)")
    ap.add_argument("--json", action="store_true", help="emit the receipt dict as JSON")
    args = ap.parse_args(argv)

    sink = InMemoryFeedbackSink()

    if args.serve:
        server = make_reference_app(sink, host="127.0.0.1", port=args.port)
        host, port = server.server_address
        print(f"[illustrative] reference intake endpoint listening at:\n  POST http://{host}:{port}/v1/feedback")
        print("  body: a claude_repro Artifact.to_dict()   (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            server.server_close()
        return 0

    # 1) SDK side — build a REAL claude_repro artifact (local redaction + request-id anchor).
    from . import claude_repro as CR

    request, response = _demo_pair()
    artifact = CR.redact_pair(request, response, "req_011CdemoANCHOR",
                              description="the model ignored my JSON schema", use_gliner=False)

    # 2) optional reputation token, bound to the artifact's effort signal (hermetic).
    public_key = None
    if not args.no_reputation:
        identity = REP._empty_reputation(REP.BACKEND, ts=1000.0)
        eff_with_tok = REP.attach_reputation_token(artifact.effort_signal, identity=identity)
        artifact.effort_signal = eff_with_tok
        public_key = None if REP._verify_key_is_public(identity["backend"]) else identity["verify_key"]

    submission = submission_from_artifact(artifact)

    # 3) optionally corrupt the submission to demonstrate the fail-closed floor.
    if args.leak:
        submission.redacted_repro = dict(submission.redacted_repro)
        submission.redacted_repro["_leak"] = "sk-ant-api03-" + "LEAK" * 12 + "ZZ"

    # 4) intake side — validate + accept (or reject).
    receipt = intake(submission, sink=sink, public_key=public_key)

    if args.json:
        print(json.dumps(receipt.to_dict(), indent=2, ensure_ascii=False))
    else:
        print("— claude_repro (SDK) —")
        print(f"  anchor: {json.dumps(artifact.anchor, ensure_ascii=False)}")
        print(f"  hard_gate_pass: {artifact.hard_gate_pass}")
        print()
        print("— reference_intake (/v1/feedback) —")
        print(render_receipt(receipt))
        print()
        print(f"sink stored: {len(sink)} record(s); accepted: {len(sink.accepted)}")

    return 0 if receipt.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
