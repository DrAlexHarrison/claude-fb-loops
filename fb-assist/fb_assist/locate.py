"""fb_assist.locate — self-locator: which on-disk ``.jsonl`` is *this* session?

The redaction/swap machinery (:mod:`fb_assist.package`) can only sanitize a
transcript it can point at on disk. But Claude Code never tells a running tool
"you are file X" — it just writes
``~/.claude*/projects/<cwd-slug>/<sessionId>.jsonl`` per turn. So before anything
can be co-authored or swapped, *something* has to answer two questions for the
MCP server that wraps this toolbox:

  1. Which file is the session I am running inside right now? (the live target)
  2. What other past sessions sit alongside it? (so the co-author can pick a
     *closed* session to give feedback about — the safe, deterministic target,
     per spec §15.)

This module answers both, read-only, and hands back a plain dict the MCP server
serializes straight to JSON (mirroring how
:func:`transcripts.find_transcripts` / :func:`transcripts.redaction_map` already
return dicts).

Why identity beats the heuristic (FIX-3)
----------------------------------------
:func:`package.is_being_written` detects an active writer, but Claude Code writes
the live transcript only *per turn* — so *between* turns the live file looks idle
and the heuristic false-negatives. The load-bearing signal for "this is the live
file" is therefore **identity**: the file whose stem equals
``$CLAUDE_CODE_SESSION_ID`` (or the session id the caller passed) IS the live
session, settle-sampling notwithstanding. :func:`resolve` sets ``is_live`` by
checking identity *first* and only falls back to the heuristic when there is no
id to match against (the newest-file fallback).

Resolution order (:func:`resolve`)
----------------------------------
An explicit ``session_id`` wins; else ``$CLAUDE_CODE_SESSION_ID``; else the
newest transcript under the cwd's slug. ``account`` is derived from the
config-dir name (``.claude`` → work, ``.claude-personal`` → personal,
``.claude-michelle`` → michelle, else custom). ``$CLAUDE_CONFIG_DIR`` overrides
the config dir. ``resolve`` never raises on "nothing found" — it returns
``path=None`` with an empty ``candidates`` list.

stdlib-only. Local only — it reads/stats, never writes, never egresses. The
discovery + live-write primitives are reused from the sibling modules
(:func:`transcripts.find_transcripts`, :func:`package.is_being_written`) rather
than reimplemented.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Union

from .package import is_being_written
from .transcripts import find_transcripts

__all__ = [
    "slugify_cwd",
    "config_dir",
    "live_session_id",
    "resolve",
    "main",
]

PathLike = Union[str, os.PathLike]

# config-dir basename -> billing account label (mirrors the triple-account layout).
_ACCOUNTS = {
    ".claude": "work",
    ".claude-personal": "personal",
    ".claude-michelle": "michelle",
}


# --------------------------------------------------------------------------- #
# Small pure helpers (the capabilities the MCP server wraps directly)          #
# --------------------------------------------------------------------------- #
def slugify_cwd(cwd: PathLike) -> str:
    """Slugify a working directory the way Claude Code names its project dir:
    every ``/`` becomes ``-`` (mirrors ``transcripts.find_transcripts``)."""
    return str(cwd).replace("/", "-")


def config_dir() -> Path:
    """The active Claude Code config dir — ``$CLAUDE_CONFIG_DIR`` if set, else
    ``~/.claude``. This is the dir whose ``projects/`` holds the transcripts."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg).expanduser()
    return Path.home() / ".claude"


def live_session_id() -> str | None:
    """The live session id from ``$CLAUDE_CODE_SESSION_ID`` (None if unset/empty).

    When Claude Code exports this, it is the *identity* of the file being written
    this very session — the primary ``is_live`` signal (see module docstring)."""
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or None


def _default_roots() -> list[Path]:
    """The ``projects`` parent dirs to scan. With ``$CLAUDE_CONFIG_DIR`` set, only
    that account's ``projects`` dir; otherwise the three standard accounts."""
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return [config_dir() / "projects"]
    home = Path.home()
    return [
        home / ".claude" / "projects",
        home / ".claude-personal" / "projects",
        home / ".claude-michelle" / "projects",
    ]


def _account(config_dir_path: PathLike) -> str:
    """Map a config dir to its account label (else ``"custom"``)."""
    return _ACCOUNTS.get(Path(config_dir_path).name, "custom")


def _config_dir_for(project_dir: PathLike) -> Path:
    """Recover the config dir from a ``<config>/projects/<slug>`` transcript dir."""
    return Path(project_dir).parent.parent


def _entry(path: PathLike) -> dict:
    """Build a ``find_transcripts``-shaped dict for a single on-disk transcript."""
    p = Path(path)
    st = p.stat()
    return {
        "path": str(p),
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, timezone.utc).isoformat(),
        "session_id": p.stem,
        "project_dir": str(p.parent),
    }


