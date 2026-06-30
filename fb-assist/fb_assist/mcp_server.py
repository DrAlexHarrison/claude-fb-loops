"""fb-assist MCP server — the in-session co-author's model-invocable toolbox.

An always-on stdio MCP server (FastMCP) exposing the fb-assist toolbox as
``mcp__fb-assist__*`` tools for the ``/fb`` skill. Every tool is a THIN wrapper
over the library — no business logic here — and returns COMPACT JSON (locators,
counts, MASKED samples), never a full transcript or a raw secret.

State is per-session, in-memory, and ephemeral (the on-disk swap journal is the
durable truth); callers should pass ``session_id`` fresh each turn since the MCP
process's own env can go stale across ``/clear``. Local only, no network.
"""

from __future__ import annotations

import os

# transformers' TensorFlow path breaks under Keras 3; force torch (defensive — the
# detection modules set this too, but the MCP process imports them lazily).
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import dataclasses
from collections import Counter
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from . import transcripts as T
from . import package as P
from . import locate as L
from . import profile as PROF


class _LazyModule:
    """Defer a heavy import until the first tool call that needs it.

    ``pipeline`` and ``genericize`` pull in the NER stack (presidio/gliner/torch);
    importing them eagerly made the MCP server slow to start, so Claude Code's
    connection timeout silently dropped it on a cold start and the co-author fell
    back to the CLI. Loading them lazily lets the server connect in well under a
    second; the heavy load happens on first *use* (a detect/redact call), when the
    user is actively working, not at connect time.
    """

    def __init__(self, name: str) -> None:
        self._name = name
        self._mod = None

    def __getattr__(self, attr: str):
        if self._mod is None:
            import importlib
            self._mod = importlib.import_module(self._name)
        return getattr(self._mod, attr)


PL = _LazyModule("fb_assist.pipeline")    # heavy: -> redact -> torch/presidio/gliner
G = _LazyModule("fb_assist.genericize")   # heavy: -> redact

mcp = FastMCP("fb-assist")


# --------------------------------------------------------------------------- #
# Per-session state                                                            #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class _Session:
    session_id: str
    path: Optional[str] = None
    cwd: Optional[str] = None
    parsed: Optional[PL.Parsed] = None
    redaction_map: Optional[list] = None
    sanitized_raws: Optional[list] = None
    description: str = ""
    effort_signal: Optional[dict] = None
    payload: Optional[P.Payload] = None
    preview: Any = None
    gate: Optional[dict] = None
    journal_path: Optional[str] = None


_STATE: dict[str, _Session] = {}


def _session(session_id: str, cwd: Optional[str] = None) -> _Session:
    s = _STATE.get(session_id)
    if s is None:
        s = _Session(session_id=session_id, cwd=cwd)
        _STATE[session_id] = s
    if cwd and not s.cwd:
        s.cwd = cwd
    if s.path is None:
        info = L.resolve(cwd=cwd, session_id=session_id)
        s.path = info.get("path")
        if not s.cwd:
            s.cwd = info.get("cwd")
    return s


def _need_path(s: _Session) -> str:
    if not s.path or not os.path.isfile(s.path):
        raise FileNotFoundError(
            f"no on-disk transcript for session {s.session_id!r} "
            f"(resolved path: {s.path!r}). Pass the right cwd, or pick a session from list_sessions()."
        )
    return s.path


def _ensure_parsed(s: _Session) -> PL.Parsed:
    if s.parsed is None:
        s.parsed = PL.parse_session(_need_path(s))
    return s.parsed


def _by_category(redaction_map: list) -> dict:
    return dict(Counter(e.get("category", "?") for e in redaction_map))


