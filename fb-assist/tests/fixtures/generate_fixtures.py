#!/usr/bin/env python3
"""Deterministic synthetic Claude-Code-transcript generator.

The test suite needs two large `.jsonl` fixtures — ``sample-mid.jsonl`` (>1 MB)
and ``sample-large.jsonl`` (>50 MB) — that exercise the parser, the twelve
category extractors, the locator round-trip, the streaming/perf path, and the
redaction floor. The ORIGINAL fixtures were real personal Claude Code sessions
and must never ship. This module manufactures *realistic-but-entirely-fake*
transcripts that satisfy every magic-number assertion those tests make, with
zero real data.

What the generated ``sample-mid.jsonl`` guarantees (the asserts it must pass):

  * ``ParseStats.ok > 1000`` and ``malformed == 0``               (parse)
  * ``> 5000`` extractable string spans, each char-span round-trips (locator)
  * a non-empty span in ALL twelve categories                     (extractors)
  * ``size_estimate(...).over_1mb is True``                       (>1 MB on disk)
  * tool output stored TWICE (model-visible ``tool_result`` block + structured
    ``toolUseResult.stdout``) sharing a ``tool_use_id``           (double-store)
  * structured ``toolUseResult`` shapes whose top key is ``prompt`` / ``answers``
    / ``questions`` / ``description`` (the completeness net)      (structured)
  * an ``mcp__``-prefixed tool call                               (exotic tools)
  * a Read file body containing the neutral header "All rights reserved"
    (the strip test asserts it is removed)                        (strip)
  * a rare keyword (``ZEPHYR_LEDGER``) for the tight-slice test   (relevant_slice)
  * ``>= 2`` sessionIds, some ``isSidechain: true`` records, spread timestamps,
    typed prompts AND injected ``<...>`` events, ``aiTitle`` / ``model`` / ``cwd``
    env metadata, and a worktree-state record with nested cwd/branch/commit.

Everything is seeded, so record/span counts are stable across runs.

Run directly::

    python generate_fixtures.py mid          # -> sample-mid.jsonl  (~1.4 MB)
    python generate_fixtures.py large        # -> sample-large.jsonl (~55 MB)
    python generate_fixtures.py all          # both

LOCAL ONLY. Pure stdlib. No network, no real data.
"""
from __future__ import annotations

import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

SEED = 20260630
MID_TARGET_BYTES = 1_350_000      # comfortably over the 1 MB budget
LARGE_TARGET_BYTES = 56_000_000   # comfortably over the 50 MB streaming floor

RARE_KEYWORD = "ZEPHYR_LEDGER"    # the tight-slice needle (replaces a real codename)
HOME = "/home/devuser"            # synthetic home — never a real user home
REPOS = ["webapp", "billing-api", "edge-worker", "analytics"]

# Deterministic fake vocabulary --------------------------------------------- #
_WORDS = (
    "diff viewer scroll cursor reload hot panel terminal latency render queue "
    "buffer token stream socket retry timeout cache evict commit branch merge "
    "rebase stash hook prompt completion shell pipe stdout stderr exit signal "
    "thread mutex deadlock heap stack frame trace span extractor locator parser "
    "schema validate budget bundle redact secret mask sentinel gate restore swap "
    "session transcript model assistant feedback submit freeze hang flicker jump"
).split()

_BUG_TOPICS = [
    "the diff viewer scrolls to the wrong line after a hot reload",
    "tab completion eats my Enter key on a long session",
    "the terminal repaints twice and the cursor flickers",
    "/feedback hangs on submit and never returns",
    "search-in-transcript jumps to the top instead of the match",
    "the status line shows a stale token count after compaction",
    "paste of a multi-line command loses the trailing newline",
    "the model keeps re-reading a file it already has in context",
]

_PERSONAS = ["Dana Okafor", "Priya Raman", "Jordan Castellano", "Mateo Alvarez"]
_BRANDS = ["Contoso", "Northwind", "Halcyon", "Initech"]


def _lorem(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(_WORDS) for _ in range(max(1, n_words)))


def _fake_path(rng: random.Random) -> str:
    repo = rng.choice(REPOS)
    parts = [rng.choice(_WORDS) for _ in range(rng.randint(1, 3))]
    return f"{HOME}/code/{repo}/" + "/".join(parts) + ".py"


