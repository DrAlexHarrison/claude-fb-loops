"""fb_assist.cowork — the Claude **Cowork / local-agent ("ditto")** edge.

WHAT THIS IS
------------
Claude Desktop's Cowork mode (the bubblewrap-sandboxed "local agent") does NOT
write the Claude Code ``projects/<slug>/<id>.jsonl`` layout. It writes a single
**``audit.jsonl``** per session, under a different root and with a **snake_case**
record envelope. This module is the thin edge that lets the *same fb-assist core*
(``transcripts`` extractors, ``redact`` detectors, ``genericize`` verify,
``package`` swap-restore) operate on that shape — without modifying any of them.

PINNED FACTS (verified live; build to these exactly)
----------------------------------------------------
* **Storage.** ``audit.jsonl`` lives at::

      macOS : ~/Library/Application Support/Claude/local-agent-mode-sessions/<a>/<b>/local_<id>/audit.jsonl
      Linux : ~/.config/Claude/local-agent-mode-sessions/<a>/<b>/local_<id>/audit.jsonl

  with a ``skills-plugin/`` tree and an ``agent/memory`` dir alongside.
* **Record envelope is snake_case** (NOT the CC camelCase). Top keys::

      {_audit_timestamp, message:{content, role}, parent_tool_use_id,
       session_id, type, uuid}

  So ``transcripts.Record``'s camelCase accessors (``sessionId`` / ``parentUuid`` /
  ``timestamp``) MISS these — hence :func:`cowork_record` (the adapter). The
  narrative ``message.content`` extraction reuses CC verbatim because
  ``message:{content, role}`` matches CC.
* **No ``toolUseResult`` mirror.** In CC, tool output is stored TWICE (structured
  ``toolUseResult`` + model-visible ``tool_result`` block). In Cowork it is stored
  **once**, inside the ``message.content`` ``tool_result`` block. The
  ``transcripts`` structured extractors (``file_contents`` / ``bash_output`` /
  ``websearch``) read ``toolUseResult`` and therefore locate **ZERO** spans here —
  the output is reachable only via ``tool_results``. This is the structural reason
  fix 7 exists.

FIX 7 (binding) — the Cowork-aware structural strip
---------------------------------------------------
``redact.strip_categories`` is built around the CC envelope. Empirically, on a
real-shaped ``audit.jsonl`` it strips the bulk tool output *only incidentally*
(via the ``tool_calls`` generic lever) and **fails to strip a tool_result whose
originating tool name it doesn't recognize** (e.g. an MCP tool) when asked for a
single tool-output category like ``file_contents``. :func:`strip_blocks` is the
Cowork-shape-native replacement: it walks the ``message.content`` blocks directly,
classifies each ``tool_result`` by its originating ``tool_use`` name, and — because
Cowork carries no structured shape to disambiguate — treats any UNRECOGNIZED
tool_result as tool output and removes it whenever the caller is stripping tool
output. It also scrubs the ``image`` sub-blocks that computer-use screenshots ride
in. It depends on NO envelope field, so it is robust to the snake_case shape.

THE REDACTION PATH (one core, Cowork shape)
-------------------------------------------
:func:`redact_cowork` = :func:`strip_blocks` (structural floor) + ``pipeline``'s
char-precise narrative mask (``mask_narrative`` composes verbatim — it only needs
``type`` ∈ {user, assistant}, which the audit shape already carries) + the profile
``allow`` / ``deny`` lists. The deterministic egress floor (``redact.scan_secrets``
+ the PII regex) and the adversarial ``leak_scan`` gate are reused unchanged.

SWAP-RESTORE — open question (fix 8 / E2)
-----------------------------------------
``package.begin_swap`` / ``swap_restore`` operate on ANY file, so they CAN swap
``audit.jsonl`` byte-exact (built + tested here). But whether Cowork's
bwrap-sandboxed ``/feedback`` gather actually READS ``audit.jsonl`` (the same way
the CLI gather reads ``projects/**/*.jsonl``) is the remaining empirical UNKNOWN —
a one-session check on a Mac. Until confirmed, "swap-restore delivers on Cowork" is
UNPROVEN; we build the path and label it clearly. See :data:`SWAP_OPEN_QUESTION`.

H6 — the reference Cowork→Anthropic intake adapter
--------------------------------------------------
The exact Cowork feedback wire (``coworkArtifact`` / ``coworkFeedback`` strings
exist in the bundle but the endpoint is undocumented) is the honest gap. Mirroring
``server_side.py``'s PORT pattern, this module ships a runnable **reference, NOT
deployed** intake: three adapter seams (:class:`CoworkSessionStore`,
:class:`CoworkConsentPolicy`, :class:`CoworkFeedbackSink`) + :func:`handle_cowork_feedback`
with the same HARD fail-closed gate. An Anthropic engineer implements the three
ports against the real wire; the privacy core is done.

LOCAL ONLY. stdlib + the existing core. No network egress (GLiNER off by default).
Forward-transform: inputs are deep-copied, never mutated.
"""

from __future__ import annotations

import os

