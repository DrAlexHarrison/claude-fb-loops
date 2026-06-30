"""fb_assist.transcripts — Claude Code session-transcript extraction engine.

Parses on-disk session transcripts (``~/.claude*/projects/<cwd-slug>/<sessionId>.jsonl``)
and extracts any part on demand by category, via ``Record``/``Span`` locators (uuid +
field path + char-span) precise enough for a redactor to mask in place. stdlib-only,
local-only, streams every file line-by-line (real transcripts run up to 76 MB).

Tool output is stored twice — structured under ``toolUseResult`` and again as a
model-visible ``tool_result`` block in the next user record. Both upload via
``/feedback``; the extractors emit both (correlated by ``meta['tool_use_id']``) so a
redactor can scrub both copies.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field as _dc_field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Union

__all__ = [
    "Record",
    "Span",
    "parse",
    "iter_records",
    "ParseStats",
    # category extractors
    "human_prompts",
    "thinking_blocks",
    "assistant_text",
    "bash_output",
    "file_contents",
    "tool_calls",
    "tool_results",
    "paths",
    "env_metadata",
    "hook_output",
    "injected_memory",
    "websearch",
    "EXTRACTORS",
    "extract",
    "extract_all",
    # scope selectors
    "by_session",
    "exclude_sidechains",
    "only_sidechains",
    "since",
    "iter_turns",
    "turn_range",
    "last_n_turns",
    # higher-order
    "relevant_slice",
    "size_estimate",
    "redaction_map",
    "find_transcripts",
    "default_roots",
    # locator helpers
    "get_at",
    "set_at",
    "replace_span",
]

TranscriptSource = Union[str, Path, Iterable["Record"]]
CHARS_PER_TOKEN = 4  # crude but matches the gather's budgeting intent (1 MB / ~tokens)


# --------------------------------------------------------------------------- #
# Normalized record + locator types
# --------------------------------------------------------------------------- #
@dataclass
class Record:
    """One parsed JSONL line, with the pervasive envelope hoisted for ergonomics.

    ``line`` is the 1-based line number — the *universal* locator (every record
    has one, including the lightweight meta records that carry no ``uuid``).
    """

    line: int
    raw: dict
    type: str

    # Convenience envelope accessors (None when the record type lacks them).
    @property
    def uuid(self) -> str | None:
        return self.raw.get("uuid")

    @property
    def parent_uuid(self) -> str | None:
        return self.raw.get("parentUuid")

    @property
    def session_id(self) -> str | None:
        return self.raw.get("sessionId")

    @property
    def timestamp(self) -> str | None:
        return self.raw.get("timestamp")

    @property
    def cwd(self) -> str | None:
        return self.raw.get("cwd")

    @property
    def git_branch(self) -> str | None:
        return self.raw.get("gitBranch")

    @property
    def version(self) -> str | None:
        return self.raw.get("version")

    @property
    def is_sidechain(self) -> bool:
        return bool(self.raw.get("isSidechain", False))

    @property
    def message(self) -> dict | None:
        m = self.raw.get("message")
        return m if isinstance(m, dict) else None


@dataclass
class Span:
    """A located, extractable piece of content.

    ``field`` is the human-readable path (e.g. ``message.content[2].thinking``).
    ``path`` is the programmatic key-tuple for navigation/mutation
    (e.g. ``("message", "content", 2, "thinking")``). ``start``/``end`` are char
    offsets into the string value at that path (whole-field => 0..len(text)).
    """

    category: str
    line: int
    uuid: str | None
    field: str
    path: tuple
    start: int
    end: int
    text: str
    session_id: str | None = None
    timestamp: str | None = None
    meta: dict = _dc_field(default_factory=dict)

    @property
    def char_len(self) -> int:
        return self.end - self.start

    def preview(self, n: int = 160) -> str:
        t = self.text
        return t[:n] + ("…" if len(t) > n else "")

    def locator(self, preview_chars: int = 160) -> dict:
        """Lightweight dict for ``redaction_map`` — locator + preview, NOT full text."""
        return {
            "category": self.category,
            "line": self.line,
            "uuid": self.uuid,
            "field": self.field,
            "path": list(self.path),
            "start": self.start,
            "end": self.end,
            "char_len": self.char_len,
            "preview": self.preview(preview_chars),
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "meta": self.meta,
        }

    def to_dict(self) -> dict:
        """Full dict including text — for ``extract`` JSON output."""
        d = self.locator()
        d["text"] = self.text
        return d


# --------------------------------------------------------------------------- #
# Parsing (streaming, malformed-tolerant)
# --------------------------------------------------------------------------- #
@dataclass
class ParseStats:
    total_lines: int = 0
    ok: int = 0
    blank: int = 0
    malformed: int = 0
    not_object: int = 0  # valid JSON but not a dict (e.g. bare array/number)
    malformed_lines: list = _dc_field(default_factory=list)  # 1-based line numbers (capped)

    _MALFORMED_CAP = 50

    def note_malformed(self, line_no: int) -> None:
        self.malformed += 1
        if len(self.malformed_lines) < self._MALFORMED_CAP:
            self.malformed_lines.append(line_no)


def parse(path: Union[str, Path], stats: ParseStats | None = None) -> Iterator[Record]:
    """Stream a transcript file → generator of normalized :class:`Record`.

    Iterates line-by-line (bounded memory). Blank lines are skipped; malformed
    or non-object lines are skipped and tallied on ``stats`` if one is passed.
    Never loads the whole file.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, start=1):
            if stats is not None:
                stats.total_lines = i
            s = line.strip()
            if not s:
                if stats is not None:
                    stats.blank += 1
                continue
            try:
                obj = json.loads(s)
            except (json.JSONDecodeError, ValueError):
                if stats is not None:
                    stats.note_malformed(i)
                continue
            if not isinstance(obj, dict):
                if stats is not None:
                    stats.not_object += 1
                continue
            if stats is not None:
                stats.ok += 1
            yield Record(line=i, raw=obj, type=str(obj.get("type", "")))


def iter_records(source: TranscriptSource, stats: ParseStats | None = None) -> Iterator[Record]:
    """Accept a path *or* an already-materialized iterable of records.

    This is what lets every extractor compose: do one ``parse`` pass and feed the
    records to many extractors, or hand an extractor a path directly.
    """
    if isinstance(source, (str, Path)):
        yield from parse(source, stats=stats)
    else:
        for r in source:
            yield r


