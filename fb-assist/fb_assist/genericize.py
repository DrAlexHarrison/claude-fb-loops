"""fb_assist.genericize — the genericize/distill guardrails (the SEMANTIC ceiling).

Why this module exists
----------------------
``redact.py`` is the DETERMINISTIC floor: it catches *patterns* (keys, emails,
SSNs, paths, env-metadata). But the highest-risk leaks are *semantic* company IP —
"the patient in Room 11", an internal codename, a proprietary algorithm, a named
customer. No regex or NER reliably catches those; only a model with full context
does. In the real product the co-author IS Opus, already holding the user's whole
session, and **it writes the genericized rewrite itself** (spec §6/§8). So the
rewrite is not this module's job, and this module calls **no LLM**.

What this module DOES is the verification bar the spec mandates around that
human/Claude rewrite (spec §7/§8 — "Genericize verification bar, two-pass, before
ship"):

  1. **Prove no leak survived** — adversarially re-scan the *generic* text
     (``redact.leak_scan``) and prove that no sensitive VALUE from the *original*
     made it through verbatim. This is the machine-decidable ``ok`` verdict.
  2. **Flag meaning risk** — heuristically surface load-bearing tokens (error
     codes, ALL-CAPS markers, quoted strings, crash/timeout-ish words) that the
     rewrite dropped, and warn when the rewrite is drastically shorter than the
     original. These are SIGNALS for the co-author and the user — never gates.

The sacred line (= the anti-impersonation principle, spec §6): meaning-PRESERVATION
is the *user's* call, surfaced for confirmation downstream. This module may FLAG
meaning risk; it must **never** veto on meaning, and it must **never** auto-ship.
``verify_genericization`` decides only "did a leak survive?" — nothing else.

The two ``distill_*`` functions are the faithful-summary appliers ("distill" =
replace an exchange with a faithful summary, spec §5/§8). They APPLY and RETURN a
sanitized copy; the result is ALWAYS surfaced to the user for confirmation before
any swap/ship. They are pure (operate on copies; never mutate the caller's input).

LOCAL ONLY. No network, no LLM, stdlib + the sibling fb_assist modules only.
"""

from __future__ import annotations

import os

# Mirror redact.py: force the torch-only path for transformers/gliner so importing
# the sibling redactor (which lazy-loads NER) never explodes on a TF import. Set
# before redact is imported below.
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
from typing import Any, Optional

from . import transcripts
from .redact import Finding, leak_scan, scan_pii, scan_secrets
from .transcripts import Span, get_at, replace_span

__all__ = [
    "verify_genericization",
    "distill_apply",
    "distill_turn_range",
]

# Local severity rank (mirrors redact._SEV_RANK; kept tiny + private so we don't
# reach into a sibling's internals). Used to report the most-severe witness for a
# surviving value and to compute the high-severity "blocking" verdict.
_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}
_HIGH_SEVERITIES = ("high", "critical")

# A surviving value shorter than this is ignored when checking verbatim survival —
# a 1-3 char fragment matching by coincidence is noise, not a recovered secret.
# (Mirrors the >=4 floor the redact recall harness uses.)
_MIN_SURVIVING_LEN = 4


