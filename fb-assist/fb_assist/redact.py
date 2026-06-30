"""fb_assist.redact — composable detection + redaction primitives for fb-assist (Build 3).

The DETERMINISTIC FLOOR of the redaction toolbox. Single-purpose functions the
co-author Claude composes freely; an LLM (Claude) genericize pass is the semantic
ceiling and is wired in later — this module is the auditable, offline floor.

Layers (per 00-investigation-findings.md §3A — "layering mandatory"):
  - secrets : gitleaks + detect-secrets + high-value regex   -> scan_secrets()
  - PII     : Microsoft Presidio (spaCy) + GLiNER (zero-shot) -> scan_pii() / anonymize_pii()
  - structural strips by transcript category                 -> strip_categories()
  - reversible tokenization (consistent ‹PERSON_1› handles)  -> reversible_tokenize()
  - adversarial egress gate over the outbound bundle         -> leak_scan()

Operates on raw text AND on parsed transcript records (a record == one parsed JSONL
line, schema per 00-investigation-findings.md §2.2 + the shared category vocabulary).

LOCAL ONLY. Nothing in this module performs network egress except a one-time,
cached model download from Hugging Face. No transcript content ever leaves the box.
"""

from __future__ import annotations

import os

# Force the torch-only path for transformers/gliner. With Keras 3 present (no
# tf-keras), transformers' lazy TF import explodes on `import gliner`; USE_TF=0
# skips it entirely. Set before any transformers/gliner import below.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

# --------------------------------------------------------------------------- #
# Shared category vocabulary (MUST match ToolExtract's transcripts.py)
# --------------------------------------------------------------------------- #
CATEGORIES = [
    "human_prompts",
    "thinking_blocks",
    "assistant_text",
    "bash_output",
    "file_contents",
    "tool_calls",
    "paths",
    "env_metadata",
    "hook_output",
    "injected_memory",
    "websearch",
]

# Tool-name groupings used to classify tool_result / tool_use records by category.
_FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
_BASH_TOOLS = {"Bash", "BashOutput", "KillShell"}
_WEB_TOOLS = {"WebSearch", "WebFetch"}

GLINER_MODEL = "knowledgator/gliner-pii-small-v1.0"
GLINER_ONNX_FILE = "onnx/model_quint8.onnx"

# Default zero-shot labels for the GLiNER pass. Tuned for transcript PII; kept
# tight to hold down false positives and latency.
GLINER_LABELS = [
    "person",
    "organization",
    "location",
    "email",
    "phone number",
    "street address",
    "date of birth",
    "credit card number",
    "social security number",
    "api key",
    "password",
    "ip address",
    "url",
    "money amount",
]

# Presidio entity -> severity. Anything not listed defaults to "medium".
_PII_SEVERITY = {
    "CREDIT_CARD": "critical",
    "US_SSN": "critical",
    "US_BANK_NUMBER": "critical",
    "IBAN_CODE": "critical",
    "CRYPTO": "high",
    "MEDICAL_LICENSE": "high",
    "US_PASSPORT": "high",
    "US_DRIVER_LICENSE": "high",
    "EMAIL_ADDRESS": "medium",
    "PHONE_NUMBER": "medium",
    "PERSON": "medium",
    "LOCATION": "low",
    "URL": "low",
    "DATE_TIME": "low",
    "NRP": "low",
    "IP_ADDRESS": "medium",
}

_GLINER_SEVERITY = {
    "api key": "critical",
    "password": "critical",
    "social security number": "critical",
    "credit card number": "critical",
    "person": "medium",
    "email": "medium",
    "phone number": "medium",
    "street address": "medium",
    "ip address": "medium",
    "date of birth": "medium",
    "organization": "low",
    "location": "low",
    "url": "low",
    "money amount": "low",
}


# --------------------------------------------------------------------------- #
# Finding
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    """One detected sensitive span. `text` holds the raw value — local only; use
    `.masked` / `to_dict()` for anything that might be displayed or persisted."""

    detector: str          # gitleaks | detect-secrets | regex | presidio | gliner | path | env | ip-marker
    category: str          # secret | pii | path | env_metadata | ip_marker
    entity: str            # AWS_ACCESS_KEY | EMAIL_ADDRESS | person | cwd | ...
    text: str = ""         # the matched substring (sensitive)
    start: int = -1
    end: int = -1
    score: float = 1.0
    severity: str = "medium"
    redactable: bool = True  # False => detector gave no usable char span (e.g. detect-secrets hashes)

    @property
    def masked(self) -> str:
        t = self.text or ""
        if len(t) <= 6:
            return (t[:1] + "…") if t else "…"
        return f"{t[:3]}…{t[-2:]}"

    def to_dict(self, reveal: bool = False) -> dict:
        d = asdict(self)
        if not reveal:
            d["text"] = self.masked
        return d