# --------------------------------------------------------------------------- #
# Locator navigation helpers (the redactor's handhold)
# --------------------------------------------------------------------------- #
def get_at(obj: Any, path: Iterable) -> Any:
    """Navigate ``obj`` by a key-tuple (dict keys + list indices). Returns the
    value, or raises KeyError/IndexError/TypeError if the path is invalid."""
    cur = obj
    for key in path:
        cur = cur[key]
    return cur


def set_at(obj: Any, path: tuple, value: Any) -> None:
    """Set the value at ``path`` (in place). ``path`` must be non-empty."""
    if not path:
        raise ValueError("path must be non-empty")
    parent = get_at(obj, path[:-1])
    parent[path[-1]] = value


def replace_span(record_or_raw: Union[Record, dict], span: Span, replacement: str) -> dict:
    """Splice ``replacement`` into the string at ``span``'s path/offsets, in place.

    Convenience for the redactor (and for round-trip tests): navigates to
    ``span.path``, replaces ``[span.start:span.end]`` with ``replacement``, writes
    it back. Returns the raw dict. Raises if the located value isn't a string.
    """
    raw = record_or_raw.raw if isinstance(record_or_raw, Record) else record_or_raw
    cur = get_at(raw, span.path)
    if not isinstance(cur, str):
        raise TypeError(f"value at {span.field} is {type(cur).__name__}, not str")
    new = cur[: span.start] + replacement + cur[span.end :]
    set_at(raw, span.path, new)
    return raw


def _field_str(path: tuple) -> str:
    """Render a key-tuple as a human path: message.content[2].thinking"""
    out = []
    for k in path:
        if isinstance(k, int):
            out.append(f"[{k}]")
        else:
            out.append(("." if out else "") + str(k))
    return "".join(out)


def _mk(record: Record, category: str, path: tuple, text: str,
        start: int | None = None, end: int | None = None, **meta) -> Span:
    """Build a Span for a (whole-field by default) string located at ``path``."""
    if start is None:
        start = 0
    if end is None:
        end = len(text)
    return Span(
        category=category,
        line=record.line,
        uuid=record.uuid,
        field=_field_str(path),
        path=path,
        start=start,
        end=end,
        text=text,
        session_id=record.session_id,
        timestamp=record.timestamp,
        meta=meta,
    )


# --------------------------------------------------------------------------- #
# Per-record extractors (the engine — each yields Spans for ONE record)
# --------------------------------------------------------------------------- #
def _human_prompts(r: Record) -> Iterator[Span]:
    if r.type != "user":
        return
    msg = r.message
    if not msg:
        return
    content = msg.get("content")
    if isinstance(content, str):
        # A human-typed prompt (or an expanded slash-command / system-injected
        # string). Mark which so callers can filter.
        stripped = content.lstrip()
        is_command = stripped.startswith("<command-") or stripped.startswith("<local-command")
        is_meta = bool(r.raw.get("isMeta"))
        yield _mk(
            r, "human_prompts", ("message", "content"), content,
            prompt_source=r.raw.get("promptSource"),
            prompt_id=r.raw.get("promptId"),
            is_command=is_command,
            is_meta=is_meta,
        )
    elif isinstance(content, list):
        # When content is a list it's normally tool_result blocks, but a typed
        # human message can also ride alongside as a `text` block (e.g. the user
        # interrupts with a note). Capture those text blocks as human prompts.
        for i, blk in enumerate(content):
            if isinstance(blk, dict) and blk.get("type") == "text":
                txt = blk.get("text")
                if isinstance(txt, str) and txt:
                    yield _mk(r, "human_prompts", ("message", "content", i, "text"), txt,
                              block_kind="text_in_list")


def _thinking_blocks(r: Record) -> Iterator[Span]:
    if r.type != "assistant":
        return
    msg = r.message
    if not msg:
        return
    for i, blk in enumerate(msg.get("content", []) or []):
        if isinstance(blk, dict) and blk.get("type") == "thinking":
            txt = blk.get("thinking")
            if isinstance(txt, str):
                yield _mk(r, "thinking_blocks", ("message", "content", i, "thinking"), txt,
                          model=msg.get("model"))


def _assistant_text(r: Record) -> Iterator[Span]:
    if r.type != "assistant":
        return
    msg = r.message
    if not msg:
        return
    for i, blk in enumerate(msg.get("content", []) or []):
        if isinstance(blk, dict) and blk.get("type") == "text":
            txt = blk.get("text")
            if isinstance(txt, str):
                yield _mk(r, "assistant_text", ("message", "content", i, "text"), txt,
                          model=msg.get("model"))


def _first_tool_use_id(r: Record) -> str | None:
    """The tool_use_id of the tool_result this user record carries (best-effort).

    Lets us correlate the structured ``toolUseResult`` with the model-visible
    ``message.content`` tool_result block (same tool call, stored twice)."""
    msg = r.message
    if not msg:
        return None
    c = msg.get("content")
    if isinstance(c, list):
        for blk in c:
            if isinstance(blk, dict) and blk.get("type") == "tool_result":
                return blk.get("tool_use_id")
    return None


def _bash_output(r: Record) -> Iterator[Span]:
    if r.type != "user":
        return
    tur = r.raw.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    # Bash shape: {stdout, stderr, interrupted, isImage, noOutputExpected, ...}
    # (+ gitOperation / backgroundTaskId variants — same stdout/stderr fields).
    if "stdout" not in tur and "stderr" not in tur:
        return
    tuid = _first_tool_use_id(r)
    for key in ("stdout", "stderr"):
        val = tur.get(key)
        if isinstance(val, str) and val:
            yield _mk(r, "bash_output", ("toolUseResult", key), val,
                      stream=key, tool_use_id=tuid,
                      git_operation=tur.get("gitOperation"))


