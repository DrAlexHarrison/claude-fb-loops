"""fb_assist.server_side — a DROP-IN reference implementation of the server-side
consent-genericize step for the claude.ai thumbs-down / feedback flow.

WHAT THIS IS
------------
The claude.ai feedback flow has a privacy gap that can ONLY be closed server-side.
This module is not a *recommendation* of that fix — it is a runnable, framework-
agnostic, stdlib-only **reference implementation** of it. An Anthropic engineer
(or Anthropic's own internal Claude) implements **three small adapter methods**
against their real infrastructure, and the privacy-bearing core — genericize-
before-attach, the two-pass re-identification verify, the fail-closed egress gate,
and the audit record — is already done, tested, and reusable verbatim.

⚠️ THE PRINCIPLED BOUNDARY (load-bearing — read this before anything else) ⚠️
---------------------------------------------------------------------------
This file is built **only** against two PUBLICLY-OBSERVABLE, ToS-clean facts.
**ZERO Anthropic-internal knowledge is used or assumed.** We make NO claim about
how Anthropic stores conversations, captures consent, or persists feedback.

  1. **The feedback contract** (observed in an authenticated HAR of the author's
     OWN account — a public API surface):
         POST /api/organizations/<org>/chat_conversations/<conv>
              /chat_messages/<msg>/chat_feedback
         body: { "type": "...", "reason": "<text>" }   (~97 bytes, 201 Created)
     The conversation is identified **purely by the URL UUIDs** and is NEVER sent
     in the request body — the server attaches its own stored copy. THIS is why
     the fix must be server-side: there is no client-side seam to redact a
     conversation that never travels in the request. The referenced **message
     UUID** is the verifiable anchor here, exactly analogous to the Messages-API
     ``request-id``.

  2. **The conversation schema** (from the PUBLIC export feature,
     Settings → Privacy → Export — the same shape the stored copy uses):
         conversation { uuid, name, account, created_at, chat_messages[] }
         message      { uuid, sender ("human"|"assistant"), text, content[], ... }
         content[]    { type, text, ... }
     Message text is EITHER ``content[].text`` joined OR the bare top-level
     ``text`` — :func:`fb_assist.desktop_chat.message_text` handles both.

EVERYTHING this module cannot know about Anthropic's internals becomes an
**adapter PORT** — an abstract seam documented as "implement this against your
real infrastructure." There are exactly THREE:

  * :class:`ConversationStore` — ``fetch(org, conv, msg) -> Conversation``. The
    seam to wherever the real conversation lives. (We model only its *shape*, the
    public export schema; never its location or storage.)
  * :class:`ConsentPolicy`     — ``decision(user, org, conv) -> ConsentDecision``.
    Models the NEW product surface: the user's consent + genericize preference.
  * :class:`FeedbackSink`      — ``attach(feedback_id, artifact, audit) -> None``.
    The seam to wherever the real feedback record is persisted.

THE FLOW (against the observable contract)
------------------------------------------
:func:`handle_feedback` is the whole step:

  1. Parse the feedback ``event`` ({org, conversation, message, type, reason}).
  2. Ask ``consent.decision(...)``.
       * ``none``       -> attach only ``{type, reason}`` (today's behavior, the
                          privacy-safe default — no conversation text leaves).
       * ``genericized`` -> ``store.fetch(...)`` the conversation, run
                          **genericize-before-attach** (reuse the ``redact`` floor
                          + the ``genericize`` two-pass verify), attach the
                          SANITIZED artifact + a redaction/effort summary + the
                          message-UUID anchor.
       * ``raw``        -> explicit power-user opt-in: attach raw, flagged.
  3. Emit an **audit record**: which categories were redacted, the re-id verify
     verdict, the consent basis, and the anchor.
  4. **HARD FAIL-CLOSED GATE**: never attach a ``genericized`` artifact whose
     deterministic floor (``scan_secrets`` + ``_scan_pii_regex``) over the actual
     OUTBOUND BYTES is non-empty. On any residue (or a failed re-id verify) the
     step **fails closed** — it attaches ``none`` + a flag rather than leak.

Reference adapters (:class:`InMemoryConversationStore`, :class:`StaticConsentPolicy`,
:class:`InMemoryFeedbackSink`) make the whole thing RUN in-repo, and
:func:`make_reference_app` shows the exact ``POST .../chat_feedback`` seam with a
stdlib ``http.server`` handler (clearly labeled illustrative). Run the demo:

    python -m fb_assist.server_side --export <conversations.json> --feedback '{...}'

LOCAL ONLY. No network egress (the optional GLiNER PII pass is off by default).
Pure forward-transform: the fetched conversation is never mutated; every redaction
builds new strings / deep copies.
"""

from __future__ import annotations

import os

# Mirror redact.py / genericize.py: force the torch-only path so importing the
# sibling redactor (which lazy-loads NER) never explodes on a TF import under
# Keras 3. Set before redact is imported below.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import argparse
import copy
import json
import re
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Protocol, runtime_checkable

# Reuse the export-schema parser + text accessor from the sibling chat edge.
from .desktop_chat import (
    DEFAULT_FIXTURE as _CHAT_FIXTURE,
    Conversation,
    Message,
    message_text,
    parse_export,
    select_conversation,
)
from .genericize import verify_genericization
from .package import _render_effort_footer
from .redact import (
    Finding,
    _scan_paths_text,
    _scan_pii_regex,
    _token_label,
    apply_redactions,
    leak_scan,
    merge_redaction_spans,
    scan_pii,
    scan_secrets,
)

__all__ = [
    # ports (the three seams an Anthropic engineer implements)
    "ConversationStore",
    "ConsentPolicy",
    "FeedbackSink",
    # data model
    "FeedbackEvent",
    "ConsentDecision",
    "GenericizeResult",
    "FeedbackArtifact",
    "AuditRecord",
    "FeedbackResult",
    # the core handler + the genericize-before-attach core
    "handle_feedback",
    "genericize_for_attach",
    # reference adapters (so the whole thing runs in-repo)
    "InMemoryConversationStore",
    "StaticConsentPolicy",
    "InMemoryFeedbackSink",
    "default_consent_policy",
    # reference HTTP endpoint (illustrative)
    "FEEDBACK_PATH_RE",
    "route",
    "make_reference_handler",
    "make_reference_app",
    # constants
    "ATTACH_NONE",
    "ATTACH_GENERICIZED",
    "ATTACH_RAW",
    "DEFAULT_FIXTURE",
    "main",
]