def _pick_by_id(candidates: list[dict], session_id: str) -> dict | None:
    for c in candidates:
        if c.get("session_id") == session_id:
            return c
    return None


def _scan_for_session_file(session_id: str, roots: Iterable[PathLike]) -> Path | None:
    """Fallback: locate ``<session_id>.jsonl`` under *any* slug across ``roots``.

    A session id is globally unique, so if the cwd-scoped lookup missed it (e.g.
    the caller passed a session from a different project), this still finds the
    real file. Cheap: one ``stat`` of an exact filename per project dir."""
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            continue
        for proj in root.iterdir():
            if not proj.is_dir():
                continue
            f = proj / f"{session_id}.jsonl"
            if f.is_file():
                return f
    return None


def _is_live(path: PathLike, live_id: str | None) -> bool:
    """``is_live`` with identity FIRST (FIX-3), heuristic only as a fallback.

    If we have a live id and this file's stem equals it, it IS the live session —
    regardless of what the per-turn settle heuristic says. Only when there is no
    id to match (the newest-file fallback) do we consult ``is_being_written``."""
    if path is None:
        return False
    if live_id and Path(path).stem == live_id:
        return True
    return bool(is_being_written(path))


# --------------------------------------------------------------------------- #
# resolve — the one call the MCP server makes                                  #
# --------------------------------------------------------------------------- #
def resolve(
    cwd: str | None = None,
    session_id: str | None = None,
    *,
    roots: Iterable[PathLike] | None = None,
) -> dict:
    """Resolve which transcript to act on (and what else is around it).

    Strategy, in priority order:
      (a) explicit ``session_id`` → the ``<session_id>.jsonl`` under ``cwd``'s
          slug (scanning ``roots`` / ``$CLAUDE_CONFIG_DIR``; a roots-wide fallback
          finds it even if it lives under a different slug);
      (b) else ``$CLAUDE_CODE_SESSION_ID`` resolved the same way;
      (c) else the newest transcript under ``cwd``'s slug.

    ``roots`` (each a ``.../projects`` dir) defaults to the active account(s); pass
    it explicitly to keep tests hermetic. ``cwd`` defaults to the process cwd.

    Returns a dict with at least::

        {session_id, path, project_dir, config_dir, account, is_live, candidates}

    ``candidates`` is the full newest-first list under the cwd slug, so the
    co-author can target a *past* session instead of the live one. Never raises:
    if nothing is found, ``path`` is None and ``candidates`` is ``[]``.
    """
    if cwd is None:
        cwd = os.getcwd()
    root_paths = [Path(r) for r in (roots if roots is not None else _default_roots())]

    # Everything under this cwd's slug, newest-first — the co-author's menu.
    candidates = find_transcripts(cwd=cwd, roots=root_paths)

    env_id = live_session_id()
    live_id = env_id or session_id      # identity for is_live; env wins (FIX-3)
    wanted = session_id or env_id       # which id to resolve to a file (a)/(b)

    if wanted:
        chosen = _pick_by_id(candidates, wanted)
        if chosen is None:
            f = _scan_for_session_file(wanted, root_paths)
            chosen = _entry(f) if f is not None else None
    else:
        chosen = candidates[0] if candidates else None  # (c) newest-first

    path = chosen["path"] if chosen else None
    project_dir = chosen["project_dir"] if chosen else None
    cfg = _config_dir_for(project_dir) if project_dir else config_dir()

    return {
        "session_id": chosen["session_id"] if chosen else live_id,
        "path": path,
        "project_dir": project_dir,
        "config_dir": str(cfg),
        "account": _account(cfg),
        "is_live": _is_live(path, live_id),
        "candidates": candidates,
        # extras — cheap, harmless, save the MCP server a round-trip
        "cwd": str(cwd),
        "slug": slugify_cwd(cwd),
        "live_session_id": env_id,
    }


# --------------------------------------------------------------------------- #
# CLI (over the same functions, matching the sibling modules' pattern)         #
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="fb-locate",
        description="Locate the current (or a chosen) Claude Code session "
                    "transcript on disk. Read-only, local-only, stdlib-only.",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("resolve", help="resolve the live (or a chosen) session transcript")
    pr.add_argument("--cwd", help="working dir to resolve under (default: process cwd)")
    pr.add_argument("--session-id", help="resolve a specific session id instead of the live one")

    sub.add_parser("live", help="print $CLAUDE_CODE_SESSION_ID (the live session id)")
    sub.add_parser("config-dir", help="print the active config dir ($CLAUDE_CONFIG_DIR or ~/.claude)")

    args = ap.parse_args(argv)

    if args.cmd == "resolve":
        print(json.dumps(resolve(cwd=args.cwd, session_id=args.session_id), indent=2))
        return 0
    if args.cmd == "live":
        print(live_session_id() or "")
        return 0
    if args.cmd == "config-dir":
        print(str(config_dir()))
        return 0
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