def _ts(base: datetime, secs: int) -> str:
    return (base + timedelta(seconds=secs)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------- #
# Record builders                                                             #
# --------------------------------------------------------------------------- #
def _env(uuid, parent, session_id, ts, *, sidechain=False, **extra):
    base = {
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": sidechain,
        "sessionId": session_id,
        "timestamp": ts,
        "cwd": f"{HOME}/code/{extra.pop('_repo', 'webapp')}",
        "gitBranch": extra.pop("_branch", "main"),
        "version": "2.1.195",
        "userType": "external",
    }
    base.update(extra)
    return base


def _cycle(rng, c, session_id, base_dt, sidechain, scale, plant_keyword):
    """One realistic conversation cycle → a list of schema-faithful records."""
    repo = rng.choice(REPOS)
    branch = rng.choice(["main", "feature/x", "fix/scroll", "HEAD"])
    t = c * 60
    p = f"c{c}"
    recs: list[dict] = []

    def env(suffix, parent, dt_off, **extra):
        return _env(f"{p}-{suffix}", parent, session_id, _ts(base_dt, t + dt_off),
                    sidechain=sidechain, _repo=repo, _branch=branch, **extra)

    topic = rng.choice(_BUG_TOPICS)
    person = rng.choice(_PERSONAS)
    kw = f" Tracking it under {RARE_KEYWORD}." if plant_keyword else ""

    # 1) human typed prompt (carries a path in free text for the scan_text path test)
    prompt = (
        f"Hey — {topic}. I'm debugging in {_fake_path(rng)} and it's blocking me. "
        f"{_lorem(rng, 12 * scale)}.{kw}"
    )
    recs.append(env("u-prompt", None, 0, type="user", promptSource="typed",
                    entrypoint="cli",
                    message={"role": "user", "content": prompt}))

    # 2) assistant: thinking + text + Bash tool_use
    recs.append(env("a-bash", f"{p}-u-prompt", 1, type="assistant", requestId=f"req_{p}_a",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "thinking",
                         "thinking": f"They hit {topic}. Let me reproduce. {_lorem(rng, 18 * scale)}",
                         "signature": "AAAABBBB=="},
                        {"type": "text", "text": f"Let me reproduce — {_lorem(rng, 8 * scale)}."},
                        {"type": "tool_use", "id": f"toolu_{p}_bash", "name": "Bash",
                         "input": {"command": f"cd {HOME}/code/{repo} && pytest -q",
                                   "description": "run the suite"}},
                    ]}))

    # 3) bash result — DOUBLE STORED (tool_result block + toolUseResult.stdout)
    stdout = (
        f"$ pytest -q\n{_lorem(rng, 40 * scale)}\n"
        f"  at {_fake_path(rng)}:42\n  at {_fake_path(rng)}:88\nFAILED 1 test\n"
    )
    recs.append(env("u-bash", f"{p}-a-bash", 2, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_bash", "content": stdout},
                    ]},
                    toolUseResult={"stdout": stdout, "stderr": "", "interrupted": False,
                                   "isImage": False}))

    # 4) assistant: Read tool_use
    recs.append(env("a-read", f"{p}-u-bash", 3, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": f"Reading the source. {_lorem(rng, 6 * scale)}"},
                        {"type": "tool_use", "id": f"toolu_{p}_read", "name": "Read",
                         "input": {"file_path": f"{HOME}/code/{repo}/scroll.py"}},
                    ]}))

    # 5) Read result — file body carries the neutral "All rights reserved" header
    file_body = (
        "# Copyright (c) 2026 Example Org. All rights reserved.\n"
        f"import os\nfrom {repo}.util import helper  # {_fake_path(rng)}\n\n"
        f"def render():\n    # {_lorem(rng, 30 * scale)}\n    return 1\n"
    )
    recs.append(env("u-read", f"{p}-a-read", 4, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_read", "content": file_body},
                    ]},
                    toolUseResult={"file": {"content": file_body,
                                            "filePath": f"{HOME}/code/{repo}/scroll.py",
                                            "numLines": 7, "startLine": 1, "totalLines": 7},
                                   "filePath": f"{HOME}/code/{repo}/scroll.py"}))

    # 6) assistant: Edit tool_use
    recs.append(env("a-edit", f"{p}-u-read", 5, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": f"Patching the scroll math. {_lorem(rng, 5 * scale)}"},
                        {"type": "tool_use", "id": f"toolu_{p}_edit", "name": "Edit",
                         "input": {"file_path": f"{HOME}/code/{repo}/scroll.py",
                                   "old_string": "return 1", "new_string": "return 0"}},
                    ]}))

    # 7) Edit result — original/new bodies (file_contents) + structuredPatch
    recs.append(env("u-edit", f"{p}-a-edit", 6, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_edit",
                         "content": "The file has been updated."},
                    ]},
                    toolUseResult={"originalFile": file_body,
                                   "oldString": "return 1", "newString": "return 0",
                                   "filePath": f"{HOME}/code/{repo}/scroll.py",
                                   "structuredPatch": [{"oldStart": 6, "oldLines": 1,
                                                        "newStart": 6, "newLines": 1,
                                                        "lines": ["-    return 1", "+    return 0"]}]}))

    # 8) assistant: WebSearch tool_use
    recs.append(env("a-web", f"{p}-u-edit", 7, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": f"Checking upstream issues. {_lorem(rng, 4 * scale)}"},
                        {"type": "tool_use", "id": f"toolu_{p}_web", "name": "WebSearch",
                         "input": {"query": f"{topic} known issue"}},
                    ]}))

    # 9) WebSearch result — query + results (websearch category)
    results = [{"title": f"Issue: {topic}", "url": f"https://example.com/issues/{c}",
                "snippet": _lorem(rng, 12 * scale)} for _ in range(2)]
    recs.append(env("u-web", f"{p}-a-web", 8, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_web",
                         "content": f"Found {len(results)} results."},
                    ]},
                    toolUseResult={"query": f"{topic} known issue", "results": results,
                                   "searchCount": len(results)}))

    # 10) assistant: Task / agent spawn tool_use
    recs.append(env("a-task", f"{p}-u-web", 9, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": "Spawning a sub-agent to bisect."},
                        {"type": "tool_use", "id": f"toolu_{p}_task", "name": "Task",
                         "input": {"description": "bisect the regression",
                                   "prompt": f"Bisect {topic} across recent commits. {_lorem(rng, 6 * scale)}"}},
                    ]}))

    # 11) agent result — structured `prompt`/`description` (completeness net)
    recs.append(env("u-task", f"{p}-a-task", 10, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_task",
                         "content": "Sub-agent finished: regression introduced in commit a1b2c3d."},
                    ]},
                    toolUseResult={"prompt": f"Bisect {topic}. {_lorem(rng, 10 * scale)}",
                                   "description": "bisect the regression",
                                   "totalDurationMs": 4200, "totalTokens": 9100}))

    # 12) assistant: AskUserQuestion tool_use
    recs.append(env("a-ask", f"{p}-u-task", 11, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": "One decision before I patch."},
                        {"type": "tool_use", "id": f"toolu_{p}_ask", "name": "AskUserQuestion",
                         "input": {"questions": [{"question": "Revert or forward-fix?",
                                                  "options": ["revert", "forward-fix"]}]}},
                    ]}))

    # 13) AskUserQuestion result — structured `questions`/`answers` (completeness net)
    recs.append(env("u-ask", f"{p}-a-ask", 12, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_ask",
                         "content": "User chose: forward-fix."},
                    ]},
                    toolUseResult={"questions": [{"question": "Revert or forward-fix?",
                                                  "options": ["revert", "forward-fix"]}],
                                   "answers": ["forward-fix"]}))

    # 14) assistant: mcp__-prefixed tool_use (exotic tool surface)
    recs.append(env("a-mcp", f"{p}-u-ask", 13, type="assistant",
                    message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                        {"type": "text", "text": "Looking it up via the connector."},
                        {"type": "tool_use", "id": f"toolu_{p}_mcp",
                         "name": "mcp__contoso__lookup",
                         "input": {"q": f"{rng.choice(_BRANDS)} {_lorem(rng, 4)}"}},
                    ]}))

    # 15) mcp result — both copies (tool_result block + structured toolUseResult)
    mcp_out = f"connector says: {_lorem(rng, 14 * scale)}"
    recs.append(env("u-mcp", f"{p}-a-mcp", 14, type="user",
                    message={"role": "user", "content": [
                        {"type": "tool_result", "tool_use_id": f"toolu_{p}_mcp", "content": mcp_out},
                    ]},
                    toolUseResult={"data": mcp_out, "rows": 3}))

    # 16) attachment: nested injected memory (CLAUDE.md)
    recs.append({"type": "attachment", "uuid": f"{p}-mem", "sessionId": session_id,
                 "timestamp": _ts(base_dt, t + 15),
                 "attachment": {"type": "nested_memory",
                                "path": f"{HOME}/code/{repo}/CLAUDE.md",
                                "content": {"type": "file",
                                            "content": f"# Project rules\n{_lorem(rng, 16 * scale)}"}}})

    # 17) attachment: hook output
    recs.append({"type": "attachment", "uuid": f"{p}-hook", "sessionId": session_id,
                 "timestamp": _ts(base_dt, t + 16),
                 "attachment": {"type": "hook_output", "hookName": "post-tool",
                                "hookEvent": "PostToolUse", "exitCode": 0,
                                "command": f"{HOME}/.local/bin/lint --fix",
                                "stdout": f"lint clean: {_lorem(rng, 8 * scale)}"}})

    # 18) every 3rd cycle: an injected <...> event (folds into the human turn)
    if c % 3 == 0:
        recs.append(env("u-inj", f"{p}-u-prompt", 17, type="user", isMeta=True,
                        message={"role": "user",
                                 "content": f"<system-reminder>context note {c}: {_lorem(rng, 6)}</system-reminder>"}))

    return recs