# Mirror redact.py / server_side.py: force the torch-only path so importing the
# sibling redactor (which lazy-loads NER) never explodes on a TF import under
# Keras 3. Set BEFORE redact is imported below.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import argparse
import copy
import json
import sys
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Protocol, Union, runtime_checkable

from . import package as P
from . import pipeline as PL
from . import transcripts as T
from .redact import (
    CATEGORIES,
    _BASH_TOOLS,
    _FILE_TOOLS,
    _WEB_TOOLS,
    _deep_scrub_paths as deep_scrub_paths,
    _index_tool_names,
    _mark,
)

__all__ = [
    # locator
    "COWORK_CONFIG_DIRS",
    "find_cowork_sessions",
    # adapter
    "ENVELOPE_ALIASES",
    "normalize_raw",
    "cowork_record",
    "parse_audit",
    "iter_cowork_records",
    "cowork_redaction_map",
    "cowork_structural_map",
    # redact (fix 7)
    "COWORK_DEFAULT_STRIP",
    "classify_tool_result",
    "strip_blocks",
    "redact_cowork",
    "egress_gate",
    # assemble + swap
    "assemble_cowork_payload",
    "begin_cowork_swap",
    "SWAP_OPEN_QUESTION",
    # H6 reference intake (reference, NOT deployed)
    "CoworkSessionStore",
    "CoworkConsentPolicy",
    "CoworkFeedbackSink",
    "CoworkFeedbackEvent",
    "CoworkConsentDecision",
    "CoworkArtifact",
    "CoworkAuditRecord",
    "CoworkFeedbackResult",
    "handle_cowork_feedback",
    "InMemoryCoworkSessionStore",
    "StaticCoworkConsentPolicy",
    "InMemoryCoworkFeedbackSink",
    "ATTACH_NONE",
    "ATTACH_GENERICIZED",
    "ATTACH_RAW",
    "main",
]

PathLike = Union[str, Path]


# =========================================================================== #
# LOCATOR — discover local-agent-mode-sessions/**/audit.jsonl                  #
# =========================================================================== #
def _default_config_dirs() -> list[Path]:
    """The Claude Desktop config roots on macOS + Linux (the two host platforms
    Cowork runs on). Each is the parent of ``local-agent-mode-sessions/``."""
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "Claude",  # macOS
        home / ".config" / "Claude",                          # Linux (Claude Desktop)
    ]


COWORK_CONFIG_DIRS: list[Path] = _default_config_dirs()
_SESSIONS_SUBDIR = "local-agent-mode-sessions"
_AUDIT_NAME = "audit.jsonl"


