"""fb_assist.locate — self-locator: which on-disk ``.jsonl`` is *this* session?

Claude Code never tells a running tool "you are file X" — it just writes
``~/.claude*/projects/<cwd-slug>/<sessionId>.jsonl`` per turn. This module
answers, read-only: which file is the live session, and what past sessions sit
alongside it (a closed one is the safe feedback target).

``is_live`` is identity-first — a stem matching the live session id IS live even
between turns (see :func:`resolve`, :func:`_is_live`). stdlib-only, local-only:
reads/stats only, never writes or egresses.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Union

from .package import is_being_written
from .transcripts import default_roots, find_transcripts, project_slug

__all__ = [
    "slugify_cwd",
    "config_dir",
    "live_session_id",
    "resolve",
    "main",
]

PathLike = Union[str, os.PathLike]

# The canonical primary config dir's account label. Every other label is DERIVED
# from the dir's own ``-<suffix>`` (never a hardcoded user-specific name), so the
# source carries no personal account names — matters for the public mirror.
_WORK_CONFIG_DIRNAME = ".claude"
_ACCOUNT_PREFIX = ".claude-"


# --------------------------------------------------------------------------- #
# Small pure helpers (the capabilities the MCP server wraps directly)          #
# --------------------------------------------------------------------------- #
def slugify_cwd(cwd: PathLike) -> str:
    """Slugify a working directory the way Claude Code names its project dir:
    every non-``[A-Za-z0-9-]`` char becomes ``-`` (portable across Linux/macOS/
    Windows). Single source of truth: :func:`transcripts.project_slug`."""
    return project_slug(cwd)


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
    """The ``projects`` parent dirs to scan — delegated to the single source of truth.

    :func:`transcripts.default_roots` discovers them generically: ``$CLAUDE_CONFIG_DIR``
    if set, else every ``~/.claude*`` config dir that has a ``projects/`` subdir. No
    account name is hardcoded here (public-mirror hygiene)."""
    return list(default_roots())


def _account(config_dir_path: PathLike) -> str:
    """Derive a config dir's account label from its NAME, never a hardcoded map.

    ``.claude`` → ``"work"``; any ``.claude-<suffix>`` → that ``<suffix>`` (so a
    multi-account layout keeps a distinct, correct label without baking any user's
    account name into the source); anything else → ``"custom"``."""
    name = Path(config_dir_path).name
    if name == _WORK_CONFIG_DIRNAME:
        return "work"
    if name.startswith(_ACCOUNT_PREFIX) and len(name) > len(_ACCOUNT_PREFIX):
        return name[len(_ACCOUNT_PREFIX):]
    return "custom"


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
    """``is_live`` with identity FIRST, heuristic only as a fallback.

    If we have a live id and this file's stem equals it, it IS the live session —
    regardless of what the per-turn settle heuristic says. Only when there is no
    id to match (the newest-file fallback) do we consult ``is_being_written``."""
    if path is None:
        return False
    if live_id and Path(path).stem == live_id:
        return True
    return bool(is_being_written(path))


def _candidates_in(project_dir: PathLike) -> list[dict]:
    """The newest-first transcript menu for ONE project dir, identity-anchored.

    Used when the caller didn't tell us the cwd: we anchor on the resolved
    session's own ``project_dir`` and list its siblings, rather than slugging a
    cwd we don't trust (the always-on MCP server's process cwd is NOT the user's
    project — see :func:`resolve`)."""
    return find_transcripts(project_dir=project_dir)


def _recorded_cwd(path: PathLike, *, max_lines: int = 8) -> str | None:
    """The user's real cwd as recorded in the transcript envelope.

    The authoritative cwd signal when the caller didn't pass one: every record
    Claude Code writes carries the session's ``cwd``. We read only the first few
    lines (cheap) and return the first non-empty ``cwd`` found, else ``None``."""
    try:
        with open(os.fspath(path), "r", encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                cwd = rec.get("cwd") if isinstance(rec, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


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

    The contract for ``cwd`` (server-vs-user cwd)
    ----------------------------------------------
    This module is wrapped by an **always-on stdio MCP server** whose process cwd
    is its *spawn* dir (the repo / the home project), NOT the user's session cwd.
    So ``os.getcwd()`` is the WRONG project for the menu. Callers must therefore
    pass either an explicit ``cwd`` (the user's project) OR a ``session_id`` (incl.
    ``$CLAUDE_CODE_SESSION_ID``) we can anchor on. The returned ``cwd_source`` tells
    the caller where the menu came from so it can refuse to act on a guess:

      * ``"explicit"`` — ``cwd`` was passed; menu is that project's slug (unchanged).
      * ``"identity"`` — ``cwd`` was None but a session resolved by id; the menu is
        that session's OWN ``project_dir`` (correct regardless of the process cwd).
      * ``"process"`` — ``cwd`` was None and nothing could be anchored on; we fall
        back to ``os.getcwd()`` (right for the *library CLI*, where the user runs in
        their project) but FLAG it, because for the MCP server it is untrustworthy.
    """
    root_paths = [Path(r) for r in (roots if roots is not None else _default_roots())]

    env_id = live_session_id()
    live_id = env_id or session_id      # identity for is_live; env wins
    wanted = session_id or env_id       # which id to resolve to a file (a)/(b)

    if cwd is not None:
        # Explicit cwd (skill passed it / hermetic test / CLI --cwd): the menu is
        # everything under this cwd's slug, newest-first — behavior unchanged.
        cwd_source = "explicit"
        cwd_out: str | None = str(cwd)
        candidates = find_transcripts(cwd=cwd, roots=root_paths)
        if wanted:
            chosen = _pick_by_id(candidates, wanted)
            if chosen is None:
                f = _scan_for_session_file(wanted, root_paths)
                chosen = _entry(f) if f is not None else None
        else:
            chosen = candidates[0] if candidates else None  # (c) newest-first
    else:
        # cwd UNKNOWN. Anchor on IDENTITY: resolve the session by id roots-wide and
        # build the menu from ITS project dir — never the process cwd's slug, which
        # for the always-on server silently targets the wrong project.
        chosen = None
        if wanted:
            f = _scan_for_session_file(wanted, root_paths)
            chosen = _entry(f) if f is not None else None
        if chosen is not None:
            cwd_source = "identity"
            candidates = _candidates_in(chosen["project_dir"])
            cwd_out = _recorded_cwd(chosen["path"])
        else:
            # Nothing to anchor on. Fall back to the process cwd (correct for the
            # library CLI) but FLAG it so MCP callers fail loud instead of guessing.
            cwd_source = "process"
            cwd_out = os.getcwd()
            candidates = find_transcripts(cwd=cwd_out, roots=root_paths)
            chosen = candidates[0] if candidates else None

    path = chosen["path"] if chosen else None
    project_dir = chosen["project_dir"] if chosen else None
    cfg = _config_dir_for(project_dir) if project_dir else config_dir()
    slug = slugify_cwd(cwd_out) if cwd_out else (Path(project_dir).name if project_dir else None)

    return {
        "session_id": chosen["session_id"] if chosen else live_id,
        "path": path,
        "project_dir": project_dir,
        "config_dir": str(cfg),
        "account": _account(cfg),
        "is_live": _is_live(path, live_id),
        "candidates": candidates,
        # extras — cheap, harmless, save the MCP server a round-trip
        "cwd": cwd_out,
        "slug": slug,
        "live_session_id": env_id,
        "cwd_source": cwd_source,
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