def _meta_records(rng, session_id, base_dt):
    """Lightweight meta records (no envelope) that must survive parsing."""
    return [
        {"type": "agent-color", "agentColor": "red", "sessionId": session_id},
        {"type": "ai-title", "aiTitle": "debugging the scroll regression",
         "sessionId": session_id},
        {"type": "agent-name", "agentName": "debug-helper", "sessionId": session_id},
        {"type": "pr-link", "uuid": "pr-1", "sessionId": session_id,
         "prUrl": "https://github.com/example-org/webapp/pull/42",
         "prRepository": "example-org/webapp"},
        {"type": "worktree-state", "uuid": "wt-1", "sessionId": session_id,
         "worktreeSession": {
             "originalCwd": f"{HOME}/code/webapp",
             "worktreePath": f"{HOME}/code/webapp/.claude/worktrees/scroll-fix",
             "worktreeBranch": "worktree-scroll-fix",
             "originalBranch": "main",
             "originalHeadCommit": "0123456789abcdef0123456789abcdef01234567",
             "sessionId": session_id,
         }},
    ]


# --------------------------------------------------------------------------- #
# Top-level generation                                                        #
# --------------------------------------------------------------------------- #
def generate(out_path: Path, target_bytes: int, *, scale: int = 1, seed: int = SEED) -> dict:
    """Write a synthetic transcript to ``out_path`` until it exceeds
    ``target_bytes``. Returns a small stats dict (records, bytes, cycles)."""
    rng = random.Random(seed)
    base_dt = datetime(2026, 6, 8, 21, 0, 0, tzinfo=timezone.utc)
    sessions = ["1a2b3c4d-0000-4000-8000-000000000001",
                "1a2b3c4d-0000-4000-8000-000000000002"]
    out_path.parent.mkdir(parents=True, exist_ok=True)

    written = 0
    records = 0
    c = 0
    with open(out_path, "w", encoding="utf-8") as fh:
        # Seed meta records (so meta record-types + ai-title/worktree exist).
        for rec in _meta_records(rng, sessions[0], base_dt):
            line = json.dumps(rec, ensure_ascii=False) + "\n"
            fh.write(line)
            written += len(line.encode("utf-8"))
            records += 1
        while written < target_bytes:
            session_id = sessions[(c // 40) % len(sessions)]   # >=2 sessionIds
            sidechain = (c % 11 == 5)                          # some sidechain records
            plant_keyword = (c % 17 == 3)                      # rare keyword in a few turns
            for rec in _cycle(rng, c, session_id, base_dt, sidechain, scale, plant_keyword):
                line = json.dumps(rec, ensure_ascii=False) + "\n"
                fh.write(line)
                written += len(line.encode("utf-8"))
                records += 1
            c += 1
    return {"path": str(out_path), "bytes": out_path.stat().st_size,
            "records": records, "cycles": c}


def generate_mid(out_dir: Path = HERE) -> dict:
    return generate(out_dir / "sample-mid.jsonl", MID_TARGET_BYTES, scale=1)


def generate_large(out_dir: Path = HERE) -> dict:
    # High content-scale keeps the record COUNT modest (~2.5k, like a real large
    # session) while the file clears 50 MB — so redaction_map's retained locators
    # stay well under the test's 120 MB bound (peak grows with span COUNT, which
    # tracks record count, not per-record text length).
    return generate(out_dir / "sample-large.jsonl", LARGE_TARGET_BYTES, scale=120)


def ensure(out_dir: Path = HERE, *, large: bool = True) -> dict:
    """Generate any missing fixture (idempotent — used by conftest + `make fixtures`)."""
    out: dict = {}
    mid = out_dir / "sample-mid.jsonl"
    if not mid.exists():
        out["mid"] = generate_mid(out_dir)
    if large:
        lg = out_dir / "sample-large.jsonl"
        if not lg.exists():
            out["large"] = generate_large(out_dir)
    return out


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    which = argv[0] if argv else "all"
    out_dir = HERE
    if which in ("mid", "all"):
        print(json.dumps(generate_mid(out_dir)))
    if which in ("large", "all"):
        print(json.dumps(generate_large(out_dir)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