def find_cowork_sessions(
    config_dirs: Optional[Iterable[PathLike]] = None,
    *,
    window_hours: Optional[float] = None,
    roots: Optional[Iterable[PathLike]] = None,
) -> list[dict]:
    """Locate Cowork ``audit.jsonl`` transcripts (read-only), newest-first.

    Walks ``<config>/local-agent-mode-sessions/**/audit.jsonl`` across the macOS +
    Linux Claude-Desktop config roots (override with ``config_dirs`` for tests, or
    pass ``roots`` to point straight at one-or-more ``local-agent-mode-sessions``
    dirs). Mirrors :func:`transcripts.find_transcripts` for the CC layout, but for
    Cowork's single-file-per-session shape.

    Returns dicts: ``{path, size, mtime, session_id, local_dir, agent_dir,
    config_root, has_memory, has_skills_plugin}``. ``session_id`` is the enclosing
    ``local_<id>`` directory name (the agent session identity), falling back to the
    file's parent dir name.
    """
    from datetime import datetime, timezone

    if roots is not None:
        sess_roots = [Path(r) for r in roots]
    else:
        cfgs = [Path(c) for c in (config_dirs if config_dirs is not None else COWORK_CONFIG_DIRS)]
        sess_roots = [c / _SESSIONS_SUBDIR for c in cfgs]

    now = datetime.now(timezone.utc).timestamp()
    out: list[dict] = []
    for sroot in sess_roots:
        if not sroot.is_dir():
            continue
        config_root = sroot.parent
        for f in sroot.rglob(_AUDIT_NAME):
            try:
                st = f.stat()
            except OSError:
                continue
            if window_hours is not None and (now - st.st_mtime) > window_hours * 3600:
                continue
            local_dir = f.parent
            # …/local-agent-mode-sessions/<a>/<b>/local_<id>/audit.jsonl
            sid = local_dir.name
            out.append({
                "path": str(f),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "session_id": sid,
                "local_dir": str(local_dir),
                "agent_dir": str(local_dir / "agent"),
                "config_root": str(config_root),
                "has_memory": (local_dir / "agent" / "memory").is_dir(),
                "has_skills_plugin": (sroot / "skills-plugin").is_dir(),
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# =========================================================================== #
# ADAPTER — snake_case audit envelope  ->  transcripts.Record shape            #
# =========================================================================== #
# snake_case audit key  ->  the camelCase key transcripts.Record / the extractors
# read. Adding the alias (without dropping the original) lets the EXISTING
# extractors / redaction_map / detect surface fire on the Cowork shape, while the
# record stays a faithful superset of the original.
ENVELOPE_ALIASES = {
    "session_id": "sessionId",
    "_audit_timestamp": "timestamp",
    "parent_tool_use_id": "parentToolUseId",  # NB: a tool-use correlation, *not*
                                              # the message parentUuid — kept under a
                                              # distinct key so we never fake a
                                              # parent-message link.
}


def normalize_raw(raw: dict, *, alias_envelope: bool = True) -> dict:
    """Return a deep-copied audit record whose ``type`` ∈ {user, assistant} and
    (optionally) whose snake_case envelope is mirrored to the camelCase keys the
    extractors read.

    ``alias_envelope=True`` (the read-only DETECTION/extraction surface): add
    ``sessionId`` / ``timestamp`` / ``parentToolUseId`` aliases so
    ``transcripts.redaction_map`` / ``detect`` work.

    ``alias_envelope=False`` (the REDACTION surface): only normalize ``type`` from
    ``message.role`` — keeping the record a clean ``audit.jsonl`` record so the
    sanitized bytes we may later swap onto disk stay shape-valid for Cowork's
    gather. Never adds keys Cowork didn't write.
    """
    out = copy.deepcopy(raw)
    msg = out.get("message")
    role = msg.get("role") if isinstance(msg, dict) else None
    if out.get("type") not in ("user", "assistant") and role in ("user", "assistant"):
        out["type"] = role
    if alias_envelope:
        for snake, camel in ENVELOPE_ALIASES.items():
            if snake in out and camel not in out:
                out[camel] = out[snake]
    return out


def cowork_record(raw: dict) -> T.Record:
    """Adapt ONE snake_case ``audit.jsonl`` record to a ``transcripts.Record`` whose
    envelope accessors + the existing extractors work. ``record.uuid`` /
    ``record.session_id`` / ``record.timestamp`` resolve; ``record.message`` is the
    untouched ``{role, content}``. Use this for extraction / detection / preview."""
    norm = normalize_raw(raw, alias_envelope=True)
    return T.Record(line=0, raw=norm, type=str(norm.get("type", "")))


def parse_audit(path: PathLike, stats: Optional[T.ParseStats] = None) -> Iterator[dict]:
    """Stream the RAW snake_case dicts from an ``audit.jsonl`` (malformed-tolerant,
    bounded memory — reuses ``transcripts.parse``)."""
    for r in T.parse(str(path), stats=stats):
        yield r.raw


def iter_cowork_records(path: PathLike, stats: Optional[T.ParseStats] = None) -> Iterator[T.Record]:
    """Stream adapted :class:`transcripts.Record` objects from an ``audit.jsonl``."""
    for raw in parse_audit(path, stats=stats):
        yield cowork_record(raw)


def cowork_redaction_map(source: Union[PathLike, Iterable[dict]],
                         categories: Optional[Iterable[str]] = None,
                         preview_chars: int = 160) -> dict:
    """``transcripts.redaction_map`` over the adapted Cowork records, AUGMENTED with
    a Cowork-shape structural map so the consent preview is accurate.

    The stock map buckets ALL tool output under ``tool_results`` and reports
    ``file_contents`` / ``bash_output`` / ``websearch`` = 0 (those extractors read
    the absent ``toolUseResult``). We add ``cowork_structural`` — counts of tool
    output located in the ``message.content`` ``tool_result`` blocks, classified by
    the originating tool — so "what will be stripped" reflects reality."""
    raws = list(_as_raws(source))
    records = [cowork_record(r) for r in raws]
    base = T.redaction_map(records, categories=categories, preview_chars=preview_chars)
    base["cowork_structural"] = cowork_structural_map(raws)
    return base


def cowork_structural_map(source: Union[PathLike, Iterable[dict]]) -> dict:
    """Count tool output located in Cowork's ``message.content`` ``tool_result``
    blocks, classified by the originating ``tool_use`` name. This is what the stock
    structured extractors miss (no ``toolUseResult`` mirror)."""
    raws = list(_as_raws(source))
    names = _index_tool_names(raws)
    counts: dict[str, int] = {}
    chars: dict[str, int] = {}
    for rec in raws:
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for blk in content:
            if not (isinstance(blk, dict) and blk.get("type") == "tool_result"):
                continue
            cat = classify_tool_result(blk, names)
            counts[cat] = counts.get(cat, 0) + 1
            chars[cat] = chars.get(cat, 0) + _tool_result_chars(blk)
    return {"located": dict(sorted(counts.items(), key=lambda kv: -kv[1])),
            "chars": chars}


def _as_raws(source: Union[PathLike, Iterable[dict]]) -> Iterable[dict]:
    if isinstance(source, (str, Path)):
        return parse_audit(source)
    return list(source)


# =========================================================================== #
# REDACT (fix 7) — the Cowork-shape-native structural strip                    #
# =========================================================================== #
# Mirrors pipeline.DEFAULT_STRIP_CATEGORIES, scoped to what the Cowork shape
# actually carries: bulk tool output + thinking + paths are stripped wholesale;
# human_prompts / assistant_text are KEPT and char-precise-masked downstream.
COWORK_DEFAULT_STRIP = [
    "file_contents", "bash_output", "tool_calls", "websearch",
    "thinking_blocks", "paths",
]

# Generic bucket for a tool_result whose originating tool name we don't recognize
# (e.g. an MCP tool). On Cowork there is no structured shape to disambiguate it, so
# it is treated as tool output and removed whenever ANY tool-output strip is asked.
_TOOL_OUTPUT = "tool_output"
_TOOL_OUTPUT_CATS = frozenset({"file_contents", "bash_output", "websearch"})


def classify_tool_result(block: dict, tool_names: dict) -> str:
    """Classify a ``tool_result`` block by the tool that produced it (Cowork shape).

    Returns ``file_contents`` / ``bash_output`` / ``websearch`` for recognized tool
    names, else ``tool_output`` (the generic bucket for MCP / unrecognized tools)."""
    name = tool_names.get(block.get("tool_use_id"), "")
    if name in _FILE_TOOLS:
        return "file_contents"
    if name in _BASH_TOOLS:
        return "bash_output"
    if name in _WEB_TOOLS:
        return "websearch"
    return _TOOL_OUTPUT


def _tool_result_chars(block: dict) -> int:
    c = block.get("content")
    if isinstance(c, str):
        return len(c)
    if isinstance(c, list):
        n = 0
        for sub in c:
            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                n += len(sub["text"])
        return n
    return 0


def _scrub_tool_result_block(block: dict, category: str) -> None:
    """Remove the bytes of a ``tool_result`` block in place, leaving a marker.

    Handles all three Cowork content shapes: a bare string, a list of ``text``
    sub-blocks, and ``image`` sub-blocks (computer-use screenshots transit here —
    their ``source`` data is dropped)."""
    c = block.get("content")
    if isinstance(c, str):
        block["content"] = _mark(category, len(c))
    elif isinstance(c, list):
        for sub in c:
            if not isinstance(sub, dict):
                continue
            if isinstance(sub.get("text"), str):
                sub["text"] = _mark(category, len(sub["text"]))
            if sub.get("type") == "image" and isinstance(sub.get("source"), dict):
                sub["source"] = {"type": "stripped", "note": _mark(category)}


def _strip_message_blocks_cowork(rec: dict, cats: set, *, strip_tool_output: bool,
                                 names: dict) -> None:
    """The Cowork-native message-block strip — envelope-agnostic, drives off
    ``message.role`` + block ``type`` only."""
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    role = msg.get("role")
    content = msg.get("content")

    if isinstance(content, str):
        if role == "user" and "human_prompts" in cats:
            msg["content"] = _mark("human_prompts", len(content))
        elif role == "assistant" and "assistant_text" in cats:
            msg["content"] = _mark("assistant_text", len(content))
        return
    if not isinstance(content, list):
        return

    for blk in content:
        if not isinstance(blk, dict):
            continue
        bt = blk.get("type")
        if bt == "thinking" and "thinking_blocks" in cats:
            if isinstance(blk.get("thinking"), str):
                blk["thinking"] = _mark("thinking_blocks", len(blk["thinking"]))
            blk.pop("signature", None)
        elif bt == "text" and role == "assistant" and "assistant_text" in cats:
            if isinstance(blk.get("text"), str):
                blk["text"] = _mark("assistant_text", len(blk["text"]))
        elif bt == "text" and role == "user" and "human_prompts" in cats:
            if isinstance(blk.get("text"), str):
                blk["text"] = _mark("human_prompts", len(blk["text"]))
        elif bt == "tool_use" and "tool_calls" in cats:
            # Keep the tool NAME (signal); scrub the inputs (paths/contents/args).
            blk["input"] = {"__stripped__": _mark("tool_calls")}
        elif bt == "tool_result":
            cat = classify_tool_result(blk, names)
            requested = (cat in cats)
            # Unrecognized tool output: strip whenever the caller is removing tool
            # output at all (no structured shape to disambiguate -> conservative).
            generic = (cat == _TOOL_OUTPUT and strip_tool_output)
            if requested or generic or "tool_calls" in cats:
                _scrub_tool_result_block(blk, cat if cat != _TOOL_OUTPUT else "tool_calls")


def _strip_paths_cowork(rec: dict) -> None:
    """Deep-scrub absolute paths from the ``message`` subtree (tool inputs, text,
    tool_result content) — reuses the proven ``redact`` path scrubber, leaving the
    snake_case envelope ids (session_id / uuid / parent_tool_use_id) intact."""
    msg = rec.get("message")
    if isinstance(msg, dict):
        rec["message"] = deep_scrub_paths(msg)


def strip_blocks(records: list[dict], categories: Iterable[str],
                 mode: str = "replace") -> list[dict]:
    """Cowork-shape-native structural strip (FIX 7). Returns a NEW list; inputs are
    never mutated.

    Unlike ``redact.strip_categories`` (which routes tool output through the absent
    ``toolUseResult`` mirror and silently drops unrecognized tool_results), this
    walks ``message.content`` directly and:

    * strips ``human_prompts`` / ``assistant_text`` / ``thinking_blocks`` by
      ``message.role`` + block type,
    * scrubs ``tool_use.input`` for ``tool_calls`` (keeping the tool name),
    * scrubs every ``tool_result`` block — classified ``file_contents`` /
      ``bash_output`` / ``websearch`` by originating tool, and any UNRECOGNIZED
      tool_result removed whenever a tool-output category is requested (closing the
      MCP-tool-output survival gap proven on the synthetic fixture),
    * deep-scrubs absolute paths from the message subtree for ``paths``.

    ``mode`` is accepted for parity with ``strip_categories``; the marker form is
    used (so the co-author can still see WHAT was removed). ``env_metadata`` /
    ``hook_output`` / ``injected_memory`` are accepted but no-op: the audit shape
    carries no CC envelope-metadata, hook-attachment, or nested-memory records.
    """
    cats = set(categories)
    unknown = cats - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories: {sorted(unknown)}; valid = {CATEGORIES}")
    out = copy.deepcopy(records)
    names = _index_tool_names(out)
    strip_tool_output = bool(cats & _TOOL_OUTPUT_CATS) or "tool_calls" in cats
    msg_cats = {"human_prompts", "assistant_text", "thinking_blocks", "tool_calls"} | _TOOL_OUTPUT_CATS
    for rec in out:
        if not isinstance(rec, dict):
            continue
        if cats & msg_cats:
            _strip_message_blocks_cowork(rec, cats, strip_tool_output=strip_tool_output, names=names)
        if "paths" in cats:
            _strip_paths_cowork(rec)
    return out


def redact_cowork(
    raws: list[dict],
    *,
    strip: Optional[Iterable[str]] = None,
    mask: bool = True,
    allow: Optional[Iterable[str]] = None,
    deny: Optional[Iterable[str]] = None,
) -> dict:
    """The Cowork redaction chain: :func:`strip_blocks` (structural floor) + the
    ``pipeline`` char-precise narrative mask (``mask_narrative`` composes verbatim).

    Returns ``{sanitized_raws, redaction_map}``. Does NOT mutate the input ``raws``.
    The output records remain valid ``audit.jsonl`` records (envelope untouched, only
    ``type`` normalized to role) so they can be assembled / swapped directly.
    """
    strip_cats = list(strip) if strip is not None else list(COWORK_DEFAULT_STRIP)
    # Normalize type (so mask_narrative's human/assistant extractors fire) WITHOUT
    # adding camelCase aliases — keep the records shape-valid for Cowork's gather.
    typed = [normalize_raw(r, alias_envelope=False) for r in raws]
    sanitized = strip_blocks(typed, strip_cats, mode="replace")
    redaction_map = PL.mask_narrative(sanitized, allow=allow, deny=deny) if mask else []
    return {"sanitized_raws": sanitized, "redaction_map": redaction_map}


def egress_gate(upload: str, content: str, *, reveal: bool = False) -> dict:
    """The two-layer egress gate over the Cowork outbound bytes — reuses
    ``pipeline.egress_gate`` verbatim (hard deterministic floor + soft NER
    candidates). ``floor_clean`` MUST be True to ship."""
    return PL.egress_gate(upload, content, reveal=reveal)


# =========================================================================== #
# ASSEMBLE + SWAP  (the local-agent delivery path)                             #
# =========================================================================== #
SWAP_OPEN_QUESTION = (
    "OPEN (fix 8 / E2): package.begin_swap/swap_restore CAN swap audit.jsonl "
    "byte-exact (built + tested here), but whether Cowork's bwrap-sandboxed "
    "/feedback gather READS audit.jsonl — the way the CLI gather reads "
    "projects/**/*.jsonl — is UNPROVEN. A one-session Mac check is required before "
    "claiming swap-restore delivers feedback on Cowork. The server-side / native "
    "coworkFeedback intake (H6) is the parallel route that does not depend on this."
)


def assemble_cowork_payload(
    description: str,
    audit_path: PathLike,
    sanitized_raws: list[dict],
    *,
    effort_signal: Optional[dict] = None,
    limit: int = P.FEEDBACK_BUDGET_BYTES,
) -> P.Payload:
    """Build the on-disk ``{description, sanitized audit.jsonl}`` payload under the
    1 MB budget (reuses ``package.assemble_payload``). ``targets`` is keyed by the
    real ``audit.jsonl`` path so it feeds straight into :func:`begin_cowork_swap`."""
    return P.assemble_payload(
        description, {str(audit_path): sanitized_raws}, limit=limit,
        effort_signal=effort_signal,
    )


def begin_cowork_swap(
    audit_path: PathLike,
    sanitized_raws: list[dict],
    *,
    live_session_id: Optional[str] = None,
    allow_live: bool = False,
    backup_root: Optional[PathLike] = None,
) -> P.SwapHandle:
    """Phase 1 of the non-destructive swap on a Cowork ``audit.jsonl`` — install the
    sanitized bytes, journal the original for byte-exact restore (reuses
    ``package.begin_swap``: atomic, crash-durable, refuses the live session by id).

    ⚠️ See :data:`SWAP_OPEN_QUESTION`: that the bwrap-sandboxed Cowork ``/feedback``
    actually READS this file is the unproven step. Call ``package.finish_swap`` (or
    ``package.recover``) afterward to restore. The swap mechanism itself is proven;
    its *delivery* on Cowork is not yet."""
    data = P.serialize_records(sanitized_raws)
    return P.begin_swap(
        {str(audit_path): data},
        backup_root=backup_root,
        allow_live=allow_live,
        live_session_id=live_session_id,
    )


# =========================================================================== #
# H6 — REFERENCE Cowork -> Anthropic intake adapter  (REFERENCE, NOT DEPLOYED) #
# Mirrors server_side.py's PORT pattern for the undocumented coworkFeedback wire.#
# =========================================================================== #
ATTACH_NONE = "none"
ATTACH_GENERICIZED = "genericized"
ATTACH_RAW = "raw"
_VALID_ATTACH = (ATTACH_NONE, ATTACH_GENERICIZED, ATTACH_RAW)

STATUS_NONE = "attached_none"
STATUS_GENERICIZED = "attached_genericized"
STATUS_RAW = "attached_raw"
STATUS_FAILED_CLOSED = "failed_closed"


@runtime_checkable
class CoworkSessionStore(Protocol):
    """PORT 1 — your Cowork session store. ``fetch`` returns the RAW ``audit.jsonl``
    records (list of snake_case dicts) for a session, or ``None`` if unavailable.
    Implement against the real host fs (or the bridge ``read_transcript`` MCP)."""

    def fetch(self, session_id: str) -> Optional[list[dict]]:
        ...


@runtime_checkable
class CoworkConsentPolicy(Protocol):
    """PORT 2 — the NEW product surface: consent + genericize preference. ``decision``
    returns a :class:`CoworkConsentDecision` (``none`` / ``genericized`` / ``raw``)."""

    def decision(self, user_id: Optional[str], session_id: str) -> "CoworkConsentDecision":
        ...


@runtime_checkable
class CoworkFeedbackSink(Protocol):
    """PORT 3 — the undocumented Cowork→Anthropic wire (``coworkFeedback`` /
    ``coworkArtifact`` / ``FeedbackWindow`` / OTEL). ``attach`` persists/emits the
    already-privacy-clean :class:`CoworkArtifact` + :class:`CoworkAuditRecord`.
    Implement against the real intake; the artifact is clean by the time it lands."""

    def attach(self, feedback_id: str, artifact: "CoworkArtifact", audit: "CoworkAuditRecord") -> None:
        ...


@dataclass
class CoworkFeedbackEvent:
    """One Cowork feedback submission. The anchor is the ``session_id`` (+ optional
    ``turn_uuid`` of the message under feedback) — the ungameable reference, the
    Cowork analogue of the Messages-API ``request-id`` / claude.ai message-UUID."""
    session_id: str
    turn_uuid: str = ""
    type: str = ""
    reason: str = ""
    user_id: Optional[str] = None

    @property
    def anchor(self) -> dict:
        return {"session_id": self.session_id, "turn_uuid": self.turn_uuid}


@dataclass
class CoworkConsentDecision:
    attach: str = ATTACH_GENERICIZED
    scope: str = "session"
    basis: str = ""
    reason: str = ""
    genericize_terms: list = field(default_factory=list)  # codenames that MUST NOT survive

    def __post_init__(self) -> None:
        if self.attach not in _VALID_ATTACH:
            raise ValueError(f"unknown attach {self.attach!r}; valid = {_VALID_ATTACH}")


@dataclass
class CoworkArtifact:
    """The ``coworkArtifact``-shaped object attached to the feedback record —
    privacy-clean by construction. For ``none``: only ``{type, reason, anchor}``."""
    type: str
    reason: str
    anchor: dict
    attach: str
    rendered: Optional[str] = None
    sanitized_records: Optional[list] = None
    effort_signal: Optional[dict] = None
    redaction_summary: Optional[dict] = None
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "type": self.type, "reason": self.reason, "anchor": self.anchor,
            "attach": self.attach, "rendered": self.rendered,
            "sanitized_records": self.sanitized_records,
            "effort_signal": self.effort_signal,
            "redaction_summary": self.redaction_summary, "flags": list(self.flags),
        }


@dataclass
class CoworkAuditRecord:
    feedback_id: str
    anchor: dict
    consent_attach: str
    attached: str
    redaction_count: int
    floor_clean: bool
    leak_candidates: int
    flags: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "feedback_id": self.feedback_id, "anchor": self.anchor,
            "consent_requested": self.consent_attach, "attached": self.attached,
            "redaction_count": self.redaction_count, "floor_clean": self.floor_clean,
            "leak_candidates": self.leak_candidates, "flags": list(self.flags),
        }


