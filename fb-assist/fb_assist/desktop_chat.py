"""fb_assist.desktop_chat — the claude.ai / Claude-Desktop *chat* surface.

The export-JSON co-pilot (the hero demo). It takes an official claude.ai
**Export Data** archive (``conversations.json``), turns ONE chosen conversation
into a privacy-safe, genericized feedback artifact, and shows a **before/after**
view plus an **effort signal**.

Why a new module (and why it is *thin*)
---------------------------------------
The Claude-Code edge (``transcripts.py`` + ``redact.strip_categories`` +
``package.swap_restore``) is bound to Claude Code's on-disk JSONL schema — message
blocks, ``toolUseResult`` mirrors, per-record envelopes, swap-restore around a real
``/feedback`` submit. **None of that applies here.** The claude.ai transcript lives
server-side; the user pulls it through Anthropic's *official* export channel, so:

  * there is **nothing on disk to swap-restore** — the privacy mechanism is
    *consent + genericize*: build a clean artifact, show the user exactly what it
    contains, and let them carry it into a first-party Share / feedback thread;
  * the export schema is claude.ai's own consumer-export shape (a JSON array of
    conversations), **not** the Messages-API schema and **not** the CC JSONL —
    so this module ships a small lenient parser of its own;
  * the **detectors are reused verbatim**. Every redaction primitive comes from
    :mod:`fb_assist.redact`; the genericize verification bar comes from
    :mod:`fb_assist.genericize`; the effort-signal footer from
    :mod:`fb_assist.package`. This module adds only the parser + a thin driver.

The two-layer egress gate (per ``INTEGRATION.md``) is honoured exactly:
  * **HARD floor** (machine-decidable): the deterministic floor —
    ``scan_secrets`` + the PII regex floor — over the **actual rendered output
    bytes**. Empty == ship-able. This is the gate.
  * **SOFT layer**: ``leak_scan`` (incl. NER) over the rendered content yields
    *candidates* the co-author adjudicates / self-repairs. Never a boolean veto.

LOCAL ONLY. Pure forward-transform: the parsed input is **never mutated**; every
redaction builds new strings / deep copies. No network egress (the optional GLiNER
PII pass is the only thing that would ever touch the network, and it is off by
default on this surface).
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
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional, Union

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
    "Message",
    "Conversation",
    "RedactedTurn",
    "ConversationFeedback",
    "message_text",
    "iter_conversations",
    "parse_export",
    "select_conversation",
    "redact_conversation",
    "genericized_conversation",
    "render_before_after",
    "render_included_stripped",
    "render_effort_signal",
    "render_report",
    "DEFAULT_FIXTURE",
    "main",
]

# The synthetic fixture the CLI defaults to (so running it touches NO real data).
DEFAULT_FIXTURE = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "sample-export.json"

# Levels mirror the spec vocabulary; on this surface they tune how hard we redact.
LEVELS = ("express", "no-code", "genericize", "surgical")


# --------------------------------------------------------------------------- #
# Data model — the claude.ai export shape (lenient, default-everything)
# --------------------------------------------------------------------------- #
@dataclass
class Message:
    """One chat message from a claude.ai export.

    The export carries TWO text shapes (both seen in real exports): a richer
    ``content[]`` block array (``{type, text, ...}``) AND a bare top-level
    ``text`` string. :attr:`text` / :func:`message_text` read whichever is
    present (content-blocks join preferred, bare ``text`` fallback). ``raw`` is
    the untouched source dict (never mutated)."""

    uuid: str
    sender: str            # "human" | "assistant"
    created_at: str
    attachments: list
    files: list
    raw: dict

    @property
    def role(self) -> str:
        # map claude.ai's "human" to the universal "user"; pass anything else through.
        return "user" if self.sender == "human" else (self.sender or "assistant")

    @property
    def text(self) -> str:
        return message_text(self)


@dataclass
class Conversation:
    """One conversation (``chat_messages`` lifted to typed :class:`Message` objects)."""

    uuid: str
    name: str
    created_at: str
    updated_at: str
    messages: list  # list[Message]
    raw: dict
    summary: str = ""
    account: dict = field(default_factory=dict)

    @property
    def human_messages(self) -> list:
        return [m for m in self.messages if m.role == "user"]

    @property
    def assistant_messages(self) -> list:
        return [m for m in self.messages if m.role != "user"]


def message_text(msg: Union["Message", dict]) -> str:
    """Robust text accessor — works on a :class:`Message` OR a raw message dict,
    and on BOTH export shapes.

    Preference order (per the export schema note): join the ``content[]`` text
    blocks when that join is non-empty, else fall back to the bare ``text``
    string. Anything malformed degrades to ``""`` rather than raising — the
    parser is deliberately lenient.
    """
    raw = msg.raw if isinstance(msg, Message) else msg
    if not isinstance(raw, dict):
        return ""
    content = raw.get("content")
    if isinstance(content, list):
        parts = [
            blk["text"]
            for blk in content
            if isinstance(blk, dict)
            and blk.get("type") == "text"
            and isinstance(blk.get("text"), str)
            and blk["text"]
        ]
        joined = "\n\n".join(parts)
        if joined.strip():
            return joined
    bare = raw.get("text")
    return bare if isinstance(bare, str) else ""


# --------------------------------------------------------------------------- #
# Parsing — streaming-friendly, lenient, format-sniffing (array OR JSONL)
# --------------------------------------------------------------------------- #
def _message_from_raw(raw: dict) -> Message:
    if not isinstance(raw, dict):
        raw = {}
    return Message(
        uuid=str(raw.get("uuid", "")),
        sender=str(raw.get("sender", "")),
        created_at=str(raw.get("created_at", "")),
        attachments=raw.get("attachments") or [],
        files=raw.get("files") or [],
        raw=raw,
    )


def _conversation_from_raw(raw: dict) -> Conversation:
    if not isinstance(raw, dict):
        raw = {}
    msgs_raw = raw.get("chat_messages")
    if not isinstance(msgs_raw, list):
        msgs_raw = []
    return Conversation(
        uuid=str(raw.get("uuid", "")),
        name=str(raw.get("name", "") or "Untitled"),
        created_at=str(raw.get("created_at", "")),
        updated_at=str(raw.get("updated_at", "")),
        summary=str(raw.get("summary", "") or ""),
        account=raw.get("account") if isinstance(raw.get("account"), dict) else {},
        messages=[_message_from_raw(m) for m in msgs_raw],
        raw=raw,
    )


def iter_conversations(source: Union[str, Path, list, Iterable]) -> Iterator[Conversation]:
    """Stream :class:`Conversation` objects from a claude.ai export.

    ``source`` may be:
      * a path to ``conversations.json`` (a JSON **array** ``[{...}]``), or
      * a path to a ``.jsonl`` twin (line-delimited ``{...}`` objects — some
        third-party tools emit this), or
      * an already-loaded ``list`` of conversation dicts.

    Format is sniffed by the first non-whitespace byte (``[`` => array,
    ``{`` => JSONL). The array path uses ``ijson`` for true streaming when it is
    installed (power-user exports exceed 500 MB), and falls back to ``json.load``
    otherwise. Lenient throughout: malformed lines / items are skipped.
    """
    if isinstance(source, list):
        for item in source:
            if isinstance(item, dict):
                yield _conversation_from_raw(item)
        return

    path = Path(source)
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        first = ""
        while True:
            ch = fh.read(1)
            if ch == "":
                break
            if not ch.isspace():
                first = ch
                break
        fh.seek(0)

        if first == "{":
            # JSONL: one conversation object per line.
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                if isinstance(obj, dict):
                    yield _conversation_from_raw(obj)
            return

        # JSON array (the canonical conversations.json). Prefer ijson streaming.
        try:
            import ijson  # type: ignore

            fh.seek(0)
            for item in ijson.items(fh, "item"):
                if isinstance(item, dict):
                    yield _conversation_from_raw(item)
            return
        except ImportError:
            pass
        except Exception:
            # A malformed array under ijson — fall through to the json.load attempt.
            pass

        fh.seek(0)
        try:
            data = json.load(fh)
        except (json.JSONDecodeError, ValueError):
            return
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield _conversation_from_raw(item)
        elif isinstance(data, dict):
            # A single-conversation export (defensive — not the documented shape).
            yield _conversation_from_raw(data)


def parse_export(source: Union[str, Path, list, Iterable]) -> list:
    """Materialize all conversations from an export (``list[Conversation]``).

    Convenience over :func:`iter_conversations` for index/needle selection and
    counting. For a genuinely huge export, iterate :func:`iter_conversations`
    and select as you go instead of holding the whole list."""
    return list(iter_conversations(source))


def select_conversation(
    source: Union[str, Path, list, Iterable],
    *,
    uuid: Optional[str] = None,
    needle: Optional[str] = None,
    index: Optional[int] = None,
) -> Optional[Conversation]:
    """Pick the one conversation the user means.

    Resolution order: explicit ``uuid`` (exact match) > ``needle`` (case-
    insensitive substring of the name/summary, else of any message text) >
    ``index`` (0-based position) > the first conversation. Returns ``None`` if
    nothing matches. ``source`` may be a path, a loaded list, or an iterable of
    :class:`Conversation`."""
    if isinstance(source, (str, Path)):
        convs = parse_export(source)
    elif isinstance(source, list) and source and isinstance(source[0], Conversation):
        convs = source
    else:
        convs = parse_export(source)

    if uuid:
        for c in convs:
            if c.uuid == uuid:
                return c
        return None
    if needle:
        nl = needle.lower()
        for c in convs:
            if nl in (c.name or "").lower() or nl in (c.summary or "").lower():
                return c
        for c in convs:
            if any(nl in message_text(m).lower() for m in c.messages):
                return c
        return None
    if index is not None:
        if -len(convs) <= index < len(convs):
            return convs[index]
        return None
    return convs[0] if convs else None


# --------------------------------------------------------------------------- #
# Redaction driver — thin, reuses redact.py wholesale
# --------------------------------------------------------------------------- #
@dataclass
class RedactedTurn:
    """One conversation turn, before vs after redaction."""

    index: int
    role: str               # "user" | "assistant"
    uuid: str
    before: str
    after: str
    findings: list          # chosen non-overlapping Findings masked in this turn

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass
class ConversationFeedback:
    """The artifact: a redacted conversation + the gate results + effort signal."""

    conversation_uuid: str
    name: str
    level: str
    turns: list             # list[RedactedTurn]
    redaction_map: list     # list[{uuid, role, category, severity, original, replacement, count}]
    effort_signal: dict
    floor_clean: bool       # HARD gate: deterministic floor over the output bytes == empty
    floor_residual: list    # any residual deterministic findings (must be empty)
    leak_candidates: list   # SOFT layer: leak_scan candidates over rendered output
    genericize_ok: bool     # verify_genericization passed for every changed turn
    meaning_risk_flags: list
    counts: dict
    rendered_after: str     # the genericized conversation rendered (the output bytes)

    @property
    def redaction_count(self) -> int:
        return len(self.redaction_map)


# severity comes off the Finding; mirror redact's rank for the summary ordering.
_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _render_turn(role: str, text: str) -> str:
    label = "Human" if role == "user" else "Assistant"
    return f"### {label}\n{text}"


def redact_conversation(
    conv: Conversation,
    *,
    level: str = "genericize",
    use_gliner: bool = False,
    quality: int = 4,
    alignment_confidence: int = 5,
    reputation_token: Optional[str] = None,
    verify: bool = True,
) -> ConversationFeedback:
    """Genericize ONE conversation into a privacy-safe feedback artifact.

    Forward-transform only — ``conv`` and its ``raw`` are never mutated. For every
    human + assistant turn we:

      1. read the narrative via :func:`message_text` (both export shapes),
      2. detect with ``scan_secrets`` + ``scan_pii`` (span-local offsets),
      3. mask the chosen non-overlapping spans with ``apply_redactions`` — meaning
         survives, values become ``‹MARKERS›``,
      4. (optional) prove the rewrite leaked nothing recoverable via
         ``verify_genericization`` (the genericize verification bar).

    Then the **two-layer egress gate** runs over the rendered output: the HARD
    deterministic floor (``scan_secrets`` + PII regex) must be empty; ``leak_scan``
    yields SOFT candidates for the co-author. The included/stripped summary is built
    from ``redaction_map`` directly (this surface has no CC-transcript to diff).

    ``use_gliner`` defaults False: the local deterministic floor + Presidio cover
    the demo without the metered GLiNER download, and avoid NER's brand/codename
    over-redaction. Flip it on for an extra adversarial NER candidate pass.
    """
    if level not in LEVELS:
        raise ValueError(f"unknown level {level!r}; valid = {LEVELS}")

    turns: list[RedactedTurn] = []
    redaction_map: list[dict] = []
    meaning_risk: list[dict] = []
    genericize_ok = True

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
            # Nothing to show/redact (attachment-only or empty turn); skip silently.
            continue

        # Detectors reused verbatim: secrets + PII (regex floor + Presidio [+ GLiNER])
        # + absolute-path leakage. Paths are folded in here so the chat surface masks
        # "/home/dana/code/contoso-internal/…"-style identity leaks in the narrative
        # (the CC edge strips them structurally; here there is only prose to mask).
        findings = (scan_secrets(before)
                    + scan_pii(before, use_gliner=use_gliner)
                    + _scan_paths_text(before))
        chosen = merge_redaction_spans(findings)
        after, _ = apply_redactions(before, findings, style="mask")

        turns.append(RedactedTurn(index=i, role=role, uuid=msg.uuid,
                                  before=before, after=after, findings=chosen))

        for f in chosen:
            label = _token_label(f.entity)
            redaction_map.append({
                "uuid": msg.uuid,
                "role": role,
                "category": f.entity,
                "severity": f.severity,
                "detector": f.detector,
                "original": f.text,
                "replacement": f"‹{label}›",
                "count": 1,
            })
            by_category[f.entity] = by_category.get(f.entity, 0) + 1
            by_severity[f.severity] = by_severity.get(f.severity, 0) + 1

        if verify and after != before:
            vg = verify_genericization(before, after, use_gliner=use_gliner)
            if not vg["ok"]:
                genericize_ok = False
            for flag in vg.get("meaning_risk_flags", []):
                meaning_risk.append({"uuid": msg.uuid, **flag})

    rendered_after = render_redacted_markdown(conv.name, turns)

    # ---- The HARD gate: deterministic floor over the ACTUAL output bytes. ----
    floor = scan_secrets(rendered_after) + _scan_pii_regex(rendered_after)
    floor_residual = [f.to_dict(reveal=False) for f in floor]
    floor_clean = len(floor) == 0

    # ---- The SOFT layer: adversarial leak_scan -> candidates (never a veto). ----
    leak = leak_scan(rendered_after, use_gliner=use_gliner)
    leak_candidates = [f.to_dict(reveal=False) for f in leak]

    counts = {
        "messages": len(conv.messages),
        "human": n_human,
        "assistant": n_assistant,
        "turns_rendered": len(turns),
        "turns_redacted": sum(1 for t in turns if t.changed),
        "redactions": len(redaction_map),
        "by_category": dict(sorted(by_category.items(), key=lambda kv: -kv[1])),
        "by_severity": dict(sorted(by_severity.items(),
                                   key=lambda kv: -_SEV_RANK.get(kv[0], 1))),
    }

    effort_signal = {
        "redaction": level,
        "quality": quality,
        "alignment_confidence": alignment_confidence,
        "reputation_token": reputation_token,
        "summary": {
            "redactions": len(redaction_map),
            "by_severity": counts["by_severity"],
            "floor_clean": floor_clean,
            "genericize_verified": genericize_ok,
        },
    }

    return ConversationFeedback(
        conversation_uuid=conv.uuid,
        name=conv.name,
        level=level,
        turns=turns,
        redaction_map=redaction_map,
        effort_signal=effort_signal,
        floor_clean=floor_clean,
        floor_residual=floor_residual,
        leak_candidates=leak_candidates,
        genericize_ok=genericize_ok,
        meaning_risk_flags=meaning_risk,
        counts=counts,
        rendered_after=rendered_after,
    )


def render_redacted_markdown(name: str, turns: Iterable[RedactedTurn]) -> str:
    """Render the genericized (post-redaction) conversation as share-ready markdown.

    This is the **output surface** — the exact bytes the egress gate scans and the
    user would carry into a first-party Share / feedback thread."""
    lines = [f"# {name}", ""]
    for t in turns:
        lines.append(_render_turn(t.role, t.after))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def genericized_conversation(conv: Conversation, feedback: ConversationFeedback) -> dict:
    """A round-trippable deep copy of the source conversation dict with each
    message's narrative replaced by its redacted text (bare ``text`` field; any
    ``content[]`` text blocks collapsed to a single redacted block).

    Pure: the input ``conv.raw`` is deep-copied, never mutated. Useful when the
    user wants a genericized ``conversations.json``-shaped file, not just markdown."""
    out = copy.deepcopy(conv.raw)
    after_by_uuid = {t.uuid: t.after for t in feedback.turns}
    msgs = out.get("chat_messages")
    if isinstance(msgs, list):
        for m in msgs:
            if not isinstance(m, dict):
                continue
            red = after_by_uuid.get(str(m.get("uuid", "")))
            if red is None:
                continue
            m["text"] = red
            # Collapse content[] to a single redacted text block so the file stays
            # coherent and no original block text survives in the round-trip.
            if isinstance(m.get("content"), list):
                m["content"] = [{"type": "text", "text": red}]
    return out


# --------------------------------------------------------------------------- #
# Rendering — the before/after money shot + included/stripped + effort signal
# --------------------------------------------------------------------------- #
def render_before_after(feedback: ConversationFeedback, *, max_turns: Optional[int] = None,
                        only_changed: bool = True, width: int = 100,
                        reveal: bool = False) -> str:
    """The demo's money shot: a compact BEFORE → AFTER per redacted turn.

    ``reveal`` defaults False — the BEFORE column shows each sensitive span in its
    SHORT-masked form (``sk-…OO``) rather than the raw value, so the preview itself
    never prints a live secret/PII while still proving the surrounding narrative is
    preserved. (The whole tool is local; ``reveal=True`` shows the raw original for
    an at-the-keyboard review.) The AFTER column is always the shippable artifact —
    sensitive spans replaced by categorized ``‹MARKERS›``."""
    shown = [t for t in feedback.turns if (t.changed or not only_changed)]
    if max_turns is not None:
        shown = shown[:max_turns]
    lines = [f"BEFORE / AFTER  —  \"{feedback.name}\"  (level={feedback.level})", ""]
    if not shown:
        lines.append("  (no narrative redactions in this conversation)")
        return "\n".join(lines)
    for t in shown:
        label = "Human" if t.role == "user" else "Assistant"
        marks = ", ".join(sorted({_token_label(f.entity) for f in t.findings})) or "—"
        before_disp = t.before if reveal else _mask_short(t.before, t.findings)
        lines.append(f"┌─ turn {t.index} · {label} · redacted: {marks}")
        lines.append("│ BEFORE: " + _clip(before_disp, width))
        lines.append("│ AFTER : " + _clip(t.after, width))
        lines.append("└" + "─" * (width + 8))
    hidden = len([t for t in feedback.turns if t.changed]) - len(shown)
    if hidden > 0:
        lines.append(f"  … +{hidden} more redacted turn(s)")
    return "\n".join(lines)


def _mask_short(text: str, findings: Iterable) -> str:
    """Splice each chosen finding's span with its SHORT mask (``Finding.masked``).
    ``findings`` are the non-overlapping chosen spans (offsets into ``text``)."""
    pieces: list[str] = []
    cursor = 0
    for f in sorted(findings, key=lambda f: f.start):
        if f.start < cursor or f.start < 0:
            continue
        pieces.append(text[cursor:f.start])
        pieces.append(f.masked)
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces)


def render_included_stripped(feedback: ConversationFeedback, *, max_samples: int = 8) -> str:
    """The included/stripped summary — built DIRECTLY from ``redaction_map`` (no
    CC-transcript diff_preview on this surface)."""
    rm = feedback.redaction_map
    c = feedback.counts
    lines = ["Genericized feedback artifact — what it contains:"]
    lines.append(
        f"  INCLUDED : {c['turns_rendered']} turns "
        f"({c['human']} human, {c['assistant']} assistant)  "
        f"— {len(feedback.rendered_after):,} bytes"
    )
    if not rm:
        lines.append("  STRIPPED : nothing (no secrets / PII detected)")
    else:
        by_cat = ", ".join(f"{n}×{cat}" for cat, n in c["by_category"].items())
        by_sev = ", ".join(f"{n}×{sev}" for sev, n in c["by_severity"].items())
        lines.append(f"  STRIPPED : {len(rm)} values across {c['turns_redacted']} turns")
        lines.append(f"    by category : {by_cat}")
        lines.append(f"    by severity : {by_sev}")
        lines.append(f"    e.g. (showing {min(len(rm), max_samples)} of {len(rm)}):")
        for e in rm[:max_samples]:
            orig = _mask_sample(e["original"])
            lines.append(f"        [{e['category']}] {orig} → {e['replacement']}")
    return "\n".join(lines)


def render_effort_signal(feedback: ConversationFeedback) -> str:
    """The effort-signal footer (reused ``package._render_effort_footer`` schema)
    plus the deterministic-floor assertion — the proof line."""
    footer = _render_effort_footer(feedback.effort_signal)
    lines = [footer]
    gate = "✅ CLEAN" if feedback.floor_clean else "❌ RESIDUAL"
    lines.append(
        f"[egress gate] deterministic floor over {len(feedback.rendered_after):,} "
        f"output bytes: {gate} "
        f"({len(feedback.floor_residual)} residual secrets/PII)"
    )
    soft = len(feedback.leak_candidates)
    lines.append(
        f"[egress gate] soft NER leak_scan candidates: {soft} "
        f"(advisory — co-author adjudicates; not a veto)"
    )
    lines.append(
        f"[genericize]  verification bar: "
        f"{'PASS' if feedback.genericize_ok else 'NEEDS REPAIR'}"
    )
    return "\n".join(lines)


def render_report(feedback: ConversationFeedback, *, max_turns: Optional[int] = None,
                  reveal: bool = False) -> str:
    """The full console report: before/after + included/stripped + effort signal."""
    return "\n\n".join([
        render_before_after(feedback, max_turns=max_turns, reveal=reveal),
        render_included_stripped(feedback),
        render_effort_signal(feedback),
    ])


def _clip(s: str, n: int) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _mask_sample(s: str, n: int = 40) -> str:
    """Short, value-hiding preview of an original (so the summary itself stays clean)."""
    s = " ".join(str(s).split())
    if len(s) <= 8:
        head = s[:1] + "…"
    else:
        head = f"{s[:3]}…{s[-2:]}"
    return head


# --------------------------------------------------------------------------- #
# CLI — python -m fb_assist.desktop_chat --export <path> [--conversation <id>]
# --------------------------------------------------------------------------- #
def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fb_assist.desktop_chat",
        description="claude.ai / Desktop chat co-pilot: genericize one exported "
                    "conversation into a privacy-safe feedback artifact "
                    "(before/after + effort signal). Defaults to a SYNTHETIC fixture; "
                    "point --export at a real conversations.json to run on real data.",
    )
    ap.add_argument("--export", default=str(DEFAULT_FIXTURE),
                    help="path to a claude.ai conversations.json (array) or .jsonl twin "
                         f"(default: the synthetic fixture {DEFAULT_FIXTURE.name})")
    ap.add_argument("--conversation", default="0",
                    help="which conversation: a uuid, a name/text substring, or a 0-based index "
                         "(default: 0)")
    ap.add_argument("--level", default="genericize", choices=list(LEVELS),
                    help="redaction level (default: genericize)")
    ap.add_argument("--gliner", action="store_true",
                    help="also run the GLiNER NER pass (downloads ~86 MB on first use)")
    ap.add_argument("--max-turns", type=int, default=None,
                    help="cap how many before/after turns to print")
    ap.add_argument("--reveal", action="store_true",
                    help="show raw original values in the BEFORE column (default: short-masked)")
    ap.add_argument("--list", action="store_true",
                    help="just list the conversations in the export and exit")
    ap.add_argument("--json", action="store_true",
                    help="emit the artifact as JSON (effort signal + redaction_map + counts)")
    args = ap.parse_args(argv)

    export_path = Path(args.export)
    if not export_path.exists():
        print(f"error: export not found: {export_path}", file=sys.stderr)
        return 2

    if args.list:
        convs = parse_export(export_path)
        print(f"{len(convs)} conversation(s) in {export_path.name}:")
        for i, c in enumerate(convs):
            print(f"  [{i:>3}] {c.uuid}  {len(c.messages):>4} msgs  {c.name}")
        return 0

    # Resolve the --conversation selector (uuid / substring / index).
    sel = args.conversation
    conv: Optional[Conversation]
    if sel.lstrip("-").isdigit():
        conv = select_conversation(export_path, index=int(sel))
    elif len(sel) >= 20 and "-" in sel and " " not in sel:
        conv = select_conversation(export_path, uuid=sel) or \
            select_conversation(export_path, needle=sel)
    else:
        conv = select_conversation(export_path, needle=sel)

    if conv is None:
        print(f"error: no conversation matched {sel!r} in {export_path.name}", file=sys.stderr)
        return 2

    feedback = redact_conversation(conv, level=args.level, use_gliner=args.gliner)

    if args.json:
        out = {
            "conversation_uuid": feedback.conversation_uuid,
            "name": feedback.name,
            "level": feedback.level,
            "counts": feedback.counts,
            "effort_signal": feedback.effort_signal,
            "floor_clean": feedback.floor_clean,
            "floor_residual": feedback.floor_residual,
            "leak_candidates": feedback.leak_candidates,
            "redaction_map": [
                {k: (_mask_sample(v) if k == "original" else v) for k, v in e.items()}
                for e in feedback.redaction_map
            ],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(render_report(feedback, max_turns=args.max_turns, reveal=args.reveal))

    # Exit non-zero if the HARD gate is dirty — a shell caller can gate on it.
    return 0 if feedback.floor_clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
