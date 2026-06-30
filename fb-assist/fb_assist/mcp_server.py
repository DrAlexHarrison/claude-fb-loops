"""fb-assist MCP server — the in-session co-author's model-invocable toolbox.

An always-on stdio MCP server (FastMCP) that exposes the proven fb-assist toolbox
as ``mcp__fb-assist__*`` tools. The ``/fb`` skill morphs the live Claude into the
feedback co-author; this server is the hands it works with.

DESIGN RULE: every tool is a THIN wrapper over the library (``pipeline`` /
``package`` / ``locate`` / ``profile`` / ``genericize``). No business logic lives
here — the validated call-sequence is in ``pipeline.py`` (re-asserted by
``test_pipeline.py``), so the runtime and the proof cannot drift. Tools return
COMPACT JSON (locators, counts, MASKED samples) and never dump a full transcript
or a raw secret into the model's context (gotcha #2).

State: per-session, keyed by ``session_id``, held in an in-memory dict (the parsed
records, the redaction map, the sanitized records, the staged journal path). State
is ephemeral; the on-disk journal is the durable truth for restore. The co-author
passes ``session_id`` fresh each turn (the skill reads ``$CLAUDE_CODE_SESSION_ID``
per-turn — avoids MCP-process env staleness across /clear, OPEN-2).

Local only. No network. No paid software.
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
    back to the CLI (dogfooding find, 2026-06-30). Loading them lazily lets the
    server connect in well under a second; the heavy load happens on first *use*
    (a detect/redact call), when the user is actively working, not at connect time.
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
    around it). Identity-first ``is_live`` (FIX-3). Returns
    ``{session_id, path, project_dir, config_dir, account, is_live, candidates, ...}``."""
    info = L.resolve(cwd=cwd, session_id=session_id)
    if info.get("session_id"):
        _session(info["session_id"], cwd=info.get("cwd")).path = info.get("path")
    # Cap candidates so a project dir with hundreds of sessions can't blow the
    # MCP token budget (same dogfooding class as list_sessions).
    cands = info.get("candidates") or []
    info["candidate_count"] = len(cands)
    info["candidates"] = cands[:25]
    return info


def list_sessions(cwd: Optional[str] = None, window_hours: Optional[float] = None,
                  limit: int = 25) -> dict:
    """The session picker: past transcripts newest-first, CAPPED at ``limit``.

    Returns ``{total, returned, truncated, sessions:[{path,size,mtime,session_id,project_dir}]}``.
    The cap is load-bearing: with ``cwd=None`` ``find_transcripts`` scans every
    project dir across all accounts, which on a heavy machine is enormous — the
    uncapped version returned 770k characters and blew the MCP token budget
    (dogfooding find, 2026-06-30). The safe swap target is a PAST/closed session (§15)."""
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
            reveal: bool = False, limit: int = 50) -> dict:
    """Locate one category's spans (``transcripts.extract``). Returns LOCATORS +
    previews by default (``reveal=True`` for full text — use sparingly). Capped."""
    s = _session(session_id, cwd)
    spans = list(T.extract(_need_path(s), category))
    out = [(sp.to_dict() if reveal else sp.locator()) for sp in spans[:limit]]
    return {"category": category, "count": len(spans), "returned": len(out), "spans": out}


def relevant_slice(session_id: str, needle: str, context_turns: int = 1,
                   cwd: Optional[str] = None) -> dict:
    """The exchange around an error/keyword/uuid (``transcripts.relevant_slice``).
    Returns compact record summaries — uuid, type, and a short preview."""
    s = _session(session_id, cwd)
    recs = T.relevant_slice(_need_path(s), needle, context_turns=context_turns)
    summ = []
    for r in recs:
        txt = P._record_text(r.raw)  # package's record->text helper
        summ.append({"uuid": r.uuid, "type": r.type, "preview": (txt or "")[:160]})
    return {"needle": needle, "matched_records": len(recs), "records": summ}


def size_estimate(session_id: str, by_category: bool = False, cwd: Optional[str] = None) -> dict:
    """On-disk byte size + the 1 MB-budget view (``transcripts.size_estimate``).
    ``by_category=True`` buckets chars per category ("what's eating the budget")."""
    s = _session(session_id, cwd)
    return T.size_estimate(_need_path(s), by_category=by_category)


