"""fb_assist.claude_repro — the `claude-repro` API SDK surface.

Turns a developer's own Anthropic Messages API request/response into a
privacy-clean, request-id-anchored repro suitable for attaching to a bug or
feedback report — with no secrets, PII, or proprietary prompt content leaked.

Ships its own structural stripper (:func:`strip_blocks`) for the Messages-API
content-block shape, since that differs from the Claude Code JSONL envelope —
while reusing the detection/replacement core from :mod:`fb_assist.redact` and
the locator helpers from :mod:`fb_assist.transcripts`. Bulk categories
(images, documents, tool-result bodies) are stripped outright; narrative text
is masked char-precise, gated by a deterministic floor over the actual upload
bytes plus a soft NER scan that only yields self-repair candidates, never a
veto.
"""
from __future__ import annotations

import os

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")

import argparse
import copy
import hashlib
import json
import sys
import urllib.parse
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional

from . import redact as R
from . import transcripts as T
from .genericize import verify_genericization

# The optional semantic-genericize callback: a pure ``(text, context) -> text``
# rewrite the CALLER supplies — their own Claude/LLM — that does the SEMANTIC layer
# the deterministic floor cannot (codenames, "the patient in Room 11", proprietary
# context). It is handed text that has ALREADY passed the deterministic mask (so no
# raw secret ever reaches the caller's model), and whatever it returns is run through
# the two-pass ``verify_genericization`` gate before it is allowed to ship.
GenericizeCallback = Callable[[str, dict], str]

# --------------------------------------------------------------------------- #
# API sensitivity taxonomy (replaces the Claude-Code category vocabulary)
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = "system_prompt"
USER_TEXT = "user_text"
ASSISTANT_TEXT = "assistant_text"
THINKING = "thinking"
TOOL_USE_INPUT = "tool_use_input"
TOOL_RESULT = "tool_result"
TOOL_DEFINITIONS = "tool_definitions"
IMAGE_DATA = "image_data"
DOCUMENT_DATA = "document_data"

NARRATIVE_CATEGORIES = (SYSTEM_PROMPT, USER_TEXT, ASSISTANT_TEXT, THINKING, TOOL_USE_INPUT)
STRUCTURAL_CATEGORIES = (IMAGE_DATA, DOCUMENT_DATA, TOOL_RESULT, TOOL_DEFINITIONS)

# Default structural strip set for a *text* bug repro: drop blobs + verbatim tool
# output. tool_definitions is opt-in (revealing the tool surface is sometimes the
# point of the report), so it is NOT in the default set.
DEFAULT_STRIP = (IMAGE_DATA, DOCUMENT_DATA, TOOL_RESULT)

# Request kwargs we keep when recording a Messages API call (drop transport-only
# keys like extra_headers / timeout that carry no model content).
_REQUEST_FIELDS = (
    "model", "system", "messages", "tools", "tool_choice", "max_tokens",
    "temperature", "top_p", "top_k", "stop_sequences", "metadata", "thinking",
    "service_tier", "anthropic_version",
)