def _file_contents(r: Record) -> Iterator[Span]:
    if r.type != "user":
        return
    tur = r.raw.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    tuid = _first_tool_use_id(r)
    # Read: toolUseResult.file = {content, filePath, numLines, ...}
    f = tur.get("file")
    if isinstance(f, dict) and isinstance(f.get("content"), str):
        yield _mk(r, "file_contents", ("toolUseResult", "file", "content"), f["content"],
                  tool="Read", file_path=f.get("filePath"), tool_use_id=tuid)
    # Edit: {originalFile, oldString, newString, filePath, structuredPatch, ...}
    # Write: {content, originalFile, filePath, structuredPatch, ...}
    fp = tur.get("filePath")
    for key, tool in (("originalFile", "Edit/Write"), ("newString", "Edit"),
                      ("oldString", "Edit"), ("content", "Write")):
        val = tur.get(key)
        if isinstance(val, str) and val:
            yield _mk(r, "file_contents", ("toolUseResult", key), val,
                      tool=tool, which=key, file_path=fp, tool_use_id=tuid)


def _iter_string_leaves(obj: Any, base_path: tuple) -> Iterator[tuple[tuple, str, Any]]:
    """Yield ``(path, string, leaf_key)`` for every non-empty string leaf within a
    nested dict/list. ``leaf_key`` is the immediate key/index of the string —
    handy for meta. Keeps every emitted Span a true char-span into a real string,
    even for arbitrarily-nested tool inputs / search results."""
    if isinstance(obj, str):
        if obj:
            yield (base_path, obj, base_path[-1] if base_path else None)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _iter_string_leaves(v, base_path + (k,))
    elif isinstance(obj, list):
        for idx, v in enumerate(obj):
            yield from _iter_string_leaves(v, base_path + (idx,))


# toolUseResult top-level keys already char-spanned by the typed extractors
# (bash_output / file_contents / websearch). The structured completeness net in
# _tool_results skips these to avoid double-locating the same content.
_TUR_OWNED_KEYS = frozenset({
    "stdout", "stderr",            # bash_output
    "file", "originalFile", "newString", "oldString", "content",  # file_contents
    "query", "results",            # websearch
    "filePath", "outputFile",      # paths (avoid cross-category duplication)
})


def _tool_calls(r: Record) -> Iterator[Span]:
    if r.type != "assistant":
        return
    msg = r.message
    if not msg:
        return
    for i, blk in enumerate(msg.get("content", []) or []):
        if isinstance(blk, dict) and blk.get("type") == "tool_use":
            inp = blk.get("input", {})
            base = ("message", "content", i, "input")
            # Descend into the (arbitrarily nested) input to char-span each string
            # leaf — e.g. a secret inside a Bash `command`, a `url`, a `prompt`.
            for path, s, leaf in _iter_string_leaves(inp, base):
                yield _mk(r, "tool_calls", path, s,
                          tool_name=blk.get("name"), tool_use_id=blk.get("id"),
                          input_key=str(leaf), model=msg.get("model"))


def _tool_results(r: Record) -> Iterator[Span]:
    """Model-visible tool output (the ``tool_result`` block in user.message.content).

    This is the second, *model-visible* copy of tool output (the structured copy
    is ``toolUseResult`` — captured by bash_output/file_contents). Content is
    usually a string; sometimes a list of sub-blocks (text/tool_reference/...)."""
    if r.type != "user":
        return
    msg = r.message
    if not msg:
        return
    content = msg.get("content")
    if not isinstance(content, list):
        return
    for i, blk in enumerate(content):
        if not (isinstance(blk, dict) and blk.get("type") == "tool_result"):
            continue
        tuid = blk.get("tool_use_id")
        c = blk.get("content")
        if isinstance(c, str):
            if c:
                yield _mk(r, "tool_results", ("message", "content", i, "content"), c,
                          tool_use_id=tuid, is_error=blk.get("is_error"))
        elif isinstance(c, list):
            for j, sub in enumerate(c):
                if isinstance(sub, dict) and isinstance(sub.get("text"), str):
                    yield _mk(r, "tool_results",
                              ("message", "content", i, "content", j, "text"),
                              sub["text"], tool_use_id=tuid,
                              sub_type=sub.get("type"))
    # Structured-result completeness net. The typed extractors (bash_output /
    # file_contents / websearch) own a fixed set of toolUseResult keys. Many OTHER
    # tools carry content in their structured result too — agent/Task `prompt`,
    # AskUserQuestion `questions`/`answers`, SendMessage `message`, `caption`,
    # `codeText`, `summary`. Without this, a redactor would MISS those. Walk every
    # string leaf under toolUseResult whose top key isn't already owned elsewhere.
    tur = r.raw.get("toolUseResult")
    if isinstance(tur, dict):
        for key, val in tur.items():
            if key in _TUR_OWNED_KEYS:
                continue
            for path, s, leaf in _iter_string_leaves(val, ("toolUseResult", key)):
                yield _mk(r, "tool_results", path, s,
                          structured=True, result_key=str(leaf), top_key=key)
    elif isinstance(tur, (str, list)):
        # Many tools — MCP servers especially — store the structured result as a
        # bare string (a JSON-encoded payload, an error message) or a list, not a
        # dict. That is still a SECOND on-disk copy that uploads via /feedback, so
        # the net must char-span it too; otherwise a redactor scrubs the
        # model-visible tool_result block above but leaves this structured copy
        # (e.g. a serialized email thread, an error carrying an absolute path)
        # intact. Its path differs from the model-visible copy, so both get
        # located and the redactor dedupes by (line, field, start, end).
        for path, s, leaf in _iter_string_leaves(tur, ("toolUseResult",)):
            yield _mk(r, "tool_results", path, s,
                      structured=True, result_key=str(leaf), top_key=None)