# The synthetic fixture the demo defaults to (NO real data ever touched).
DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample-feedback-conversation.json"
)

# Consent dispositions.
ATTACH_NONE = "none"
ATTACH_GENERICIZED = "genericized"
ATTACH_RAW = "raw"
_VALID_ATTACH = (ATTACH_NONE, ATTACH_GENERICIZED, ATTACH_RAW)

# Result statuses.
STATUS_NONE = "attached_none"
STATUS_GENERICIZED = "attached_genericized"
STATUS_RAW = "attached_raw"
STATUS_FAILED_CLOSED = "failed_closed"

# Severity ordering for the summaries (mirrors redact._SEV_RANK).
_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# ===========================================================================  #
# PORTS — the three adapter seams.  Implement these against your real stack.    #
# ===========================================================================  #
@runtime_checkable
class ConversationStore(Protocol):
    """PORT 1 — your conversation store.

    ``fetch`` returns the stored conversation **in the public export schema**
    (:class:`fb_assist.desktop_chat.Conversation`) identified by the URL UUIDs
    from the feedback contract. Implement it against wherever your conversations
    actually live; this reference makes NO assumption about that location or
    storage engine — only about the returned *shape*, which is the public export
    schema. Return ``None`` if the conversation is unavailable (the handler then
    fails closed to an ``attach="none"`` artifact)."""

    def fetch(self, org_id: str, conversation_id: str, message_id: str) -> Optional[Conversation]:
        ...


@runtime_checkable
class ConsentPolicy(Protocol):
    """PORT 2 — the NEW product surface: consent + genericize preference.

    ``decision`` returns a :class:`ConsentDecision` capturing whether the user has
    consented to attach conversation context to this feedback, and at what fidelity
    (``none`` / ``genericized`` / ``raw``). Implement it against your real consent
    capture (a per-account default, a per-conversation toggle shown in the
    thumbs-down sheet, an org policy, …). The reference default returns
    ``genericized`` — the privacy-preserving middle that makes the feedback useful
    without shipping raw user content."""

    def decision(self, user_id: Optional[str], org_id: str, conversation_id: str) -> "ConsentDecision":
        ...


@runtime_checkable
class FeedbackSink(Protocol):
    """PORT 3 — your feedback record.

    ``attach`` persists the sanitized :class:`FeedbackArtifact` + the
    :class:`AuditRecord` onto your feedback record (keyed by ``feedback_id``).
    Implement it against wherever feedback is actually stored; this reference makes
    NO assumption about that store. The artifact is already privacy-clean by the
    time it reaches here — your job is only to durably write it."""

    def attach(self, feedback_id: str, artifact: "FeedbackArtifact", audit: "AuditRecord") -> None:
        ...


# ===========================================================================  #
# DATA MODEL                                                                    #
# ===========================================================================  #
@dataclass
class FeedbackEvent:
    """One feedback submission, in the OBSERVABLE contract shape.

    ``org_id`` / ``conversation_id`` / ``message_id`` come from the request **URL**
    (never the body — that is the whole point); ``type`` / ``reason`` are the
    ~97-byte JSON body; ``user_id`` is whatever your auth layer already knows about
    the caller (the body never carries it)."""

    org_id: str
    conversation_id: str
    message_id: str
    type: str = ""
    reason: str = ""
    user_id: Optional[str] = None

    @classmethod
    def from_request(
        cls,
        *,
        org_id: str,
        conversation_id: str,
        message_id: str,
        body: Any,
        user_id: Optional[str] = None,
    ) -> "FeedbackEvent":
        """Build from the URL path parts + the parsed JSON body ``{type, reason}``."""
        if isinstance(body, (bytes, bytearray)):
            body = body.decode("utf-8", "replace")
        if isinstance(body, str):
            try:
                body = json.loads(body or "{}")
            except (json.JSONDecodeError, ValueError):
                body = {}
        if not isinstance(body, dict):
            body = {}
        return cls(
            org_id=str(org_id),
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            type=str(body.get("type", "")),
            reason=str(body.get("reason", "")),
            user_id=user_id,
        )

    @property
    def anchor(self) -> dict:
        """The verifiable reference: the URL UUIDs. The ``message_id`` ties the
        report to a real stored message with zero extra user content — analogous to
        the Messages-API ``request-id`` anchor on the API surface."""
        return {
            "org_id": self.org_id,
            "conversation_id": self.conversation_id,
            "message_id": self.message_id,
        }


@dataclass
class ConsentDecision:
    """The consent + genericize preference for one feedback event.

    ``attach`` is the disposition (``none`` / ``genericized`` / ``raw``). ``scope``
    is opaque to this reference (e.g. ``"message"`` vs ``"conversation"`` — how much
    context the user agreed to share); your policy interprets it. The remaining
    fields are reference conveniences carried into the audit:

      * ``basis``           — human-readable provenance of the consent (for audit).
      * ``reason``          — why the policy chose this disposition.
      * ``genericize_terms`` — caller/profile-named codenames or IP strings that
        MUST NOT survive (fed to ``verify_genericization(expect_absent=...)`` and
        masked literally). This is how org-specific semantic IP — which no regex or
        NER catches — is removed and *verified absent* even in this LLM-free
        reference. In production, your Claude-powered rewrite handles the open-ended
        semantic layer; this list is the deterministic, auditable backstop."""

    attach: str = ATTACH_GENERICIZED
    scope: str = "message"
    basis: str = ""
    reason: str = ""
    genericize_terms: list = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.attach not in _VALID_ATTACH:
            raise ValueError(f"unknown attach {self.attach!r}; valid = {_VALID_ATTACH}")