# --------------------------------------------------------------------------- #
# Small dict/object accessors (Messages may arrive as SDK objects OR plain dicts)
# --------------------------------------------------------------------------- #
def _get(obj: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a dict OR an attribute from an SDK object."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _to_plain(obj: Any) -> Any:
    """Best-effort convert an SDK object (pydantic) / nested structure to plain
    JSON-able dicts+lists. Never raises; falls back to a shallow copy."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    # pydantic v2 model
    for meth in ("model_dump", "to_dict", "dict"):
        fn = getattr(obj, meth, None)
        if callable(fn):
            try:
                return _to_plain(fn(mode="python") if meth == "model_dump" else fn())
            except TypeError:
                try:
                    return _to_plain(fn())
                except Exception:
                    pass
            except Exception:
                pass
    if isinstance(obj, Mapping):
        return {k: _to_plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_plain(v) for v in obj]
    return obj


def message_text(msg: Any) -> str:
    """Lenient ``Message.text`` over BOTH shapes:

      * an **array-of-blocks** ``content`` (the real Messages API shape) — joins
        every ``text`` (and ``thinking``) block;
      * a **bare ``text``** field (a flattened/partial export) — returns it.

    Accepts an SDK ``Message`` OR a plain dict. Never raises on a missing field.
    """
    content = _get(msg, "content", None)
    if content is None:
        bare = _get(msg, "text", None)
        return bare if isinstance(bare, str) else ""
    if isinstance(content, str):
        return content
    if not isinstance(content, (list, tuple)):
        # Unknown shape: try a bare text before giving up.
        bare = _get(msg, "text", None)
        return bare if isinstance(bare, str) else ""
    parts: list[str] = []
    for blk in content:
        bt = _get(blk, "type", None)
        if bt == "text" or (bt is None and _get(blk, "text", None) is not None):
            t = _get(blk, "text", None)
            if isinstance(t, str):
                parts.append(t)
        elif bt == "thinking":
            t = _get(blk, "thinking", None)
            if isinstance(t, str):
                parts.append(t)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# ReproPair — the one normalized form every ingest path produces
# --------------------------------------------------------------------------- #
@dataclass
class ReproPair:
    request: dict                      # Messages API request body (model/system/messages/tools/…)
    response: Optional[dict] = None    # Message response body (id/content/usage/stop_reason/…)
    request_id: Optional[str] = None   # req_…  — the verifiable anchor (from the response header)
    source: str = "raw"                # raw | otel | langfuse | helicone
    provider: str = "anthropic"        # anthropic | bedrock | vertex


def to_pair(request: Any, response: Any = None, request_id: Optional[str] = None,
            *, provider: str = "anthropic", source: str = "raw") -> ReproPair:
    """Normalize a request/response (SDK objects OR dicts) into a :class:`ReproPair`
    with deep-copied, JSON-able payloads — the caller's objects are never mutated."""
    req = _to_plain(request) or {}
    if not isinstance(req, dict):
        req = {"messages": req}
    resp = _to_plain(response) if response is not None else None
    # The request-id lives on the response object as the public ``_request_id``
    # attribute and is EXCLUDED from model_dump — read it before/around the dump.
    rid = request_id or extract_request_id(response, provider=provider)
    return ReproPair(request=copy.deepcopy(req),
                     response=copy.deepcopy(resp) if isinstance(resp, dict) else resp,
                     request_id=rid, source=source, provider=provider)


# --------------------------------------------------------------------------- #
# The verifiable anchor: request-id extraction + deterministic fallback
# --------------------------------------------------------------------------- #
def extract_request_id(obj: Any, *, provider: str = "anthropic") -> Optional[str]:
    """Pull the Anthropic ``request-id`` (``req_…``) from a response/stream.

    Robust across known shapes (verified against ``anthropic`` SDK 0.76.0):
      * ``messages.create(...)`` -> ``Message`` exposing the public ``_request_id``
        attribute (absent from ``Message.model_fields``; set by ``add_request_id``);
      * ``stream.get_final_message()`` — the accumulated snapshot may LACK
        ``_request_id``; the reliable source is ``stream.request_id`` (the header).
        Pass the *stream* object here and it reads ``.request_id``;
      * ``messages.parse(...)`` — not present on 0.76.0; when present it routes
        through the same ``add_request_id`` path, so ``_request_id`` covers it;
      * a plain ``dict`` carrying ``_request_id`` / ``request_id``.

    Returns ``None`` when no Anthropic request-id is available (e.g. a Bedrock /
    Vertex response whose header is absent — :func:`anchor_for` then falls back).
    """
    if obj is None:
        return None
    rid = getattr(obj, "_request_id", None)
    if isinstance(rid, str) and rid:
        return rid
    # Stream objects expose `.request_id` (the response header).
    rid = getattr(obj, "request_id", None)
    if isinstance(rid, str) and rid:
        return rid
    if isinstance(obj, Mapping):
        for k in ("_request_id", "request_id"):
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
    return None


def anchor_for(request: Optional[dict], response: Optional[dict],
               request_id: Optional[str], provider: str = "anthropic") -> dict:
    """Build the verifiable anchor that ties a report to a REAL metered call.

    First-party Anthropic + a ``req_…`` id -> a **verifiable** request-id anchor
    (Anthropic can correlate it to its own 7-day server-side log with zero extra
    user content). Otherwise (Bedrock/Vertex, or a missing/non-``req_`` id) -> a
    **deterministic** fallback ``{provider, provider_id, model, usage,
    fingerprint}`` that is NOT first-party-verifiable (flagged ``verifiable:
    False``) but still uniquely identifies the interaction."""
    response = response or {}
    request = request or {}
    is_anthropic_rid = bool(request_id and str(request_id).startswith("req_"))
    if provider == "anthropic" and is_anthropic_rid:
        return {
            "type": "request_id",
            "request_id": request_id,
            "provider": provider,
            "verifiable": True,
        }
    provider_id = response.get("id") if isinstance(response, dict) else None
    usage = response.get("usage") if isinstance(response, dict) else None
    model = (response.get("model") if isinstance(response, dict) else None) or request.get("model")
    fp_src = json.dumps({"provider_id": provider_id, "model": model, "usage": usage},
                        sort_keys=True, ensure_ascii=False)
    fingerprint = hashlib.sha256(fp_src.encode("utf-8")).hexdigest()[:16]
    return {
        "type": "deterministic",
        "provider": provider,
        "provider_id": provider_id,
        "model": model,
        "usage": usage,
        "fingerprint": fingerprint,
        "request_id": request_id,   # may be a non-Anthropic id, or None
        "verifiable": False,
    }


# --------------------------------------------------------------------------- #
# strip_blocks — the NEW structural stripper over the Messages-API union
# --------------------------------------------------------------------------- #
def _mark(category: str, n: Optional[int] = None, extra: str = "") -> str:
    inner = category + (f" {extra}" if extra else "") + (f": {n} chars" if n is not None else "")
    return f"‹{inner} stripped›"


def _strip_image_or_document(blk: dict, kind: str, events: list, where: str) -> None:
    src = blk.get("source")
    if not isinstance(src, dict):
        return
    stype = src.get("type")
    if stype == "base64" and isinstance(src.get("data"), str):
        media = src.get("media_type", "")
        n = len(src["data"])
        src["data"] = _mark(kind, n, extra=media)
        events.append({"location": where, "api_category": kind, "method": "strip",
                       "entity": None, "replacement": src["data"], "count": 1, "bytes": n})
    elif stype == "url" and isinstance(src.get("url"), str):
        # A URL can leak a private/internal endpoint -> blank it too.
        n = len(src["url"])
        src["url"] = _mark(kind, extra="url")
        events.append({"location": where, "api_category": kind, "method": "strip",
                       "entity": None, "replacement": src["url"], "count": 1, "bytes": n})


def _strip_tool_result(blk: dict, events: list, where: str) -> None:
    content = blk.get("content")
    if isinstance(content, str):
        n = len(content)
        blk["content"] = _mark(TOOL_RESULT, n)
        events.append({"location": where, "api_category": TOOL_RESULT, "method": "strip",
                       "entity": None, "replacement": blk["content"], "count": 1, "bytes": n})
    elif isinstance(content, list):
        total = 0
        for sub in content:
            if isinstance(sub, dict):
                if isinstance(sub.get("text"), str):
                    total += len(sub["text"])
                    sub["text"] = _mark(TOOL_RESULT, len(sub["text"]))
                elif sub.get("type") in ("image", "document"):
                    _strip_image_or_document(sub, IMAGE_DATA if sub.get("type") == "image" else DOCUMENT_DATA,
                                             events, where)
        events.append({"location": where, "api_category": TOOL_RESULT, "method": "strip",
                       "entity": None, "replacement": _mark(TOOL_RESULT, total), "count": 1,
                       "bytes": total})


def _strip_tool_definition(tool: dict, events: list, where: str) -> None:
    # Keep the NAME (signal); scrub description + schema (the surface reveal).
    if isinstance(tool.get("description"), str) and tool["description"]:
        n = len(tool["description"])
        tool["description"] = _mark(TOOL_DEFINITIONS, n, extra="description")
        events.append({"location": where + ".description", "api_category": TOOL_DEFINITIONS,
                       "method": "strip", "entity": None, "replacement": tool["description"],
                       "count": 1, "bytes": n})
    if isinstance(tool.get("input_schema"), dict) and tool["input_schema"]:
        n = len(json.dumps(tool["input_schema"], ensure_ascii=False))
        tool["input_schema"] = {"__stripped__": _mark(TOOL_DEFINITIONS, extra="input_schema")}
        events.append({"location": where + ".input_schema", "api_category": TOOL_DEFINITIONS,
                       "method": "strip", "entity": None,
                       "replacement": tool["input_schema"]["__stripped__"], "count": 1, "bytes": n})


def _iter_content_blocks(pair: ReproPair) -> Iterator[tuple[str, dict]]:
    """Yield (human-location, block-dict) for every content block in request
    messages and the response (in-place dicts, so callers can mutate)."""
    req = pair.request or {}
    msgs = req.get("messages")
    if isinstance(msgs, list):
        for i, msg in enumerate(msgs):
            content = msg.get("content") if isinstance(msg, dict) else None
            if isinstance(content, list):
                for j, blk in enumerate(content):
                    if isinstance(blk, dict):
                        yield (f"request.messages[{i}].content[{j}]", blk)
    resp = pair.response or {}
    rc = resp.get("content") if isinstance(resp, dict) else None
    if isinstance(rc, list):
        for j, blk in enumerate(rc):
            if isinstance(blk, dict):
                yield (f"response.content[{j}]", blk)


def strip_blocks(pair: ReproPair, categories: Iterable[str] = DEFAULT_STRIP,
                 mode: str = "replace") -> list[dict]:
    """Drop/blank whole STRUCTURAL categories from a :class:`ReproPair`, IN PLACE.

    Walks the Messages-API union (NOT the Claude Code envelope):
      * ``image_data`` / ``document_data`` -> replace ``source.data`` base64 blob
        (or a ``source.url``) with a dimensional marker — a blob can't be redacted
        in place, and a *text* repro almost never needs the pixels;
      * ``tool_result`` -> replace the block ``content`` (the verbatim tool output —
        the worst leak surface) with a ``N chars`` marker;
      * ``tool_definitions`` -> opt-in; scrub each tool's ``description`` /
        ``input_schema`` (keeping the name).

    Narrative categories (system / user / assistant / thinking / tool_use input)
    are intentionally left for :func:`narrative_spans` + the char-precise mask.
    Returns the list of strip *events* (used to build the redaction-map preview)."""
    cats = set(categories)
    events: list[dict] = []

    for where, blk in _iter_content_blocks(pair):
        bt = blk.get("type")
        if bt == "image" and IMAGE_DATA in cats:
            _strip_image_or_document(blk, IMAGE_DATA, events, where)
        elif bt == "document" and DOCUMENT_DATA in cats:
            _strip_image_or_document(blk, DOCUMENT_DATA, events, where)
        elif bt == "tool_result" and TOOL_RESULT in cats:
            _strip_tool_result(blk, events, where)

    if TOOL_DEFINITIONS in cats:
        tools = (pair.request or {}).get("tools")
        if isinstance(tools, list):
            for i, tool in enumerate(tools):
                if isinstance(tool, dict):
                    _strip_tool_definition(tool, events, f"request.tools[{i}]")

    return events


# --------------------------------------------------------------------------- #
# narrative_spans — locate the char-precise maskable text across the pair
# --------------------------------------------------------------------------- #
def _string_leaves(obj: Any, base: tuple) -> Iterator[tuple[tuple, str]]:
    """Yield (path-tuple, string) for every string leaf under ``obj``."""
    if isinstance(obj, str):
        yield (base, obj)
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            yield from _string_leaves(v, base + (k,))
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _string_leaves(v, base + (i,))


def _span(category: str, path: tuple, text: str) -> T.Span:
    """Construct a transcripts.Span covering a WHOLE field (start=0..len) so the
    locator<->rmap bridge (`replace_span`) works unchanged."""
    return T.Span(category=category, line=0, uuid=None, field=_field_str(path),
                  path=path, start=0, end=len(text), text=text)


def _field_str(path: tuple) -> str:
    out = []
    for k in path:
        out.append(f"[{k}]" if isinstance(k, int) else (("." + k) if out else k))
    return "".join(out)


def narrative_spans(pair: ReproPair, stripped: Iterable[str] = ()) -> list[T.Span]:
    """Locate every char-precise-maskable narrative region across the pair.

    Categories: system prompt, user/assistant text (request prior turns + the
    response), thinking, and the string leaves of ``tool_use`` inputs. ``stripped``
    is the structural strip set — a ``tool_result`` already stripped by
    :func:`strip_blocks` is skipped here (its content is a marker)."""
    stripped = set(stripped)
    spans: list[T.Span] = []
    req = pair.request or {}

    # --- system prompt -----------------------------------------------------
    system = req.get("system")
    if isinstance(system, str) and system:
        spans.append(_span(SYSTEM_PROMPT, ("request", "system"), system))
    elif isinstance(system, list):
        for i, blk in enumerate(system):
            if isinstance(blk, dict) and isinstance(blk.get("text"), str):
                spans.append(_span(SYSTEM_PROMPT, ("request", "system", i, "text"), blk["text"]))

    # --- request messages --------------------------------------------------
    msgs = req.get("messages")
    if isinstance(msgs, list):
        for i, msg in enumerate(msgs):
            if not isinstance(msg, dict):
                continue
            role = msg.get("role")
            txt_cat = USER_TEXT if role == "user" else ASSISTANT_TEXT
            content = msg.get("content")
            if isinstance(content, str):
                if content:
                    spans.append(_span(txt_cat, ("request", "messages", i, "content"), content))
            elif isinstance(content, list):
                for j, blk in enumerate(content):
                    if not isinstance(blk, dict):
                        continue
                    bt = blk.get("type")
                    base = ("request", "messages", i, "content", j)
                    if bt == "text" and isinstance(blk.get("text"), str) and blk["text"]:
                        spans.append(_span(txt_cat, base + ("text",), blk["text"]))
                    elif bt == "thinking" and isinstance(blk.get("thinking"), str) and blk["thinking"]:
                        spans.append(_span(THINKING, base + ("thinking",), blk["thinking"]))
                    elif bt == "tool_use" and isinstance(blk.get("input"), (dict, list)):
                        for leaf_path, leaf_text in _string_leaves(blk["input"], base + ("input",)):
                            if leaf_text:
                                spans.append(_span(TOOL_USE_INPUT, leaf_path, leaf_text))
                    elif bt == "tool_result" and TOOL_RESULT not in stripped:
                        c = blk.get("content")
                        if isinstance(c, str) and c:
                            spans.append(_span(TOOL_RESULT, base + ("content",), c))
                        elif isinstance(c, list):
                            for k, sub in enumerate(c):
                                if isinstance(sub, dict) and isinstance(sub.get("text"), str) and sub["text"]:
                                    spans.append(_span(TOOL_RESULT, base + ("content", k, "text"), sub["text"]))

    # --- response content --------------------------------------------------
    resp = pair.response or {}
    rc = resp.get("content") if isinstance(resp, dict) else None
    if isinstance(rc, list):
        for j, blk in enumerate(rc):
            if not isinstance(blk, dict):
                continue
            bt = blk.get("type")
            base = ("response", "content", j)
            if bt == "text" and isinstance(blk.get("text"), str) and blk["text"]:
                spans.append(_span(ASSISTANT_TEXT, base + ("text",), blk["text"]))
            elif bt == "thinking" and isinstance(blk.get("thinking"), str) and blk["thinking"]:
                spans.append(_span(THINKING, base + ("thinking",), blk["thinking"]))

    return spans


# --------------------------------------------------------------------------- #
# The redaction pipeline (reuse redact.py + the locator bridge)
# --------------------------------------------------------------------------- #
def _scan_narrative(text: str, use_gliner: bool = True) -> list[R.Finding]:
    """The per-span detector floor: secrets + PII + filesystem paths. Paths are
    NOT secrets/PII, so we add ``redact``'s path detector explicitly so a system
    prompt that pastes ``/home/user/...`` gets masked to ``‹FS_PATH›``."""
    findings = R.scan_secrets(text)
    findings += R.scan_pii(text, use_gliner=use_gliner)
    findings += R._scan_paths_text(text)
    return findings


def _enforce_secret_floor(root: dict, rmap: list, max_iter: int = 4) -> int:
    """Belt-and-suspenders: guarantee the deterministic floor over the ACTUAL upload
    bytes is empty — both SECRETS *and* the PII regex floor (email / IPv4 / US SSN).
    The targeted narrative pass already masks the common case with nice entity labels;
    this sweep literal-removes any secret OR floor-PII value that survived in a
    non-narrative field (metadata, a kept tool-result leaf), so the hard gate can never
    fail with one of those sitting in the bytes. Returns the number removed."""
    removed = 0
    for _ in range(max_iter):
        upload = json.dumps(root, ensure_ascii=False)
        findings = R.scan_secrets(upload) + R._scan_pii_regex(upload)
        # Keep the highest-fidelity label per surviving value for attribution.
        val_label: dict = {}
        for f in findings:
            if f.text:
                val_label.setdefault(f.text, R._token_label(f.entity))
        if not val_label:
            break

        def _scrub(s: str) -> str:
            nonlocal removed
            hit = [v for v in val_label if v and v in s]
            if not hit:
                return s
            new = s
            for v in hit:
                label = val_label.get(v) or "REDACTED"
                new = new.replace(v, f"‹{label}›")
                removed += 1
                rmap.append({"location": "(residual)", "api_category": "residual_floor",
                             "method": "mask", "entity": label, "original": v,
                             "replacement": f"‹{label}›", "count": 1})
            return new

        _walk_mutate_strings(root, _scrub)
    return removed


def _walk_mutate_strings(obj: Any, fn) -> None:
    if isinstance(obj, dict):
        for k, v in list(obj.items()):
            if isinstance(v, str):
                obj[k] = fn(v)
            else:
                _walk_mutate_strings(v, fn)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            if isinstance(v, str):
                obj[i] = fn(v)
            else:
                _walk_mutate_strings(v, fn)


@dataclass
class Artifact:
    """The forward-transform output (the substrate a dev forwards to Anthropic)."""
    redacted_repro: dict              # {"request": …, "response": …} post-redaction
    request_id: Optional[str]         # req_…  (the anchor, when first-party)
    anchor: dict                      # request_id OR deterministic fallback
    provider: str
    structured_description: str
    effort_signal: dict
    redaction_map: list               # unified mask + strip events
    preview: "ReproPreview"           # built from redaction_map, not diff_preview
    hard_gate_pass: bool              # deterministic secret + PII-regex floor over upload == []
    leak_candidates: list = field(default_factory=list)  # soft NER self-repair candidates

    def to_dict(self) -> dict:
        d = asdict(self)
        d["preview"] = self.preview.to_dict()
        return d

    def upload_text(self) -> str:
        return json.dumps(self.redacted_repro, ensure_ascii=False)


def redact_pair(request: Any, response: Any = None, request_id: Optional[str] = None,
                *, provider: str = "anthropic", source: str = "raw",
                strip: Iterable[str] = DEFAULT_STRIP, include_tool_defs: bool = False,
                use_gliner: bool = True, description: str = "",
                recipe: str = "surgical", quality: Optional[int] = None,
                alignment_confidence: Optional[int] = None,
                reputation_token: Optional[str] = None,
                genericize: Optional[GenericizeCallback] = None) -> Artifact:
    """PURE forward transform: request+response -> sanitized copy + :class:`Artifact`.

    Never mutates the caller's objects (everything is deep-copied via
    :func:`to_pair`). Pipeline (retargeted to the Messages API):
      1) STRUCTURAL strip (bulk) via :func:`strip_blocks`;
      2) NARRATIVE mask (char-precise) via the locator<->rmap bridge;
      2b) OPTIONAL SEMANTIC GENERICIZE — if ``genericize`` is given, the
         caller's own Claude/LLM rewrites each already-floored narrative span and the
         two-pass :func:`fb_assist.genericize.verify_genericization` gate runs over its
         output; any surviving leak fails CLOSED to the deterministic mask;
      3) HARD GATE — deterministic secret floor over the ACTUAL upload bytes
         (enforced empty by :func:`_enforce_secret_floor`);
      4) SOFT GATE — NER :func:`redact.leak_scan` over the rendered narrative ->
         self-repair candidates (never a veto).

    ``genericize`` is the no-live-Claude seam: on the API surface there is no Opus
    already holding the session, so the deterministic floor is the only guaranteed
    layer. A caller who DOES have a model can pass ``genericize=(text, ctx) -> text``
    to add the semantic layer; it is handed the deterministically-masked text (never
    a raw secret) plus a ``ctx`` of ``{category, location, provider, entities}``, and
    its rewrite only ships if ``verify_genericization`` proves nothing recoverable
    survived. This mirrors :func:`fb_assist.server_side.genericize_for_attach`'s
    ``genericize`` seam so both no-live-Claude surfaces behave identically. See
    :func:`make_ollama_genericizer` for a zero-dependency local fallback callback."""
    pair = to_pair(request, response, request_id, provider=provider, source=source)

    strip_set = set(strip)
    if include_tool_defs:
        strip_set.add(TOOL_DEFINITIONS)

    rmap: list[dict] = []
    # (1) structural strip
    strip_events = strip_blocks(pair, strip_set)
    rmap.extend(strip_events)

    # (2) narrative mask via the bridge. Operate on a COMBINED root so a single
    #     replace_span call can reach into either the request or the response.
    root = {"request": pair.request, "response": pair.response}
    rendered_parts: list[str] = []
    gen_stats: Optional[dict] = (
        {"used": True, "applied": 0, "rejected": 0, "spans": [], "meaning_risk_flags": []}
        if genericize is not None else None
    )
    for sp in narrative_spans(pair, stripped=strip_set):
        rendered_parts.append(sp.text)
        before = sp.text
        findings = _scan_narrative(before, use_gliner=use_gliner)
        chosen = R.merge_redaction_spans(findings)
        masked = before
        if chosen:
            masked, _ = R.apply_redactions(before, findings, style="mask")
            for f in chosen:
                rmap.append({
                    "location": sp.field, "api_category": sp.category, "method": "mask",
                    "entity": f.entity, "original": f.masked,
                    "replacement": f"‹{R._token_label(f.entity)}›", "count": 1,
                })
        # (2b) OPTIONAL semantic genericize: the caller's own Claude/LLM rewrites
        #      the already-floored text; the two-pass verify_genericization gate runs
        #      over its output and we fail CLOSED to the deterministic mask on any leak.
        final_text = masked
        if genericize is not None and masked.strip():
            final_text = _apply_genericize_callback(
                genericize, before, masked, sp, pair.provider, chosen, use_gliner, gen_stats)
        if final_text != before:
            T.replace_span(root, sp, final_text)
    pair.request, pair.response = root["request"], root["response"]

    # (3) HARD GATE — deterministic secret + PII-regex floor over the real upload bytes
    #     (an email/IP/SSN in a non-narrative field must fail the gate too, not just
    #     secrets — matching every other surface's floor).
    _enforce_secret_floor(root, rmap)
    pair.request, pair.response = root["request"], root["response"]
    upload_text = json.dumps({"request": pair.request, "response": pair.response}, ensure_ascii=False)
    residual_floor = R.scan_secrets(upload_text) + R._scan_pii_regex(upload_text)
    hard_gate_pass = (residual_floor == [])

    # (4) SOFT GATE — NER leak_scan over the rendered narrative (candidates only).
    leak_candidates = [f.to_dict() for f in R.leak_scan("\n".join(rendered_parts), use_gliner=use_gliner)]

    anchor = anchor_for(pair.request, pair.response, pair.request_id, provider)
    preview = preview_from_rmap(rmap, pair)
    effort = build_effort_signal(pair, rmap, recipe=recipe, quality=quality,
                                 alignment_confidence=alignment_confidence,
                                 reputation_token=reputation_token, anchor=anchor,
                                 genericize_stats=gen_stats)

    return Artifact(
        redacted_repro={"request": pair.request, "response": pair.response},
        request_id=pair.request_id if anchor.get("type") == "request_id" else None,
        anchor=anchor, provider=provider,
        structured_description=description.strip(),
        effort_signal=effort, redaction_map=rmap, preview=preview,
        hard_gate_pass=hard_gate_pass, leak_candidates=leak_candidates,
    )


# --------------------------------------------------------------------------- #
# The pluggable semantic-genericize callback (the no-live-Claude seam)
# --------------------------------------------------------------------------- #
def _apply_genericize_callback(callback: GenericizeCallback, before: str, masked: str,
                               sp: T.Span, provider: str, chosen: list,
                               use_gliner: bool, stats: Optional[dict]) -> str:
    """Run ONE narrative span through the caller's semantic-genericize callback and
    the two-pass ``verify_genericization`` gate. FAIL-CLOSED: returns the caller's
    rewrite ONLY when the gate proves nothing recoverable survived; otherwise returns
    the deterministic ``masked`` text unchanged.

    The callback is handed ``masked`` (post-deterministic-floor — a raw secret never
    reaches the caller's model) plus a context dict; the gate is run against the TRUE
    ``before`` so a rewrite that re-introduced any original secret/PII would be caught.
    Only leak-free, non-revealing signals are recorded in ``stats`` (never a raw value).
    """
    ctx = {
        "category": sp.category,
        "location": sp.field,
        "provider": provider,
        "entities": sorted({getattr(f, "entity", None) for f in chosen} - {None}),
    }
    try:
        generic = callback(masked, ctx)
    except Exception as exc:  # a misbehaving callback must never break the transform
        if stats is not None:
            stats["rejected"] += 1
            stats["spans"].append({"location": sp.field, "category": sp.category,
                                   "accepted": False, "reason": f"callback_error:{type(exc).__name__}"})
        return masked

    if not isinstance(generic, str) or generic == masked:
        # A no-op (or non-string) rewrite: nothing to gate, keep the deterministic mask.
        if stats is not None and isinstance(generic, str):
            stats["spans"].append({"location": sp.field, "category": sp.category,
                                   "accepted": False, "reason": "noop"})
        elif stats is not None:
            stats["rejected"] += 1
            stats["spans"].append({"location": sp.field, "category": sp.category,
                                   "accepted": False, "reason": "non_str"})
        return masked

    vg = verify_genericization(before, generic, use_gliner=use_gliner)
    if vg.get("ok"):
        if stats is not None:
            stats["applied"] += 1
            stats["spans"].append({"location": sp.field, "category": sp.category,
                                   "accepted": True, "reason": "ok"})
            for fl in vg.get("meaning_risk_flags", []):
                stats["meaning_risk_flags"].append({"location": sp.field, **fl})
        return generic

    # Gate failed -> a leak survived the rewrite. Fail closed to the deterministic mask
    # and record only leak-free verdict signals (counts + masked re-id findings).
    if stats is not None:
        stats["rejected"] += 1
        stats["spans"].append({
            "location": sp.field, "category": sp.category, "accepted": False,
            "reason": "verify_failed",
            "leaked_originals": len(vg.get("leaked_originals", [])),
            "expect_absent_hits": len(vg.get("expect_absent_hits", [])),
            "reid_findings": vg.get("reid_findings", []),  # reveal=False — masked, safe
        })
    return masked


# A tight, model-agnostic instruction for the local-fallback genericizer. The
# deterministic floor has ALREADY run, so the model's only job is the semantic layer.
_OLLAMA_GENERICIZE_SYSTEM = (
    "You rewrite text to remove proprietary or identifying SEMANTIC content while "
    "preserving the technical meaning verbatim. Replace internal codenames, product "
    "names, customer/person names, specific places, and any business-identifying "
    "specifics with neutral generic stand-ins (e.g. 'a service', 'a user', 'an internal "
    "tool'). KEEP all error messages, codes, identifiers-in-quotes, stack traces, and "
    "the exact technical problem intact. Markers like ‹EMAIL_ADDRESS› are already-"
    "redacted placeholders — leave them exactly as-is. Output ONLY the rewritten text, "
    "no preamble."
)


def make_ollama_genericizer(*, model: str = "llama3.2", host: str = "http://127.0.0.1:11434",
                            timeout: float = 30.0,
                            system: str = _OLLAMA_GENERICIZE_SYSTEM) -> GenericizeCallback:
    """Build an OPTIONAL :data:`GenericizeCallback` backed by a LOCAL Ollama daemon.

    This is a documented, zero-extra-dependency fallback for callers who want the
    semantic layer but have no Claude wired in: it talks to a local Ollama over its
    HTTP API (preferring the ``ollama`` Python package when installed, else stdlib
    ``urllib``). It is **never required and never invoked by the test suite** — the
    returned callback only contacts ``localhost`` when actually called by
    :func:`redact_pair`, and if Ollama is unreachable the callback raises, at which
    point ``redact_pair`` fails CLOSED to the deterministic mask. Nothing leaves the
    machine: Ollama runs entirely locally.

    Returns a ``(text, ctx) -> text`` callback ready to pass as ``genericize=``."""
    def _genericize(text: str, ctx: dict) -> str:
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": text}]
        # Prefer the official client if it is installed (import-availability gate).
        try:
            import ollama  # type: ignore
        except Exception:
            ollama = None  # type: ignore
        if ollama is not None:
            resp = ollama.chat(model=model, messages=messages,
                               options={"temperature": 0})
            return str((resp.get("message") or {}).get("content", "")).strip() or text
        # stdlib fallback — POST to the local Ollama REST endpoint (no third-party dep).
        import urllib.request
        body = json.dumps({"model": model, "messages": messages, "stream": False,
                           "options": {"temperature": 0}}).encode("utf-8")
        req = urllib.request.Request(host.rstrip("/") + "/api/chat", data=body,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 (localhost)
            data = json.loads(r.read().decode("utf-8"))
        return str((data.get("message") or {}).get("content", "")).strip() or text

    return _genericize


# --------------------------------------------------------------------------- #
# The included/stripped PREVIEW built from the redaction_map directly
# --------------------------------------------------------------------------- #
@dataclass
class ReproPreview:
    """A thin, API-SHAPED included/stripped summary built from the redaction map.

    ``package.diff_preview([request, response])`` yields meaningless structural
    counts ("1 record modified") on a 2-element pair, so the API preview is
    summarized from the ``redaction_map`` instead — per-category and per-entity
    counts of what was masked vs structurally stripped."""
    masked_total: int
    stripped_total: int
    by_category: dict
    by_method: dict
    by_entity: dict
    samples: list  # list of (api_category, "location: replacement")

    def to_dict(self) -> dict:
        return asdict(self)

    def render(self, max_samples: int = 6) -> str:
        lines = ["claude-repro — what will be sent:"]
        cat = ", ".join(f"{n}×{c}" for c, n in sorted(self.by_category.items(), key=lambda kv: -kv[1]))
        lines.append(f"  MASKED   : {self.masked_total} narrative span(s)"
                     + (f"   [{', '.join(f'{n}×{e}' for e, n in sorted(self.by_entity.items(), key=lambda kv: -kv[1]))}]"
                        if self.by_entity else ""))
        lines.append(f"  STRIPPED : {self.stripped_total} structural block(s)")
        if cat:
            lines.append(f"  by category: {cat}")
        if self.samples:
            lines.append(f"  e.g. (showing {min(len(self.samples), max_samples)} of {len(self.samples)}):")
            for c, s in self.samples[:max_samples]:
                lines.append(f"      [{c}] {s}")
        return "\n".join(lines)


def preview_from_rmap(rmap: Iterable[Mapping[str, Any]], pair: Optional[ReproPair] = None) -> ReproPreview:
    """Build the included/stripped preview straight from the redaction map."""
    by_category: dict[str, int] = {}
    by_method: dict[str, int] = {}
    by_entity: dict[str, int] = {}
    masked_total = stripped_total = 0
    samples: list[tuple[str, str]] = []
    for e in rmap:
        cat = str(e.get("api_category", "redacted"))
        method = str(e.get("method", "mask"))
        cnt = int(e.get("count", 1))
        by_category[cat] = by_category.get(cat, 0) + cnt
        by_method[method] = by_method.get(method, 0) + cnt
        if method == "strip":
            stripped_total += cnt
        else:
            masked_total += cnt
            ent = e.get("entity")
            if ent:
                by_entity[str(ent)] = by_entity.get(str(ent), 0) + cnt
        loc = str(e.get("location", "?"))
        rep = str(e.get("replacement", ""))
        if len(samples) < 12:
            samples.append((cat, f"{loc}: {rep[:48]}"))
    # de-dup samples, preserve order
    seen, uniq = set(), []
    for s in samples:
        if s not in seen:
            seen.add(s)
            uniq.append(s)
    return ReproPreview(masked_total=masked_total, stripped_total=stripped_total,
                        by_category=by_category, by_method=by_method, by_entity=by_entity,
                        samples=uniq)


# --------------------------------------------------------------------------- #
# Effort signal (cross-surface schema — the "one platform" proof)
# --------------------------------------------------------------------------- #
def build_effort_signal(pair: ReproPair, rmap: Iterable[Mapping[str, Any]], *,
                        recipe: str = "surgical", quality: Optional[int] = None,
                        alignment_confidence: Optional[int] = None,
                        reputation_token: Optional[str] = None,
                        anchor: Optional[dict] = None, overrides: int = 0,
                        genericize_stats: Optional[dict] = None) -> dict:
    """The API-side effort signal — SAME shape as every other fb-assist surface,
    with the request-id anchor swapped in for the CC swap-restore mechanism."""
    req = pair.request or {}
    msgs = req.get("messages") if isinstance(req.get("messages"), list) else []
    tools = req.get("tools")
    by_category: dict[str, int] = {}
    for e in rmap:
        c = str(e.get("api_category", "redacted"))
        by_category[c] = by_category.get(c, 0) + int(e.get("count", 1))
    thinking_included = any(
        isinstance(m, dict) and isinstance(m.get("content"), list)
        and any(isinstance(b, dict) and b.get("type") == "thinking" for b in m["content"])
        for m in msgs
    ) or bool(isinstance((pair.response or {}).get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "thinking" for b in (pair.response or {}).get("content", [])))
    signal = {
        "surface": "api",
        "request_id": pair.request_id if (anchor or {}).get("type") == "request_id" else None,
        "anchor": anchor or anchor_for(pair.request, pair.response, pair.request_id, pair.provider),
        "provider": pair.provider,
        "repro_completeness": {
            "has_request": bool(req),
            "has_response": pair.response is not None,
            "turns": len(msgs),
            "tools_included": bool(tools),
            "thinking_included": bool(thinking_included),
        },
        "redaction": recipe,
        "redaction_decisions": {
            "by_category": by_category,
            "method": "mask",
            "overrides": overrides,
        },
        "self_rating": {
            "quality": quality,
            "alignment_confidence": alignment_confidence,
        },
        "reputation_token": reputation_token,
    }
    # Record the semantic-genericize pass (categories/counts/verdicts only; never
    # a raw value), present only when a genericize callback was actually supplied.
    if genericize_stats is not None:
        signal["semantic_genericize"] = genericize_stats
    return signal


# --------------------------------------------------------------------------- #
# Draft builder — the support@anthropic.com delivery (never auto-sends)
# --------------------------------------------------------------------------- #
SUPPORT_EMAIL = "support@anthropic.com"


@dataclass
class Draft:
    to: str
    subject: str
    body: str
    mailto_url: str
    files: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _render_body(artifact: Artifact) -> str:
    a = artifact
    anchor_line = (f"request_id: {a.request_id}" if a.request_id
                   else f"anchor: deterministic fingerprint {a.anchor.get('fingerprint')} "
                        f"(provider={a.provider}, not first-party-verifiable)")
    eff = a.effort_signal
    parts = [
        a.structured_description or "(no description provided)",
        "",
        "— repro anchor —",
        anchor_line,
        "",
        "— redaction summary —",
        a.preview.render(),
        "",
        "— effort signal —",
        "```json",
        json.dumps(eff, indent=2, ensure_ascii=False),
        "```",
        "",
        "— redacted repro (privacy-scrubbed; secrets/PII/IP removed locally) —",
        "```json",
        json.dumps(a.redacted_repro, indent=2, ensure_ascii=False),
        "```",
    ]
    return "\n".join(parts)


def _copy_clipboard(text: str) -> bool:
    """Try pyperclip; fall back to an OSC-52 escape for headless/SSH. Best-effort."""
    try:
        import pyperclip  # type: ignore
        pyperclip.copy(text)
        return True
    except Exception:
        pass
    try:
        import base64
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\033]52;c;{b64}\a")
        sys.stdout.flush()
        return True
    except Exception:
        return False


def build_draft(artifact: Artifact, *, to: str = SUPPORT_EMAIL, deliver: str = "return",
                out_dir: str = ".") -> Draft:
    """Build a ready-to-send feedback/bug draft embedding the redacted repro, the
    request-id anchor, and the effort/redaction summary. NEVER auto-sends — the dev
    reviews and hits send (mirrors fb-assist's confirmation gate + anti-impersonation).

    ``deliver``: ``return`` (just build) | ``file`` (write .md + .json) |
    ``clipboard`` (pyperclip/OSC-52) | ``mailto`` (build the URL only)."""
    anchor_id = artifact.request_id or artifact.anchor.get("fingerprint") or "no-request-id"
    subject = f"Feedback on completion {anchor_id}"
    body = _render_body(artifact)
    mailto = "mailto:" + urllib.parse.quote(to) + "?" + urllib.parse.urlencode(
        {"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    draft = Draft(to=to, subject=subject, body=body, mailto_url=mailto, files=[])

    if deliver == "file":
        base = os.path.join(out_dir, f"claude-repro-{_slug(anchor_id)}")
        with open(base + ".md", "w", encoding="utf-8") as fh:
            fh.write(f"To: {to}\nSubject: {subject}\n\n{body}\n")
        with open(base + ".json", "w", encoding="utf-8") as fh:
            json.dump({"to": to, "subject": subject, "artifact": artifact.to_dict()},
                      fh, indent=2, ensure_ascii=False)
        draft.files = [base + ".md", base + ".json"]
    elif deliver == "clipboard":
        _copy_clipboard(f"To: {to}\nSubject: {subject}\n\n{body}")
    # "mailto" / "return": URL is already on the draft; nothing is ever sent.
    return draft


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:64] or "repro"


def build_and_deliver(request: Any, response: Any, request_id: Optional[str],
                      description: str, *, provider: str = "anthropic",
                      recipe: str = "surgical", quality: Optional[int] = None,
                      alignment_confidence: Optional[int] = None,
                      deliver: str = "return", out_dir: str = ".",
                      strip: Iterable[str] = DEFAULT_STRIP,
                      include_tool_defs: bool = False,
                      genericize: Optional[GenericizeCallback] = None) -> Artifact:
    """redact -> assemble -> draft, in one call (used by ``report_last``)."""
    art = redact_pair(request, response, request_id, provider=provider, description=description,
                      recipe=recipe, quality=quality, alignment_confidence=alignment_confidence,
                      strip=strip, include_tool_defs=include_tool_defs, genericize=genericize)
    art.draft = build_draft(art, deliver=deliver, out_dir=out_dir)  # type: ignore[attr-defined]
    return art


# --------------------------------------------------------------------------- #
# Ingest paths — OTEL raw bodies / Langfuse / Helicone -> ReproPair
# --------------------------------------------------------------------------- #
def from_otel_line(line: str, *, provider: str = "anthropic") -> ReproPair:
    """Parse one ``OTEL_LOG_RAW_API_BODIES`` log record into a :class:`ReproPair`.

    Claude Code's OTEL exporter, with ``OTEL_LOG_RAW_API_BODIES=1``, emits the raw
    request/response bodies on a structured log record. Field names vary by
    exporter; we probe the common shapes (top-level / ``body`` / ``attributes``)
    for the request body, the response body, and the request id.

    NOTE: the exact OTEL attribute keys are exporter-version-dependent and were NOT
    pinned against a captured sample — this is a best-effort mapping. Verify field
    names against your collector before relying on it in production."""
    try:
        rec = json.loads(line)
    except Exception as exc:
        raise ValueError(f"not a JSON OTEL log line: {exc}") from None
    scopes = [rec]
    for k in ("body", "attributes", "Body", "Attributes"):
        v = rec.get(k) if isinstance(rec, dict) else None
        if isinstance(v, dict):
            scopes.append(v)

    def _find(*keys):
        for sc in scopes:
            for k in keys:
                if k in sc and sc[k] not in (None, ""):
                    return sc[k]
        return None

    req = _find("request", "request_body", "gen_ai.request.body", "http.request.body")
    resp = _find("response", "response_body", "gen_ai.response.body", "http.response.body")
    rid = _find("request_id", "request-id", "gen_ai.response.id", "anthropic.request_id")
    req = _maybe_json(req) or {}
    resp = _maybe_json(resp)
    return to_pair(req, resp, rid if isinstance(rid, str) else None,
                   provider=provider, source="otel")


def from_langfuse(obj: Mapping[str, Any], *, provider: str = "anthropic") -> ReproPair:
    """Map a Langfuse **generation** observation onto a :class:`ReproPair`.

    Schema pinned from Langfuse docs (data-model + Observations API, June 2026): a
    generation carries ``input`` (the prompt — messages/system), ``output`` (the
    completion), ``model``, ``model_parameters``, ``usage``, ``metadata``, ``id``.
    The ``input``/``output`` *contents* are whatever the integration logged, so we
    map defensively: ``input`` -> the request (a dict with ``messages``/``system``,
    or a bare messages list), ``output`` -> the response (a Message-shaped dict, or
    text wrapped into one). Source:
    https://langfuse.com/docs/observability/data-model
    https://langfuse.com/docs/api-and-data-platform/features/observations-api"""
    inp = obj.get("input")
    out = obj.get("output")
    model = obj.get("model")
    params = obj.get("model_parameters") or {}

    request: dict = {}
    if isinstance(inp, dict):
        request = dict(inp)
    elif isinstance(inp, list):
        request = {"messages": inp}
    elif isinstance(inp, str):
        request = {"messages": [{"role": "user", "content": inp}]}
    if model and "model" not in request:
        request["model"] = model
    for k in ("max_tokens", "temperature", "top_p", "top_k"):
        if isinstance(params, dict) and k in params and k not in request:
            request[k] = params[k]

    response: Optional[dict] = None
    if isinstance(out, dict):
        response = dict(out)
        if "content" not in response and isinstance(out.get("text"), str):
            response = {"content": [{"type": "text", "text": out["text"]}], **response}
    elif isinstance(out, str):
        response = {"content": [{"type": "text", "text": out}]}
    if response is not None:
        response.setdefault("usage", obj.get("usage"))
        if model:
            response.setdefault("model", model)

    rid = obj.get("request_id")
    meta = obj.get("metadata")
    if not rid and isinstance(meta, dict):
        rid = meta.get("request_id")
    return to_pair(request, response, rid if isinstance(rid, str) else None,
                   provider=provider, source="langfuse")


def from_helicone(obj: Mapping[str, Any], *, provider: str = "anthropic") -> ReproPair:
    """Map a Helicone request export row onto a :class:`ReproPair`.

    Schema pinned from Helicone docs (June 2026): Helicone is a proxy that stores
    the **raw provider bodies** — an export with ``--include-body`` (or the
    query API) surfaces ``request_body`` and ``response_body``, which ARE the
    literal Anthropic request/response JSON. We read those (with ``request`` /
    ``response`` fallbacks). Source:
    https://docs.helicone.ai/rest/request/post-v1requestquery-clickhouse"""
    req = obj.get("request_body")
    if req is None:
        req = obj.get("request")
    resp = obj.get("response_body")
    if resp is None:
        resp = obj.get("response")
    rid = obj.get("request_id") or obj.get("provider_request_id") or obj.get("anthropic_request_id")
    return to_pair(_maybe_json(req) or {}, _maybe_json(resp),
                   rid if isinstance(rid, str) else None, provider=provider, source="helicone")


def _maybe_json(v: Any) -> Any:
    """Helicone/OTEL may store a body as a JSON STRING — decode if so."""
    if isinstance(v, str):
        s = v.strip()
        if s and s[0] in "{[":
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


# --------------------------------------------------------------------------- #
# The SDK wrapper — ReportingClient + ring buffer + report_last (+ Bedrock/Vertex)
# --------------------------------------------------------------------------- #
try:  # the `api` extra; the redaction/parse core does NOT need anthropic
    import anthropic as _anthropic
except Exception:  # pragma: no cover
    _anthropic = None


def _request_from_kwargs(kwargs: Mapping[str, Any]) -> dict:
    out: dict = {}
    for k in _REQUEST_FIELDS:
        if k in kwargs and kwargs[k] is not None:
            out[k] = _to_plain(kwargs[k])
    return copy.deepcopy(out)


class _RecordingMessages:
    """Wraps a real ``messages`` resource: delegates everything, records each
    completed ``create`` and ``stream`` onto the client's ring buffer."""

    def __init__(self, client: "_RingBufferMixin", inner: Any):
        self._client = client
        self._inner = inner

    def create(self, **kwargs):
        resp = self._inner.create(**kwargs)
        # Non-stream create returns a Message (has _request_id). A stream=True call
        # returns a Stream — skip recording there (use .stream() to capture).
        if _anthropic is not None and isinstance(resp, getattr(_anthropic, "Stream", ())):
            return resp
        if getattr(resp, "content", None) is not None or isinstance(resp, Mapping):
            self._client._push(kwargs, resp, extract_request_id(resp, provider=self._client._provider))
        return resp

    def stream(self, **kwargs):
        mgr = self._inner.stream(**kwargs)
        return _RecordingStreamManager(self._client, mgr, dict(kwargs))

    def __getattr__(self, name):  # parse / with_raw_response / count_tokens / …
        return getattr(self._inner, name)


class _RecordingStreamManager:
    def __init__(self, client, mgr, kwargs):
        self._client, self._mgr, self._kwargs = client, mgr, kwargs

    def __enter__(self):
        stream = self._mgr.__enter__()
        return _RecordingStream(self._client, stream, self._kwargs)

    def __exit__(self, *exc):
        return self._mgr.__exit__(*exc)

    def __getattr__(self, name):
        return getattr(self._mgr, name)


class _RecordingStream:
    def __init__(self, client, stream, kwargs):
        self._client, self._stream, self._kwargs = client, stream, kwargs
        self._recorded = False

    def __iter__(self):
        return iter(self._stream)

    def get_final_message(self):
        msg = self._stream.get_final_message()
        if not self._recorded:
            # Streaming path: the snapshot may lack _request_id; the reliable
            # source is stream.request_id (the response header).
            rid = getattr(self._stream, "request_id", None) or extract_request_id(msg)
            self._client._push(self._kwargs, msg, rid)
            self._recorded = True
        return msg

    def __getattr__(self, name):
        return getattr(self._stream, name)


class _RingBufferMixin:
    """Adds a (request, response, request_id, provider) ring buffer + report_last."""

    def _install_ring(self, report_buffer: int, provider: str) -> None:
        self._ring: deque = deque(maxlen=report_buffer)
        self._provider = provider
        self.messages = _RecordingMessages(self, self.messages)

    def _push(self, kwargs: Mapping[str, Any], response: Any, request_id: Optional[str]) -> None:
        req = _request_from_kwargs(kwargs)
        resp = _to_plain(response) if response is not None else None
        rid = request_id or extract_request_id(response, provider=self._provider)
        self._ring.append((req, resp, rid, self._provider))

    @property
    def report_buffer(self) -> list:
        """A read-only view of the ring (oldest → newest)."""
        return list(self._ring)

    def report_last(self, description: str, *, index: int = -1, recipe: str = "surgical",
                    quality: Optional[int] = None, alignment_confidence: Optional[int] = None,
                    deliver: str = "return", out_dir: str = ".",
                    strip: Iterable[str] = DEFAULT_STRIP,
                    include_tool_defs: bool = False,
                    genericize: Optional[GenericizeCallback] = None) -> Artifact:
        """Redact the N-th-from-last recorded (request, response) and produce a
        ready-to-send :class:`Artifact` (+ ``.draft``). One-liner the dev calls to
        report what just happened. Raises ``IndexError`` if the ring is empty.

        Pass ``genericize`` to add the optional semantic-rewrite layer (e.g. a second
        Claude call) on top of the deterministic floor — see :func:`redact_pair`."""
        if not self._ring:
            raise IndexError("report ring buffer is empty — no recorded calls to report")
        req, resp, rid, provider = self._ring[index]
        return build_and_deliver(req, resp, rid, description, provider=provider, recipe=recipe,
                                 quality=quality, alignment_confidence=alignment_confidence,
                                 deliver=deliver, out_dir=out_dir, strip=strip,
                                 include_tool_defs=include_tool_defs, genericize=genericize)


if _anthropic is not None:
    class ReportingClient(_RingBufferMixin, _anthropic.Anthropic):
        """Drop-in ``anthropic.Anthropic`` that remembers the last N request/response
        pairs (+ request-id) and reports one with :meth:`report_last`."""
        def __init__(self, *a, report_buffer: int = 20, **kw):
            super().__init__(*a, **kw)
            self._install_ring(report_buffer, "anthropic")

    class ReportingBedrock(_RingBufferMixin, _anthropic.AnthropicBedrock):
        """Bedrock variant. The Anthropic ``request-id`` header may be ABSENT here →
        :func:`anchor_for` falls back to the deterministic anchor."""
        def __init__(self, *a, report_buffer: int = 20, **kw):
            super().__init__(*a, **kw)
            self._install_ring(report_buffer, "bedrock")

    class ReportingVertex(_RingBufferMixin, _anthropic.AnthropicVertex):
        """Vertex variant — same deterministic-anchor fallback as Bedrock."""
        def __init__(self, *a, report_buffer: int = 20, **kw):
            super().__init__(*a, **kw)
            self._install_ring(report_buffer, "vertex")
else:  # pragma: no cover - anthropic always installed in this repo
    class ReportingClient:  # type: ignore[no-redef]
        def __init__(self, *a, **kw):
            raise ImportError("the `anthropic` package is required for ReportingClient "
                              "(install fb-assist[api])")
    ReportingBedrock = ReportingVertex = ReportingClient  # type: ignore[assignment]


# --------------------------------------------------------------------------- #
# CLI — mirror fb_assist.redact's argparse style
# --------------------------------------------------------------------------- #
def _load_json(arg: str) -> Any:
    if arg == "-":
        return json.loads(sys.stdin.read())
    with open(arg) as fh:
        return json.load(fh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="claude-repro",
                                 description="Privacy-clean, request-id-anchored repros for the Anthropic Messages API.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("redact", help="redact a request[/response] pair -> sanitized bundle")
    pr.add_argument("request", help="request JSON file (or - for stdin)")
    pr.add_argument("response", nargs="?", help="response JSON file")
    pr.add_argument("--request-id")
    pr.add_argument("--provider", default="anthropic", choices=["anthropic", "bedrock", "vertex"])
    pr.add_argument("--include-tool-defs", action="store_true")
    pr.add_argument("--out", help="write sanitized bundle JSON here (default: stdout)")

    rp = sub.add_parser("report", help="redact + build a support@anthropic.com draft")
    rp.add_argument("request")
    rp.add_argument("response", nargs="?")
    rp.add_argument("--request-id")
    rp.add_argument("--provider", default="anthropic", choices=["anthropic", "bedrock", "vertex"])
    rp.add_argument("--description", required=True)
    rp.add_argument("--deliver", default="file", choices=["file", "clipboard", "mailto", "return"])
    rp.add_argument("--quality", type=int)
    rp.add_argument("--alignment", type=int)
    rp.add_argument("--out-dir", default=".")

    lf = sub.add_parser("from-langfuse", help="redact + report a Langfuse generation export")
    lf.add_argument("trace"); lf.add_argument("--description", required=True)
    hl = sub.add_parser("from-helicone", help="redact + report a Helicone request export")
    hl.add_argument("trace"); hl.add_argument("--description", required=True)

    ls = sub.add_parser("leak-scan", help="adversarial egress scan over a sanitized bundle")
    ls.add_argument("bundle")

    sub.add_parser("demo", help="self-contained synthetic redaction demo (no input needed)")

    args = ap.parse_args(argv)

    if args.cmd == "demo":
        req = {
            "model": "claude-sonnet-4-5", "max_tokens": 256,
            "system": "You are Contoso's billing agent. Config at /home/devx/contoso/secrets.yaml.",
            "messages": [{"role": "user", "content": (
                "I'm Dana (dana@contoso.example). My key sk-ant-api03-DEMO1111DEMO2222DEMO3333 "
                "leaked into the trace. The bug: streaming stalls after the first tool_use.")}],
        }
        resp = {"id": "msg_demo", "type": "message", "role": "assistant",
                "content": [{"type": "text", "text": "Acknowledged — the streaming stall is the issue."}],
                "usage": {"input_tokens": 40, "output_tokens": 12}}
        art = redact_pair(req, resp, "req_011DEMOanchor00000000000")
        planted = ("dana@contoso.example", "sk-ant-api03-DEMO1111DEMO2222DEMO3333",
                   "/home/devx/contoso/secrets.yaml")
        print("[ BEFORE ]  the user turn as the SDK captured it (secrets visible):")
        print("    " + req["messages"][0]["content"])
        print("\n[ AFTER ]  the redacted repro that would ship:")
        print(art.preview.render())
        leaked = [v for v in planted if v in art.upload_text()]
        print(f"\nhard_gate_pass (deterministic floor empty): {art.hard_gate_pass}")
        print(f"request-id anchor: {json.dumps(art.anchor, ensure_ascii=False)}")
        print("RESULT:", "GREEN — every planted value absent from the upload bytes."
              if not leaked else f"LEAK: {leaked}")
        return 0 if (art.hard_gate_pass and not leaked) else 1

    if args.cmd == "redact":
        art = redact_pair(_load_json(args.request),
                          _load_json(args.response) if args.response else None,
                          args.request_id, provider=args.provider,
                          include_tool_defs=args.include_tool_defs)
        out = {"redacted_repro": art.redacted_repro, "anchor": art.anchor,
               "redaction_map": art.redaction_map, "preview": art.preview.to_dict(),
               "hard_gate_pass": art.hard_gate_pass}
        text = json.dumps(out, indent=2, ensure_ascii=False)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text)
            print(art.preview.render(), file=sys.stderr)
        else:
            print(text)
        return 0 if art.hard_gate_pass else 1

    if args.cmd in ("report", "from-langfuse", "from-helicone"):
        if args.cmd == "report":
            art = build_and_deliver(_load_json(args.request),
                                    _load_json(args.response) if args.response else None,
                                    args.request_id, args.description, provider=args.provider,
                                    quality=args.quality, alignment_confidence=args.alignment,
                                    deliver=args.deliver, out_dir=args.out_dir)
        else:
            obj = _load_json(args.trace)
            pair = from_langfuse(obj) if args.cmd == "from-langfuse" else from_helicone(obj)
            art = build_and_deliver(pair.request, pair.response, pair.request_id,
                                    args.description, provider=pair.provider, deliver="file")
        print(art.preview.render(), file=sys.stderr)
        draft = getattr(art, "draft", None)
        if draft and draft.files:
            print("wrote:", *draft.files, file=sys.stderr)
        print(f"anchor: {json.dumps(art.anchor, ensure_ascii=False)}")
        return 0 if art.hard_gate_pass else 1

    if args.cmd == "leak-scan":
        bundle = _load_json(args.bundle)
        findings = R.leak_scan(json.dumps(bundle, ensure_ascii=False))
        summary = R.summarize_findings(findings)
        print(json.dumps({"summary": summary,
                          "findings": [f.to_dict() for f in findings]}, indent=2, ensure_ascii=False))
        return 1 if summary["blocking"] else 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
