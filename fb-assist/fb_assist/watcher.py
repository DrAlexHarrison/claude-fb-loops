"""fb_assist.watcher — the frustration/delight signal-capture hook.

The bug or breakthrough nobody bothers to report evaporates the moment the user moves
on. This hook watches Claude Code session events for a few high-precision tells (a
retry storm, a rage pattern, "ugh/wtf", "perfect/finally") and offers a single one-tap
``/fb`` nudge — precision over recall, since a false nudge nags, and each signal-class
fires at most once per session. Nothing is ever sent; only the triggering needle + turn
range is recorded for ``/fb`` to pick up. As a hook it always exits 0 and never raises —
a hook must never break the session.

Pure-stdlib, no network, no sibling imports — it must run standalone as a hook.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

__all__ = [
    "WatcherState",
    "detect_sentiment",
    "detect_rage",
    "register_tool_event",
    "should_notify",
    "record_predraft",
    "make_nudge",
    "load_state",
    "save_state",
    "state_path",
    "watcher_dir",
    "disable_file_path",
    "is_disabled",
    "dispatch",
    "handle_prompt",
    "handle_post_tool",
    "main",
    "SIGNAL_CLASSES",
    "RETRY_STORM_THRESHOLD",
    "RETRY_WINDOW_S",
]

# --------------------------------------------------------------------------- #
# Tunables (named once, precision-first)                                       #
# --------------------------------------------------------------------------- #
RETRY_STORM_THRESHOLD = 3          # same tool erroring N times => storm
RETRY_WINDOW_S = 120.0             # ...within this short rolling window
REPEAT_LOOKBACK = 3                # near-identical prompt within last N prompts => rage
RECENT_PROMPTS_KEEP = 5            # bounded history we retain per session

# Signal classes, in surfacing-priority order (a single nudge picks the first
# eligible one — retry storms are the most objective tell, delight the softest).
SIGNAL_CLASSES = ("retry_storm", "rage", "frustration", "delight")

# Sentiment lexicons. Deliberately TIGHT — every token here is a near-unambiguous
# tell. Word boundaries keep precision (no matching inside larger words). "wtf"
# IS a signal; "fine"/"ok" are not.
_FRUSTRATION_RE = re.compile(
    r"\b(?:ugh|wtf|come on|seriously)\b"
    r"|\bstill (?:not|broken)\b"
    r"|\bwhy (?:won'?t|isn'?t)\b"
    r"|\bthis is broken\b",
    re.IGNORECASE,
)
_DELIGHT_RE = re.compile(
    r"\b(?:nice|perfect|wow|finally|exactly)\b"
    r"|\blove (?:it|this)\b"
    r"|\bso good\b",
    re.IGNORECASE,
)


# --------------------------------------------------------------------------- #
# Per-session state                                                            #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class WatcherState:
    """Everything the watcher remembers for one session.

    Persisted as JSON at :func:`state_path`. ``notified`` is the offer-once ledger;
    ``predraft`` is the consent-ready handoff for ``/fb`` (one entry per fired
    signal-class: the needle + approximate turn range/timestamp). ``pending`` stages
    signals detected on non-prompt events so the next UserPromptSubmit can surface
    them (only that event can inject context back to the model).
    """

    session_id: str = ""
    turn: int = 0                                              # ~UserPromptSubmit count
    notified: set = dataclasses.field(default_factory=set)     # signal-classes already offered
    predraft: dict = dataclasses.field(default_factory=dict)   # signal_class -> {needle, turn_range, ...}
    tool_errors: dict = dataclasses.field(default_factory=dict)  # tool_name -> [error_ts, ...]
    recent_prompts: list = dataclasses.field(default_factory=list)  # normalized, bounded
    pending: list = dataclasses.field(default_factory=list)    # staged signal-classes awaiting a prompt
    last_tool_failed: bool = False                             # for /clear-after-failure rage

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["notified"] = sorted(self.notified)  # set -> stable list for JSON
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "WatcherState":
        return cls(
            session_id=str(d.get("session_id", "")),
            turn=int(d.get("turn", 0)),
            notified=set(d.get("notified", []) or []),
            predraft=dict(d.get("predraft", {}) or {}),
            tool_errors={k: list(v) for k, v in (d.get("tool_errors", {}) or {}).items()},
            recent_prompts=list(d.get("recent_prompts", []) or []),
            pending=list(d.get("pending", []) or []),
            last_tool_failed=bool(d.get("last_tool_failed", False)),
        )


# --------------------------------------------------------------------------- #
# Paths — every location is env-overridable so tests stay hermetic             #
# --------------------------------------------------------------------------- #
def watcher_dir() -> Path:
    """Directory holding per-session state. ``$FB_ASSIST_WATCHER_DIR`` overrides
    the ``/tmp`` default (the default mirrors the spec's
    ``/tmp/fb-assist-watcher-<session_id>.json``)."""
    return Path(os.environ.get("FB_ASSIST_WATCHER_DIR", "/tmp"))


def state_path(session_id: str, state_dir: Optional[os.PathLike] = None) -> Path:
    base = Path(state_dir) if state_dir is not None else watcher_dir()
    safe = _safe_id(session_id)
    return base / f"fb-assist-watcher-{safe}.json"


def disable_file_path() -> Path:
    """The kill switch. ``$FB_ASSIST_WATCHER_OFF`` overrides the default
    ``~/.config/fb-assist/watcher.off`` (tests point it at a tmp path)."""
    override = os.environ.get("FB_ASSIST_WATCHER_OFF")
    if override:
        return Path(override)
    return Path.home() / ".config" / "fb-assist" / "watcher.off"


def is_disabled(off_path: Optional[os.PathLike] = None) -> bool:
    """True if the watcher is switched off (the off-file exists). Best-effort —
    any stat error is treated as 'not disabled' (we never fail the session)."""
    p = Path(off_path) if off_path is not None else disable_file_path()
    try:
        return p.exists()
    except OSError:
        return False


def _safe_id(session_id: str) -> str:
    """Keep the state filename to a safe, flat token (session ids are normally
    uuids, but never trust input enough to let it traverse the filesystem)."""
    sid = session_id or "unknown"
    return re.sub(r"[^A-Za-z0-9._-]", "_", sid)[:128]


def load_state(session_id: str, *, state_dir: Optional[os.PathLike] = None) -> WatcherState:
    """Load a session's state, or a fresh one if absent/corrupt. Never raises —
    a missing or unreadable state file just means 'start clean'."""
    p = state_path(session_id, state_dir)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        st = WatcherState.from_dict(data)
        if not st.session_id:
            st.session_id = session_id
        return st
    except (OSError, ValueError, TypeError):
        return WatcherState(session_id=session_id)


def save_state(state: WatcherState, *, state_dir: Optional[os.PathLike] = None) -> Path:
    """Persist state (best-effort, atomic-ish via tmp+rename). Returns the path."""
    p = state_path(state.session_id, state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(state.to_dict(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)
    return p


# --------------------------------------------------------------------------- #
# PURE detectors                                                               #
# --------------------------------------------------------------------------- #
def detect_sentiment(prompt: Optional[str]) -> Optional[str]:
    """Classify a prompt as ``"frustration"``, ``"delight"``, or ``None``.

    Precision-first: matches only the tight lexicons above, frustration taking
    precedence (catching the bug matters more than catching the cheer). ``None``
    for anything neutral — the common case, and the one we must not nag on.
    """
    text = prompt or ""
    if _FRUSTRATION_RE.search(text):
        return "frustration"
    if _DELIGHT_RE.search(text):
        return "delight"
    return None


def _norm_prompt(prompt: Optional[str]) -> str:
    """Collapse whitespace + lowercase, so 'fix it' / ' Fix  it ' compare equal."""
    return " ".join((prompt or "").split()).lower()


def detect_rage(state: WatcherState, prompt: Optional[str]) -> Optional[str]:
    """Detect a rage pattern, returning ``"rage"`` or ``None`` (pure over ``state``).

    Two high-precision tells:
      * a **near-identical prompt** repeated within the last few turns (the user is
        re-asking the same thing because it isn't working);
      * a bare **``/clear`` (or empty prompt) right after a tool failure** — the
        classic "burn it down and start over" move.
    """
    norm = _norm_prompt(prompt)
    if not norm:
        # An empty/no-text prompt right after a failure reads as a frustrated reset.
        return "rage" if state.last_tool_failed else None
    recent = [_norm_prompt(p) for p in state.recent_prompts[-REPEAT_LOOKBACK:]]
    if norm in recent:
        return "rage"
    if norm in ("/clear", "clear") and state.last_tool_failed:
        return "rage"
    return None


def register_tool_event(
    state: WatcherState,
    tool_name: str,
    is_error: bool,
    *,
    now: Optional[float] = None,
    window_s: float = RETRY_WINDOW_S,
    threshold: int = RETRY_STORM_THRESHOLD,
) -> bool:
    """Record one PostToolUse outcome; return True iff this completes a retry storm.

    A storm = the **same** ``tool_name`` erroring ``threshold`` (default 3) times
    within ``window_s``. Mutates ``state.tool_errors`` (a per-tool list of error
    timestamps). A **success clears that tool's streak** (precision: a storm is a
    run of failures, not failures interleaved with wins), and other tools never
    count toward each other. ``now`` is injectable for deterministic tests.
    """
    now = time.time() if now is None else now
    tool_name = tool_name or "?"
    errs = list(state.tool_errors.get(tool_name, []))
    if not is_error:
        # A win breaks the streak for this tool.
        if tool_name in state.tool_errors:
            state.tool_errors.pop(tool_name, None)
        return False
    # Keep only errors inside the rolling window, then add this one.
    errs = [t for t in errs if (now - t) <= window_s]
    errs.append(now)
    state.tool_errors[tool_name] = errs
    return len(errs) >= threshold


def should_notify(state: WatcherState, signal_class: str, disabled: bool = False) -> bool:
    """Offer-once gate: True only if not disabled and this class hasn't fired yet."""
    if disabled:
        return False
    return signal_class not in state.notified