# --------------------------------------------------------------------------- #
# verify_genericization — the two-pass egress bar for a genericized rewrite
# --------------------------------------------------------------------------- #
def verify_genericization(original: str, generic: str, *,
                          expect_absent: Optional[list[str]] = None,
                          use_gliner: bool = True) -> dict:
    """Prove a genericized rewrite leaked nothing recoverable; flag meaning risk.

    ``original`` — the raw text the co-author/user genericized (one field, one
    exchange, or the whole bundle). ``generic`` — the proposed rewrite.

    Returns::

        {
          "reid_findings":       [Finding.to_dict(), ...],  # leak_scan(generic): can the
                                                            # company/person/IP be recovered?
          "leaked_originals":    [Finding.to_dict(reveal=True), ...],  # secret/PII VALUES from
                                                            # `original` still present VERBATIM in
                                                            # `generic` — MUST be empty for a pass
          "expect_absent_hits":  [{"literal": str, "count": int}, ...],  # caller-named codenames /
                                                            # IP strings still present in `generic`
          "meaning_risk_flags":  [{"kind": str, ...}, ...], # load-bearing tokens dropped /
                                                            # rewrite drastically shorter (SIGNALS)
          "ok":                  bool,                       # leaked_originals empty AND
                                                            # expect_absent_hits empty AND no
                                                            # high-severity reid finding
        }

    ``ok`` is the machine-decidable "no leak survived" verdict — and ONLY that.
    Meaning-PRESERVATION is the user's confirm (sacred); this function never vetoes
    on ``meaning_risk_flags``. ``leaked_originals`` carries the *literal* surviving
    value (``reveal=True``) because it is the actionable fix-list the co-author
    needs to know exactly what to remove — it is consumed locally, never shipped.
    """
    # Pass 1a — adversarial re-identification: re-run the full egress gate over the
    # GENERIC text. Anything it still finds is recoverable from what would ship.
    reid = leak_scan(generic, use_gliner=use_gliner)
    blocking = any(f.severity in _HIGH_SEVERITIES for f in reid)

    # Pass 1b — verbatim-survival: every sensitive VALUE detectable in the ORIGINAL
    # that is still present, byte-for-byte, in the generic. This catches the case
    # the rewrite "genericized" the prose but left a real secret/name/email sitting
    # in it. This list MUST be empty to pass.
    orig_findings = scan_secrets(original) + scan_pii(original, use_gliner=use_gliner)
    leaked = _surviving_values(orig_findings, generic)

    # Pass 1c — caller-named literals (codenames, internal IP strings) the user/
    # profile told us must not survive (e.g. expect_absent=["Athena"]).
    expect_absent_hits = _expect_absent_hits(expect_absent or [], generic)

    # Pass 2 — meaning risk (SIGNALS ONLY; never affects `ok`).
    meaning = _meaning_risk_flags(original, generic)

    ok = (not leaked) and (not expect_absent_hits) and (not blocking)
    return {
        "reid_findings": [f.to_dict(reveal=False) for f in reid],
        "leaked_originals": [f.to_dict(reveal=True) for f in leaked],
        "expect_absent_hits": expect_absent_hits,
        "meaning_risk_flags": meaning,
        "ok": ok,
    }


def _surviving_values(findings: list[Finding], generic: str,
                      min_len: int = _MIN_SURVIVING_LEN) -> list[Finding]:
    """Findings whose raw value still appears VERBATIM (case-sensitive) in ``generic``.

    Deduped by value, keeping the highest-severity witness so the reported entry is
    the most alarming attribution for that string."""
    out: list[Finding] = []
    seen: set[str] = set()
    # Most-severe first so the witness we keep per value is the scariest one.
    for f in sorted(findings, key=lambda f: -_SEV_RANK.get(f.severity, 1)):
        val = f.text or ""
        if len(val.strip()) < min_len or val in seen:
            continue
        if val in generic:  # verbatim, case-sensitive — a real survival, not a recase
            seen.add(val)
            out.append(f)
    return out


def _expect_absent_hits(literals: list[str], generic: str) -> list[dict]:
    """Caller-named literals still present in ``generic`` (case-insensitive, so a
    recased codename still trips it). Conservative substring match: better to flag a
    near-miss than let a codename ship."""
    gl = generic.lower()
    out: list[dict] = []
    for lit in literals:
        if not lit:
            continue
        n = gl.count(lit.lower())
        if n > 0:
            out.append({"literal": lit, "count": n})
    return out


# --------------------------------------------------------------------------- #
# meaning-risk heuristic (SIGNALS for the co-author/user — never a gate)
# --------------------------------------------------------------------------- #
# Small, tasteful set of "load-bearing" words: if the original named one of these
# and the rewrite dropped it, the bug/request may have lost its teeth. Kept lower-
# cased; matched case-insensitively.
_ERRORISH = frozenset({
    "error", "errors", "crash", "crashed", "crashes", "crashloop",
    "freeze", "freezing", "frozen", "hang", "hangs", "hung",
    "timeout", "timeouts", "deadlock", "panic", "exception", "traceback",
    "fail", "failed", "fails", "failure", "failures", "segfault",
    "null", "nil", "undefined", "nan", "leak", "leaks", "oom",
    "overflow", "underflow", "corrupt", "corruption", "stuck",
    "broke", "broken", "regression", "stacktrace", "throws",
})

