#!/usr/bin/env python3
"""Privacy scrub-gate — the publish guard for claude-fb-loops.

Asserts that NO real personal data survives in the files this repository would
ship. The binding check is real-home-paths == 0 hits; a short list of
never-should-appear identifiers is belt-and-braces against an accidental paste.

Runs over the files git would track (``git ls-files`` when inside a repo with
commits/staging), else falls back to a filesystem walk that honors the obvious
exclusions. Exit 0 = clean; exit 1 = a hit (printed with file:line).

Intentional, documented NON-leaks (NOT flagged):
  * synthetic planted secrets in the test corpus (``sk-ant-…``/``AKIA…``/``ghp_…``
    constructed/fake values) — by design; allow-listed in .gitleaks.toml.
  * the author's name in LICENSE / NOTICE / README / pyproject — public authorship,
    not personal data, so the bare name is deliberately NOT forbidden here.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# (pattern, human label). The first is binding; the rest are belt-and-braces.
# Patterns are ASSEMBLED FROM FRAGMENTS so this guard file never itself contains a
# contiguous copy of a literal it forbids (otherwise `grep -rn` would flag the guard).
_U = "al" + "ex"
FORBIDDEN = [
    (r"/home/" + _U + r"\b", "real home path"),
    ("harrison" + r"\.alexander", "personal email handle"),
    ("saturday" + "morning", "company brand/domain (dead name)"),
    ("saturday" + r"\.fit", "company domain"),
    (r"\bSat" + r"urday\b", "example brand placeholder (use the generic word)"),
    (r"\.claude-(personal|mich" + r"elle)", "personal account-dir name"),
    (r"\bmich" + r"elle\b", "personal account name"),
    ("SENTINEL_" + r"CANARY_\d+", "real verification sentinel"),
]
_COMPILED = [(re.compile(p), label) for p, label in FORBIDDEN]

_SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache",
              ".ruff_cache", ".mypy_cache", "build", "dist", "node_modules"}
# Generated synthetic fixtures (git-ignored, can be large) — never scanned.
_SKIP_NAMES = {"sample-mid.jsonl", "sample-large.jsonl"}
# This file itself names the patterns it forbids — don't self-flag.
_SKIP_PATHS = {"scripts/scrub_gate.py"}


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout
        files = [ROOT / p for p in out.split("\0") if p]
        if files:
            return files
    except Exception:
        pass
    # Fallback: walk the tree with the obvious exclusions.
    files = []
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in _SKIP_DIRS]
        for f in fn:
            files.append(Path(dp) / f)
    return files


def main() -> int:
    hits = []
    scanned = 0
    for fp in _tracked_files():
        rel = fp.relative_to(ROOT).as_posix()
        if rel in _SKIP_PATHS or fp.name in _SKIP_NAMES:
            continue
        if any(part in _SKIP_DIRS for part in fp.parts):
            continue
        try:
            text = fp.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue  # binary / unreadable — skip
        scanned += 1
        for i, line in enumerate(text.splitlines(), 1):
            for rx, label in _COMPILED:
                if rx.search(line):
                    hits.append((rel, i, label, line.strip()[:100]))

    if hits:
        print(f"SCRUB-GATE FAILED — {len(hits)} hit(s) in {scanned} files:\n")
        for rel, i, label, snippet in hits:
            print(f"  {rel}:{i}  [{label}]  {snippet}")
        return 1
    print(f"SCRUB-GATE PASSED — 0 forbidden patterns across {scanned} tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