# Structured path-bearing fields we know about, by record kind. Each entry is a
# (path, meta-kind) — only emitted when the value is a non-empty string.
def _paths(r: Record, scan_text: bool = False) -> Iterator[Span]:
    raw = r.raw
    # Envelope path leakage (cwd + gitBranch are on nearly every enveloped record;
    # commitSha appears in some versions).
    for key, kind in (("cwd", "cwd"), ("gitBranch", "git_branch"),
                      ("commitSha", "commit_sha")):
        v = raw.get(key)
        if isinstance(v, str) and v:
            yield _mk(r, "paths", (key,), v, kind=kind)
    # Tool-result file paths.
    tur = raw.get("toolUseResult")
    if isinstance(tur, dict):
        if isinstance(tur.get("filePath"), str):
            yield _mk(r, "paths", ("toolUseResult", "filePath"), tur["filePath"], kind="file_path")
        f = tur.get("file")
        if isinstance(f, dict) and isinstance(f.get("filePath"), str):
            yield _mk(r, "paths", ("toolUseResult", "file", "filePath"), f["filePath"], kind="file_path")
        if isinstance(tur.get("outputFile"), str):
            yield _mk(r, "paths", ("toolUseResult", "outputFile"), tur["outputFile"], kind="output_file")
    # Attachment-borne paths.
    att = raw.get("attachment")
    if isinstance(att, dict):
        if isinstance(att.get("path"), str):
            yield _mk(r, "paths", ("attachment", "path"), att["path"], kind="memory_path")
        if isinstance(att.get("filename"), str):
            yield _mk(r, "paths", ("attachment", "filename"), att["filename"], kind="edited_file")
        inner = att.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("path"), str):
            yield _mk(r, "paths", ("attachment", "content", "path"), inner["path"], kind="memory_path")
    # Worktree-state records nest cwd / paths / branches / the original HEAD
    # commit SHA under `worktreeSession` — pervasive path+branch+commit leakage
    # the envelope scan misses (this is where a real commitSha actually appears).
    ws = raw.get("worktreeSession")
    if isinstance(ws, dict):
        for key, kind in (("originalCwd", "cwd"), ("worktreePath", "worktree_path"),
                          ("worktreeBranch", "git_branch"), ("originalBranch", "git_branch"),
                          ("originalHeadCommit", "commit_sha")):
            v = ws.get(key)
            if isinstance(v, str) and v:
                yield _mk(r, "paths", ("worktreeSession", key), v, kind=kind)
    if scan_text:
        # Opt-in: regex-scan free text for absolute paths. Off by default — the
        # structured fields above are deterministic; deep text scanning for paths
        # belongs to the redactor's PII/secrets layer. Provided for completeness.
        import re
        pat = re.compile(r"(?:/[\w.\-]+){2,}/?|[A-Za-z]:\\(?:[\w.\-]+\\?)+")
        for sp in _text_bearing_spans(r):
            for m in pat.finditer(sp.text):
                yield Span("paths", sp.line, sp.uuid, sp.field, sp.path,
                           sp.start + m.start(), sp.start + m.end(), m.group(),
                           sp.session_id, sp.timestamp, {"kind": "scanned_path", "in_field": sp.field})


def _env_metadata(r: Record) -> Iterator[Span]:
    raw = r.raw
    # Envelope metadata on enveloped records.
    for key, kind in (("version", "version"), ("entrypoint", "entrypoint"),
                      ("userType", "user_type"), ("sessionId", "session_id"),
                      ("gitBranch", "git_branch"), ("cwd", "cwd")):
        v = raw.get(key)
        if isinstance(v, str) and v and "uuid" in raw:  # only enveloped records
            yield _mk(r, "env_metadata", (key,), v, kind=kind)
    # Assistant model + request id.
    if r.type == "assistant":
        msg = r.message or {}
        if isinstance(msg.get("model"), str):
            yield _mk(r, "env_metadata", ("message", "model"), msg["model"], kind="model")
        if isinstance(raw.get("requestId"), str):
            yield _mk(r, "env_metadata", ("requestId",), raw["requestId"], kind="request_id")
    # Session titles / agent identity leak the topic & project — treat as metadata.
    for key, kind in (("aiTitle", "ai_title"), ("customTitle", "custom_title"),
                      ("agentName", "agent_name")):
        v = raw.get(key)
        if isinstance(v, str) and v:
            yield _mk(r, "env_metadata", (key,), v, kind=kind)
    # PR links leak repo + number.
    if r.type == "pr-link":
        if isinstance(raw.get("prUrl"), str):
            yield _mk(r, "env_metadata", ("prUrl",), raw["prUrl"], kind="pr_url")
        if isinstance(raw.get("prRepository"), str):
            yield _mk(r, "env_metadata", ("prRepository",), raw["prRepository"], kind="pr_repo")


def _hook_output(r: Record) -> Iterator[Span]:
    if r.type == "attachment":
        att = r.raw.get("attachment")
        if isinstance(att, dict) and str(att.get("type", "")).startswith("hook"):
            for key in ("stdout", "stderr", "content", "command"):
                v = att.get(key)
                if isinstance(v, str) and v:
                    yield _mk(r, "hook_output", ("attachment", key), v,
                              hook_name=att.get("hookName"),
                              hook_event=att.get("hookEvent"),
                              subtype=att.get("type"),
                              exit_code=att.get("exitCode"))
    elif r.type == "system":
        sub = r.raw.get("subtype")
        if sub in ("stop_hook_summary",) and isinstance(r.raw.get("content"), str):
            yield _mk(r, "hook_output", ("content",), r.raw["content"], subtype=sub)


def _injected_memory(r: Record) -> Iterator[Span]:
    if r.type != "attachment":
        return
    att = r.raw.get("attachment")
    if not isinstance(att, dict):
        return
    if att.get("type") == "nested_memory":
        inner = att.get("content")
        if isinstance(inner, dict) and isinstance(inner.get("content"), str):
            yield _mk(r, "injected_memory", ("attachment", "content", "content"),
                      inner["content"], memory_path=att.get("path"),
                      memory_type=inner.get("type"))
        elif isinstance(att.get("content"), str):
            yield _mk(r, "injected_memory", ("attachment", "content"),
                      att["content"], memory_path=att.get("path"))


def _websearch(r: Record) -> Iterator[Span]:
    if r.type != "user":
        return
    tur = r.raw.get("toolUseResult")
    if not isinstance(tur, dict):
        return
    if "query" not in tur or "results" not in tur:
        return
    if isinstance(tur.get("query"), str):
        yield _mk(r, "websearch", ("toolUseResult", "query"), tur["query"], kind="query")
    results = tur.get("results")
    if isinstance(results, list):
        # Char-span each string leaf (titles, urls, snippets) under results.
        for path, s, leaf in _iter_string_leaves(results, ("toolUseResult", "results")):
            yield _mk(r, "websearch", path, s, kind="result_field",
                      result_key=str(leaf), count=tur.get("searchCount"))


# Text-bearing spans (for scan_text path-finding and relevant_slice keyword search).
_TEXT_EXTRACTORS_FOR_SEARCH: tuple[Callable[[Record], Iterator[Span]], ...]