def record_predraft(
    state: WatcherState,
    signal_class: str,
    needle: str,
    *,
    turn_range: Optional[Sequence[int]] = None,
    now: Optional[float] = None,
) -> dict:
    """Stash the consent-ready handoff for ``/fb`` (the pre-draft).

    Records *what* tripped the signal (``needle``) and *roughly where* (an
    approximate ``turn_range`` + ``turn`` + ``timestamp``) so saying "yes" to the
    nudge is one-tap: the co-author reads this and jumps straight to the moment.
    """
    if turn_range is None:
        turn_range = [max(0, state.turn - 1), state.turn]
    entry = {
        "signal_class": signal_class,
        "needle": needle,
        "turn": state.turn,
        "turn_range": list(turn_range),
        "timestamp": time.time() if now is None else now,
    }
    state.predraft[signal_class] = entry
    return entry


def make_nudge(signal_class: str) -> str:
    """The tiny one-line nudge for a signal-class. Delight gets a positive flavor;
    the rough-patch signals share the spec's wording. One sentence, never a wall."""
    if signal_class == "delight":
        return (
            "✨ That felt like a win — want me to capture it for Anthropic? "
            "Run /fb (I've noted the moment)."
        )
    return (
        "💡 Rough patch detected — want me to capture that for Anthropic? "
        "Run /fb (I've noted the turn range)."
    )