# digit-bearing identifiers / error codes / versions: E-4521, v2.1.168, Room11, 0x1F
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_\-./:]*")
# pure-alpha words, for ALL-CAPS markers + errorish lookup (no surrounding punct)
_WORD_RE = re.compile(r"[A-Za-z]{2,}")
# quoted spans — the exact phrasing a user quotes is usually load-bearing
_QUOTED_RE = re.compile(r"\"([^\"\n]{2,120})\"|'([^'\n]{2,120})'|`([^`\n]{2,120})`")

# Drastically-shorter trip point + a floor so we don't fire on trivially short text.
_LENGTH_RATIO = 0.25
_LENGTH_MIN_ORIGINAL = 40
# Cap the dropped-token list so a pathological input can't return thousands.
_MAX_DROPPED_TOKENS = 50


def _load_bearing_tokens(text: str) -> list[str]:
    """The set of tokens whose disappearance would risk dropping meaning:
    digit-bearing identifiers, ALL-CAPS markers, errorish words, quoted phrases."""
    toks: set[str] = set()
    for m in _TOKEN_RE.finditer(text):
        t = m.group(0)
        if len(t) >= 2 and any(ch.isdigit() for ch in t):
            toks.add(t)
    for m in _WORD_RE.finditer(text):
        w = m.group(0)
        if w.isupper() and len(w) >= 3:
            toks.add(w)
        elif w.lower() in _ERRORISH:
            toks.add(w)
    for m in _QUOTED_RE.finditer(text):
        inner = next((g for g in m.groups() if g is not None), "").strip()
        if inner:
            toks.add(inner)
    return sorted(toks)


def _meaning_risk_flags(original: str, generic: str) -> list[dict]:
    """Heuristic signals that the rewrite may have shed meaning. NOT gates."""
    flags: list[dict] = []
    olen, glen = len(original), len(generic)
    if olen >= _LENGTH_MIN_ORIGINAL and glen < _LENGTH_RATIO * olen:
        flags.append({
            "kind": "drastically_shorter",
            "original_len": olen,
            "generic_len": glen,
            "ratio": round(glen / olen, 3) if olen else 0.0,
        })
    generic_lower = generic.lower()
    dropped = [t for t in _load_bearing_tokens(original) if t.lower() not in generic_lower]
    for t in dropped[:_MAX_DROPPED_TOKENS]:
        flags.append({"kind": "dropped_load_bearing_token", "token": t})
    if len(dropped) > _MAX_DROPPED_TOKENS:
        flags.append({"kind": "more_dropped_tokens", "count": len(dropped) - _MAX_DROPPED_TOKENS})
    return flags


# --------------------------------------------------------------------------- #
# distill — replace verbose content with a faithful summary (appliers, not LLM)
# --------------------------------------------------------------------------- #
def distill_apply(records: list[dict], span: Span, summary: str) -> list[dict]:
    """Replace ONE located ``span``'s text with a faithful ``summary``.

    Splices ``summary`` into ``span``'s field at ``[span.start:span.end]`` via
    ``transcripts.replace_span`` (whole-field if the span covers the whole value).
    ``records`` is a list of raw record dicts (one parsed JSONL line each); the
    span's record is located by ``uuid`` (falling back to path+text match).

    Pure: operates on a deep copy, returns the mutated copy, leaves ``records``
    untouched. The distilled result is ALWAYS surfaced to the user for confirmation
    downstream — this only APPLIES and RETURNS, it never ships.
    """
    out = copy.deepcopy(records)
    idx = _locate_record(out, span)
    if idx is None:
        raise ValueError(
            f"distill_apply: no record in `records` matches span at line {span.line} "
            f"(uuid={span.uuid!r}, field={span.field}). Pass the records the span was "
            "extracted from."
        )
    rec = out[idx]
    # Integrity guard: refuse to splice if the span no longer points at its own text
    # (stale span vs. these records) — fail loud rather than corrupt the transcript.
    located = get_at(rec, span.path)
    if not isinstance(located, str) or located[span.start:span.end] != span.text:
        raise ValueError(
            f"distill_apply: span no longer resolves to its text at {span.field}; "
            "refusing to splice (stale span or wrong records)."
        )
    replace_span(rec, span, summary)
    return out