def _text_bearing_spans(r: Record) -> Iterator[Span]:
    for fn in _TEXT_EXTRACTORS_FOR_SEARCH:
        yield from fn(r)


# Registry: category name -> per-record extractor.
EXTRACTORS: dict[str, Callable[[Record], Iterator[Span]]] = {
    "human_prompts": _human_prompts,
    "thinking_blocks": _thinking_blocks,
    "assistant_text": _assistant_text,
    "bash_output": _bash_output,
    "file_contents": _file_contents,
    "tool_calls": _tool_calls,
    "tool_results": _tool_results,
    "paths": _paths,
    "env_metadata": _env_metadata,
    "hook_output": _hook_output,
    "injected_memory": _injected_memory,
    "websearch": _websearch,
}

_TEXT_EXTRACTORS_FOR_SEARCH = (
    _human_prompts, _thinking_blocks, _assistant_text,
    _bash_output, _file_contents, _tool_results,
    _hook_output, _injected_memory, _websearch,
)


# --------------------------------------------------------------------------- #
# Public category-extractor API (path-or-records in; Span generator out)
# --------------------------------------------------------------------------- #
def _make_public(per_record: Callable[[Record], Iterator[Span]]):
    def public(source: TranscriptSource, stats: ParseStats | None = None) -> Iterator[Span]:
        for r in iter_records(source, stats=stats):
            yield from per_record(r)
    return public


human_prompts = _make_public(_human_prompts)
thinking_blocks = _make_public(_thinking_blocks)
assistant_text = _make_public(_assistant_text)
bash_output = _make_public(_bash_output)
file_contents = _make_public(_file_contents)
tool_calls = _make_public(_tool_calls)
tool_results = _make_public(_tool_results)
env_metadata = _make_public(_env_metadata)
hook_output = _make_public(_hook_output)
injected_memory = _make_public(_injected_memory)
websearch = _make_public(_websearch)


def paths(source: TranscriptSource, scan_text: bool = False,
          stats: ParseStats | None = None) -> Iterator[Span]:
    """Locate path/identifier leakage. Structured known fields by default;
    pass ``scan_text=True`` to also regex-scan free text for absolute paths."""
    for r in iter_records(source, stats=stats):
        yield from _paths(r, scan_text=scan_text)


def extract(source: TranscriptSource, category: str,
            stats: ParseStats | None = None) -> Iterator[Span]:
    """Run a single named category extractor (see ``EXTRACTORS`` keys)."""
    try:
        fn = EXTRACTORS[category]
    except KeyError:
        raise KeyError(f"unknown category {category!r}; choices: {sorted(EXTRACTORS)}")
    for r in iter_records(source, stats=stats):
        yield from fn(r)


def extract_all(source: TranscriptSource, categories: Iterable[str] | None = None,
                stats: ParseStats | None = None) -> Iterator[Span]:
    """Run many extractors in ONE streaming pass (memory-bounded over the file).

    Yields Spans across all requested categories, record by record. This is the
    efficient way to gather everything — one read, every lens."""
    cats = list(categories) if categories is not None else list(EXTRACTORS)
    fns = []
    for c in cats:
        if c not in EXTRACTORS:
            raise KeyError(f"unknown category {c!r}; choices: {sorted(EXTRACTORS)}")
        fns.append(EXTRACTORS[c])
    for r in iter_records(source, stats=stats):
        for fn in fns:
            yield from fn(r)


# --------------------------------------------------------------------------- #
# Scope selectors (records in -> filtered records out; all lazy)
# --------------------------------------------------------------------------- #
def by_session(source: TranscriptSource, session_id: str) -> Iterator[Record]:
    for r in iter_records(source):
        if r.session_id == session_id:
            yield r


def exclude_sidechains(source: TranscriptSource) -> Iterator[Record]:
    for r in iter_records(source):
        if not r.is_sidechain:
            yield r


def only_sidechains(source: TranscriptSource) -> Iterator[Record]:
    for r in iter_records(source):
        if r.is_sidechain:
            yield r


def _to_dt(ts: Any) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str) and ts:
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def since(source: TranscriptSource, cutoff: Union[str, datetime],
          until: Union[str, datetime, None] = None) -> Iterator[Record]:
    """Records with ``timestamp`` >= cutoff (and < until if given). Records
    lacking a timestamp are skipped (the lightweight meta records have none)."""
    lo = _to_dt(cutoff)
    hi = _to_dt(until) if until is not None else None
    if lo is not None and lo.tzinfo is None:
        lo = lo.replace(tzinfo=timezone.utc)
    if hi is not None and hi.tzinfo is None:
        hi = hi.replace(tzinfo=timezone.utc)
    for r in iter_records(source):
        dt = _to_dt(r.timestamp)
        if dt is None:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if lo is not None and dt < lo:
            continue
        if hi is not None and dt >= hi:
            continue
        yield r


@dataclass
class Turn:
    """One human turn: the prompt record + every record up to the next prompt."""
    index: int                  # 1-based; index 0 = preamble before first prompt
    prompt: Record | None       # the human-prompt record (None for the preamble)
    records: list               # all records in this turn (incl. the prompt)

    @property
    def prompt_text(self) -> str | None:
        if self.prompt is None:
            return None
        c = (self.prompt.message or {}).get("content")
        return c if isinstance(c, str) else None


def _is_human_prompt(r: Record) -> bool:
    """Any string-content user record (typed prompt, slash-command expansion, or
    injected ``<task-notification>``/``<system-reminder>`` input event)."""
    if r.type != "user":
        return False
    c = (r.message or {}).get("content")
    return isinstance(c, str)


def _is_typed_human_prompt(r: Record) -> bool:
    """Only genuinely human-typed prompts — not synthetic/injected input events.

    Signal: ``promptSource == "typed"`` (clean in current transcripts). Falls back
    to "string content that doesn't open with a synthetic ``<...>`` tag" for older
    records that predate ``promptSource``."""
    if r.type != "user":
        return False
    c = (r.message or {}).get("content")
    if not isinstance(c, str):
        return False
    ps = r.raw.get("promptSource")
    if ps == "typed":
        return True
    if ps in ("system",):
        return False
    return not c.lstrip().startswith("<")