# --------------------------------------------------------------------------- #
# Self-location                                                                #
# --------------------------------------------------------------------------- #
def locate_session(cwd: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """Resolve which on-disk transcript is THIS session (and the past sessions
    around it). Identity-first ``is_live``. Returns
    ``{session_id, path, project_dir, config_dir, account, is_live, candidates, ...}``.

    Server-vs-user cwd: this server is long-lived and its process cwd is its spawn
    dir, NOT the user's session cwd. If the caller gives neither a cwd nor a
    resolvable session id, ``locate.resolve`` falls back to the *process* cwd
    (``cwd_source=="process"``) — which would silently offer the WRONG project's
    sessions as the swap menu. We refuse that LOUDLY instead of guessing. The skill
    must pass the fresh ``$CLAUDE_CODE_SESSION_ID`` (preferred) or the user's cwd."""
    info = L.resolve(cwd=cwd, session_id=session_id)
    if info.get("cwd_source") == "process":
        return {
            "error": "can't determine your project — no cwd, no session_id, and "
                     "$CLAUDE_CODE_SESSION_ID is unset. This MCP server's own working "
                     "directory is NOT your session's, so any candidate I picked would "
                     "target the WRONG project. Pass session_id=<fresh "
                     "$CLAUDE_CODE_SESSION_ID> (preferred) or cwd=<your project dir>.",
            "cwd_source": "process",
            "session_id": info.get("session_id"),
            "candidates": [],
            "candidate_count": 0,
        }
    if info.get("session_id"):
        _session(info["session_id"], cwd=info.get("cwd")).path = info.get("path")
    # Cap candidates so a project dir with hundreds of sessions can't blow the
    # MCP token budget.
    cands = info.get("candidates") or []
    info["candidate_count"] = len(cands)
    info["candidates"] = cands[:25]
    return info


def list_sessions(cwd: Optional[str] = None, window_hours: Optional[float] = None,
                  limit: int = 25) -> dict:
    """The session picker: past transcripts newest-first, CAPPED at ``limit``.

    Returns ``{total, returned, truncated, sessions:[{path,size,mtime,session_id,project_dir}]}``.
    The cap is load-bearing: with ``cwd=None`` ``find_transcripts`` scans every
    project dir across all accounts, which on a heavy machine is enormous — an
    uncapped result can blow the MCP token budget. The safe swap target is a
    PAST/closed session."""
    found = T.find_transcripts(cwd=cwd, window_hours=window_hours)
    return {
        "total": len(found),
        "returned": min(limit, len(found)),
        "truncated": len(found) > limit,
        "sessions": found[:limit],
    }


# --------------------------------------------------------------------------- #
# See (extract)                                                                #
# --------------------------------------------------------------------------- #
def extract(session_id: str, category: str, cwd: Optional[str] = None,
            reveal: bool = False, limit: int = 50, reveal_char_cap: int = 2000) -> dict:
    """Locate one category's spans (``transcripts.extract``). Returns LOCATORS +
    previews by default (``reveal=True`` for full text — use sparingly). Capped on BOTH
    span COUNT (``limit``) and revealed BYTES per span (``reveal_char_cap``): a handful
    of ``file_contents`` spans can be megabytes, which would blow the MCP token budget."""
    s = _session(session_id, cwd)
    spans = list(T.extract(_need_path(s), category))
    out = []
    for sp in spans[:limit]:
        if reveal:
            d = sp.to_dict()
            t = d.get("text") or ""
            if len(t) > reveal_char_cap:
                d["text"] = t[:reveal_char_cap]
                d["full_len"] = len(t)
                d["truncated"] = True
            out.append(d)
        else:
            out.append(sp.locator())
    return {"category": category, "count": len(spans), "returned": len(out),
            "truncated": len(spans) > limit, "spans": out}


def relevant_slice(session_id: str, needle: str, context_turns: int = 1,
                   cwd: Optional[str] = None, limit: int = 40) -> dict:
    """The exchange around an error/keyword/uuid (``transcripts.relevant_slice``).
    Returns compact record summaries — uuid, type, and a short preview. Rejects an
    empty needle (which matches the WHOLE transcript) and caps returned records."""
    if not (needle or "").strip():
        return {"error": "needle must be non-empty — an empty needle matches every "
                         "record and would return the whole transcript."}
    s = _session(session_id, cwd)
    recs = T.relevant_slice(_need_path(s), needle, context_turns=context_turns)
    summ = []
    for r in recs[:limit]:
        txt = P._record_text(r.raw)  # package's record->text helper
        summ.append({"uuid": r.uuid, "type": r.type, "preview": (txt or "")[:160]})
    return {"needle": needle, "matched_records": len(recs), "returned": len(summ),
            "truncated": len(recs) > limit, "records": summ}


def size_estimate(session_id: str, by_category: bool = False, cwd: Optional[str] = None) -> dict:
    """On-disk byte size + the 1 MB-budget view (``transcripts.size_estimate``).
    ``by_category=True`` buckets chars per category ("what's eating the budget")."""
    s = _session(session_id, cwd)
    return T.size_estimate(_need_path(s), by_category=by_category)


# --------------------------------------------------------------------------- #
# Detect                                                                       #
# --------------------------------------------------------------------------- #
def detect(session_id: str, cwd: Optional[str] = None, limit: int = 200) -> dict:
    """The unified WHERE+WHAT pass (``pipeline.analyze``): where each category lives
    + what's sensitive in the kept narrative. Values MASKED by default. Caps the
    (masked) narrative_findings list so a finding-dense session can't blow the MCP
    token budget — the count + flag stay accurate."""
    s = _session(session_id, cwd)
    result = PL.analyze(_ensure_parsed(s))
    nf = result.get("narrative_findings") or []
    if len(nf) > limit:
        result["narrative_findings"] = nf[:limit]
        result["narrative_findings_total"] = len(nf)
        result["narrative_findings_truncated"] = True
    return result


# --------------------------------------------------------------------------- #
# Redact (+ profile)                                                           #
# --------------------------------------------------------------------------- #
def redact_recipe(session_id: str, recipe: Optional[dict] = None,
                  cwd: Optional[str] = None) -> dict:
    """The heart: bulk structural strip + char-precise narrative mask (the bridge),
    honoring the resolved profile (allow rescue / deny codename strip).

    ``recipe`` keys: ``strip`` (list of categories; default the proven 9-set),
    ``mask_narrative`` (default True), ``profile_apply`` (default True). Stores the
    sanitized records in session state. Returns counts only — never raw originals."""
    s = _session(session_id, cwd)
    parsed = _ensure_parsed(s)
    recipe = recipe or {}
    strip = recipe.get("strip")  # None => pipeline default 9-category set
    mask = recipe.get("mask_narrative", True)
    allow = deny = None
    profile_src = None
    if recipe.get("profile_apply", True):
        resolved = PROF.resolve(s.cwd, session_id=session_id, repo_root=s.cwd)
        ents = resolved.get("entities", {}) or {}
        allow = ents.get("allow") or None
        deny = ents.get("deny") or None
        profile_src = {"allow": allow or [], "deny": deny or [],
                       "hard_floors": resolved.get("hard_floors", [])}
    red = PL.redact_recipe(parsed.raws, strip=strip, mask=mask, allow=allow, deny=deny)
    s.sanitized_raws = red["sanitized_raws"]
    s.redaction_map = red["redaction_map"]
    residual = len(P.serialize_records(s.sanitized_raws))
    return {
        "redactions": len(red["redaction_map"]),
        "by_category": _by_category(red["redaction_map"]),
        "strip_categories": list(strip) if strip is not None else list(PL.DEFAULT_STRIP_CATEGORIES),
        "residual_bytes": residual,
        "over_1mb": residual > P.FEEDBACK_BUDGET_BYTES,
        "profile_applied": profile_src,
    }


def genericize_verify(session_id: str, original_excerpt: str, generic_text: str,
                      expect_absent: Optional[list] = None, cwd: Optional[str] = None) -> dict:
    """The semantic-ceiling guardrail (the co-author writes ``generic_text``; this
    proves no leak survived + flags meaning risk). ``ok`` is the machine-decidable
    "no leak survived" verdict ONLY — meaning-preservation is the user's confirm."""
    return G.verify_genericization(original_excerpt, generic_text, expect_absent=expect_absent)


def distill_range(session_id: str, start_idx: int, end_idx: int, summary: str,
                  cwd: Optional[str] = None) -> dict:
    """Collapse a verbose record range into one faithful summary record
    (``genericize.distill_turn_range``), operating on the staged sanitized records.
    The distilled version is ALWAYS surfaced to the user for confirmation."""
    s = _session(session_id, cwd)
    if s.sanitized_raws is None:
        _ensure_parsed(s)
        s.sanitized_raws = [dict(r) for r in s.parsed.raws]
    s.sanitized_raws = G.distill_turn_range(s.sanitized_raws, start_idx, end_idx, summary)
    return {"records_after": len(s.sanitized_raws), "summary_applied": True}


# --------------------------------------------------------------------------- #
# Assemble / Preview / Gate                                                    #
# --------------------------------------------------------------------------- #
def _prepare_extra(identifier: str, cwd: Optional[str]) -> Optional[dict]:
    """Resolve+parse+redact ONE extra session for a multi-session bundle.

    ``identifier`` is either a path to a ``.jsonl`` transcript or a ``session_id``
    (resolved against ``cwd``). Runs the SAME proven redaction recipe as the primary
    (profile-aware) so every bundled session ships sanitized. Returns
    ``{path, sanitized, originals, redaction_map}`` or ``None`` if it can't be resolved
    to a file. The ``redaction_map`` is load-bearing: ``assemble`` concatenates every
    bundled session's map so the gate preview's by-category counts + samples cover the
    WHOLE bundle, not just the primary."""
    if os.path.isfile(identifier):
        path: Optional[str] = identifier
    else:
        info = L.resolve(cwd=cwd, session_id=identifier)
        path = info.get("path")
    if not path or not os.path.isfile(path):
        return None
    parsed = PL.parse_session(path)
    allow = deny = None
    resolved = PROF.resolve(cwd, repo_root=cwd)
    ents = resolved.get("entities", {}) or {}
    allow = ents.get("allow") or None
    deny = ents.get("deny") or None
    red = PL.redact_recipe(parsed.raws, allow=allow, deny=deny)
    return {"path": path, "sanitized": red["sanitized_raws"], "originals": parsed.raws,
            "redaction_map": red["redaction_map"]}


def assemble(session_id: str, description: str, effort_signal: Optional[dict] = None,
             cwd: Optional[str] = None, extra_sessions: Optional[list] = None) -> dict:
    """Build the on-disk payload under the 1 MB budget + the concise gate preview
    (``pipeline.assemble_and_preview``). Stores the Payload in session state.

    Bundles the PRIMARY session (the common case) and, when given,
    ``extra_sessions`` — a list of session_ids or transcript paths to bundle ALONGSIDE
    it (the related runs). Every extra is parsed + redacted with the same
    proven recipe; ``budget_pack`` then fits the set under the 1 MB cap newest-first,
    so the primary (most recent) is never starved by older extras — anything that
    doesn't fit is reported in ``over_budget`` and aged OUT-of-window by submit_begin.
    ``submit_begin`` swaps every selected target, so the native ``/feedback`` gather
    picks up exactly the sanitized bundle."""
    s = _session(session_id, cwd)
    parsed = _ensure_parsed(s)
    if s.sanitized_raws is None:
        # No explicit redact step — default to the proven recipe so we never ship raw.
        # Resolve and apply the profile here too (mirroring redact_recipe's handling):
        # without it a profile-DENIED codename could ship, and a profile-ALLOWED brand
        # would get needlessly masked, when the co-author calls assemble() directly.
        allow = deny = None
        resolved = PROF.resolve(s.cwd, session_id=session_id, repo_root=s.cwd)
        ents = resolved.get("entities", {}) or {}
        allow = ents.get("allow") or None
        deny = ents.get("deny") or None
        red = PL.redact_recipe(parsed.raws, allow=allow, deny=deny)
        s.sanitized_raws, s.redaction_map = red["sanitized_raws"], red["redaction_map"]
    s.description = description
    s.effort_signal = effort_signal

    targets: dict[str, list] = {s.path: s.sanitized_raws}
    originals: dict[str, list] = {s.path: parsed.raws}
    # Aggregate every bundled session's redaction_map (primary + each included extra) so
    # the gate preview's stripped_by_category + samples reflect the WHOLE bundle, not just
    # the primary (the bytes/record counts already aggregate in assemble_and_preview).
    redaction_map: list = list(s.redaction_map or [])
    extras_report: list[dict] = []
    for ident in (extra_sessions or []):
        prepared = _prepare_extra(ident, cwd or s.cwd)
        if prepared is None:
            extras_report.append({"identifier": ident, "included": False, "reason": "unresolved"})
            continue
        if prepared["path"] in targets:
            extras_report.append({"identifier": ident, "path": prepared["path"],
                                  "included": False, "reason": "duplicate"})
            continue
        targets[prepared["path"]] = prepared["sanitized"]
        originals[prepared["path"]] = prepared["originals"]
        redaction_map.extend(prepared.get("redaction_map") or [])
        extras_report.append({"identifier": ident, "path": prepared["path"], "included": True})

    ap = PL.assemble_and_preview(
        description, targets,
        originals=originals, redaction_map=redaction_map,
        effort_signal=effort_signal,
    )
    s.payload, s.preview = ap["payload"], ap["preview"]
    return {
        "targets": list(s.payload.targets),
        "total_bytes": ap["total_bytes"],
        "over_budget": ap["over_budget"],
        "sessions": s.payload.sessions,
        "extra_sessions": extras_report,
        "primary": s.path,
    }


def preview(session_id: str, cwd: Optional[str] = None) -> dict:
    """The concise included/stripped gate view (``package.diff_preview``). Never a
    wall-of-diff. Call ``assemble`` first."""
    s = _session(session_id, cwd)
    if s.preview is None:
        return {"error": "nothing assembled yet — call assemble() first"}
    pv = s.preview
    return {
        "render": pv.render(),
        "modified_records": pv.modified_records,
        "dropped_records": pv.dropped_records,
        "kept_records": pv.kept_records,
        "bytes_before": pv.bytes_before,
        "bytes_after": pv.bytes_after,
        "stripped_by_category": pv.stripped_by_category,
    }


def leak_scan(session_id: str, cwd: Optional[str] = None, candidate_limit: int = 100) -> dict:
    """The two-layer egress gate (``pipeline.egress_gate``). Returns the HARD,
    machine-decidable FLOOR (must be empty to ship) + NER CANDIDATES for self-repair
    (never a boolean veto). Call ``assemble`` first. Caps the candidate list (the
    count stays exact) so a noisy NER pass can't blow the MCP token budget."""
    s = _session(session_id, cwd)
    if s.payload is None:
        return {"error": "nothing assembled yet — call assemble() first"}
    gate = PL.egress_gate(PL.upload_text(s.payload),
                          PL.content_surface(s.sanitized_raws or [], s.description))
    s.gate = gate
    cands = gate.get("candidates") or []
    if len(cands) > candidate_limit:
        gate = {**gate, "candidates": cands[:candidate_limit], "candidates_truncated": True}
    return gate


# --------------------------------------------------------------------------- #
# Ship (the two-phase handoff)                                                 #
# --------------------------------------------------------------------------- #
def submit_begin(session_id: str, allow_live_gate: bool = False,
                 cwd: Optional[str] = None, live_session_id: Optional[str] = None) -> dict:
    """Stage the sanitized bytes for the user's interactive ``/feedback`` turn.

    ``live_session_id`` — the CURRENTLY-LIVE session id (the skill passes a fresh
    ``$CLAUDE_CODE_SESSION_ID`` here). It identifies which on-disk file the native
    gather would co-upload raw, and which file begin_swap must refuse to swap.
    Falls back to ``session_id`` (correct when the feedback IS about the live session);
    never the MCP server's frozen spawn-time env.

    (1) The gather-gate: read the LIVE session's on-disk bytes *as of now* and run
        the DETERMINISTIC egress scan over them (secrets + PII-regex + paths +
        env-metadata + IP-markers — the floor PLUS the structural-leak detectors, but
        no NER, which hallucinates over raw JSONL). So the co-author sees what the live
        session would co-upload *raw*. If it's not clean and ``allow_live_gate`` is
        False, REFUSE and recommend checkpoint (the airtight path). This catches the
        file-contents / paths / cwd-gitBranch leaks the secret-only floor missed.
    (2) Fail-closed: re-assert the deterministic floor over the staged payload bytes
        here (the mechanism, not just the model's leak_scan call, holds the line).
    (3) ``begin_swap`` the targets (live-id refusal + journaled windowing: age every
        OTHER past file out of the +7d window so the native gather matches what we
        staged).
    Returns the journal path + the exact ``/feedback`` instruction."""
    s = _session(session_id, cwd)
    if s.payload is None:
        return {"staged": False, "error": "nothing assembled yet — call assemble() first"}

    # Nothing fit the 1 MB /feedback gather budget → assemble dropped every session and
    # payload.targets is empty. begin_swap() raises ValueError on an empty mapping, so
    # refuse GRACEFULLY here with an actionable next step instead of throwing out of the
    # tool. (A legitimate case: one big session, or all sessions over-budget.)
    if not s.payload.targets:
        return {
            "staged": False,
            "reason": "nothing fit the 1 MB /feedback gather budget — every session was "
                      "dropped over-budget, so there is nothing to stage.",
            "recommend": "shrink the bundle: drop extra_sessions, distill the largest "
                         "session (distill_range) or send words-only, then re-assemble. "
                         "assemble()'s over_budget list shows exactly what didn't fit.",
            "over_budget": [p for p, _ in s.payload.dropped],
        }

    # The mechanism holds the floor: never stage a payload whose own bytes fail the
    # deterministic floor, regardless of whether the co-author ran leak_scan first.
    payload_gate = PL.egress_gate(PL.upload_text(s.payload),
                                  PL.content_surface(s.sanitized_raws or [], s.description))
    if not payload_gate.get("floor_clean") and not allow_live_gate:
        return {
            "staged": False,
            "reason": "the staged payload itself still trips the deterministic floor",
            "recommend": "run leak_scan(), self-repair the floor findings, re-assemble, "
                         "then submit_begin again (or pass allow_live_gate=True to override).",
            "payload_floor": payload_gate.get("floor"),
            "gather_floor_clean": False,
        }

    # Identify the LIVE session (the one /feedback co-uploads raw) by the freshest
    # authoritative id: the skill's `live_session_id` if given, else the env-resolved id.
    # NOT the target id (s.session_id) — the target is usually a *past* session, and
    # conflating them makes begin_swap's live-id refusal block the legitimate target.
    info = L.resolve(cwd=s.cwd)
    live_id = live_session_id or info.get("live_session_id")
    # Resolve the live session BY ID — this anchors path + the windowing menu on the
    # live file's OWN project dir (identity), so even if s.cwd is unknown we never
    # window out an unrelated project's sessions (server-vs-user cwd).
    live_info = L.resolve(cwd=s.cwd, session_id=live_id) if live_id else {}
    live_path = live_info.get("path")

    # Fail-closed: if we can't positively identify the live session, we can't prove what
    # /feedback would co-upload — and begin_swap's identity refusal is also disabled
    # without a live id. Don't assume clean; recommend checkpoint or an explicit override.
    # The skill should pass live_session_id.
    if not live_id and not allow_live_gate:
        return {
            "staged": False,
            "reason": "couldn't identify the live session to scan what /feedback would co-upload",
            "recommend": "pass live_session_id=<fresh $CLAUDE_CODE_SESSION_ID>, or checkpoint "
                         "(/clear, then submit from the thin session), or allow_live_gate=True "
                         "if you are certain no live session will co-upload.",
            "gather_floor_clean": False,
        }

    live_contrib: dict = {"path": None, "floor_clean": True}
    if live_path and os.path.isfile(live_path) and live_path not in s.payload.targets:
        live_text = Path(live_path).read_text(encoding="utf-8", errors="replace")
        # The FULL deterministic egress scan over the raw live bytes (not just
        # secrets+PII): a content-rich live session correctly trips the gate.
        live_findings = R_deterministic_leak_scan(live_text)
        by_cat: dict = {}
        for f in live_findings:
            by_cat[f.category] = by_cat.get(f.category, 0) + 1
        live_contrib = {
            "path": live_path,
            "bytes": os.path.getsize(live_path),
            "floor_clean": not live_findings,
            "finding_count": len(live_findings),
            "by_category": by_cat,
        }
    gather_floor_clean = bool(live_contrib["floor_clean"])
    if not gather_floor_clean and not allow_live_gate:
        return {
            "staged": False,
            "reason": "the live session would co-upload sensitive content (raw)",
            "recommend": "checkpoint: run /clear (closes & flushes the live file → it "
                         "becomes a swappable past session), then submit from the fresh "
                         "thin session. Or call submit_begin(allow_live_gate=True) to override.",
            "live_session_contribution": live_contrib,
            "gather_floor_clean": False,
        }

    # Age every OTHER past file in the project out of the +7d window (journaled).
    # Prefer the live session's own project menu (identity-anchored); fall back to
    # the cwd-resolved menu only if we couldn't resolve the live session by id.
    targets = set(s.payload.targets)
    menu = live_info.get("candidates") or info.get("candidates") or []
    others = [c["path"] for c in menu
              if c.get("path") and c["path"] not in targets and c["path"] != live_path]
    handle = P.begin_swap(s.payload.targets, live_session_id=live_id,
                          window_out=others, window="week")
    s.journal_path = handle.journal_path
    return {
        "staged": True,
        "journal_path": handle.journal_path,
        "scope_instruction": "Run `/feedback` → pick the **+7 days** window (anything ≥ the "
                             "session I staged is safe — I aged the rest out) → submit. Then "
                             "press **1** (or say 'done') and I'll restore your originals.",
        "swapped_targets": list(s.payload.targets),
        "windowed_out": len(others),
        "live_session_contribution": live_contrib,
        "gather_floor_clean": gather_floor_clean,
    }


def submit_finish(session_id: str, cwd: Optional[str] = None) -> dict:
    """Restore the originals byte-exact after the user's ``/feedback`` run
    (``finish_swap``). Idempotent.

    If the in-memory journal path is gone (the MCP server restarted mid-flow),
    fall back to ``package.recover()`` SCOPED to this session's transcript path, so it
    restores THIS swap now without un-swapping a concurrent ``/fb`` flow's still-staged
    journal. Data was never at risk (the journal is durable); this just restores the
    user's real transcript immediately instead of waiting for the next ``/fb`` startup.
    If the session's path can't be resolved, we do NOT blanket-recover (that could
    un-swap someone else) — recover_orphans() on the next startup heals it safely."""
    s = _session(session_id, cwd)
    if not s.journal_path:
        if not s.path:
            return {"restored": False,
                    "error": "no staged swap for this session, and its transcript path "
                             "couldn't be resolved to scope a recover (try recover_orphans)."}
        healed = P.recover(only_paths={s.path})
        restored = [h for h in healed if h.get("status") == "restored"]
        if restored:
            return {"restored": True, "via": "recover", "healed": restored,
                    "note": "restored this session's swap from its durable journal after a restart"}
        return {"restored": False, "error": "no staged swap for this session (submit_begin first)"}
    report = P.finish_swap(s.journal_path, raise_on_failure=False)
    s.journal_path = None if report.ok else s.journal_path
    return {
        "restored": report.ok,
        "already_done": report.already_done,
        "restored_files": report.restored,
        "mtime_restored": report.mtime_restored,
        "failures": report.failures,
    }


def recover_orphans(cwd: Optional[str] = None) -> dict:
    """Self-heal any swap orphaned by a crash/exit (``package.recover``). Run on
    ``/fb`` startup so a swap left dangling between submit_begin and submit_finish is
    restored before the user does anything."""
    healed = P.recover()
    return {"journals": healed, "healed": sum(1 for h in healed if h.get("status") == "restored")}


def stage_review(session_id: str, review_dir: Optional[str] = None,
                 cwd: Optional[str] = None) -> dict:
    """Write a non-destructive reviewable copy of the bundle (the ``/feedback save``
    look) without touching real paths (``Payload.stage``). For the cautious user."""
    s = _session(session_id, cwd)
    if s.payload is None:
        return {"error": "nothing assembled yet — call assemble() first"}
    target = Path(review_dir) if review_dir else Path(P.DEFAULT_BACKUP_ROOT).parent / "review" / session_id
    written = s.payload.stage(target)
    return {"review_dir": str(target), "files": written}


# --------------------------------------------------------------------------- #
# Profile (set-once intelligence)                                              #
# --------------------------------------------------------------------------- #
def profile_resolve(cwd: Optional[str] = None, session_id: Optional[str] = None) -> dict:
    """The effective privacy policy after precedence (global ⊕ repo .feedbackpolicy ⊕
    session; most-specific-wins; hard floors). For transparency / pre-apply."""
    return PROF.resolve(cwd, session_id=session_id, repo_root=cwd)


def profile_learn(correction: dict) -> dict:
    """Record a correction (rescued brand, added redaction, 'always strip X here') as
    a durable learned rule, narrowest sensible scope. Next resolve applies it silently."""
    return PROF.learn(correction)


def policy_read(repo_path: str) -> dict:
    """Raw read of a repo's committed ``.feedbackpolicy`` (transparency)."""
    return PROF.read_policy(repo_path)


# --------------------------------------------------------------------------- #
# Question-loop — the CLI consumer of the org's open-questions list             #
# --------------------------------------------------------------------------- #
def open_questions(report_context: str = "", surface: str = "cli",
                   cwd: Optional[str] = None) -> dict:
    """Return the SINGLE most-relevant open question for this report — never a survey.

    Reads the living open-questions list produced by the Feedback OS (fb-os)
    at ``$FB_ASSIST_OPEN_QUESTIONS`` or ``~/.config/fb-assist/open-questions.json``.

    This is a faithful DUPLICATE of ``fb_os.questions.rank_for``'s selection rule, NOT
    an import of it: ``fb-assist`` is a standalone, separately-clonable package and
    cannot depend on ``fb-os``, so the rule is necessarily reimplemented here. The two
    are pinned in lockstep by a shared-fixture seam test (fb-os ``tests/test_seam.py``)
    so the duplicate can't drift. The rule: candidate = ``status=="open"`` AND not
    expired AND (``match.surfaces`` empty OR ``surface`` in it); ``relevance`` = fraction
    of ``match.keywords`` present in the report; ``score = priority × relevance``; return
    the single argmax with ``score > 0`` and the SAME tie-break as ``rank_for`` —
    ``(priority, uncertainty, id)`` highest-wins (a tie favors the question the org is
    MOST uncertain about, i.e. most worth asking). This closes the CLI side of the
    bidirectional question-loop (the producer is fb-os; the seam is this file's path)."""
    import json
    import os
    import datetime as _dt

    path = (os.environ.get("FB_ASSIST_OPEN_QUESTIONS")
            or os.path.expanduser("~/.config/fb-assist/open-questions.json"))
    if not os.path.isfile(path):
        return {"question": None, "open_count": 0,
                "reason": "no open-questions file yet (Feedback OS not publishing)", "path": path}
    try:
        data = json.loads(open(path, encoding="utf-8").read())
    except Exception as e:  # noqa: BLE001
        return {"question": None, "open_count": 0, "reason": f"unreadable: {e}", "path": path}

    now = _dt.datetime.now(_dt.timezone.utc)
    report_low = (report_context or "").lower()

    def _expired(exp) -> bool:
        # Mirror fb_os.questions._parse_iso/is_expired: tolerate Z and naive stamps
        # (coerce naive -> UTC) and never raise — an unparseable stamp is "not expired".
        if not exp:
            return False
        try:
            dt = _dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00"))
        except Exception:  # noqa: BLE001
            return False
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        return now >= dt

    def _is_open(q: dict) -> bool:
        if q.get("status") != "open":
            return False
        if _expired(q.get("expires_at")):
            return False
        surfaces = (q.get("match") or {}).get("surfaces") or []
        return (not surfaces) or (surface in surfaces)

    def _relevance(q: dict) -> float:
        kws = [k.lower() for k in (q.get("match") or {}).get("keywords", []) if k]
        if not kws:
            return 0.0
        return sum(1 for k in kws if k in report_low) / len(kws)

    cands = [q for q in data.get("questions", []) if _is_open(q)]
    # score desc, then the SAME deterministic tie-break as fb_os.questions.rank_for:
    # (priority, uncertainty, id) highest-wins. Sort by the first four tuple slots only
    # (ids are unique) so two dicts are never compared.
    scored = sorted(
        ((q.get("priority", 0.0) * _relevance(q), float(q.get("priority", 0.0)),
          float(q.get("uncertainty", 0.0)), str(q.get("id", "")), q) for q in cands),
        key=lambda t: t[:4],
        reverse=True,
    )
    best = None
    for score, _p, _u, _id, q in scored:
        if score > 0.0:
            best = {"id": q.get("id"), "question": q.get("question"),
                    "hypothesis": q.get("hypothesis"), "cluster_label": q.get("cluster_label"),
                    "score": round(score, 3)}
            break
    return {"question": best, "open_count": len(cands),
            "generator": data.get("generator"), "path": path}


# --------------------------------------------------------------------------- #
# tiny shims so submit_begin doesn't import the whole redact module surface     #
# --------------------------------------------------------------------------- #
def R_scan_secrets(text: str):
    from . import redact as R
    return R.scan_secrets(text)


def R_scan_pii_regex(text: str):
    from . import redact as R
    return R._scan_pii_regex(text)


def R_deterministic_leak_scan(text: str):
    """The FP-resistant egress scan for RAW bytes (secrets + PII-regex + paths + env +
    IP-markers; NO NER). The right gate for the un-redacted live transcript."""
    from . import redact as R
    return R.deterministic_leak_scan(text)


# --------------------------------------------------------------------------- #
# Register every tool with FastMCP. Defining them as plain module functions and  #
# registering here keeps them directly callable from tests (no MCP round-trip).  #
# --------------------------------------------------------------------------- #
TOOLS = [
    locate_session, list_sessions,
    extract, relevant_slice, size_estimate,
    detect,
    redact_recipe, genericize_verify, distill_range,
    assemble, preview, leak_scan,
    submit_begin, submit_finish, recover_orphans, stage_review,
    profile_resolve, profile_learn, policy_read,
    open_questions,
]
for _fn in TOOLS:
    mcp.tool()(_fn)


def main(argv=None) -> int:
    """Run the stdio MCP server (the entry point registered in ~/.claude.json)."""
    mcp.run()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