@dataclass
class CoworkFeedbackResult:
    feedback_id: str
    status: str
    artifact: CoworkArtifact
    audit: CoworkAuditRecord

    def to_public_dict(self) -> dict:
        return {
            "feedback_id": self.feedback_id, "status": self.status,
            "attach": self.artifact.attach, "anchor": self.artifact.anchor,
            "flags": list(self.artifact.flags), "audit": self.audit.to_dict(),
        }


def _render_cowork_markdown(records: list[dict]) -> str:
    """Render the (already-sanitized) Cowork narrative as share-ready markdown — the
    exact bytes the egress gate scans."""
    recs = [cowork_record(r) for r in records]
    lines: list[str] = []
    for rec in recs:
        for sp in list(T.human_prompts([rec])):
            lines += ["### Human", sp.text, ""]
        for sp in list(T.assistant_text([rec])):
            lines += ["### Assistant", sp.text, ""]
    return "\n".join(lines).rstrip() + "\n"


def _new_feedback_id() -> str:
    return "cwfb-" + _uuid.uuid4().hex


def handle_cowork_feedback(
    event: CoworkFeedbackEvent,
    *,
    store: CoworkSessionStore,
    consent: CoworkConsentPolicy,
    sink: CoworkFeedbackSink,
    strip: Optional[Iterable[str]] = None,
    feedback_id: Optional[str] = None,
) -> CoworkFeedbackResult:
    """The reference Cowork consent-genericize intake step, end-to-end (REFERENCE,
    NOT DEPLOYED — the real ``coworkFeedback`` wire is the honest gap, §H/§J).

    * ``none``        -> attach only ``{type, reason}`` (privacy-safe default).
    * ``genericized`` -> ``store.fetch`` + :func:`redact_cowork` + the HARD
      fail-closed gate (the deterministic floor re-run over the ACTUAL outbound
      bytes; non-empty -> attach ``none`` + flag, never the leaky artifact).
    * ``raw``         -> explicit opt-in; attach raw with a loud flag.

    Always emits a :class:`CoworkAuditRecord` and calls ``sink.attach`` exactly once.
    """
    fid = feedback_id or _new_feedback_id()
    decision = consent.decision(event.user_id, event.session_id)
    anchor = event.anchor

    def _finish(status, artifact, audit):
        sink.attach(fid, artifact, audit)
        return CoworkFeedbackResult(fid, status, artifact, audit)

    if decision.attach == ATTACH_NONE:
        art = CoworkArtifact(event.type, event.reason, anchor, ATTACH_NONE)
        au = CoworkAuditRecord(fid, anchor, decision.attach, ATTACH_NONE, 0, True, 0, [])
        return _finish(STATUS_NONE, art, au)

    raws = store.fetch(event.session_id)
    if not raws:
        flags = ["session_unavailable", "fail_closed"]
        art = CoworkArtifact(event.type, event.reason, anchor, ATTACH_NONE, flags=flags)
        au = CoworkAuditRecord(fid, anchor, decision.attach, ATTACH_NONE, 0, True, 0, flags)
        return _finish(STATUS_FAILED_CLOSED, art, au)

    if decision.attach == ATTACH_RAW:
        rendered = _render_cowork_markdown(raws)
        flags = ["raw_optin", "no_redaction"]
        art = CoworkArtifact(event.type, event.reason, anchor, ATTACH_RAW,
                             rendered=rendered, sanitized_records=copy.deepcopy(raws), flags=flags)
        au = CoworkAuditRecord(fid, anchor, decision.attach, ATTACH_RAW, 0, False, 0, flags)
        return _finish(STATUS_RAW, art, au)

    # genericized: strip + mask, then the HARD fail-closed gate over outbound bytes.
    red = redact_cowork(raws, strip=list(strip) if strip is not None else None,
                        deny=list(decision.genericize_terms or []))
    sanitized = red["sanitized_raws"]
    rendered = _render_cowork_markdown(sanitized)
    upload = rendered + "\n" + P.serialize_records(sanitized).decode("utf-8", "replace")
    gate = egress_gate(upload, rendered)
    floor_clean = gate["floor_clean"]
    leak_n = gate["candidate_count"]
    rmap = red["redaction_map"]

    if not floor_clean:
        flags = ["fail_closed", "residual_floor_leak"]
        art = CoworkArtifact(event.type, event.reason, anchor, ATTACH_NONE, flags=flags)
        au = CoworkAuditRecord(fid, anchor, decision.attach, ATTACH_NONE,
                               len(rmap), floor_clean, leak_n, flags)
        return _finish(STATUS_FAILED_CLOSED, art, au)

    summary = {"redactions": len(rmap),
               "by_category": _count_by(rmap, "category")}
    effort = {"surface": "cowork", "redaction": "genericize",
              "anchor": anchor, "summary": {"redactions": len(rmap), "floor_clean": True}}
    art = CoworkArtifact(event.type, event.reason, anchor, ATTACH_GENERICIZED,
                         rendered=rendered, sanitized_records=sanitized,
                         effort_signal=effort, redaction_summary=summary)
    au = CoworkAuditRecord(fid, anchor, decision.attach, ATTACH_GENERICIZED,
                           len(rmap), floor_clean, leak_n, [])
    return _finish(STATUS_GENERICIZED, art, au)