@dataclass
class GenericizeResult:
    """Output of the genericize-before-attach core (:func:`genericize_for_attach`).

    ``rendered`` is the OUTBOUND BYTES — the exact text the egress gate scans and
    that would be attached. ``conversation`` is a round-trippable, export-shaped
    dict with each message narrative replaced by its redacted text."""

    rendered: str
    conversation: dict
    redaction_map: list
    counts: dict
    effort_signal: dict
    reid_ok: bool                 # two-pass verify_genericization passed for every changed turn
    meaning_risk_flags: list
    leak_candidates: list
    floor_clean: bool             # the genericizer's own floor check (advisory)
    floor_residual: list


@dataclass
class FeedbackArtifact:
    """What gets attached to the feedback record — privacy-clean by construction.

    For ``attach="none"`` only ``{type, reason, anchor}`` are populated (no
    conversation text). For ``attach="genericized"`` the sanitized, export-shaped
    ``conversation`` + ``rendered`` markdown + ``effort_signal`` + ``redaction_summary``
    are filled. For ``attach="raw"`` the raw conversation is attached with a loud
    flag."""

    type: str
    reason: str
    anchor: dict
    attach: str
    conversation: Optional[dict] = None
    rendered: Optional[str] = None
    effort_signal: Optional[dict] = None
    redaction_summary: Optional[dict] = None
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "reason": self.reason,
            "anchor": self.anchor,
            "attach": self.attach,
            "conversation": self.conversation,
            "rendered": self.rendered,
            "effort_signal": self.effort_signal,
            "redaction_summary": self.redaction_summary,
            "flags": list(self.flags),
        }


@dataclass
class AuditRecord:
    """The audit trail persisted alongside the artifact.

    Carries ONLY categories + verdicts + the anchor — never a raw sensitive value —
    so the audit itself is safe to store and review."""

    feedback_id: str
    anchor: dict
    consent_attach: str           # what consent asked for
    consent_basis: str
    consent_scope: str
    attached: str                 # what was ACTUALLY attached (differs on fail-closed)
    redacted_categories: dict     # entity -> count
    redaction_count: int
    reid_verdict: bool            # two-pass re-identification verify ok
    floor_clean: bool             # deterministic floor over the outbound bytes was empty
    leak_candidates: int          # soft NER candidates (advisory; not a gate)
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feedback_id": self.feedback_id,
            "anchor": self.anchor,
            "consent": {
                "requested": self.consent_attach,
                "basis": self.consent_basis,
                "scope": self.consent_scope,
            },
            "attached": self.attached,
            "redacted_categories": self.redacted_categories,
            "redaction_count": self.redaction_count,
            "reid_verify_ok": self.reid_verdict,
            "floor_clean": self.floor_clean,
            "leak_candidates": self.leak_candidates,
            "flags": list(self.flags),
        }


@dataclass
class FeedbackResult:
    """The outcome of :func:`handle_feedback` (also what was handed to the sink)."""

    feedback_id: str
    status: str
    artifact: FeedbackArtifact
    audit: AuditRecord

    def to_public_dict(self) -> dict:
        """The minimal body a ``201 Created`` would return — NO conversation text,
        only the status, the anchor, the disposition, and the audit summary."""
        return {
            "feedback_id": self.feedback_id,
            "status": self.status,
            "attach": self.artifact.attach,
            "anchor": self.artifact.anchor,
            "flags": list(self.artifact.flags),
            "audit": self.audit.to_dict(),
        }


# A genericize-before-attach function: (conversation, decision) -> GenericizeResult.
# The default is :func:`genericize_for_attach` (the deterministic reference core).
# This is the seam where Anthropic plugs in a Claude-powered semantic rewrite — the
# hard fail-closed gate in :func:`handle_feedback` is enforced over whatever it
# returns, so a stronger genericizer can only ever make the output *safer*.
GenericizeFn = Callable[[Conversation, ConsentDecision], GenericizeResult]


# ===========================================================================  #
# The genericize-before-attach CORE (reuse redact floor + genericize verify)    #
# ===========================================================================  #
def _term_findings(text: str, terms: Iterable[str]) -> list[Finding]:
    """Literal codename / IP-string findings (case-insensitive) so org-specific
    semantic IP — which no regex or NER catches — is masked deterministically and
    can be *verified absent*. Each becomes a high-severity ``IP_CODENAME`` span."""
    out: list[Finding] = []
    low = text.lower()
    for term in terms:
        t = (term or "").strip()
        if not t:
            continue
        tl = t.lower()
        start = 0
        while True:
            i = low.find(tl, start)
            if i < 0:
                break
            out.append(Finding("term", "ip", "IP_CODENAME", text[i:i + len(t)],
                               i, i + len(t), 1.0, "high"))
            start = i + len(t)
    return out


def _mask_text(text: str, terms: Iterable[str], use_gliner: bool) -> str:
    """Detect (secrets + PII + paths + literal codenames) and mask, meaning-preserving."""
    findings = (
        scan_secrets(text)
        + scan_pii(text, use_gliner=use_gliner)
        + _scan_paths_text(text)
        + _term_findings(text, terms)
    )
    masked, _ = apply_redactions(text, findings, style="mask")
    return masked


def _outbound_bytes(rendered: str, conversation: dict) -> str:
    """The full attached payload as text — the rendered markdown AND the round-trip
    conversation dict. The egress gate scans THIS (not just the markdown), so the
    fail-closed guarantee covers the entire artifact."""
    return rendered + "\n" + json.dumps(conversation, ensure_ascii=False)