# --------------------------------------------------------------------------- #
# Event handlers (thin: detect via the pure fns, mutate state, return nudge)   #
# --------------------------------------------------------------------------- #
def _short(s: str, n: int = 80) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _needle_for(signal_class: str, text: str) -> str:
    if signal_class == "rage":
        return f"repeated/near-identical prompt: {_short(text)}" if text.strip() else "reset after failure"
    return _short(text) or signal_class


def _ordered_eligible(state: WatcherState, candidates: set, *, disabled: bool) -> Optional[str]:
    """Pick the single highest-priority signal-class still eligible to fire."""
    for c in SIGNAL_CLASSES:
        if c in candidates and should_notify(state, c, disabled):
            return c
    return None


def handle_prompt(
    state: WatcherState,
    data: Mapping[str, Any],
    *,
    disabled: bool = False,
    now: Optional[float] = None,
) -> Optional[str]:
    """Handle UserPromptSubmit. Returns the nudge string to surface, or ``None``.

    Surfaces at most ONE nudge (the highest-priority eligible signal), drawing
    candidates from this prompt's sentiment/rage *plus* any signal staged earlier
    (e.g. a retry storm seen on PostToolUse). Marks it notified and ensures its
    pre-draft is recorded before returning.
    """
    state.turn += 1
    prompt = str(data.get("prompt") or "")

    candidates: set = set(state.pending)
    rage = detect_rage(state, prompt)
    if rage:
        candidates.add(rage)
    sentiment = detect_sentiment(prompt)
    if sentiment:
        candidates.add(sentiment)

    chosen = _ordered_eligible(state, candidates, disabled=disabled)
    nudge: Optional[str] = None
    if chosen is not None:
        # Storm/staged signals already have a pre-draft (recorded on PostToolUse);
        # for fresh sentiment/rage, record one now.
        if chosen not in state.predraft:
            record_predraft(state, chosen, _needle_for(chosen, prompt), now=now)
        state.notified.add(chosen)
        if chosen in state.pending:
            state.pending.remove(chosen)
        nudge = make_nudge(chosen)

    # Bounded history for repeat-detection on subsequent turns.
    norm = _norm_prompt(prompt)
    if norm:
        state.recent_prompts.append(norm)
        del state.recent_prompts[:-RECENT_PROMPTS_KEEP]
    return nudge