def _count_by(rows: list[dict], key: str) -> dict:
    out: dict[str, int] = {}
    for r in rows:
        k = str(r.get(key, ""))
        out[k] = out.get(k, 0) + int(r.get("count", 1))
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# ---- reference adapters (so the H6 intake RUNS in-repo) --------------------- #
class InMemoryCoworkSessionStore:
    """Reference :class:`CoworkSessionStore`. Serves ``audit.jsonl`` raws by
    session id — standing in for the real host fs / bridge ``read_transcript``."""

    def __init__(self, by_session: Optional[dict] = None):
        self._by: dict[str, list[dict]] = dict(by_session or {})

    @classmethod
    def from_audit(cls, session_id: str, path: PathLike) -> "InMemoryCoworkSessionStore":
        return cls({session_id: list(parse_audit(path))})

    def add(self, session_id: str, raws: list[dict]) -> None:
        self._by[session_id] = list(raws)

    def fetch(self, session_id: str) -> Optional[list[dict]]:
        return self._by.get(session_id)


class StaticCoworkConsentPolicy:
    """Reference :class:`CoworkConsentPolicy` returning one fixed decision."""

    def __init__(self, decision: Optional[CoworkConsentDecision] = None, **kw):
        if decision is None:
            kw.setdefault("basis", "reference StaticCoworkConsentPolicy")
            decision = CoworkConsentDecision(**kw)
        self._decision = decision

    def decision(self, user_id: Optional[str], session_id: str) -> CoworkConsentDecision:
        return self._decision