def _render_markdown(name: str, turns: Iterable[tuple]) -> str:
    """Render the post-redaction conversation as share-ready markdown — the exact
    output bytes the egress gate scans. ``turns`` items: (index, role, uuid, after)."""
    lines = [f"# {name}", ""]
    for _i, role, _uuid_, after in turns:
        label = "Human" if role == "user" else "Assistant"
        lines.append(f"### {label}")
        lines.append(after)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _genericized_dict(conv: Conversation, after_by_uuid: dict,
                      terms: Iterable[str], use_gliner: bool) -> dict:
    """A round-trippable, fully-scrubbed deep copy of the source conversation dict.

    Message bodies reuse the already-computed per-turn redactions (``after_by_uuid``);
    the envelope free-text (``name``, ``summary``), account identity strings, and any
    attachment ``extracted_content`` / ``file_name`` are masked with the SAME
    redactor — so no sensitive value survives ANYWHERE in the attached conversation,
    not just in the message bodies. Pure: ``conv.raw`` is deep-copied, never mutated.
    (Targeted, not a per-leaf NER sweep — that would multiply the Presidio cost.)"""
    out = copy.deepcopy(conv.raw)

    msgs = out.get("chat_messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            red = after_by_uuid.get(str(m.get("uuid", "")))
            if red is not None:
                m["text"] = red
                # Collapse content[] to a single redacted block so no original block
                # text survives in the round-trip.
                if isinstance(m.get("content"), list):
                    m["content"] = [{"type": "text", "text": red}]
            # Attachment payloads can carry extracted text even when the body is empty.
            for att in (m.get("attachments") or []):
                if isinstance(att, dict):
                    for k in ("extracted_content", "file_name"):
                        v = att.get(k)
                        if isinstance(v, str) and v.strip():
                            att[k] = _mask_text(v, terms, use_gliner)

    for k in ("name", "summary"):
        v = out.get(k)
        if isinstance(v, str) and v.strip():
            out[k] = _mask_text(v, terms, use_gliner)

    acct = out.get("account")
    if isinstance(acct, dict):
        for k, v in list(acct.items()):
            if isinstance(v, str) and v.strip():
                acct[k] = _mask_text(v, terms, use_gliner)

    return out


def _genericize_turn(callback: Callable[[str, dict], str], before: str, after: str,
                     msg: Message, role: str, terms: list, use_gliner: bool,
                     stats: Optional[dict]) -> str:
    """Run ONE turn through the caller's semantic-genericize callback + the two-pass
    ``verify_genericization`` gate (with ``expect_absent`` = the codename terms).
    FAIL-CLOSED: returns the caller's rewrite ONLY when the gate proves nothing
    recoverable survived; otherwise returns the deterministic ``after`` unchanged.

    The callback is handed ``after`` (post-deterministic-mask — a raw secret never
    reaches the caller's model); the gate runs against the TRUE ``before`` so a
    rewrite that re-introduced any original value is caught. Only leak-free signals
    (counts + masked re-id findings) are recorded in ``stats`` — never a raw value."""
    ctx = {"role": role, "uuid": msg.uuid, "terms": list(terms)}
    try:
        generic = callback(after, ctx)
    except Exception as exc:  # a misbehaving callback must never break the transform
        if stats is not None:
            stats["rejected"] += 1
            stats["spans"].append({"uuid": msg.uuid, "role": role, "accepted": False,
                                   "reason": f"callback_error:{type(exc).__name__}"})
        return after
    if not isinstance(generic, str) or generic == after:
        if stats is not None:
            stats["spans"].append({"uuid": msg.uuid, "role": role, "accepted": False,
                                   "reason": "noop" if isinstance(generic, str) else "non_str"})
            if not isinstance(generic, str):
                stats["rejected"] += 1
        return after
    vg = verify_genericization(before, generic, expect_absent=terms, use_gliner=use_gliner)
    if vg.get("ok"):
        if stats is not None:
            stats["applied"] += 1
            stats["spans"].append({"uuid": msg.uuid, "role": role, "accepted": True, "reason": "ok"})
        return generic
    if stats is not None:
        stats["rejected"] += 1
        stats["spans"].append({
            "uuid": msg.uuid, "role": role, "accepted": False, "reason": "verify_failed",
            "leaked_originals": len(vg.get("leaked_originals", [])),
            "expect_absent_hits": len(vg.get("expect_absent_hits", [])),
            "reid_findings": vg.get("reid_findings", []),  # reveal=False — masked, safe
        })
    return after


def genericize_for_attach(
    conv: Conversation,
    decision: ConsentDecision,
    *,
    level: str = "genericize",
    use_gliner: bool = False,
    verify: bool = True,
    quality: int = 4,
    alignment_confidence: int = 5,
    reputation_token: Optional[str] = None,
    genericize: Optional[Callable[[str, dict], str]] = None,
) -> GenericizeResult:
    """Genericize ONE conversation into a privacy-safe artifact — the reference core.

    Forward-transform only (``conv`` / ``conv.raw`` never mutated). For each turn:

      1. read the narrative via :func:`message_text` (both export shapes),
      2. detect with ``scan_secrets`` + ``scan_pii`` + literal ``genericize_terms``
         (the codename backstop),
      3. mask the chosen non-overlapping spans with ``apply_redactions`` (meaning
         survives; values become ``‹MARKERS›``),
      3b. OPTIONAL semantic genericize (C8): if a ``genericize`` callback is given,
         the caller's own Claude/LLM rewrites the already-masked turn and the
         ``verify_genericization`` gate runs over its output — fail-closed to the
         deterministic mask on any surviving leak,
      4. prove the rewrite leaked nothing recoverable via ``verify_genericization``
         (the two-pass re-identification bar, with ``expect_absent`` = the codenames).

    ``genericize`` is the per-turn no-live-Claude seam — the SAME ``(text, ctx) -> text``
    shape as :func:`fb_assist.claude_repro.redact_pair`'s ``genericize`` callback, so
    the API and claude.ai-server surfaces behave identically. It is handed the
    deterministically-masked turn (never a raw secret) and a ``ctx`` of
    ``{role, uuid, terms}``; its rewrite only ships if the two-pass verify proves
    nothing recoverable survived. (This is the convenient per-turn analog of the
    whole-pipeline ``genericize=`` GenericizeFn seam on :func:`handle_feedback`.)

    Returns a :class:`GenericizeResult` carrying the outbound bytes + the audit
    inputs. The authoritative fail-closed gate lives in :func:`handle_feedback`,
    which re-runs the deterministic floor over ``rendered`` regardless of what this
    function reports — so this core is *trusted but verified*.
    """
    terms = list(decision.genericize_terms or [])
    semantic_stats: Optional[dict] = (
        {"used": True, "applied": 0, "rejected": 0, "spans": []}
        if genericize is not None else None
    )
    rendered_turns: list[tuple] = []      # (index, role, uuid, after)
    after_by_uuid: dict[str, str] = {}
    redaction_map: list[dict] = []
    meaning_risk: list[dict] = []
    reid_ok = True
    n_human = n_assistant = 0
    by_category: dict[str, int] = {}
    by_severity: dict[str, int] = {}

    for i, msg in enumerate(conv.messages):
        role = msg.role
        if role == "user":
            n_human += 1
        else:
            n_assistant += 1

        before = message_text(msg)
        if not before.strip():
            continue  # attachment-only / empty turn — nothing to show or redact

        findings = (
            scan_secrets(before)
            + scan_pii(before, use_gliner=use_gliner)
            + _scan_paths_text(before)          # filesystem paths leak usernames + internal layout
            + _term_findings(before, terms)
        )
        chosen = merge_redaction_spans(findings)
        after, _ = apply_redactions(before, findings, style="mask")

        # (3b) OPTIONAL semantic genericize — the caller's own Claude/LLM rewrites the
        #      already-masked turn; verify_genericization gates it, fail-closed to `after`.
        if genericize is not None and after.strip():
            after = _genericize_turn(genericize, before, after, msg, role, terms,
                                     use_gliner, semantic_stats)

        rendered_turns.append((i, role, msg.uuid, after))
        after_by_uuid[str(msg.uuid)] = after

        for f in chosen:
            label = _token_label(f.entity)
            redaction_map.append({
                "uuid": msg.uuid,
                "role": role,
                "category": f.entity,
                "severity": f.severity,
                "detector": f.detector,
                "replacement": f"‹{label}›",
                "count": 1,
            })
            by_category[f.entity] = by_category.get(f.entity, 0) + 1
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

        if verify and after != before:
            vg = verify_genericization(before, after, expect_absent=terms, use_gliner=use_gliner)
            if not vg["ok"]:
                reid_ok = False
            for flag in vg.get("meaning_risk_flags", []):
                meaning_risk.append({"uuid": msg.uuid, **flag})

    rendered = _render_markdown(conv.name, rendered_turns)
    conversation = _genericized_dict(conv, after_by_uuid, terms, use_gliner)
    turns_redacted = len({e["uuid"] for e in redaction_map})

    # The genericizer's own floor check over the FULL outbound payload (advisory;
    # handle_feedback re-checks authoritatively).
    outbound = _outbound_bytes(rendered, conversation)
    floor = scan_secrets(outbound) + _scan_pii_regex(outbound)
    floor_clean = len(floor) == 0
    floor_residual = [f.to_dict(reveal=False) for f in floor]

    leak = leak_scan(outbound, use_gliner=use_gliner)

    counts = {
        "messages": len(conv.messages),
        "human": n_human,
        "assistant": n_assistant,
        "turns_rendered": len(rendered_turns),
        "turns_redacted": turns_redacted,
        "redactions": len(redaction_map),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "by_severity": dict(sorted(by_severity.items(), key=lambda kv: -_SEV_RANK.get(kv[0], 1))),
    }

    effort_signal = {
        "surface": "claude.ai-server",
        "redaction": level,
        "quality": quality,
        "alignment_confidence": alignment_confidence,
        "reputation_token": reputation_token,
        "anchor": {"conversation_uuid": conv.uuid},
        "summary": {
            "redactions": len(redaction_map),
            "by_severity": counts["by_severity"],
            "floor_clean": floor_clean,
            "genericize_verified": reid_ok,
        },
    }
    # C8 — record the optional semantic-genericize pass (categories/counts/verdicts
    # only; never a raw value), present only when a genericize callback was supplied.
    if semantic_stats is not None:
        effort_signal["semantic_genericize"] = semantic_stats

    return GenericizeResult(
        rendered=rendered,
        conversation=conversation,
        redaction_map=redaction_map,
        counts=counts,
        effort_signal=effort_signal,
        reid_ok=reid_ok,
        meaning_risk_flags=meaning_risk,
        leak_candidates=[f.to_dict(reveal=False) for f in leak],
        floor_clean=floor_clean,
        floor_residual=floor_residual,
    )


# ===========================================================================  #
# THE CORE HANDLER                                                              #
# ===========================================================================  #
def _new_feedback_id() -> str:
    return "fb-" + _uuid.uuid4().hex


def _raw_markdown(conv: Conversation) -> str:
    lines = [f"# {conv.name}", ""]
    for msg in conv.messages:
        body = message_text(msg)
        if not body.strip():
            continue
        label = "Human" if msg.role == "user" else "Assistant"
        lines.append(f"### {label}")
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def handle_feedback(
    event: FeedbackEvent,
    *,
    store: ConversationStore,
    consent: ConsentPolicy,
    sink: FeedbackSink,
    genericize: GenericizeFn = genericize_for_attach,
    level: str = "genericize",
    use_gliner: bool = False,
    verify: bool = True,
    feedback_id: Optional[str] = None,
) -> FeedbackResult:
    """The server-side consent-genericize step, end-to-end.

    See the module docstring for the flow. The privacy guarantees enforced here:

      * **none**       -> attach only ``{type, reason}``; no conversation text ever
        leaves (today's behavior, the privacy-safe default).
      * **genericized** -> ``store.fetch`` + ``genericize`` + the **HARD fail-closed
        gate**: the deterministic floor (``scan_secrets`` + ``_scan_pii_regex``)
        is re-run HERE over the actual outbound bytes; non-empty (or a failed re-id
        verify) -> attach ``none`` + a flag, never the leaky artifact.
      * **raw**        -> explicit opt-in; attach raw with a loud flag (no gate — raw
        is raw by definition; the audit records that no redaction occurred).

    Always emits an :class:`AuditRecord` and calls ``sink.attach`` exactly once.
    """
    fid = feedback_id or _new_feedback_id()
    decision = consent.decision(event.user_id, event.org_id, event.conversation_id)
    anchor = event.anchor

    def _finish(status: str, artifact: FeedbackArtifact, audit: AuditRecord) -> FeedbackResult:
        sink.attach(fid, artifact, audit)
        return FeedbackResult(feedback_id=fid, status=status, artifact=artifact, audit=audit)

    # ---- attach=none : the privacy-safe default (today's behavior). ----------
    if decision.attach == ATTACH_NONE:
        artifact = FeedbackArtifact(type=event.type, reason=event.reason, anchor=anchor,
                                    attach=ATTACH_NONE)
        audit = AuditRecord(
            feedback_id=fid, anchor=anchor, consent_attach=decision.attach,
            consent_basis=decision.basis, consent_scope=decision.scope,
            attached=ATTACH_NONE, redacted_categories={}, redaction_count=0,
            reid_verdict=True, floor_clean=True, leak_candidates=0, flags=[],
        )
        return _finish(STATUS_NONE, artifact, audit)

    # Both genericized + raw need the stored conversation.
    conv = store.fetch(event.org_id, event.conversation_id, event.message_id)
    if conv is None:
        # Can't fetch -> fail closed to none (never block feedback on a fetch miss).
        flags = ["conversation_unavailable", "fail_closed"]
        artifact = FeedbackArtifact(type=event.type, reason=event.reason, anchor=anchor,
                                    attach=ATTACH_NONE, flags=flags)
        audit = AuditRecord(
            feedback_id=fid, anchor=anchor, consent_attach=decision.attach,
            consent_basis=decision.basis, consent_scope=decision.scope,
            attached=ATTACH_NONE, redacted_categories={}, redaction_count=0,
            reid_verdict=True, floor_clean=True, leak_candidates=0, flags=flags,
        )
        return _finish(STATUS_FAILED_CLOSED, artifact, audit)

    # ---- attach=raw : explicit power-user opt-in (no gate). ------------------
    if decision.attach == ATTACH_RAW:
        rendered = _raw_markdown(conv)
        flags = ["raw_optin", "no_redaction"]
        artifact = FeedbackArtifact(
            type=event.type, reason=event.reason, anchor=anchor, attach=ATTACH_RAW,
            conversation=copy.deepcopy(conv.raw), rendered=rendered, flags=flags,
        )
        audit = AuditRecord(
            feedback_id=fid, anchor=anchor, consent_attach=decision.attach,
            consent_basis=decision.basis, consent_scope=decision.scope,
            attached=ATTACH_RAW, redacted_categories={}, redaction_count=0,
            reid_verdict=True, floor_clean=False, leak_candidates=0, flags=flags,
        )
        return _finish(STATUS_RAW, artifact, audit)

    # ---- attach=genericized : genericize-before-attach + HARD fail-closed gate.
    # Forward the handler's knobs to the DEFAULT core; a custom genericizer keeps the
    # clean 2-arg (conversation, decision) seam.
    if genericize is genericize_for_attach:
        g = genericize_for_attach(conv, decision, level=level, use_gliner=use_gliner, verify=verify)
    else:
        g = genericize(conv, decision)

    # AUTHORITATIVE gate: re-run the deterministic floor over the ACTUAL outbound
    # bytes — the rendered markdown AND the round-trip conversation dict — without
    # trusting the genericizer's self-report. Empty AND the two-pass re-id verify
    # passed == ship-able; anything else fails closed.
    outbound = _outbound_bytes(g.rendered, g.conversation or {})
    floor = scan_secrets(outbound) + _scan_pii_regex(outbound)
    floor_clean = len(floor) == 0
    gate_ok = floor_clean and g.reid_ok

    redacted_categories = dict(g.counts.get("by_category", {}))
    leak_n = len(g.leak_candidates)

    if not gate_ok:
        flags = ["fail_closed"]
        if not floor_clean:
            flags.append("residual_floor_leak")
        if not g.reid_ok:
            flags.append("reid_verify_failed")
        # Fail closed: ship the privacy-safe {type, reason} artifact instead of the
        # leaky one. The audit records WHY (categories + verdicts), never the value.
        artifact = FeedbackArtifact(type=event.type, reason=event.reason, anchor=anchor,
                                    attach=ATTACH_NONE, flags=flags)
        audit = AuditRecord(
            feedback_id=fid, anchor=anchor, consent_attach=decision.attach,
            consent_basis=decision.basis, consent_scope=decision.scope,
            attached=ATTACH_NONE, redacted_categories=redacted_categories,
            redaction_count=g.counts.get("redactions", 0),
            reid_verdict=g.reid_ok, floor_clean=floor_clean, leak_candidates=leak_n,
            flags=flags,
        )
        return _finish(STATUS_FAILED_CLOSED, artifact, audit)

    # Clean — attach the genericized artifact + summary + anchor.
    artifact = FeedbackArtifact(
        type=event.type, reason=event.reason, anchor=anchor, attach=ATTACH_GENERICIZED,
        conversation=g.conversation, rendered=g.rendered,
        effort_signal=g.effort_signal, redaction_summary=g.counts,
        flags=(["meaning_risk_flagged"] if g.meaning_risk_flags else []),
    )
    audit = AuditRecord(
        feedback_id=fid, anchor=anchor, consent_attach=decision.attach,
        consent_basis=decision.basis, consent_scope=decision.scope,
        attached=ATTACH_GENERICIZED, redacted_categories=redacted_categories,
        redaction_count=g.counts.get("redactions", 0),
        reid_verdict=g.reid_ok, floor_clean=floor_clean, leak_candidates=leak_n,
        flags=list(artifact.flags),
    )
    return _finish(STATUS_GENERICIZED, artifact, audit)


# ===========================================================================  #
# REFERENCE ADAPTERS — concrete, so the whole pipeline RUNS in-repo.            #
# (Swap each for a real implementation of the matching PORT.)                   #
# ===========================================================================  #
class InMemoryConversationStore:
    """Reference :class:`ConversationStore`. Loads a claude.ai export (the public
    schema) and serves conversations by UUID — standing in for your real store.

    Build it with :meth:`from_export` (a path / loaded list) or pass an iterable of
    :class:`Conversation`. ``fetch`` ignores ``org_id`` / ``message_id`` for lookup
    (it keys on the conversation UUID, like the real URL would) but a real store
    would naturally scope by org and could validate the message exists."""

    def __init__(self, conversations: Iterable[Conversation]):
        self._by_uuid: dict[str, Conversation] = {}
        for c in conversations:
            self._by_uuid[str(c.uuid)] = c

    @classmethod
    def from_export(cls, source) -> "InMemoryConversationStore":
        return cls(parse_export(source))

    def fetch(self, org_id: str, conversation_id: str, message_id: str) -> Optional[Conversation]:
        return self._by_uuid.get(str(conversation_id))

    def __len__(self) -> int:
        return len(self._by_uuid)


class StaticConsentPolicy:
    """Reference :class:`ConsentPolicy` that returns one fixed
    :class:`ConsentDecision` for everyone — standing in for your real consent
    capture. Construct with a decision, or with kwargs for convenience::

        StaticConsentPolicy(ConsentDecision(attach="genericized"))
        StaticConsentPolicy(attach="genericized", genericize_terms=["Project Halcyon"])
    """

    def __init__(self, decision: Optional[ConsentDecision] = None, **kw):
        if decision is None:
            kw.setdefault("basis", "reference StaticConsentPolicy")
            decision = ConsentDecision(**kw)
        self._decision = decision

    def decision(self, user_id: Optional[str], org_id: str, conversation_id: str) -> ConsentDecision:
        return self._decision


class InMemoryFeedbackSink:
    """Reference :class:`FeedbackSink` that captures what WOULD be persisted —
    standing in for your real feedback record. Inspect via :attr:`records` /
    :attr:`last` / :meth:`get`."""

    def __init__(self):
        self.records: list[dict] = []

    def attach(self, feedback_id: str, artifact: FeedbackArtifact, audit: AuditRecord) -> None:
        self.records.append({"feedback_id": feedback_id, "artifact": artifact, "audit": audit})

    @property
    def last(self) -> Optional[dict]:
        return self.records[-1] if self.records else None

    def get(self, feedback_id: str) -> Optional[dict]:
        for r in self.records:
            if r["feedback_id"] == feedback_id:
                return r
        return None


def default_consent_policy(genericize_terms: Optional[list] = None) -> StaticConsentPolicy:
    """The reference default: consent to a GENERICIZED attachment."""
    return StaticConsentPolicy(ConsentDecision(
        attach=ATTACH_GENERICIZED,
        basis="reference default: user consented to genericized context",
        reason="default reference policy",
        genericize_terms=list(genericize_terms or []),
    ))


# ===========================================================================  #
# REFERENCE HTTP ENDPOINT — illustrative stdlib http.server handler.            #
# Shows the EXACT seam: a POST .../chat_feedback -> handle_feedback.            #
# This is NOT production HTTP plumbing — it is the wiring diagram an engineer    #
# reads to see where the step plugs in. Real deployments use their own router.  #
# ===========================================================================  #
FEEDBACK_PATH_RE = re.compile(
    r"^/api/organizations/(?P<org>[^/]+)/chat_conversations/(?P<conv>[^/]+)"
    r"/chat_messages/(?P<msg>[^/]+)/chat_feedback/?$"
)


def route(
    method: str,
    path: str,
    body: Any,
    *,
    store: ConversationStore,
    consent: ConsentPolicy,
    sink: FeedbackSink,
    user_id: Optional[str] = None,
    **handle_kw,
) -> tuple[int, dict]:
    """Pure routing of one request -> ``(status_code, json_body)``.

    Matches the observable feedback-contract path, parses the ``{type, reason}``
    body, and runs :func:`handle_feedback`. Returns a ``201`` with the minimal
    public body (no conversation text) — mirroring the real endpoint. Call this
    directly in a test, or let :func:`make_reference_handler` call it over a socket."""
    if method != "POST":
        return 405, {"error": "method not allowed"}
    m = FEEDBACK_PATH_RE.match(path)
    if not m:
        return 404, {"error": "not found"}
    event = FeedbackEvent.from_request(
        org_id=m["org"], conversation_id=m["conv"], message_id=m["msg"],
        body=body, user_id=user_id,
    )
    result = handle_feedback(event, store=store, consent=consent, sink=sink, **handle_kw)
    return 201, result.to_public_dict()


def make_reference_handler(store: ConversationStore, consent: ConsentPolicy,
                           sink: FeedbackSink, **handle_kw):
    """Build a :class:`BaseHTTPRequestHandler` subclass bound to the three adapters.
    ILLUSTRATIVE — it shows where the step plugs into an HTTP layer, nothing more."""

    class _FeedbackHTTPRequestHandler(BaseHTTPRequestHandler):
        server_version = "fb-assist-server-side-reference/0.1"

        def do_POST(self):  # noqa: N802 (stdlib naming)
            length = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            user_id = self.headers.get("X-User-Id")
            status, payload = route(self.command, self.path, body, store=store,
                                    consent=consent, sink=sink, user_id=user_id, **handle_kw)
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):  # keep the demo quiet
            pass

    return _FeedbackHTTPRequestHandler