def iter_turns(source: TranscriptSource, human_only: bool = False) -> Iterator[Turn]:
    """Segment a transcript into turns. A new turn begins at each ``user`` record
    whose ``message.content`` is a string. Records before the first such prompt
    form turn 0 (preamble).

    ``human_only=False`` (default) breaks on *every* input event — typed prompts,
    slash-commands, and injected ``<task-notification>``/``<system-reminder>``
    records — the literal turn structure. ``human_only=True`` breaks only on
    genuinely human-typed prompts (``promptSource == "typed"``), folding injected
    events into the surrounding turn — the human-meaningful conversation rhythm.
    Indices are renumbered per call, so they differ between the two modes."""
    boundary = _is_typed_human_prompt if human_only else _is_human_prompt
    idx = 0
    prompt: Record | None = None
    bucket: list = []
    started = False
    for r in iter_records(source):
        if boundary(r):
            if started or bucket:
                yield Turn(idx, prompt, bucket)
            idx += 1
            prompt = r
            bucket = [r]
            started = True
        else:
            bucket.append(r)
    if started or bucket:
        yield Turn(idx, prompt, bucket)


def turn_range(source: TranscriptSource, start: int, end: int | None = None,
               human_only: bool = True) -> Iterator[Record]:
    """Records from turns ``start``..``end`` inclusive (1-based). ``end=None`` =>
    through the last turn. Turn 0 (preamble) is included only if ``start==0``.
    Defaults to human-meaningful turns (``human_only=True``)."""
    for t in iter_turns(source, human_only=human_only):
        if t.index < start:
            continue
        if end is not None and t.index > end:
            break
        yield from t.records


def last_n_turns(source: TranscriptSource, n: int, human_only: bool = True) -> Iterator[Record]:
    """Records from the last ``n`` turns (preamble excluded). Buffers only the
    tail (``n`` turns), not the whole file. Defaults to human-meaningful turns
    (``human_only=True``) — i.e. the last ``n`` things the human actually typed
    and everything that happened in response."""
    from collections import deque
    tail: deque = deque(maxlen=n)
    for t in iter_turns(source, human_only=human_only):
        if t.index == 0:
            continue
        tail.append(t)
    for t in tail:
        yield from t.records


# --------------------------------------------------------------------------- #
# relevant_slice — contiguous exchange(s) around a needle
# --------------------------------------------------------------------------- #
def _record_matches(r: Record, needle: str, needle_lower: str) -> bool:
    if r.uuid == needle or r.parent_uuid == needle:
        return True
    # Search the content-bearing strings of the record (not the whole envelope —
    # avoids matching on ubiquitous paths/sessionIds unless the needle is one).
    for sp in _text_bearing_spans(r):
        if needle_lower in sp.text.lower():
            return True
    return False


def relevant_slice(source: TranscriptSource, needle: str,
                   context_turns: int = 1, max_turns: int | None = None) -> list[Record]:
    """Return the contiguous exchange(s) around ``needle`` (a keyword, error
    substring, or uuid). Finds the turn(s) containing a match and expands by
    ``context_turns`` on each side; merges overlapping windows.

    Materializes turns (needs look-around), but only retains records, not a
    second copy of the file. For very large files prefer scoping first
    (e.g. ``by_session``) then slicing.
    """
    needle_lower = needle.lower()
    turns = list(iter_turns(source))
    hit_indices: list[int] = []
    for pos, t in enumerate(turns):
        if any(_record_matches(r, needle, needle_lower) for r in t.records):
            hit_indices.append(pos)
    if not hit_indices:
        return []
    # Build merged windows of turn positions.
    keep: set[int] = set()
    for h in hit_indices:
        for p in range(max(0, h - context_turns), min(len(turns), h + context_turns + 1)):
            keep.add(p)
    out: list[Record] = []
    for pos in sorted(keep):
        out.extend(turns[pos].records)
    if max_turns is not None and len(keep) > max_turns:
        # Trim to the windows nearest the hits (keep hit turns + closest context).
        out = []
        trimmed = sorted(keep)[:max_turns]
        for pos in trimmed:
            out.extend(turns[pos].records)
    return out