_SEV_RANK = {"low": 0, "medium": 1, "high": 2, "critical": 3}


# --------------------------------------------------------------------------- #
# Secret detection
# --------------------------------------------------------------------------- #
# The deterministic floor. `/feedback`'s built-in redaction is keys-only — these
# must, at minimum, match what it strips, then go further. Order matters: more
# specific patterns first so overlap-resolution keeps the precise entity.
SECRET_PATTERNS: list[tuple[str, str, str]] = [
    ("ANTHROPIC_KEY", r"sk-ant-(?:api|admin)?[A-Za-z0-9_\-]{20,}", "critical"),
    ("AWS_ACCESS_KEY", r"\b(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)[0-9A-Z]{16}\b", "critical"),
    ("GITHUB_TOKEN", r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}\b", "critical"),
    ("GITHUB_PAT", r"\bgithub_pat_[A-Za-z0-9_]{22,}\b", "critical"),
    ("STRIPE_KEY", r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b", "critical"),
    ("OPENAI_KEY", r"\bsk-(?:proj-)?[A-Za-z0-9]{32,}\b", "critical"),
    ("GCP_API_KEY", r"\bAIza[0-9A-Za-z_\-]{35}\b", "high"),
    ("GOOGLE_OAUTH", r"\bya29\.[A-Za-z0-9_\-]{20,}", "high"),
    ("SLACK_TOKEN", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b", "high"),
    ("PRIVATE_KEY", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP )?PRIVATE KEY-----", "critical"),
    ("JWT", r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b", "high"),
    ("AUTH_BEARER", r"(?i)\b(?:bearer|authorization:\s*bearer)\s+[A-Za-z0-9_\-\.=]{20,}", "high"),
    ("GENERIC_SECRET_ASSIGN",
     r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\b\s*[:=]\s*['\"]?[A-Za-z0-9_\-/+.]{12,}['\"]?",
     "medium"),
]
_SECRET_RE = [(name, re.compile(pat), sev) for name, pat, sev in SECRET_PATTERNS]


def _scan_secrets_regex(text: str) -> list[Finding]:
    out: list[Finding] = []
    for name, rx, sev in _SECRET_RE:
        for m in rx.finditer(text):
            out.append(Finding("regex", "secret", name, m.group(0), m.start(), m.end(), 1.0, sev))
    return out


def _gitleaks_path() -> Optional[str]:
    return shutil.which("gitleaks") or _exists(os.path.expanduser("~/.local/bin/gitleaks"))


def _exists(p: str) -> Optional[str]:
    return p if os.path.exists(p) else None


def _scan_secrets_gitleaks(text: str) -> list[Finding]:
    exe = _gitleaks_path()
    if not exe:
        return []
    out: list[Finding] = []
    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, "input.txt")
        rep = os.path.join(td, "report.json")
        with open(src, "w") as fh:
            fh.write(text)
        # gitleaks v8: `dir` scans a filesystem path with no git history needed.
        cmd = [exe, "dir", src, "--report-format", "json", "--report-path", rep,
               "--no-banner", "--exit-code", "0"]
        try:
            subprocess.run(cmd, capture_output=True, timeout=120)
        except Exception:
            return []
        if not os.path.exists(rep):
            return []
        try:
            with open(rep) as fh:
                data = json.load(fh) or []
        except Exception:
            return []
    for item in data:
        secret = item.get("Secret") or item.get("Match") or ""
        rule = item.get("RuleID") or "gitleaks"
        start = text.find(secret) if secret else -1
        end = start + len(secret) if start >= 0 else -1
        out.append(Finding("gitleaks", "secret", rule, secret, start, end, 1.0, "critical",
                           redactable=start >= 0))
    return out


# detect-secrets' two entropy plugins fire on every base64/hex blob (JWT bodies,
# the base64 `signature` on every thinking block, git hashes...) — pure noise in
# a transcript. The structured-credential plugins are the additive value (they
# cover Artifactory/Azure/Cloudant/IBM/Discord/GitLab/Mailchimp/NPM/SendGrid/
# Square/Stripe/Twilio/... that we don't hand-roll), so keep those and drop these.
_DS_SKIP_TYPES = {"Base64 High Entropy String", "Hex High Entropy String"}
# Transcript JSONL lines can be a single 100 KB+ blob (a full file dump). detect-
# secrets' entropy plugins are ~O(n) per line but still pricey on mega-lines, so
# skip lines past this length — the regex floor + gitleaks scan the whole text
# linearly and cover those lines anyway.
_DS_MAX_LINE = 20_000


def _scan_secrets_detect_secrets(text: str) -> list[Finding]:
    """detect-secrets via its `scan_line` Python API.

    NOTE: the `detect-secrets scan <file>` CLI path applies aggressive heuristic
    filters that strip even valid AWS keys to an empty result; `scan_line` runs
    the plugins directly AND exposes the raw `secret_value`, so these findings are
    real, attributable, and redactable (precise char spans)."""
    try:
        from detect_secrets.core.scan import scan_line
        from detect_secrets.settings import default_settings
    except Exception:
        return []
    out: list[Finding] = []
    try:
        with default_settings():
            offset = 0
            for line in text.split("\n"):
                if len(line) > _DS_MAX_LINE:
                    offset += len(line) + 1
                    continue
                for s in scan_line(line):
                    if s.type in _DS_SKIP_TYPES:
                        continue
                    val = getattr(s, "secret_value", None) or ""
                    col = line.find(val) if val else -1
                    start = offset + col if col >= 0 else -1
                    end = start + len(val) if start >= 0 else -1
                    out.append(Finding("detect-secrets", "secret", s.type, val, start, end,
                                       0.9, "high", redactable=start >= 0))
                offset += len(line) + 1
    except Exception:
        return []
    return out


def scan_secrets(text: str, use_gitleaks: bool = True, use_detect_secrets: bool = True) -> list[Finding]:
    """Detect secrets in `text` (layered: regex floor + gitleaks + detect-secrets).
    Returns ALL findings with per-detector attribution (overlaps preserved)."""
    findings = _scan_secrets_regex(text)
    if use_gitleaks:
        findings += _scan_secrets_gitleaks(text)
    if use_detect_secrets:
        findings += _scan_secrets_detect_secrets(text)
    return findings


# --------------------------------------------------------------------------- #
# PII detection (Presidio + GLiNER)
# --------------------------------------------------------------------------- #
# Deterministic PII floor. Per the investigation ("regex email beats all NER"),
# a plain email regex catches addresses with any TLD (incl. reserved .example /
# .test / .invalid) that Presidio's stricter pattern rejects and NER may miss.
_PII_REGEX: list[tuple[str, str, str]] = [
    ("EMAIL_ADDRESS", r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", "medium"),
    ("IP_ADDRESS", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", "medium"),
    ("US_SSN", r"\b\d{3}-\d{2}-\d{4}\b", "critical"),
]
_PII_REGEX_RE = [(name, re.compile(pat), sev) for name, pat, sev in _PII_REGEX]


def _scan_pii_regex(text: str) -> list[Finding]:
    out: list[Finding] = []
    for name, rx, sev in _PII_REGEX_RE:
        for m in rx.finditer(text):
            out.append(Finding("regex", "pii", name, m.group(0), m.start(), m.end(), 0.95, sev))
    return out


_analyzer = None
_gliner = None
_gliner_failed = False


def _get_analyzer():
    global _analyzer
    if _analyzer is None:
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider

        provider = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy",
            "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
        })
        _analyzer = AnalyzerEngine(nlp_engine=provider.create_engine())
    return _analyzer


# Big weight files we never want (metered bandwidth): the fp32/fp16 ONNX and the
# PyTorch checkpoint. We run inference on the ~83 MB quantized-uint8 ONNX only.
_GLINER_IGNORE = ["pytorch_model.bin", "*.safetensors", "onnx/model.onnx",
                  "onnx/model_fp16.onnx", "*.h5", "tf_model*", "rust_model*", "*.msgpack"]


def _ensure_gliner_files() -> str:
    """Fetch ONLY the quantized-uint8 ONNX + tokenizer/config for the GLiNER model
    (idempotent; skips anything already cached). Returns the local snapshot dir.
    Keeps the on-disk + over-the-wire footprint to ~86 MB instead of ~860 MB."""
    from huggingface_hub import snapshot_download

    return snapshot_download(GLINER_MODEL, ignore_patterns=_GLINER_IGNORE)


def _get_gliner():
    global _gliner, _gliner_failed
    if _gliner is None and not _gliner_failed:
        try:
            from gliner import GLiNER

            local_dir = _ensure_gliner_files()
            # Load from the local snapshot with local_files_only=True so GLiNER
            # never re-resolves the repo and re-pulls the big weights we skipped.
            _gliner = GLiNER.from_pretrained(
                local_dir,
                load_onnx_model=True,
                onnx_model_file=GLINER_ONNX_FILE,
                load_tokenizer=True,
                local_files_only=True,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            _gliner_failed = True
            print(f"[redact] GLiNER unavailable ({exc}); PII falls back to Presidio only.",
                  file=sys.stderr)
    return _gliner


def _chunks(text: str, size: int = 1200, overlap: int = 120):
    """Yield (chunk_text, char_offset). GLiNER caps at a few hundred tokens, so
    long transcripts must be windowed; overlap avoids splitting an entity."""
    if len(text) <= size:
        yield text, 0
        return
    i = 0
    n = len(text)
    while i < n:
        yield text[i:i + size], i
        if i + size >= n:
            break
        i += size - overlap


# spaCy's parser/NER needs ~1 GB temp RAM per 100k chars and hard-caps at 1M
# chars, so large transcripts MUST be windowed. 200k keeps memory bounded; the
# overlap re-catches entities that straddle a window boundary.
_PRESIDIO_WINDOW = 200_000
_PRESIDIO_OVERLAP = 512


def _scan_pii_presidio(text: str, entities: Optional[list[str]] = None) -> list[Finding]:
    try:
        analyzer = _get_analyzer()
    except Exception as exc:  # pragma: no cover
        print(f"[redact] Presidio unavailable ({exc}).", file=sys.stderr)
        return []
    out: list[Finding] = []
    seen: set[tuple[int, int, str]] = set()
    for chunk, offset in _chunks(text, size=_PRESIDIO_WINDOW, overlap=_PRESIDIO_OVERLAP):
        try:
            results = analyzer.analyze(text=chunk, language="en", entities=entities)
        except Exception as exc:  # pragma: no cover
            print(f"[redact] Presidio analyze failed ({exc}).", file=sys.stderr)
            continue
        for r in results:
            s, e = r.start + offset, r.end + offset
            key = (s, e, r.entity_type)
            if key in seen:
                continue
            seen.add(key)
            out.append(Finding("presidio", "pii", r.entity_type, text[s:e],
                               s, e, float(r.score),
                               _PII_SEVERITY.get(r.entity_type, "medium")))
    return out


def _scan_pii_gliner(text: str, labels: Optional[list[str]] = None,
                     threshold: float = 0.45) -> list[Finding]:
    model = _get_gliner()
    if model is None:
        return []
    labels = labels or GLINER_LABELS
    out: list[Finding] = []
    seen: set[tuple[int, int, str]] = set()
    for chunk, offset in _chunks(text):
        try:
            ents = model.predict_entities(chunk, labels, threshold=threshold)
        except Exception:
            continue
        for e in ents:
            s, en = e["start"] + offset, e["end"] + offset
            key = (s, en, e["label"])
            if key in seen:
                continue
            seen.add(key)
            out.append(Finding("gliner", "pii", e["label"], text[s:en], s, en,
                               float(e["score"]), _GLINER_SEVERITY.get(e["label"], "medium")))
    return out


def scan_pii(text: str, entities: Optional[list[str]] = None,
             gliner_labels: Optional[list[str]] = None, gliner_threshold: float = 0.45,
             use_gliner: bool = True) -> list[Finding]:
    """Detect PII (deterministic regex floor + Presidio regex/NER + GLiNER zero-shot
    NER). Returns ALL findings with per-detector attribution so recall is
    measurable per layer."""
    findings = _scan_pii_regex(text)
    findings += _scan_pii_presidio(text, entities)
    if use_gliner:
        findings += _scan_pii_gliner(text, gliner_labels, gliner_threshold)
    return findings


# --------------------------------------------------------------------------- #
# Span resolution + the replacement engine (mask / reversible-token)
# --------------------------------------------------------------------------- #
def merge_redaction_spans(findings: Iterable[Finding]) -> list[Finding]:
    """Pick a non-overlapping set of redactable spans. Prefers higher severity,
    then longer span, then higher score — so a precise secret beats a loose
    PII guess covering the same bytes."""
    usable = [f for f in findings if f.redactable and f.start >= 0 and f.end > f.start]
    usable.sort(key=lambda f: (-_SEV_RANK.get(f.severity, 1), -(f.end - f.start), -f.score, f.start))
    chosen: list[Finding] = []
    taken: list[tuple[int, int]] = []
    for f in usable:
        if any(f.start < e and f.end > s for s, e in taken):
            continue
        chosen.append(f)
        taken.append((f.start, f.end))
    chosen.sort(key=lambda f: f.start)
    return chosen


def _token_label(entity: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", entity.upper()).strip("_") or "REDACTED"


def apply_redactions(text: str, findings: Iterable[Finding], style: str = "mask",
                     mapping: Optional[dict] = None) -> tuple[str, dict]:
    """Replace each chosen span in `text`.

    style="mask"  -> ‹ENTITY›
    style="token" -> ‹ENTITY_n› with a consistent value->token map (relationships
                     preserved: the same value always gets the same token).
    Returns (redacted_text, mapping) where mapping is token -> original value
    (style="token") so a trusted local caller can reverse it.
    """
    spans = merge_redaction_spans(findings)
    mapping = mapping if mapping is not None else {}
    # Key the token map on the VALUE (not the label): the same value must always
    # map to the same token, even when different detectors win at different
    # occurrences (e.g. one sk-ant key tagged ANTHROPIC_KEY here, "api key" there).
    value_to_token: dict[str, str] = {}
    counters: dict[str, int] = {}
    for tok, val in mapping.items():  # seed from any prior mapping (cross-call consistency)
        value_to_token[val] = tok

    pieces = []
    cursor = 0
    for f in spans:
        pieces.append(text[cursor:f.start])
        label = _token_label(f.entity)
        if style == "token":
            tok = value_to_token.get(f.text)
            if tok is None:
                counters[label] = counters.get(label, 0) + 1
                tok = f"‹{label}_{counters[label]}›"
                value_to_token[f.text] = tok
                mapping[tok] = f.text
            pieces.append(tok)
        else:
            pieces.append(f"‹{label}›")
        cursor = f.end
    pieces.append(text[cursor:])
    return "".join(pieces), mapping


def anonymize_pii(text: str, use_gliner: bool = True, style: str = "mask") -> str:
    """Detect + replace all PII in `text` in one call (Presidio + GLiNER)."""
    findings = scan_pii(text, use_gliner=use_gliner)
    redacted, _ = apply_redactions(text, findings, style=style)
    return redacted


def reversible_tokenize(text: str, findings: Optional[Iterable[Finding]] = None,
                        include_secrets: bool = True, include_pii: bool = True,
                        use_gliner: bool = True) -> tuple[str, dict]:
    """Replace sensitive values with consistent ‹ENTITY_n› placeholders so meaning
    stays traceable while values are hidden. Returns (text, token->value mapping)."""
    if findings is None:
        findings = []
        if include_secrets:
            findings += scan_secrets(text)
        if include_pii:
            findings += scan_pii(text, use_gliner=use_gliner)
    return apply_redactions(text, findings, style="token")


# --------------------------------------------------------------------------- #
# Structural strips by transcript category (operate on parsed records)
# --------------------------------------------------------------------------- #
def _mark(category: str, n: Optional[int] = None) -> str:
    return f"‹{category} stripped" + (f": {n} chars›" if n is not None else "›")


def _index_tool_names(records: list[dict]) -> dict[str, str]:
    """Map tool_use_id -> tool name, so tool_result records can be classified
    (Bash vs Read vs WebSearch) by the call that produced them."""
    names: dict[str, str] = {}
    for rec in records:
        msg = rec.get("message")
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for blk in msg["content"]:
                if isinstance(blk, dict) and blk.get("type") == "tool_use":
                    if blk.get("id"):
                        names[blk["id"]] = blk.get("name", "")
    return names


def _replace_value(obj: dict, key: str, category: str, mode: str) -> bool:
    val = obj.get(key)
    if not isinstance(val, str) or not val:
        return False
    if mode == "drop":
        obj.pop(key, None)
    elif mode == "blank":
        obj[key] = ""
    else:
        obj[key] = _mark(category, len(val))
    return True


def _strip_message_blocks(rec: dict, cats: set, mode: str) -> None:
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
            _replace_value(blk, "text", "assistant_text", mode)
        elif bt == "text" and role == "user" and "human_prompts" in cats:
            _replace_value(blk, "text", "human_prompts", mode)
        elif bt == "tool_use" and "tool_calls" in cats:
            # Keep the tool name (signal); scrub the inputs (paths/contents/args).
            blk["input"] = {"__stripped__": _mark("tool_calls")}


def _strip_tool_results(rec: dict, cats: set, names: dict, mode: str) -> None:
    msg = rec.get("message")
    tur = rec.get("toolUseResult")

    # Determine which category this tool_result belongs to (by originating tool).
    tool_name = ""
    tool_use_id = None
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for blk in msg["content"]:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                tool_use_id = blk.get("tool_use_id")
                break
    if tool_use_id:
        tool_name = names.get(tool_use_id, "")

    def _category_for() -> Optional[str]:
        if tool_name in _BASH_TOOLS:
            return "bash_output"
        if tool_name in _FILE_TOOLS:
            return "file_contents"
        if tool_name in _WEB_TOOLS:
            return "websearch"
        # Fall back to structural shape of toolUseResult when name unknown.
        if isinstance(tur, dict):
            if "file" in tur or "originalFile" in tur or "structuredPatch" in tur:
                return "file_contents"
            if "query" in tur and ("results" in tur or "links" in tur):
                return "websearch"
            if "stdout" in tur or "stderr" in tur:
                return "bash_output"
        return None

    category = _category_for()
    if category is None or category not in cats:
        return

    # Scrub the structured mirror (toolUseResult).
    if isinstance(tur, dict):
        if category == "file_contents":
            f = tur.get("file")
            if isinstance(f, dict):
                _replace_value(f, "content", "file_contents", mode)
            for k in ("originalFile", "newString", "oldString", "content"):
                _replace_value(tur, k, "file_contents", mode)
            if tur.get("structuredPatch"):
                tur["structuredPatch"] = []
        elif category == "bash_output":
            for k in ("stdout", "stderr"):
                _replace_value(tur, k, "bash_output", mode)
        elif category == "websearch":
            for k in ("results", "links", "content"):
                if k in tur:
                    tur[k] = _mark("websearch")

    # Scrub the human-readable mirror (message.content tool_result blocks).
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for blk in msg["content"]:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                c = blk.get("content")
                if isinstance(c, str):
                    blk["content"] = _mark(category, len(c))
                elif isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                            sub["text"] = _mark(category, len(sub["text"]))


def _strip_tool_calls_output(rec: dict, mode: str) -> None:
    """Generic tool-OUTPUT strip for the `tool_calls` lever — shape-agnostic, so it
    covers the long tail of ~44 toolUseResult shapes (MCP results, Task, Glob,
    TodoWrite, …) that the specific bash_output/file_contents/websearch strippers
    don't recognize. Removes BOTH stored copies: the model-visible tool_result
    block AND the entire structured toolUseResult mirror."""
    msg = rec.get("message")
    if isinstance(msg, dict) and isinstance(msg.get("content"), list):
        for blk in msg["content"]:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                c = blk.get("content")
                if isinstance(c, str):
                    blk["content"] = _mark("tool_calls", len(c))
                elif isinstance(c, list):
                    for sub in c:
                        if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                            sub["text"] = _mark("tool_calls", len(sub["text"]))
    tur = rec.get("toolUseResult")
    if tur not in (None, "", {}, []):
        n = len(tur) if isinstance(tur, str) else len(json.dumps(tur, ensure_ascii=False))
        rec["toolUseResult"] = _mark("tool_calls", n)


_PATH_RE = re.compile(
    r"(/home/[^/\s]+|/Users/[^/\s]+|/root)(/[^\s\"'`)>\]]*)?"
    r"|[A-Za-z]:\\Users\\[^\\\s]+(\\[^\s\"'`)>\]]*)?"
)


def _scrub_paths_in_str(s: str) -> str:
    return _PATH_RE.sub("‹path›", s)


def _deep_scrub_paths(obj: Any) -> Any:
    if isinstance(obj, str):
        return _scrub_paths_in_str(obj)
    if isinstance(obj, list):
        return [_deep_scrub_paths(x) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # Paths leak through dict KEYS too — e.g. file-history-snapshot records
            # key `trackedFileBackups` by absolute path. Scrub the key as well.
            nk = _scrub_paths_in_str(k) if isinstance(k, str) else k
            if k in ("filePath", "file_path", "cwd", "workingDirectory") and isinstance(v, str):
                out[nk] = "‹path›"
            else:
                out[nk] = _deep_scrub_paths(v)
        return out
    return obj


_ENV_FIELDS = ("cwd", "gitBranch", "version", "entrypoint", "userType",
               "workingDirectory", "commitSha", "platform", "terminal", "teammateIds")


def _strip_env(rec: dict, mode: str) -> None:
    for k in _ENV_FIELDS:
        if k in rec and rec[k] not in (None, ""):
            rec[k] = f"‹{k}›"
    tur = rec.get("toolUseResult")
    if isinstance(tur, dict):
        for k in ("cwd", "gitBranch", "version"):
            if k in tur:
                tur[k] = f"‹{k}›"


def _strip_paths(rec: dict, mode: str) -> None:
    for k, v in list(rec.items()):
        if k in ("uuid", "parentUuid", "sessionId", "leafUuid", "requestId"):
            continue
        rec[k] = _deep_scrub_paths(v)


def _is_hook_record(rec: dict) -> bool:
    if rec.get("type") == "attachment":
        att = rec.get("attachment")
        if isinstance(att, dict) and str(att.get("type", "")).startswith("hook"):
            return True
    return any(k in rec for k in ("hookInfos", "hookAdditionalContext", "hookErrors", "hookCount"))


def _strip_hook(rec: dict, mode: str) -> None:
    att = rec.get("attachment")
    if isinstance(att, dict) and (str(att.get("type", "")).startswith("hook") or "hookName" in att):
        for k in ("stdout", "stderr", "content"):
            _replace_value(att, k, "hook_output", mode)
    for k in ("hookAdditionalContext", "hookInfos", "hookErrors"):
        if k in rec and rec[k]:
            rec[k] = _mark("hook_output")


_MEMORY_MARKERS = ("MEMORY.md", "CLAUDE.md", "/memory/", "nested_memory", "additionalContext")


def _strip_memory(rec: dict, mode: str) -> None:
    att = rec.get("attachment")
    if not isinstance(att, dict):
        return
    atype = str(att.get("type", ""))
    blob = json.dumps(att) if att else ""
    if atype == "nested_memory" or any(m in blob for m in _MEMORY_MARKERS):
        for k in ("stdout", "content", "stderr"):
            _replace_value(att, k, "injected_memory", mode)


def strip_categories(records: list[dict], categories: Iterable[str],
                     mode: str = "replace") -> list[dict]:
    """Remove/replace whole categories of content across parsed transcript records.

    categories: any subset of CATEGORIES.
    mode: "replace" (default, leaves a ‹category stripped: N chars› marker so the
          transcript stays coherent and the co-author can see WHAT was removed),
          "blank" (empty string), or "drop" (delete the key where possible).
    Returns a NEW list (input records are never mutated)."""
    cats = set(categories)
    unknown = cats - set(CATEGORIES)
    if unknown:
        raise ValueError(f"unknown categories: {sorted(unknown)}; valid = {CATEGORIES}")
    out = copy.deepcopy(records)
    names = _index_tool_names(out)
    msg_cats = {"human_prompts", "assistant_text", "thinking_blocks", "tool_calls"}
    tool_cats = {"bash_output", "file_contents", "websearch"}
    for rec in out:
        if not isinstance(rec, dict):
            continue
        if cats & msg_cats:
            _strip_message_blocks(rec, cats, mode)
        if cats & tool_cats:
            _strip_tool_results(rec, cats, names, mode)
        if "tool_calls" in cats:
            _strip_tool_calls_output(rec, mode)
        if "hook_output" in cats:
            _strip_hook(rec, mode)
        if "injected_memory" in cats:
            _strip_memory(rec, mode)
        if "env_metadata" in cats:
            _strip_env(rec, mode)
        if "paths" in cats:
            _strip_paths(rec, mode)
    return out


# --------------------------------------------------------------------------- #
# leak_scan — the adversarial egress gate
# --------------------------------------------------------------------------- #
_ENV_LEAK_RE = [
    ("env:cwd", re.compile(r'"cwd"\s*:\s*"([^"]+)"'), "high"),
    ("env:gitBranch", re.compile(r'"gitBranch"\s*:\s*"([^"]+)"'), "medium"),
    ("env:commitSha", re.compile(r'"commitSha"\s*:\s*"([0-9a-f]{7,40})"'), "medium"),
    ("env:teammateIds", re.compile(r'"teammateIds"\s*:\s*\[[^\]]*\]'), "medium"),
    ("ip:v4", re.compile(r'\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b'), "medium"),
]
# Markers that strongly suggest proprietary / IP content survived (semantic IP is
# Claude's job; this is the deterministic floor that flags the obvious ones).
_IP_MARKER_RE = re.compile(
    r"(?i)\b(all rights reserved|proprietary and confidential|confidential|"
    r"internal use only|company confidential|trade secret|do not distribute|"
    r"may not be reproduced)\b")


def _scan_paths_text(text: str) -> list[Finding]:
    out = []
    for m in _PATH_RE.finditer(text):
        out.append(Finding("path", "path", "FS_PATH", m.group(0), m.start(), m.end(), 0.9, "medium"))
    return out


def _scan_env_leak_text(text: str) -> list[Finding]:
    out = []
    for name, rx, sev in _ENV_LEAK_RE:
        for m in rx.finditer(text):
            val = m.group(1) if m.groups() else m.group(0)
            # Skip private/loopback IPs being mentioned generically? Keep them — a
            # leaked internal IP is exactly what the gate should catch.
            out.append(Finding("env", "env_metadata", name, val, m.start(), m.end(), 0.85, sev))
    return out


def _scan_ip_markers(text: str) -> list[Finding]:
    out = []
    for m in _IP_MARKER_RE.finditer(text):
        out.append(Finding("ip-marker", "ip_marker", "PROPRIETARY_MARKER", m.group(0),
                           m.start(), m.end(), 0.6, "high", redactable=False))
    return out


def leak_scan(bundle_text: str, use_gliner: bool = True) -> list[Finding]:
    """Adversarially scan an OUTBOUND bundle for residual secrets / PII / paths /
    env-metadata / obvious-IP markers. This is the egress gate the confirmation
    step calls: a non-empty result means DO NOT SHIP — hand the findings to the
    co-author Claude to self-repair, then re-scan.

    Returns a deduped, severity-sorted list of Findings (empty == clean)."""
    findings: list[Finding] = []
    findings += scan_secrets(bundle_text)
    findings += scan_pii(bundle_text, use_gliner=use_gliner)
    findings += _scan_paths_text(bundle_text)
    findings += _scan_env_leak_text(bundle_text)
    findings += _scan_ip_markers(bundle_text)
    findings = _dedup_for_report(findings)
    findings.sort(key=lambda f: (-_SEV_RANK.get(f.severity, 1), f.start))
    return findings


def _dedup_for_report(findings: list[Finding]) -> list[Finding]:
    """Collapse identical spans across detectors, keeping the highest-severity /
    highest-score witness (but preserving distinct entities at the same location)."""
    best: dict[tuple, Finding] = {}
    for f in findings:
        key = (f.start, f.end, f.entity) if f.start >= 0 else (f.detector, f.entity, f.text)
        cur = best.get(key)
        if cur is None or (_SEV_RANK.get(f.severity, 1), f.score) > (_SEV_RANK.get(cur.severity, 1), cur.score):
            best[key] = f
    return list(best.values())


def summarize_findings(findings: Iterable[Finding]) -> dict:
    """Roll findings up for the confirmation gate / effort-signal block."""
    findings = list(findings)
    by_sev: dict[str, int] = {}
    by_cat: dict[str, int] = {}
    by_det: dict[str, int] = {}
    for f in findings:
        by_sev[f.severity] = by_sev.get(f.severity, 0) + 1
        by_cat[f.category] = by_cat.get(f.category, 0) + 1
        by_det[f.detector] = by_det.get(f.detector, 0) + 1
    return {
        "clean": len(findings) == 0,
        "total": len(findings),
        "by_severity": by_sev,
        "by_category": by_cat,
        "by_detector": by_det,
        "blocking": any(f.severity in ("high", "critical") for f in findings),
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _read_input(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    with open(arg) as fh:
        return fh.read()


def _load_records(path: str) -> list[dict]:
    recs = []
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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fb_assist.redact",
                                 description="fb-assist redaction toolbox (detection + redaction floor).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, helptext in [
        ("scan-secrets", "detect secrets in a text file (or - for stdin)"),
        ("scan-pii", "detect PII in a text file (or - for stdin)"),
        ("anonymize", "mask all PII in a text file (or - for stdin)"),
        ("tokenize", "reversibly tokenize secrets+PII in a text file"),
        ("leak-scan", "adversarial egress scan of an outbound bundle"),
    ]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("input", help="path to a text file, or - for stdin")
        p.add_argument("--reveal", action="store_true", help="show raw matched values (default: masked)")
        p.add_argument("--no-gliner", action="store_true", help="skip the GLiNER PII pass")

    sp = sub.add_parser("strip", help="strip whole categories from a transcript .jsonl")
    sp.add_argument("input", help="path to a transcript .jsonl")
    sp.add_argument("--categories", required=True,
                    help="comma-separated subset of: " + ",".join(CATEGORIES))
    sp.add_argument("--mode", default="replace", choices=["replace", "blank", "drop"])
    sp.add_argument("--out", help="write stripped jsonl here (default: stdout)")

    args = ap.parse_args(argv)

    if args.cmd in ("scan-secrets", "scan-pii", "anonymize", "tokenize", "leak-scan"):
        text = _read_input(args.input)
        no_gliner = getattr(args, "no_gliner", False)
        findings: list = []  # bound for every path that reaches the summary below
        if args.cmd == "scan-secrets":
            findings = scan_secrets(text)
        elif args.cmd == "scan-pii":
            findings = scan_pii(text, use_gliner=not no_gliner)
        elif args.cmd == "leak-scan":
            findings = leak_scan(text, use_gliner=not no_gliner)
        elif args.cmd == "anonymize":
            print(anonymize_pii(text, use_gliner=not no_gliner))
            return 0
        elif args.cmd == "tokenize":
            red, mapping = reversible_tokenize(text, use_gliner=not no_gliner)
            print(json.dumps({"text": red, "mapping": mapping}, indent=2, ensure_ascii=False))
            return 0
        out = {
            "summary": summarize_findings(findings),
            "findings": [f.to_dict(reveal=args.reveal) for f in findings],
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        # Exit non-zero on a blocking leak so shell callers can gate on it.
        return 1 if (args.cmd == "leak-scan" and out["summary"]["blocking"]) else 0

    if args.cmd == "strip":
        records = _load_records(args.input)
        cats = [c.strip() for c in args.categories.split(",") if c.strip()]
        stripped = strip_categories(records, cats, mode=args.mode)
        lines = "\n".join(json.dumps(r, ensure_ascii=False) for r in stripped)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(lines + "\n")
            print(f"wrote {len(stripped)} records -> {args.out}", file=sys.stderr)
        else:
            print(lines)
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