def _post_tool_is_error(data: Mapping[str, Any]) -> bool:
    """Best-effort: did this tool call fail? Tolerant of the several shapes a
    ``tool_response`` takes (an ``is_error`` flag, an ``error`` payload, a failed
    ``status``, or a top-level ``is_error``)."""
    if data.get("is_error"):
        return True
    resp = data.get("tool_response")
    if isinstance(resp, Mapping):
        if resp.get("is_error") or resp.get("error"):
            return True
        if str(resp.get("status", "")).lower() in ("error", "failed", "failure"):
            return True
    return False


def handle_post_tool(
    state: WatcherState,
    data: Mapping[str, Any],
    *,
    disabled: bool = False,
    now: Optional[float] = None,
) -> None:
    """Handle PostToolUse: track retry storms. Never emits stdout itself — a storm
    is *staged* into ``pending`` (with its pre-draft) and surfaced on the next
    prompt, since only UserPromptSubmit can inject context back to the model."""
    tool_name = str(data.get("tool_name") or "")
    is_error = _post_tool_is_error(data)
    storm = register_tool_event(state, tool_name, is_error, now=now)
    state.last_tool_failed = is_error
    if storm and should_notify(state, "retry_storm", disabled) and "retry_storm" not in state.pending:
        record_predraft(
            state,
            "retry_storm",
            f"retry storm: {tool_name} failed ≥{RETRY_STORM_THRESHOLD}×",
            now=now,
        )
        state.pending.append("retry_storm")


def dispatch(
    event: str,
    state: WatcherState,
    data: Mapping[str, Any],
    *,
    disabled: bool = False,
    now: Optional[float] = None,
) -> Optional[str]:
    """Route an event to its handler. Returns a nudge string (UserPromptSubmit) or
    ``None``. Unknown events (Stop/SessionEnd/anything else) are no-ops."""
    e = (event or "").strip()
    if e == "UserPromptSubmit":
        return handle_prompt(state, data, disabled=disabled, now=now)
    if e == "PostToolUse":
        handle_post_tool(state, data, disabled=disabled, now=now)
        return None
    return None


# --------------------------------------------------------------------------- #
# I/O glue — deliberately thin; ALWAYS exits 0, NEVER raises                    #
# --------------------------------------------------------------------------- #
def _read_stdin_json() -> dict:
    try:
        raw = sys.stdin.read()
    except Exception:
        return {}
    if not raw or not raw.strip():
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except ValueError:
        return {}


def _emit_user_prompt_context(nudge: str) -> None:
    out = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": nudge,
        }
    }
    sys.stdout.write(json.dumps(out))
    sys.stdout.flush()


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Hook entrypoint. Reads the event name (argv[0] or stdin's ``hook_event_name``)
    and the payload from stdin, dispatches, and prints at most one nudge JSON.

    Contract: **always returns 0 and never raises** — a hook must never break the
    user's session. Any error (including a disabled watcher) is a silent no-op.
    """
    try:
        args = list(sys.argv[1:] if argv is None else argv)

        # Kill switch: do nothing at all (don't even touch state).
        if is_disabled():
            return 0

        data = _read_stdin_json()
        event = (args[0] if args else "") or str(data.get("hook_event_name") or "")
        session_id = str(data.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID") or "unknown")

        state = load_state(session_id)
        nudge = dispatch(event, state, data, disabled=False)
        save_state(state)
        if nudge:
            _emit_user_prompt_context(nudge)
        return 0
    except Exception:
        # Last-resort guard: a hook failure must be invisible to the session.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