def make_reference_app(store: ConversationStore, consent: ConsentPolicy, sink: FeedbackSink,
                       *, host: str = "127.0.0.1", port: int = 0, **handle_kw) -> HTTPServer:
    """An :class:`HTTPServer` wired to the three adapters (ephemeral port by
    default). ILLUSTRATIVE. Use ``server.server_address`` to discover the bound
    port, then ``server.serve_forever()`` (or ``handle_request()`` in a test)."""
    return HTTPServer((host, port), make_reference_handler(store, consent, sink, **handle_kw))


# ===========================================================================  #
# Rendering for the demo (human-readable artifact + audit)                      #
# ===========================================================================  #
def render_result(result: FeedbackResult, *, show_rendered: bool = True) -> str:
    a, au = result.artifact, result.audit
    lines = [
        f"feedback_id : {result.feedback_id}",
        f"status      : {result.status}",
        f"consent     : requested={au.consent_attach}  attached={au.attached}"
        + (f"  basis={au.consent_basis}" if au.consent_basis else ""),
        f"anchor      : conv={a.anchor.get('conversation_id')}  msg={a.anchor.get('message_id')}",
    ]
    if a.flags:
        lines.append(f"flags       : {', '.join(a.flags)}")
    lines.append("")
    lines.append("— what would be attached —")
    if a.attach == ATTACH_NONE:
        lines.append("  {type, reason} only — NO conversation text attached (privacy-safe).")
        lines.append(f"    type   = {a.type!r}")
        lines.append(f"    reason = {a.reason!r}")
    elif a.attach == ATTACH_RAW:
        lines.append("  RAW conversation attached (explicit power-user opt-in; UNREDACTED).")
    else:
        c = a.redaction_summary or {}
        by_cat = ", ".join(f"{n}×{cat}" for cat, n in c.get("by_category", {}).items()) or "none"
        by_sev = ", ".join(f"{n}×{sev}" for sev, n in c.get("by_severity", {}).items()) or "none"
        lines.append(f"  GENERICIZED conversation ({c.get('turns_rendered', 0)} turns; "
                     f"{c.get('redactions', 0)} values masked).")
        lines.append(f"    by category : {by_cat}")
        lines.append(f"    by severity : {by_sev}")
        footer = _render_effort_footer(a.effort_signal or {})
        if footer:
            lines.append(f"  {footer}")
    lines.append("")
    lines.append("— audit —")
    lines.append(f"  re-id verify : {'PASS' if au.reid_verdict else 'FAIL'}")
    gate = "CLEAN ✅" if au.floor_clean else "RESIDUAL ❌"
    lines.append(f"  egress floor : {gate}  (soft NER candidates: {au.leak_candidates})")
    lines.append(f"  categories   : {au.redacted_categories or '{}'}")
    if show_rendered and a.attach == ATTACH_GENERICIZED and a.rendered:
        lines.append("")
        lines.append("— rendered (the outbound bytes) —")
        for ln in a.rendered.splitlines():
            lines.append("  " + ln)
    return "\n".join(lines)


