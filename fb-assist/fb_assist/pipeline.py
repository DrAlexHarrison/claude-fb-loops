"""The validated fb-assist call sequence, lifted into reusable functions.

Order: parse -> detect -> redact -> assemble -> preview -> gate — what the
integration tests validate end-to-end. The MCP server is a thin wrapper over
this module so the runtime and tests share one source of truth.

Two type seams to keep straight: ``transcripts.*`` takes Record objects;
``redact.strip_categories`` and ``package.*`` take raw dicts. Parse once,
keep both views.

Local only. No network. No paid software.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Union

from . import transcripts as T
from . import redact as R
from . import package as P

PathLike = Union[str, Path]

# Strip these bulk categories wholesale — buried secrets the user never wants
# shipped ...
DEFAULT_STRIP_CATEGORIES = [
    "file_contents", "bash_output", "tool_calls", "websearch",
    "thinking_blocks", "hook_output", "injected_memory",
    "env_metadata", "paths",
]
# ... and KEEP these narrative categories but mask them char-precise (meaning
# survives; the pasted values don't).
KEEP_BUT_MASK = ["human_prompts", "assistant_text"]


# --------------------------------------------------------------------------- #
# 1) PARSE — keep BOTH views (Record objects + raw dicts).                     #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Parsed:
    """The two views of one transcript, parsed once."""
    path: str
    records: list[T.Record]
    raws: list[dict]

    @property
    def session_id(self) -> Optional[str]:
        for r in self.records:
            if r.session_id:
                return r.session_id
        return Path(self.path).stem


def parse_session(path: PathLike) -> Parsed:
    """Parse an on-disk ``.jsonl`` into both Record objects and raw dicts."""
    records = list(T.parse(str(path)))
    return Parsed(path=str(path), records=records, raws=[r.raw for r in records])


# --------------------------------------------------------------------------- #
# 2) DETECT — WHERE (locators) + WHAT (findings).                              #
# --------------------------------------------------------------------------- #
def analyze(parsed: Parsed, *, reveal: bool = False) -> dict:
    """The unified detect pass: where each category lives + what is sensitive.

    ``transcripts.redaction_map`` (Record objects) locates every category; the
    detectors run over the located narrative spans (the kept human/assistant text)
    to surface the secrets/PII a bulk strip would miss. Values are masked by
    default — never echo a raw secret back into the model's context.
    """
    location_map = T.redaction_map(parsed.records)  # WHERE (pass Record objects)
    narrative_findings: list[dict] = []
    for rec in parsed.records:
        for sp in list(T.human_prompts([rec])) + list(T.assistant_text([rec])):
            for f in R.scan_secrets(sp.text) + R.scan_pii(sp.text):
                d = f.to_dict(reveal=reveal)  # category = secret|pii|...; entity = e.g. PERSON
                d["uuid"] = sp.uuid
                d["location"] = sp.category   # WHERE it sits: human_prompts / assistant_text
                narrative_findings.append(d)
    return {
        "by_category": location_map.get("summary", {}),
        "narrative_findings": narrative_findings,
        "secret_count": sum(1 for d in narrative_findings if d.get("category") == "secret"),
        "pii_count": sum(1 for d in narrative_findings if d.get("category") == "pii"),
        "summary": {
            "categories_located": [c for c, v in location_map.get("summary", {}).items()
                                   if v.get("count")],
            "narrative_sensitive": len(narrative_findings),
        },
    }


# --------------------------------------------------------------------------- #
# 3) REDACT — bulk structural strip + char-precise narrative mask (the bridge).#
# --------------------------------------------------------------------------- #
def _deny_findings(text: str, deny: Iterable[str]) -> list[R.Finding]:
    """Make char-precise Findings for every occurrence of each ``deny`` literal —
    codenames the pattern detectors miss but the user's profile says always strip.
    Case-sensitive, longest-first to avoid overlaps."""
    out: list[R.Finding] = []
    for term in sorted({t for t in deny if t}, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(term, start)
            if i < 0:
                break
            out.append(R.Finding(detector="profile", category="ip_marker",
                                  entity="CODENAME", text=term, start=i, end=i + len(term),
                                  severity="high"))
            start = i + len(term)
    return out


def mask_narrative(
    raws: list[dict],
    *,
    allow: Optional[Iterable[str]] = None,
    deny: Optional[Iterable[str]] = None,
) -> list[dict]:
    """In-place char-precise mask of secrets/PII inside the kept narrative fields.

    The locator<->finding bridge between the modules: for each located narrative
    Span, run the detectors on its text, mask in place via the Span's path
    (``transcripts.replace_span``), and emit one ``diff_preview``-shaped
    redaction_map entry per chosen Finding. Mutates ``raws`` in place; also returns
    the redaction_map.

    Profile hooks (the persistent profile is load-bearing):
      * ``allow`` — brand/codename literals to rescue: any Finding whose text is on
        this list is not masked (e.g. "Tuesday", which Presidio mis-eats as a
        DATE_TIME). The user trained this once; it survives into the bundle.
      * ``deny`` — extra literals to always strip even though no detector flags them
        (internal codenames). Masked the same as a detected Finding.
    """
    allow_set = {t for t in (allow or []) if t}
    deny_list = list(deny or [])
    redaction_map: list[dict] = []
    for i, raw in enumerate(raws):
        rec = T.Record(line=i + 1, raw=raw, type=str(raw.get("type", "")))
        spans = list(T.human_prompts([rec])) + list(T.assistant_text([rec]))
        for sp in spans:
            findings = R.scan_secrets(sp.text) + R.scan_pii(sp.text)
            if deny_list:
                findings = findings + _deny_findings(sp.text, deny_list)
            # Profile rescue: drop findings the user allow-listed.
            if allow_set:
                findings = [f for f in findings if f.text not in allow_set]
            # apply_redactions(value_consistent=True, the default) masks EVERY literal
            # occurrence of a detected value, not just the one span a detector returned.
            # Expand to those occurrences HERE and build the redaction_map from the spans
            # ACTUALLY masked — otherwise a value that repeats inside one narrative span
            # is masked more times than the map records, and the preview/gate undercount.
            expanded = R._expand_value_occurrences(sp.text, findings)
            chosen = R.merge_redaction_spans(expanded)
            if not chosen:
                continue
            # value_consistent=False: `expanded` is already occurrence-complete, so the
            # masked bytes are spliced from EXACTLY `chosen` (same spans, same winning
            # entity per span) — the map and the mutation stay in lockstep.
            masked, _ = R.apply_redactions(sp.text, expanded, style="mask",
                                           value_consistent=False)
            if masked == sp.text:
                continue
            T.replace_span(raw, sp, masked)            # locator -> in-place mutation
            for f in chosen:                            # one entry per span actually masked
                redaction_map.append({
                    "uuid": sp.uuid,
                    "category": f.entity,               # ANTHROPIC_KEY / PERSON / EMAIL_ADDRESS / CODENAME / ...
                    "original": f.text,
                    "replacement": f"‹{R._token_label(f.entity)}›",
                    "count": 1,
                })
    return redaction_map


def redact_recipe(
    raws: list[dict],
    *,
    strip: Optional[Iterable[str]] = None,
    mask: bool = True,
    allow: Optional[Iterable[str]] = None,
    deny: Optional[Iterable[str]] = None,
) -> dict:
    """Execute the validated redaction chain on raw dicts.

    ``strip`` defaults to the 9-category bulk strip set; ``mask`` (default True)
    additionally char-precise-masks the kept narrative via the bridge, honoring the
    profile ``allow`` (rescue) / ``deny`` (codename strip) lists. Returns
    ``{sanitized_raws, redaction_map}``. Does NOT mutate the input ``raws``
    (operates on a structural copy) — callers keep the originals for preview/diff.
    """
    strip_cats = list(strip) if strip is not None else list(DEFAULT_STRIP_CATEGORIES)
    sanitized = R.strip_categories(raws, strip_cats, mode="replace")  # returns new dicts
    redaction_map = mask_narrative(sanitized, allow=allow, deny=deny) if mask else []
    return {"sanitized_raws": sanitized, "redaction_map": redaction_map}


# --------------------------------------------------------------------------- #
# 4-5) ASSEMBLE + PREVIEW.                                                      #
# --------------------------------------------------------------------------- #
def assemble_and_preview(
    description: str,
    targets: Mapping[str, list[dict]],
    *,
    originals: Optional[Mapping[str, list[dict]]] = None,
    redaction_map: Optional[list[dict]] = None,
    effort_signal: Optional[Mapping[str, Any]] = None,
    limit: int = P.FEEDBACK_BUDGET_BYTES,
) -> dict:
    """Build the on-disk payload under the 1 MB budget and the concise gate preview.

    ``targets`` = ``{real_path: sanitized_raws}``. ``originals`` (optional, same
    keys) drives a real before/after ``diff_preview``; without it the preview is
    structural-only. Returns ``{payload, preview, total_bytes, over_budget}``.
    """
    payload = P.assemble_payload(description, dict(targets), limit=limit,
                                 effort_signal=effort_signal)
    # diff_preview is per-file; aggregate it across EVERY session that actually ships
    # (the post-budget payload.targets) so a multi-session bundle's gate summary reflects
    # the WHOLE bundle — kept/modified records and bytes summed across all sessions, not
    # just the first (which silently undercounts redactions/bytes in every other session).
    orig_records: list[dict] = []
    red_records: list[dict] = []
    for path, recs in targets.items():
        if os.fspath(path) not in payload.targets:
            continue  # dropped for budget; surfaced via over_budget below
        red_records.extend(recs)
        if originals and path in originals:
            orig_records.extend(originals[path])
        else:
            orig_records.extend(recs)
    preview = P.diff_preview(orig_records, red_records, redaction_map=redaction_map or [])
    return {
        "payload": payload,
        "preview": preview,
        "total_bytes": payload.total_bytes,
        "over_budget": [path for path, _ in payload.dropped],
    }


# --------------------------------------------------------------------------- #
# 6-7) EGRESS GATE — the two-layer gate.                                        #
# --------------------------------------------------------------------------- #
def upload_text(payload: "P.Payload") -> str:
    """The ACTUAL bytes that leave: description (+effort footer) + sanitized JSONL."""
    parts = [payload.description]
    for b in payload.targets.values():
        parts.append(b.decode("utf-8", errors="replace"))
    return "\n".join(parts)


def content_surface(sanitized_raws: list[dict], description: str = "") -> str:
    """The human-meaningful narrative rendered from the sanitized records — the
    right input for the NER recall gate (not raw JSONL, which makes NER hallucinate
    PII from structural tokens)."""
    recs = [T.Record(line=i + 1, raw=r, type=str(r.get("type", "")))
            for i, r in enumerate(sanitized_raws)]
    narrative = list(T.human_prompts(recs)) + list(T.assistant_text(recs))
    body = "\n".join(s.text for s in narrative)
    return (description + "\n" + body) if description else body


def egress_gate(upload: str, content: str, *, reveal: bool = False) -> dict:
    """The two-layer egress gate.

    Layer (a) — the hard, machine-decidable floor: ``scan_secrets`` + the PII regex
    floor over the actual upload bytes. Zero false positives; must be empty to ship.
    Layer (b) — semantic NER ``leak_scan`` over the rendered content surface: a
    recall layer yielding candidates for the co-author to self-repair, never a
    boolean veto.
    """
    floor_secrets = R.scan_secrets(upload)
    floor_pii = R._scan_pii_regex(upload)
    candidates = R.leak_scan(content)
    floor_clean = not floor_secrets and not floor_pii
    return {
        "floor": {
            "secrets": [f.to_dict(reveal=reveal) for f in floor_secrets],
            "pii": [f.to_dict(reveal=reveal) for f in floor_pii],
        },
        "floor_clean": floor_clean,        # the HARD gate — must be True to ship
        "candidates": [f.to_dict(reveal=reveal) for f in candidates],  # for self-repair
        "candidate_count": len(candidates),
    }


# --------------------------------------------------------------------------- #
# Convenience: the whole happy path in one call (used by the MCP demo + tests). #
# --------------------------------------------------------------------------- #
def run_flow(
    path: PathLike,
    description: str,
    *,
    strip: Optional[Iterable[str]] = None,
    effort_signal: Optional[Mapping[str, Any]] = None,
    limit: int = P.FEEDBACK_BUDGET_BYTES,
) -> dict:
    """parse -> analyze -> redact_recipe -> assemble_and_preview -> egress_gate.

    Returns every artifact (no swap — swapping is the runtime's ``submit_begin``).
    This is the read-only analysis half of the flow, safe to call anytime.
    """
    parsed = parse_session(path)
    detection = analyze(parsed)
    red = redact_recipe(parsed.raws, strip=strip)
    sanitized = red["sanitized_raws"]
    ap = assemble_and_preview(
        description, {str(path): sanitized},
        originals={str(path): parsed.raws},
        redaction_map=red["redaction_map"],
        effort_signal=effort_signal, limit=limit,
    )
    gate = egress_gate(upload_text(ap["payload"]), content_surface(sanitized, description))
    return {
        "parsed": parsed,
        "detection": detection,
        "redaction_map": red["redaction_map"],
        "sanitized_raws": sanitized,
        "payload": ap["payload"],
        "preview": ap["preview"],
        "gate": gate,
    }