# --------------------------------------------------------------------------- #
# Detect                                                                       #
# --------------------------------------------------------------------------- #
def detect(session_id: str, cwd: Optional[str] = None) -> dict:
    """The unified WHERE+WHAT pass (``pipeline.analyze``): where each category lives
    + what's sensitive in the kept narrative. Values MASKED by default (gotcha #2)."""
    s = _session(session_id, cwd)
    return PL.analyze(_ensure_parsed(s))


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
    The distilled version is ALWAYS surfaced to the user for confirmation (sacred)."""
    s = _session(session_id, cwd)
    if s.sanitized_raws is None:
        _ensure_parsed(s)
        s.sanitized_raws = [dict(r) for r in s.parsed.raws]
    s.sanitized_raws = G.distill_turn_range(s.sanitized_raws, start_idx, end_idx, summary)
    return {"records_after": len(s.sanitized_raws), "summary_applied": True}


# --------------------------------------------------------------------------- #
# Assemble / Preview / Gate                                                    #
# --------------------------------------------------------------------------- #
def assemble(session_id: str, description: str, effort_signal: Optional[dict] = None,
             cwd: Optional[str] = None) -> dict:
    """Build the on-disk payload under the 1 MB budget + the concise gate preview
    (``pipeline.assemble_and_preview``). Stores the Payload in session state.

    NOTE: bundles the PRIMARY session (the common one-session case, spec §5).
    Multi-session bundling is a documented v-next item."""
    s = _session(session_id, cwd)
    parsed = _ensure_parsed(s)
    if s.sanitized_raws is None:
        # No explicit redact step — default to the proven recipe so we never ship raw.
        red = PL.redact_recipe(parsed.raws)
        s.sanitized_raws, s.redaction_map = red["sanitized_raws"], red["redaction_map"]
    s.description = description
    s.effort_signal = effort_signal
    ap = PL.assemble_and_preview(
        description, {s.path: s.sanitized_raws},
        originals={s.path: parsed.raws}, redaction_map=s.redaction_map or [],
        effort_signal=effort_signal,
    )
    s.payload, s.preview = ap["payload"], ap["preview"]
    return {
        "targets": list(s.payload.targets),
        "total_bytes": ap["total_bytes"],
        "over_budget": ap["over_budget"],
        "sessions": s.payload.sessions,
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


def leak_scan(session_id: str, cwd: Optional[str] = None) -> dict:
    """The two-layer egress gate (``pipeline.egress_gate``). Returns the HARD,
    machine-decidable FLOOR (must be empty to ship) + NER CANDIDATES for self-repair
    (never a boolean veto). Call ``assemble`` first."""
    s = _session(session_id, cwd)
    if s.payload is None:
        return {"error": "nothing assembled yet — call assemble() first"}
    gate = PL.egress_gate(PL.upload_text(s.payload),
                          PL.content_surface(s.sanitized_raws or [], s.description))
    s.gate = gate
    return gate


# --------------------------------------------------------------------------- #
# Ship (the two-phase handoff)                                                 #
# --------------------------------------------------------------------------- #
def submit_begin(session_id: str, allow_live_gate: bool = False,
                 cwd: Optional[str] = None) -> dict:
    """Stage the sanitized bytes for the user's interactive ``/feedback`` turn.

    (1) FIX 1 — the gather-gate: read the LIVE session's on-disk bytes *as of now*
        and run the deterministic floor over them, so the co-author sees exactly
        what the live session would co-upload. If it's not clean and
        ``allow_live_gate`` is False, REFUSE and recommend checkpoint (the airtight
        path) — the live tail is what the staged-bytes scan can't cover.
    (2) ``begin_swap`` the targets (FIX 3 live-id refusal + FIX 2 journaled
        windowing: age every OTHER past file out of the +7d window so the native
        gather matches what we staged).
    Returns the journal path + the exact ``/feedback`` instruction."""
    s = _session(session_id, cwd)
    if s.payload is None:
        return {"staged": False, "error": "nothing assembled yet — call assemble() first"}

    info = L.resolve(cwd=s.cwd)
    live_path = info.get("path") if info.get("is_live") else None
    live_id = info.get("live_session_id")
    live_contrib: dict = {"path": None, "floor_clean": True}
    if live_path and os.path.isfile(live_path) and live_path not in s.payload.targets:
        live_text = Path(live_path).read_text(encoding="utf-8", errors="replace")
        fs = R_scan_secrets(live_text)
        fp = R_scan_pii_regex(live_text)
        live_contrib = {
            "path": live_path,
            "bytes": os.path.getsize(live_path),
            "floor_clean": not fs and not fp,
            "secret_count": len(fs),
            "pii_floor_count": len(fp),
        }
    gather_floor_clean = bool(live_contrib["floor_clean"])
    if not gather_floor_clean and not allow_live_gate:
        return {
            "staged": False,
            "reason": "the live session would co-upload sensitive content",
            "recommend": "checkpoint: run /clear (closes & flushes the live file → it "
                         "becomes a swappable past session), then submit from the fresh "
                         "thin session. Or call submit_begin(allow_live_gate=True) to override.",
            "live_session_contribution": live_contrib,
            "gather_floor_clean": False,
        }

    # Age every OTHER past file in the project out of the +7d window (journaled).
    targets = set(s.payload.targets)
    others = [c["path"] for c in (info.get("candidates") or [])
              if c.get("path") and c["path"] not in targets and c["path"] != live_path]
    handle = P.begin_swap(s.payload.targets, live_session_id=live_id,
                          window_out=others, window="week")
    s.journal_path = handle.journal_path
    return {
        "staged": True,
        "journal_path": handle.journal_path,
        "scope_instruction": "Run /feedback, choose scope +7 days (so it gathers the session "
                             "I prepared), submit, then tell me 'done'.",
        "swapped_targets": list(s.payload.targets),
        "windowed_out": len(others),
        "live_session_contribution": live_contrib,
        "gather_floor_clean": gather_floor_clean,
    }


def submit_finish(session_id: str, cwd: Optional[str] = None) -> dict:
    """Restore the originals byte-exact after the user's ``/feedback`` run
    (``finish_swap``). Idempotent."""
    s = _session(session_id, cwd)
    if not s.journal_path:
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
# Question-loop (§14) — the CLI consumer of the org's open-questions list       #
# --------------------------------------------------------------------------- #
def open_questions(report_context: str = "", surface: str = "cli",
                   cwd: Optional[str] = None) -> dict:
    """Return the SINGLE most-relevant open question for this report — never a survey.

    Reads the living open-questions list produced by the Feedback OS (fb-os / Build 1)
    at ``$FB_ASSIST_OPEN_QUESTIONS`` or ``~/.config/fb-assist/open-questions.json`` and
    applies the SAME selection rule as ``fb_os.questions.rank_for`` (kept in sync so the
    two ends can't drift): candidate = ``status=="open"`` AND not expired AND
    (``match.surfaces`` empty OR ``surface`` in it); ``relevance`` = fraction of
    ``match.keywords`` present in the report; ``score = priority × relevance``; return the
    single argmax with ``score > 0``, else ``None``. This closes the CLI side of the
    bidirectional question-loop (the producer is Build 1; the seam is this file's path)."""
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

    def _is_open(q: dict) -> bool:
        if q.get("status") != "open":
            return False
        exp = q.get("expires_at")
        if exp:
            try:
                if now >= _dt.datetime.fromisoformat(str(exp).replace("Z", "+00:00")):
                    return False
            except ValueError:
                pass
        surfaces = (q.get("match") or {}).get("surfaces") or []
        return (not surfaces) or (surface in surfaces)

    def _relevance(q: dict) -> float:
        kws = [k.lower() for k in (q.get("match") or {}).get("keywords", []) if k]
        if not kws:
            return 0.0
        return sum(1 for k in kws if k in report_low) / len(kws)

    cands = [q for q in data.get("questions", []) if _is_open(q)]
    # score, then deterministic tie-break (priority desc, uncertainty asc, id)
    scored = sorted(
        ((q.get("priority", 0.0) * _relevance(q), q.get("priority", 0.0),
          -q.get("uncertainty", 0.0), str(q.get("id", "")), q) for q in cands),
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