def _locate_record(records: list[dict], span: Span) -> Optional[int]:
    """Index of the record ``span`` points into: by uuid, else by path+text match."""
    if span.uuid is not None:
        for i, rec in enumerate(records):
            if isinstance(rec, dict) and rec.get("uuid") == span.uuid:
                return i
    # Fallback for records without a uuid (lightweight meta records): find the one
    # whose span.path resolves to exactly span.text.
    for i, rec in enumerate(records):
        if not isinstance(rec, dict):
            continue
        try:
            v = get_at(rec, span.path)
        except (KeyError, IndexError, TypeError):
            continue
        if isinstance(v, str) and v[span.start:span.end] == span.text:
            return i
    return None


def distill_turn_range(records: list[dict], start_idx: int, end_idx: int,
                       summary: str) -> list[dict]:
    """Collapse a contiguous record range ``[start_idx, end_idx]`` (inclusive,
    0-based into ``records``) into a SINGLE faithful summary record.

    The verbose exchange is replaced by one synthesized ``user`` record carrying
    ``summary`` as its message content, with the envelope (sessionId / timestamp /
    parentUuid / isSidechain) inherited from the first record in the range so the
    result stays a coherent, parseable transcript. The synthesized record is marked
    (``fbAssistDistilled``) so it is never mistaken for a real human turn.

    Pure: operates on a deep copy; ``records`` is left untouched. The result is
    ALWAYS surfaced to the user for confirmation downstream — applies + returns only.
    """
    n = len(records)
    if not (0 <= start_idx <= end_idx < n):
        raise IndexError(
            f"distill_turn_range: bad range [{start_idx}, {end_idx}] for {n} records"
        )
    out = copy.deepcopy(records)
    anchor = out[start_idx] if isinstance(out[start_idx], dict) else {}
    count = end_idx - start_idx + 1
    summary_rec = {
        "type": "user",
        "uuid": "fb-assist-distill-" + _uuid.uuid4().hex,
        "parentUuid": anchor.get("parentUuid"),
        "sessionId": anchor.get("sessionId"),
        "timestamp": anchor.get("timestamp"),
        "isSidechain": bool(anchor.get("isSidechain", False)),
        "message": {"role": "user", "content": summary},
        # Honesty markers: this record is a distillation, not a captured human turn.
        "fbAssistDistilled": True,
        "fbAssistDistilledCount": count,
    }
    return out[:start_idx] + [summary_rec] + out[end_idx + 1:]


# --------------------------------------------------------------------------- #
# CLI (peer to redact.py / transcripts.py / package.py — shellable by the MCP)
# --------------------------------------------------------------------------- #
def _load_records(path: str) -> list[dict]:
    recs: list[dict] = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                recs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return recs


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fb_assist.genericize",
        description="Genericize/distill guardrails: prove a rewrite leaked nothing "
                    "recoverable + flag meaning risk; apply faithful distillations.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("verify", help="prove a genericized rewrite leaked nothing recoverable")
    v.add_argument("original", help="path to the original text file (or - for stdin)")
    v.add_argument("generic", help="path to the genericized rewrite text file")
    v.add_argument("--expect-absent", nargs="*", default=[],
                   help="literals (codenames / IP strings) that must NOT survive")
    v.add_argument("--no-gliner", action="store_true", help="skip the GLiNER PII pass")

    d = sub.add_parser("distill-range", help="collapse a record range in a .jsonl into one summary record")
    d.add_argument("input", help="path to a transcript .jsonl")
    d.add_argument("--start", type=int, required=True, help="0-based start index (inclusive)")
    d.add_argument("--end", type=int, required=True, help="0-based end index (inclusive)")
    d.add_argument("--summary", required=True, help="the faithful summary text")
    d.add_argument("--out", help="write result jsonl here (default: stdout)")

    args = ap.parse_args(argv)

    if args.cmd == "verify":
        original = sys.stdin.read() if args.original == "-" else open(args.original).read()
        generic = open(args.generic).read()
        result = verify_genericization(original, generic, expect_absent=args.expect_absent,
                                       use_gliner=not args.no_gliner)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if args.cmd == "distill-range":
        records = _load_records(args.input)
        out = distill_turn_range(records, args.start, args.end, args.summary)
        lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in out)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(lines + "\n")
            print(f"wrote {len(out)} records -> {args.out}", file=sys.stderr)
        else:
            print(lines)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
