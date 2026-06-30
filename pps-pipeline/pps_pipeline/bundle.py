"""pps_pipeline.bundle — the SessionBundle contract + loader/validator.

A ``SessionBundle`` is the capture<->pipeline swap point: a directory holding a
``manifest.json`` (single clock origin ``t0`` + per-stream offsets) and a set of
stream files. This module:

* validates the manifest against ``schema/bundle.schema.json``;
* confirms every *present* stream's file exists on disk;
* loads each present stream and **normalizes it to offset-seconds relative to a
  single ``t0``** — so the packager sees one coherent timeline even though
  capture tools stamp time differently (whisper/captions emit offsets; the HAR
  and the Claude Code ``.jsonl`` carry absolute timestamps, normalized here);
* surfaces clock-drift (events outside ``[0, duration]``) instead of silently
  trusting capture.

The candidate's ``session.jsonl`` IS a Claude Code transcript, so it is parsed
with ``fb_assist.transcripts`` (full reuse) — never re-implemented here.

Output: a flat list of :class:`RawEvent` (``t``, ``kind``, ``text``, ``source``)
that ``chunk`` / ``redact_pass`` / ``interleave`` consume. Raw video/image bytes
are *never* read into an event — only frame captions (text) are.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator

from . import _schema_util as _su

# fb_assist is guaranteed importable by the package bootstrap (see __init__).
from fb_assist import transcripts as _tx


# --------------------------------------------------------------------------- #
# The canonical event the whole pipeline speaks in
# --------------------------------------------------------------------------- #
KINDS = ("prompt", "caption", "speech", "tool_call", "tool_result", "net", "event")

# Stream-name -> the file tools whose results we summarize, reused from fb_assist
# semantics. (We keep our own small map to stay decoupled from redact internals.)
_FILE_TOOLS = {"Read", "Edit", "Write", "MultiEdit", "NotebookEdit", "NotebookRead"}
_BASH_TOOLS = {"Bash", "BashOutput", "KillShell"}


@dataclass
class RawEvent:
    """One observation, normalized to offset-seconds from the session ``t0``.

    ``kind`` is one of :data:`KINDS`. ``text`` is the (pre-redaction) human-
    readable surface. ``source`` is a stable provenance ref used later for
    evidence citation and for the every-event-exactly-once invariant.
    """

    t: float
    kind: str
    text: str
    source: str
    meta: dict = field(default_factory=dict)


class BundleError(ValueError):
    """Raised when a bundle manifest is invalid or a present stream is missing."""


# --------------------------------------------------------------------------- #
# Time normalization
# --------------------------------------------------------------------------- #
def _iso_to_epoch(ts: str) -> float | None:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _normalize_t(raw_t: Any, time_base: str, t0_epoch: float) -> float | None:
    """Map a stream-native timestamp to offset-seconds from ``t0``.

    ``time_base`` ∈ {"offset", "epoch", "iso"}. Returns None if unparseable.
    """
    if time_base == "offset":
        try:
            return float(raw_t)
        except (TypeError, ValueError):
            return None
    if time_base == "epoch":
        try:
            return float(raw_t) - t0_epoch
        except (TypeError, ValueError):
            return None
    if time_base == "iso":
        ep = _iso_to_epoch(raw_t) if isinstance(raw_t, str) else None
        return None if ep is None else ep - t0_epoch
    return None


# --------------------------------------------------------------------------- #
# Small JSONL reader (stdlib; malformed-tolerant)
# --------------------------------------------------------------------------- #
def _read_jsonl(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                out.append(obj)
    return out


# --------------------------------------------------------------------------- #
# SessionBundle
# --------------------------------------------------------------------------- #
@dataclass
class SessionBundle:
    """A loaded, validated bundle. Construct via :func:`load_bundle`."""

    path: str
    manifest: dict

    @property
    def session_id(self) -> str:
        return self.manifest["session_id"]

    @property
    def t0_epoch(self) -> float:
        return float(self.manifest["t0_epoch"])

    @property
    def duration_s(self) -> float:
        return float(self.manifest["duration_s"])

    def stream(self, name: str) -> dict | None:
        s = self.manifest.get("streams", {}).get(name)
        if isinstance(s, dict) and s.get("present"):
            return s
        return None

    def _stream_path(self, name: str) -> str | None:
        s = self.stream(name)
        if not s:
            return None
        ref = s.get("ref")
        if not ref:
            return None
        return os.path.join(self.path, ref)

    def present_streams(self) -> list[str]:
        return [n for n, s in self.manifest.get("streams", {}).items()
                if isinstance(s, dict) and s.get("present")]

    # --- per-stream event extraction (each normalized to offset-seconds) ----- #
    def _events_transcript(self) -> Iterator[RawEvent]:
        s = self.stream("transcript")
        p = self._stream_path("transcript")
        if not s or not p or not os.path.exists(p):
            return
        tb = s.get("time_base", "offset")
        for i, seg in enumerate(_read_jsonl(p)):
            text = (seg.get("text") or "").strip()
            if not text:
                continue
            t = _normalize_t(seg.get("start", seg.get("t")), tb, self.t0_epoch)
            if t is None:
                continue
            yield RawEvent(t, "speech", text, f"transcript.jsonl#seg{i}",
                           {"end": seg.get("end")})

    def _events_captions(self) -> Iterator[RawEvent]:
        s = self.stream("captions")
        p = self._stream_path("captions")
        if not s or not p or not os.path.exists(p):
            return
        tb = s.get("time_base", "offset")
        for i, cap in enumerate(_read_jsonl(p)):
            text = (cap.get("text") or cap.get("caption") or "").strip()
            if not text:
                continue
            t = _normalize_t(cap.get("t", cap.get("start")), tb, self.t0_epoch)
            if t is None:
                continue
            yield RawEvent(t, "caption", text, f"captions.jsonl#frame{i}",
                           {"frame": cap.get("frame")})

    def _events_network(self) -> Iterator[RawEvent]:
        s = self.stream("network")
        p = self._stream_path("network")
        if not s or not p or not os.path.exists(p):
            return
        tb = s.get("time_base", "iso")
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                har = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return
        entries = (har.get("log", {}) or {}).get("entries", []) or []
        for i, e in enumerate(entries):
            req = e.get("request", {}) or {}
            resp = e.get("response", {}) or {}
            method = req.get("method", "?")
            url = req.get("url", "?")
            status = resp.get("status", "?")
            text = f"{method} {url} -> {status}"
            # Surface auth headers + bodies so redact_pass scrubs them (HAR is a
            # known leak surface). These are exactly what must NOT reach the LLM.
            for h in req.get("headers", []) or []:
                if str(h.get("name", "")).lower() == "authorization":
                    text += f" | req-auth: {h.get('value', '')}"
            body = (resp.get("content", {}) or {}).get("text")
            if isinstance(body, str) and body.strip():
                text += f" | resp-body: {body.strip()[:200]}"
            t = _normalize_t(e.get("startedDateTime", e.get("t")), tb, self.t0_epoch)
            if t is None:
                continue
            yield RawEvent(t, "net", text, f"network.har#entry{i}", {})

    def _events_ccode(self) -> Iterator[RawEvent]:
        """Parse the candidate's Claude Code ``session.jsonl`` via fb_assist.

        Emits ``prompt`` (typed human prompts), ``tool_call`` (assistant
        tool_use, summarized as ``Name: arg``) and ``tool_result`` (the
        model-visible result content) events — the natural units of work.
        Assistant prose / thinking are intentionally excluded: the observation
        is of the *candidate's* actions, not the model's narration.
        """
        p = self._stream_path("ccode_session")
        if not p or not os.path.exists(p):
            return
        s = self.stream("ccode_session")
        tb = s.get("time_base", "iso") if s else "iso"
        for r in _tx.parse(p):
            t = _normalize_t(r.timestamp, tb, self.t0_epoch)
            if t is None:
                continue
            ref = f"session.jsonl#{r.uuid or ('L' + str(r.line))}"
            msg = r.message or {}
            content = msg.get("content")
            if r.type == "user" and isinstance(content, str):
                txt = content.strip()
                if txt:
                    yield RawEvent(t, "prompt", txt, ref, {})
            elif r.type == "assistant" and isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_use":
                        yield RawEvent(t, "tool_call",
                                       _summarize_tool_use(blk), ref,
                                       {"tool": blk.get("name")})
            elif r.type == "user" and isinstance(content, list):
                for blk in content:
                    if isinstance(blk, dict) and blk.get("type") == "tool_result":
                        txt = _summarize_tool_result(blk)
                        if txt:
                            yield RawEvent(t, "tool_result", txt, ref, {})

    def _events_events(self) -> Iterator[RawEvent]:
        s = self.stream("events")
        p = self._stream_path("events")
        if not s or not p or not os.path.exists(p):
            return
        tb = s.get("time_base", "offset")
        for i, ev in enumerate(_read_jsonl(p)):
            text = (ev.get("text") or ev.get("kind") or "").strip()
            if not text:
                continue
            t = _normalize_t(ev.get("t"), tb, self.t0_epoch)
            if t is None:
                continue
            yield RawEvent(t, "event", text, f"events.jsonl#{i}", {})

    def raw_events(self, drop_drift: bool = True) -> list[RawEvent]:
        """All events from all present streams, normalized to offset-seconds.

        ``drop_drift=True`` discards events whose normalized ``t`` falls outside
        ``[0, duration_s]`` (a small grace is allowed) — the clock-drift guard.
        Returns events in stream order (NOT yet time-sorted; that is the
        packager's job, asserted there).
        """
        evs: list[RawEvent] = []
        for gen in (self._events_transcript, self._events_captions,
                    self._events_network, self._events_ccode,
                    self._events_events):
            evs.extend(gen())
        if drop_drift:
            grace = 1.0
            hi = self.duration_s + grace
            evs = [e for e in evs if -grace <= e.t <= hi]
            # Clamp tiny negative offsets to 0 so the timeline starts at >=0.
            for e in evs:
                if e.t < 0:
                    e.t = 0.0
        return evs

    def drift_report(self) -> dict:
        """Diagnostics: how many events fell outside ``[0, duration]`` (drift)."""
        total = 0
        drifted = 0
        for gen in (self._events_transcript, self._events_captions,
                    self._events_network, self._events_ccode,
                    self._events_events):
            for e in gen():
                total += 1
                if e.t < -1.0 or e.t > self.duration_s + 1.0:
                    drifted += 1
        return {"events": total, "drifted": drifted,
                "duration_s": self.duration_s}


# --------------------------------------------------------------------------- #
# Tool-use / tool-result summarizers (concise, deterministic, text-only)
# --------------------------------------------------------------------------- #
def _summarize_tool_use(blk: dict) -> str:
    name = blk.get("name") or "tool"
    inp = blk.get("input") or {}
    if name in _FILE_TOOLS:
        fp = inp.get("file_path") or inp.get("notebook_path") or ""
        base = os.path.basename(fp) if fp else ""
        return f"{name}: {base}" if base else name
    if name in _BASH_TOOLS:
        cmd = str(inp.get("command", "")).strip()
        cmd = " ".join(cmd.split())
        return f"{name}: {cmd[:80]}" if cmd else name
    # Generic: surface one short scalar arg if present.
    for k in ("pattern", "query", "url", "prompt", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return f"{name}: {' '.join(v.split())[:80]}"
    return str(name)


def _summarize_tool_result(blk: dict) -> str:
    c = blk.get("content")
    if isinstance(c, str):
        return " ".join(c.split())[:200]
    if isinstance(c, list):
        for sub in c:
            if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                return " ".join(sub["text"].split())[:200]
    return ""


# --------------------------------------------------------------------------- #
# Loading + validation
# --------------------------------------------------------------------------- #
def load_manifest(bundle_dir: str) -> dict:
    mp = os.path.join(bundle_dir, "manifest.json")
    if not os.path.exists(mp):
        raise BundleError(f"no manifest.json in {bundle_dir}")
    with open(mp, "r", encoding="utf-8") as fh:
        return json.load(fh)


def validate_manifest(manifest: dict) -> list[str]:
    """Validate against the bundle JSON Schema. Returns a list of error strings
    (empty == valid)."""
    return _su.validation_errors("bundle.schema.json", manifest)


def load_bundle(bundle_dir: str, strict: bool = True) -> SessionBundle:
    """Load + validate a bundle directory.

    ``strict=True`` raises :class:`BundleError` on a schema violation or a
    *present* stream whose ref file is missing on disk.
    """
    manifest = load_manifest(bundle_dir)
    errs = validate_manifest(manifest)
    if errs and strict:
        raise BundleError("manifest schema errors: " + "; ".join(errs))
    b = SessionBundle(path=bundle_dir, manifest=manifest)
    # Present-stream existence check.
    missing = []
    for name in b.present_streams():
        p = b._stream_path(name)
        if p is None or not os.path.exists(p):
            missing.append(name)
    if missing and strict:
        raise BundleError(f"present streams missing on disk: {missing}")
    return b