# ===========================================================================  #
# CLI — python -m fb_assist.server_side --export <path> --feedback '{...}'       #
# ===========================================================================  #
_DEFAULT_FEEDBACK = {"type": "thumbs_down", "reason": "The submit flow froze."}


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fb_assist.server_side",
        description="Reference server-side consent-genericize step for the claude.ai "
                    "feedback flow. Defaults to a SYNTHETIC fixture; prints the sanitized "
                    "artifact that WOULD be attached + the audit. Zero Anthropic-internal "
                    "assumptions — only the public feedback contract + export schema.",
    )
    ap.add_argument("--export", default=str(DEFAULT_FIXTURE),
                    help=f"claude.ai export JSON (default: synthetic fixture {DEFAULT_FIXTURE.name})")
    ap.add_argument("--feedback", default=None,
                    help='feedback body JSON, e.g. \'{"type":"thumbs_down","reason":"froze"}\' '
                         "(default: a synthetic thumbs-down)")
    ap.add_argument("--conversation", default=None,
                    help="conversation UUID / name substring / 0-based index "
                         "(default: the first conversation in the export)")
    ap.add_argument("--message", default=None,
                    help="message UUID to anchor on (default: the first message of the conversation)")
    ap.add_argument("--consent", default=ATTACH_GENERICIZED, choices=list(_VALID_ATTACH),
                    help="consent disposition (default: genericized)")
    ap.add_argument("--genericize-term", action="append", default=[],
                    help="a codename / IP string that MUST NOT survive (repeatable)")
    ap.add_argument("--gliner", action="store_true",
                    help="also run the GLiNER NER pass (downloads ~86 MB on first use)")
    ap.add_argument("--json", action="store_true", help="emit the public result dict as JSON")
    ap.add_argument("--serve", action="store_true",
                    help="instead of a one-shot, start the illustrative HTTP server and print its URL")
    ap.add_argument("--port", type=int, default=0, help="port for --serve (default: ephemeral)")
    args = ap.parse_args(argv)

    export_path = Path(args.export)
    if not export_path.exists():
        print(f"error: export not found: {export_path}", file=sys.stderr)
        return 2

    store = InMemoryConversationStore.from_export(export_path)
    consent = StaticConsentPolicy(ConsentDecision(
        attach=args.consent,
        basis="CLI --consent",
        genericize_terms=list(args.genericize_term),
    ))
    sink = InMemoryFeedbackSink()

    if args.serve:
        server = make_reference_app(store, consent, sink, host="127.0.0.1", port=args.port,
                                    use_gliner=args.gliner)
        host, port = server.server_address
        url = (f"http://{host}:{port}/api/organizations/<org>/chat_conversations/"
               f"<conv>/chat_messages/<msg>/chat_feedback")
        print(f"[illustrative] reference feedback endpoint listening at:\n  POST {url}")
        print("  body: {\"type\": \"...\", \"reason\": \"...\"}   (Ctrl-C to stop)")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            server.server_close()
        return 0

    # Pick the conversation + the anchor message.
    if args.conversation is None:
        conv = select_conversation(export_path, index=0)
    else:
        sel = args.conversation
        if sel.lstrip("-").isdigit():
            conv = select_conversation(export_path, index=int(sel))
        elif len(sel) >= 12 and "-" in sel and " " not in sel:
            conv = select_conversation(export_path, uuid=sel) or select_conversation(export_path, needle=sel)
        else:
            conv = select_conversation(export_path, needle=sel)
    if conv is None:
        print(f"error: no conversation matched {args.conversation!r}", file=sys.stderr)
        return 2

    message_id = args.message or (conv.messages[0].uuid if conv.messages else "")

    body = _DEFAULT_FEEDBACK
    if args.feedback:
        try:
            body = json.loads(args.feedback)
        except (json.JSONDecodeError, ValueError) as e:
            print(f"error: --feedback is not valid JSON: {e}", file=sys.stderr)
            return 2

    event = FeedbackEvent.from_request(
        org_id="org-demo", conversation_id=conv.uuid, message_id=message_id,
        body=body, user_id="user-demo",
    )
    result = handle_feedback(event, store=store, consent=consent, sink=sink, use_gliner=args.gliner)

    if args.json:
        print(json.dumps(result.to_public_dict(), indent=2, ensure_ascii=False))
    else:
        print(render_result(result))

    # Exit non-zero if a genericized attach failed closed (a shell caller can gate).
    return 1 if (event.user_id and result.status == STATUS_FAILED_CLOSED
                 and "conversation_unavailable" not in result.artifact.flags) else 0


if __name__ == "__main__":
    raise SystemExit(main())