class InMemoryCoworkFeedbackSink:
    """Reference :class:`CoworkFeedbackSink` capturing what WOULD be emitted."""

    def __init__(self):
        self.records: list[dict] = []

    def attach(self, feedback_id, artifact, audit) -> None:
        self.records.append({"feedback_id": feedback_id, "artifact": artifact, "audit": audit})

    @property
    def last(self) -> Optional[dict]:
        return self.records[-1] if self.records else None


# =========================================================================== #
# CLI / demo                                                                   #
# =========================================================================== #
_DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "cowork-audit.jsonl"


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fb_assist.cowork",
        description="The Claude Cowork / local-agent (audit.jsonl) fb-assist edge. "
                    "Defaults to a SYNTHETIC fixture; prints the structural map, the "
                    "redaction summary, and the genericized artifact that WOULD be "
                    "attached. LOCAL ONLY; no real data.",
    )
    sub = ap.add_subparsers(dest="cmd")

    p_find = sub.add_parser("find", help="discover Cowork audit.jsonl sessions (newest-first)")
    p_find.add_argument("--config-dir", action="append", default=None,
                        help="override config root(s) (repeatable)")
    p_find.add_argument("--window-hours", type=float, default=None)

    p_map = sub.add_parser("map", help="structural map: where each category lives (Cowork shape)")
    p_map.add_argument("path", nargs="?", default=str(_DEFAULT_FIXTURE))

    p_red = sub.add_parser("redact", help="strip + mask an audit.jsonl; print the gate verdict")
    p_red.add_argument("path", nargs="?", default=str(_DEFAULT_FIXTURE))
    p_red.add_argument("--deny", action="append", default=[], help="codename that MUST NOT survive")
    p_red.add_argument("--json", action="store_true")

    p_fb = sub.add_parser("feedback", help="run the H6 reference intake on a session (REFERENCE)")
    p_fb.add_argument("path", nargs="?", default=str(_DEFAULT_FIXTURE))
    p_fb.add_argument("--consent", default=ATTACH_GENERICIZED, choices=list(_VALID_ATTACH))
    p_fb.add_argument("--deny", action="append", default=[])

    args = ap.parse_args(argv)
    if args.cmd == "find":
        rows = find_cowork_sessions(config_dirs=args.config_dir, window_hours=args.window_hours)
        print(json.dumps(rows, indent=2))
        return 0
    if args.cmd == "map":
        print(json.dumps(cowork_structural_map(args.path), indent=2))
        return 0
    if args.cmd == "redact":
        raws = list(parse_audit(args.path))
        red = redact_cowork(raws, deny=args.deny)
        rendered = _render_cowork_markdown(red["sanitized_raws"])
        upload = rendered + "\n" + P.serialize_records(red["sanitized_raws"]).decode("utf-8", "replace")
        gate = egress_gate(upload, rendered)
        if args.json:
            print(json.dumps({"redactions": len(red["redaction_map"]),
                              "floor_clean": gate["floor_clean"],
                              "candidates": gate["candidate_count"]}, indent=2))
        else:
            print(f"redactions   : {len(red['redaction_map'])}")
            print(f"egress floor : {'CLEAN ✅' if gate['floor_clean'] else 'RESIDUAL ❌'}")
            print(f"soft NER     : {gate['candidate_count']} candidate(s)")
            print("\n— rendered (outbound bytes) —")
            print(rendered)
        return 0
    if args.cmd == "feedback":
        store = InMemoryCoworkSessionStore.from_audit("local_demo", args.path)
        consent = StaticCoworkConsentPolicy(CoworkConsentDecision(
            attach=args.consent, genericize_terms=list(args.deny)))
        sink = InMemoryCoworkFeedbackSink()
        ev = CoworkFeedbackEvent(session_id="local_demo", type="thumbs_down",
                                 reason="Cowork agent froze on submit.", user_id="user-demo")
        res = handle_cowork_feedback(ev, store=store, consent=consent, sink=sink)
        print(json.dumps(res.to_public_dict(), indent=2, ensure_ascii=False))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