# --------------------------------------------------------------------------- #
# size_estimate — for the 1 MB /feedback budget
# --------------------------------------------------------------------------- #
def size_estimate(source: TranscriptSource, by_category: bool = False) -> dict:
    """Exact byte size + char count + crude token estimate (chars/4).

    For a path, ``bytes`` is the on-disk size (what the gather's 1 MB budget
    measures). For an in-memory record iterable, ``bytes`` is the re-serialized
    size. Set ``by_category=True`` to also bucket extractable chars per category
    (useful for "what's eating the budget / what to strip first")."""
    result: dict[str, Any] = {
        "records": 0, "chars": 0, "est_tokens": 0,
        "by_type": {},
    }
    is_path = isinstance(source, (str, Path))
    if is_path:
        result["bytes"] = os.path.getsize(source)
        result["path"] = str(source)
    else:
        result["bytes"] = 0

    stats = ParseStats()
    cat_chars: dict[str, int] = {}
    for r in iter_records(source, stats=stats):
        result["records"] += 1
        line_json = json.dumps(r.raw, ensure_ascii=False)
        result["chars"] += len(line_json)
        if not is_path:
            result["bytes"] += len(line_json.encode("utf-8")) + 1  # +newline
        result["by_type"][r.type] = result["by_type"].get(r.type, 0) + 1
        if by_category:
            for fn in EXTRACTORS.values():
                for sp in fn(r):
                    cat_chars[sp.category] = cat_chars.get(sp.category, 0) + sp.char_len

    result["est_tokens"] = result["chars"] // CHARS_PER_TOKEN
    result["est_tokens_from_bytes"] = result["bytes"] // CHARS_PER_TOKEN
    result["parse"] = {
        "ok": stats.ok, "malformed": stats.malformed,
        "blank": stats.blank, "not_object": stats.not_object,
    }
    result["over_1mb"] = result["bytes"] > 1_000_000
    if by_category:
        result["by_category_chars"] = dict(sorted(cat_chars.items(), key=lambda kv: -kv[1]))
        result["by_category_est_tokens"] = {k: v // CHARS_PER_TOKEN for k, v in cat_chars.items()}
    return result


# --------------------------------------------------------------------------- #
# redaction_map — the key handoff to the redaction module
# --------------------------------------------------------------------------- #
def redaction_map(source: TranscriptSource, categories: Iterable[str] | None = None,
                  preview_chars: int = 160) -> dict:
    """Structured index of WHERE each sensitive category lives.

    ONE streaming pass. Returns lightweight locators (uuid + line + field + path
    + char-span + preview), NOT the full content — so it stays memory-bounded
    even on the 76 MB fixture. This is the redaction module's primary input.

    Returns::

        {
          "summary": {category: {"count": N, "total_chars": C}, ...},
          "totals": {"spans": ..., "records": ..., "chars_located": ...},
          "by_category": {category: [locator, ...], ...},
          "parse": {ok, malformed, blank, not_object, malformed_lines},
        }

    Each ``locator`` is ``Span.locator()`` (path-tuple as a list). The redactor
    navigates ``record[path][start:end]`` to mask in place. Note tool output
    appears under BOTH ``bash_output``/``file_contents`` (structured) and
    ``tool_results`` (model-visible) — correlate via ``meta.tool_use_id`` and
    redact both; dedupe identical targets by ``(line, field, start, end)``."""
    cats = list(categories) if categories is not None else list(EXTRACTORS)
    fns = [(c, EXTRACTORS[c]) for c in cats if c in EXTRACTORS]
    unknown = [c for c in cats if c not in EXTRACTORS]
    if unknown:
        raise KeyError(f"unknown categories {unknown}; choices: {sorted(EXTRACTORS)}")

    by_category: dict[str, list] = {c: [] for c, _ in fns}
    summary: dict[str, dict] = {c: {"count": 0, "total_chars": 0} for c, _ in fns}
    stats = ParseStats()
    total_spans = 0
    chars_located = 0

    for r in iter_records(source, stats=stats):
        for c, fn in fns:
            for sp in fn(r):
                by_category[c].append(sp.locator(preview_chars))
                summary[c]["count"] += 1
                summary[c]["total_chars"] += sp.char_len
                total_spans += 1
                chars_located += sp.char_len

    return {
        "source": str(source) if isinstance(source, (str, Path)) else "<records>",
        "summary": summary,
        "totals": {
            "spans": total_spans,
            "records": stats.ok,
            "chars_located": chars_located,
            "est_tokens_located": chars_located // CHARS_PER_TOKEN,
        },
        "by_category": by_category,
        "parse": {
            "ok": stats.ok, "malformed": stats.malformed, "blank": stats.blank,
            "not_object": stats.not_object, "malformed_lines": stats.malformed_lines,
        },
    }


# --------------------------------------------------------------------------- #
# Transcript discovery (mirrors the /feedback gather: project dir + mtime window)
# --------------------------------------------------------------------------- #
# Claude Code names a session's project dir by replacing every character that is
# NOT ASCII alphanumeric or '-' with '-' — so '/', '\\', '.', ':', spaces and '_'
# all collapse to '-'. Verified against real on-disk dirs: a cwd ending in
# '/.claude/worktrees/x' lands under '...--claude-worktrees-x' (the '/.' → '--').
# This is the portable rule: it slugifies a Windows 'C:\\Users\\dana\\proj' to
# 'C--Users-dana-proj' AND fixes the long-standing Linux miss on dotted/underscored
# paths that a bare '/'->'-' replace produced the wrong dir name for.
_SLUG_RE = re.compile(r"[^A-Za-z0-9-]")


def project_slug(cwd: Union[str, Path]) -> str:
    """The ``projects/<slug>`` directory name Claude Code writes for ``cwd``.

    Portable across Linux/macOS/Windows: every non-``[A-Za-z0-9-]`` char becomes
    ``-`` (the rule Claude Code itself uses; confirmed against real project dirs)."""
    return _SLUG_RE.sub("-", str(cwd))


def default_roots() -> list[Path]:
    """The ``projects`` parent dirs to scan, discovered generically for ANY user.

    No account name is ever hardcoded. ``$CLAUDE_CONFIG_DIR`` (one explicit config
    dir) wins; otherwise every Claude Code config dir is discovered by globbing
    ``~/.claude*`` and keeping those that actually contain a ``projects/`` dir — so
    a plain ``~/.claude`` install, a multi-account layout (``~/.claude`` plus any
    ``~/.claude-<suffix>`` siblings), or a custom setup all resolve, with no
    user-specific name baked into the source. The default ``~/.claude/projects`` is
    always included (even before it exists on a fresh install) so the common
    single-account case never resolves empty. Newest-first ordering is applied
    later by :func:`find_transcripts` (mtime sort), so root order is irrelevant.
    """
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return [Path(cfg).expanduser() / "projects"]
    home = Path.home()
    default = home / ".claude" / "projects"
    roots: list[Path] = [default]
    seen = {default}
    for cfg_dir in sorted(home.glob(".claude*")):
        proj = cfg_dir / "projects"
        if proj in seen:
            continue
        # A real config dir, not a stray file (e.g. ~/.claude.json) — and it must
        # actually hold transcripts (a projects/ child) to count.
        if cfg_dir.is_dir() and proj.is_dir():
            roots.append(proj)
            seen.add(proj)
    return roots


def find_transcripts(project_dir: Union[str, Path, None] = None,
                     window_hours: float | None = None,
                     roots: Iterable[Union[str, Path]] | None = None,
                     cwd: Union[str, Path, None] = None) -> list[dict]:
    """Locate on-disk transcripts (read-only), newest-first.

    Mirrors how ``/feedback`` gathers: it reads ``*.jsonl`` from the current
    project's dir, optionally filtered to files modified within a window
    (24 h / 7 d). Useful for the co-author to discover which session(s) to act on.

    * ``project_dir`` — a specific ``<cwd-slug>`` dir to scan; OR
    * ``cwd`` — a working directory to slugify (:func:`project_slug`) and find
      across roots; OR
    * ``roots`` — explicit ``projects`` parents (default: the Claude config dirs
      discovered by :func:`default_roots` — ``$CLAUDE_CONFIG_DIR`` if set, else
      every ``~/.claude*`` that holds a ``projects/`` dir; no account name is
      hardcoded).

    Returns dicts: ``{path, size, mtime, session_id, project_dir}``."""
    out: list[dict] = []
    candidate_dirs: list[Path] = []
    if project_dir is not None:
        candidate_dirs.append(Path(project_dir))
    else:
        if roots is None:
            roots = default_roots()
        slug = None
        if cwd is not None:
            slug = project_slug(cwd)
        for root in roots:
            root = Path(root)
            if not root.is_dir():
                continue
            if slug is not None:
                d = root / slug
                if d.is_dir():
                    candidate_dirs.append(d)
            else:
                for d in root.iterdir():
                    if d.is_dir():
                        candidate_dirs.append(d)

    now = datetime.now(timezone.utc).timestamp()
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                st = f.stat()
            except OSError:
                continue
            if window_hours is not None and (now - st.st_mtime) > window_hours * 3600:
                continue
            out.append({
                "path": str(f),
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
                "session_id": f.stem,
                "project_dir": str(d),
            })
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return out


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _scoped_records(path: str, args) -> Iterator[Record]:
    """Apply CLI scope flags (--session/--no-sidechains/--since/--turns/--last-n)
    as a lazy pipeline over the parsed records."""
    src: TranscriptSource = path
    if getattr(args, "session", None):
        src = by_session(src, args.session)
    if getattr(args, "no_sidechains", False):
        src = exclude_sidechains(src)
    if getattr(args, "since", None):
        src = since(src, args.since)
    if getattr(args, "turns", None):
        a, _, b = args.turns.partition(":")
        start = int(a) if a else 0
        end = int(b) if b else None
        src = turn_range(src, start, end)
    if getattr(args, "last_n", None):
        src = last_n_turns(src, args.last_n)
    return iter_records(src)


def _add_scope_flags(p) -> None:
    p.add_argument("--session", help="restrict to this sessionId")
    p.add_argument("--no-sidechains", action="store_true", help="drop sidechain (sub-agent) records")
    p.add_argument("--since", help="ISO timestamp lower bound (e.g. 2026-06-08T00:00:00Z)")
    p.add_argument("--turns", help="turn range START:END (1-based, inclusive); END optional")
    p.add_argument("--last-n", type=int, help="only the last N human turns")


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="fb-transcripts",
        description="Extract any part of a Claude Code session transcript (.jsonl). "
                    "Streaming, stdlib-only, local-only.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_parse = sub.add_parser("parse", help="stream normalized records as JSONL")
    p_parse.add_argument("path")
    p_parse.add_argument("--type", help="filter to one record type")
    _add_scope_flags(p_parse)

    sub.add_parser("categories", help="list extractable categories")

    p_ext = sub.add_parser("extract", help="extract one category -> spans")
    p_ext.add_argument("category", choices=sorted(EXTRACTORS))
    p_ext.add_argument("path")
    p_ext.add_argument("--text", action="store_true", help="human text output (default JSON)")
    p_ext.add_argument("--scan-text", action="store_true", help="(paths only) also regex-scan free text")
    _add_scope_flags(p_ext)

    p_map = sub.add_parser("map", help="redaction_map: where every category lives (JSON)")
    p_map.add_argument("path")
    p_map.add_argument("--categories", help="comma-separated subset")
    p_map.add_argument("--summary", action="store_true", help="print only the summary block")
    _add_scope_flags(p_map)

    p_size = sub.add_parser("size", help="exact bytes + token estimate (1 MB budget)")
    p_size.add_argument("path")
    p_size.add_argument("--by-category", action="store_true")
    _add_scope_flags(p_size)

    p_slice = sub.add_parser("slice", help="contiguous exchange(s) around a needle")
    p_slice.add_argument("path")
    p_slice.add_argument("needle")
    p_slice.add_argument("--context", type=int, default=1, help="turns of context each side")
    p_slice.add_argument("--text", action="store_true", help="human text output")
    _add_scope_flags(p_slice)

    p_find = sub.add_parser("find", help="discover on-disk transcripts (newest-first)")
    p_find.add_argument("--project-dir")
    p_find.add_argument("--cwd")
    p_find.add_argument("--window-hours", type=float)

    args = ap.parse_args(argv)

    if args.cmd == "categories":
        for c in sorted(EXTRACTORS):
            print(c)
        return 0

    if args.cmd == "find":
        rows = find_transcripts(project_dir=args.project_dir, cwd=args.cwd,
                                window_hours=args.window_hours)
        print(json.dumps(rows, indent=2))
        return 0

    if args.cmd == "parse":
        for r in _scoped_records(args.path, args):
            if args.type and r.type != args.type:
                continue
            print(json.dumps({"line": r.line, **r.raw}, ensure_ascii=False))
        return 0

    if args.cmd == "extract":
        records = list(_scoped_records(args.path, args)) if _any_scope(args) else args.path
        if args.category == "paths":
            gen = paths(records, scan_text=args.scan_text)
        else:
            gen = extract(records, args.category)
        for sp in gen:
            if args.text:
                print(f"L{sp.line} {sp.field} [{sp.char_len}c] {sp.preview()}")
            else:
                print(json.dumps(sp.to_dict(), ensure_ascii=False))
        return 0

    if args.cmd == "map":
        records = list(_scoped_records(args.path, args)) if _any_scope(args) else args.path
        cats = args.categories.split(",") if args.categories else None
        m = redaction_map(records, categories=cats)
        if args.summary:
            print(json.dumps({"source": m["source"], "summary": m["summary"],
                              "totals": m["totals"], "parse": m["parse"]}, indent=2))
        else:
            print(json.dumps(m, ensure_ascii=False))
        return 0

    if args.cmd == "size":
        records = list(_scoped_records(args.path, args)) if _any_scope(args) else args.path
        print(json.dumps(size_estimate(records, by_category=args.by_category), indent=2))
        return 0

    if args.cmd == "slice":
        records = list(_scoped_records(args.path, args))
        sl = relevant_slice(records, args.needle, context_turns=args.context)
        if args.text:
            for r in sl:
                print(f"L{r.line} {r.type} {r.uuid or ''}")
        else:
            for r in sl:
                print(json.dumps({"line": r.line, **r.raw}, ensure_ascii=False))
        return 0

    return 1


def _any_scope(args) -> bool:
    return any(getattr(args, k, None) for k in ("session", "since", "turns", "last_n")) \
        or getattr(args, "no_sidechains", False)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
