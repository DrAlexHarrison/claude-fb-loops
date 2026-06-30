"""fb_os.ingest — a ``stage_review`` bundle -> a normalized ``FeedbackArtifact`` row.

Reuses Build 3's toolbox at every step (the literal "one platform" reuse):

  * ``fb_assist.package.parse_jsonl``        — read the redacted transcript substrate,
  * ``fb_assist.transcripts``                — (validate parse; extract if needed),
  * the inverse of ``package._render_effort_footer`` — recover the effort-signal from
    a ``description.txt`` footer when no ``effort-signal.json`` sidecar is present,
  * ``fb_assist.redact.leak_scan``           — re-run the leak-scan **floor** on the
    way in (defense-in-depth: the artifact is already redacted, but the OS never
    trusts blindly). A blocking hit **quarantines** the bundle — it is stored but
    NEVER embedded, clustered, or shown.

The inbound unit is a directory ``inbox/<artifact_id>/`` written by
``Payload.stage`` (``description.txt`` + per-session ``.jsonl`` + ``effort-signal.json``).
Build 1 adds one **additive, optional** file, ``artifact.json`` (the manifest). When
it is absent, every field is **derived** from the three files Build 3 already writes
(plan §4.1) — so no Build-3 change is required for the core.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable, Optional

from fb_assist import redact
from fb_assist.package import parse_jsonl

from . import questions as Q

PathLike = os.PathLike | str

# Inverse of package._render_effort_footer:
#   "[fb-assist effort signal] redaction=...; quality=4; alignment_confidence=5; rep=tok"
_FOOTER_PREFIX = "[fb-assist effort signal]"
_FOOTER_KEYMAP = {
    "redaction": "redaction",
    "quality": "quality",
    "alignment_confidence": "alignment_confidence",
    "rep": "reputation_token",
}


def strip_effort_footer(description: str) -> str:
    """Return the distilled feedback text with the effort-signal footer removed.

    Build 3 appends ``\\n\\n---\\n[fb-assist effort signal] ...`` to ``description.txt``;
    that metadata must NOT pollute the embedding/clustering vocabulary, so ingest
    stores only the text above the footer (the actual co-authored feedback)."""
    out: list[str] = []
    for ln in (description or "").splitlines():
        if ln.strip().startswith(_FOOTER_PREFIX):
            while out and out[-1].strip() in ("", "---"):
                out.pop()
            break
        out.append(ln)
    return "\n".join(out).strip()


def parse_effort_footer(description: str) -> Optional[dict]:
    """Recover ``{redaction, quality, alignment_confidence, reputation_token}`` from a
    ``description.txt`` footer (the inverse of ``package._render_effort_footer``).
    Returns None if no footer line is present."""
    line = None
    for ln in (description or "").splitlines():
        if ln.strip().startswith(_FOOTER_PREFIX):
            line = ln.strip()[len(_FOOTER_PREFIX):].strip()
            break
    if line is None:
        return None
    sig: dict = {"redaction": None, "quality": None,
                 "alignment_confidence": None, "reputation_token": None}
    for part in line.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        k, _, v = part.partition("=")
        k, v = k.strip(), v.strip()
        key = _FOOTER_KEYMAP.get(k)
        if not key:
            continue
        # Coerce numerics for quality / alignment_confidence (footer renders them bare).
        if key in ("quality", "alignment_confidence"):
            sig[key] = _coerce_num(v)
        else:
            sig[key] = v
    return sig


def _coerce_num(v: str):
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v


def _bundle_files(bundle_dir: Path) -> dict:
    files = {"description": None, "effort_signal": None, "manifest": None, "transcripts": []}
    for p in sorted(bundle_dir.iterdir()):
        if p.name == "description.txt":
            files["description"] = p
        elif p.name == "effort-signal.json":
            files["effort_signal"] = p
        elif p.name == "artifact.json":
            files["manifest"] = p
        elif p.suffix == ".jsonl":
            files["transcripts"].append(p)
    return files


def derive_manifest(bundle_dir: PathLike) -> dict:
    """Build the artifact manifest for a bundle — **present-or-derive** (plan §4.1).

    If ``artifact.json`` exists it is loaded + schema-validated; any absent field is
    backfilled from the other files. If it is absent, every field is derived from
    ``description.txt`` + ``effort-signal.json``/footer + ``*.jsonl``."""
    bundle_dir = Path(bundle_dir)
    files = _bundle_files(bundle_dir)

    description = files["description"].read_text(encoding="utf-8") if files["description"] else ""

    # effort-signal: prefer the sidecar; else parse the description footer.
    effort_signal = None
    if files["effort_signal"]:
        try:
            effort_signal = json.loads(files["effort_signal"].read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            effort_signal = None
    if effort_signal is None:
        effort_signal = parse_effort_footer(description) or {}

    transcript_refs = [p.name for p in files["transcripts"]]

    manifest: dict = {}
    if files["manifest"]:
        manifest = json.loads(files["manifest"].read_text(encoding="utf-8"))
        Q.validate_artifact_manifest(manifest)

    # Derive / backfill.
    artifact_id = manifest.get("artifact_id") or bundle_dir.name
    created_at = manifest.get("created_at")
    if not created_at:
        try:
            from datetime import datetime, timezone
            mt = bundle_dir.stat().st_mtime
            created_at = Q.now_iso(datetime.fromtimestamp(mt, timezone.utc))
        except OSError:
            created_at = Q.now_iso()

    derived = {
        "schema_version": manifest.get("schema_version", "1.0"),
        "artifact_id": artifact_id,
        "surface": manifest.get("surface", "cli"),
        "created_at": created_at,
        "session_ids": manifest.get("session_ids", [p.stem for p in files["transcripts"]]),
        "description_ref": manifest.get("description_ref", "description.txt"),
        "transcript_refs": manifest.get("transcript_refs", transcript_refs),
        "report_only": manifest.get("report_only", len(transcript_refs) == 0),
        "answers_question_id": manifest.get("answers_question_id"),
        "effort_signal": manifest.get("effort_signal", effort_signal),
        # carried for ingest, not part of the manifest schema:
        "_description": description,
        "_transcript_paths": [str(p) for p in files["transcripts"]],
    }
    return derived


# gitleaks adds a ~480 ms subprocess per scan. It's high-value defense-in-depth for
# unknown secret shapes, but the deterministic regex floor already catches the
# high-value vendor keys (Anthropic/AWS/GitHub/Stripe/...), so the CORE gate runs
# regex + detect-secrets (fast, deterministic, no subprocess) and leaves gitleaks
# opt-in via $FB_OS_LEAK_GITLEAKS=1 for the production path.
_USE_GITLEAKS = os.environ.get("FB_OS_LEAK_GITLEAKS", "0") == "1"


def default_leak_scan(text: str):
    """The ingest quarantine floor — the **high-precision** subset of
    ``fb_assist.redact``: ``scan_secrets`` (regex + detect-secrets; gitleaks opt-in) +
    the deterministic regex PII (email / IP / SSN) + proprietary-IP markers.

    The probabilistic NER layers (Presidio / GLiNER) are deliberately EXCLUDED from
    the auto-quarantine gate: they are high-recall/low-precision (they tag a bare
    ``u1`` as a driver's licence and ``gitBranch`` as an organization), so gating on
    them would quarantine everything. They remain available in ``fb_assist`` for the
    redaction *authoring* pass; the *gate* must be precise. GLiNER is OFF to avoid the
    ~86 MB model download; gitleaks is OFF by default (subprocess cost) but enabled by
    ``$FB_OS_LEAK_GITLEAKS=1``. Returns the Findings list."""
    findings = list(redact.scan_secrets(text, use_gitleaks=_USE_GITLEAKS))
    # Deterministic, high-precision regex PII (email / IPv4 / US SSN).
    scan_pii_regex = getattr(redact, "_scan_pii_regex", None)
    if scan_pii_regex is not None:
        findings += scan_pii_regex(text)
    # Obvious proprietary-IP markers ("confidential", "do not distribute", ...).
    scan_ip_markers = getattr(redact, "_scan_ip_markers", None)
    if scan_ip_markers is not None:
        findings += scan_ip_markers(text)
    return findings


def _content_hash(description: str, transcript_paths: list[str]) -> str:
    h = hashlib.sha256()
    h.update(description.encode("utf-8"))
    for tp in transcript_paths:
        try:
            h.update(Path(tp).read_bytes())
        except OSError:
            continue
    return h.hexdigest()[:12]


def ingest_bundle(
    bundle_dir: PathLike,
    *,
    leak_scan_fn: Callable[[str], list] = default_leak_scan,
    quarantine_on: str = "blocking",
) -> dict:
    """Turn one bundle directory into a ``FeedbackArtifact`` dict (no embedding yet).

    Runs the leak-scan floor over the **full** bundle text (description + raw
    transcript bytes). ``quarantine_on``:
      * ``"blocking"`` (default) — quarantine only on high/critical findings (the
        egress-gate semantics; benign redacted ``‹path›`` markers won't trip it),
      * ``"any"`` — quarantine on any finding (strict).

    A quarantined artifact carries ``quarantined=True`` + ``quarantine_reason`` and
    must never be embedded/clustered/shown.
    """
    manifest = derive_manifest(bundle_dir)
    description_full = manifest.pop("_description")
    transcript_paths = manifest.pop("_transcript_paths")
    # Store only the distilled feedback (footer stripped) so effort-signal metadata
    # never pollutes the embedding/clustering vocabulary.
    description = strip_effort_footer(description_full)

    # Validate the transcript actually parses through fb_assist (it is the redacted
    # substrate Build 3 wrote; a malformed one is itself a red flag). Scan the FULL
    # text (footer + transcript) for leaks, but store only the distilled description.
    transcript_text_parts = [description_full]
    parse_ok = True
    for tp in transcript_paths:
        try:
            raw = Path(tp).read_text(encoding="utf-8", errors="replace")
            parse_jsonl(raw)  # raises on a malformed non-blank line
            transcript_text_parts.append(raw)
        except (OSError, ValueError):
            parse_ok = False
    bundle_text = "\n".join(transcript_text_parts)

    findings = leak_scan_fn(bundle_text) or []
    summary = redact.summarize_findings(findings)
    if quarantine_on == "any":
        hit = summary["total"] > 0
    else:
        hit = summary["blocking"]

    artifact = {
        "artifact_id": manifest["artifact_id"],
        "surface": manifest.get("surface", "cli"),
        "created_at": manifest.get("created_at"),
        "description": description,
        "transcript_path": transcript_paths[0] if transcript_paths else None,
        "report_only": bool(manifest.get("report_only", not transcript_paths)),
        "answers_question_id": manifest.get("answers_question_id"),
        "effort_signal": manifest.get("effort_signal", {}) or {},
        "embedding": None,
        "cluster_id": None,
        "triaged_at": None,
        "quarantined": bool(hit),
        "quarantine_reason": None,
        "_leak_summary": summary,
        "_content_hash": _content_hash(description, transcript_paths),
    }
    if hit:
        cats = ",".join(f"{k}:{v}" for k, v in summary.get("by_category", {}).items())
        artifact["quarantine_reason"] = (
            f"leak-scan floor: {summary['total']} finding(s) "
            f"({'blocking' if summary['blocking'] else 'non-blocking'}); by_category={cats}"
        )
    if not parse_ok:
        # A transcript that won't parse is suspicious; quarantine defensively.
        artifact["quarantined"] = True
        artifact["quarantine_reason"] = (artifact.get("quarantine_reason") or "") + " | transcript parse failed"
    return artifact


def ingest_inbox(
    store,
    inbox_dir: PathLike,
    embedder=None,
    *,
    leak_scan_fn: Callable[[str], list] = default_leak_scan,
    quarantine_on: str = "blocking",
) -> list[dict]:
    """Ingest every bundle directory under ``inbox_dir`` into ``store``, embedding
    the non-quarantined ones. Returns the list of ingested artifact dicts.

    Idempotent: re-ingesting the same ``artifact_id`` upserts (so the inbox can be
    re-scanned safely — the closed loop re-runs this on every new drop)."""
    inbox_dir = Path(inbox_dir)
    from .embed import Embedder

    embedder = embedder or Embedder()
    out: list[dict] = []
    if not inbox_dir.is_dir():
        return out
    for bundle in sorted(p for p in inbox_dir.iterdir() if p.is_dir()):
        if not (bundle / "description.txt").exists():
            continue
        art = ingest_bundle(bundle, leak_scan_fn=leak_scan_fn, quarantine_on=quarantine_on)
        if not art["quarantined"]:
            art["embedding"] = embedder.embed(art["description"])
        store.upsert_artifact(art)
        out.append(art)
    return out
