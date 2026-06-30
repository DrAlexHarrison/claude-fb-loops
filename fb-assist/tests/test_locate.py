"""Tests for fb_assist.locate — the self-locator.

Fully hermetic: every test builds a fake ``projects/<slug>/`` tree under
``tmp_path`` and points resolution at it via ``$CLAUDE_CONFIG_DIR`` and/or an
explicit ``roots=`` argument. The real ``~/.claude*`` dirs are never read, and
``locate.is_being_written`` is monkeypatched so no test ever does the 0.15 s
settle-sample (or depends on a live writer).

Coverage:
  * explicit session_id wins over the newest file;
  * ``$CLAUDE_CODE_SESSION_ID`` is used when no id is passed;
  * newest-first fallback when neither is present;
  * ``is_live`` via session-id IDENTITY even when ``is_being_written`` is False
    — identity short-circuits the heuristic entirely;
  * account derivation (work / a named account suffix / custom);
  * graceful empty result when the dir doesn't exist.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

# Make the package importable when run directly (pytest also handles this).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import locate as L  # noqa: E402

CWD = "/home/devuser/code/secret-proj"
SLUG = CWD.replace("/", "-")


# --------------------------------------------------------------------------- #
# Builders                                                                     #
# --------------------------------------------------------------------------- #
def _write_session(proj_dir: Path, session_id: str, *, mtime: float, n: int = 2) -> Path:
    """Write a schema-faithful ``<session_id>.jsonl`` and stamp its mtime."""
    proj_dir.mkdir(parents=True, exist_ok=True)
    p = proj_dir / f"{session_id}.jsonl"
    lines = [
        json.dumps({
            "type": "user",
            "uuid": f"{session_id}-u{i}",
            "sessionId": session_id,
            "cwd": CWD,
            "message": {"role": "user", "content": f"line {i}"},
        })
        for i in range(n)
    ]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    import os
    os.utime(p, (mtime, mtime))
    return p


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A ``.claude`` config dir with two sessions under the cwd slug.

    ``sess-A`` is older, ``sess-B`` is newer. Returns a namespace of the paths and
    the projects root. ``is_being_written`` defaults to False (overridable per
    test); ``$CLAUDE_CODE_SESSION_ID`` is cleared.
    """
    cfg = tmp_path / ".claude"
    proj = cfg / "projects" / SLUG
    now = time.time()
    a = _write_session(proj, "sess-A", mtime=now - 100)
    b = _write_session(proj, "sess-B", mtime=now)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: False)

    ns = type("Env", (), {})()
    ns.cfg, ns.proj, ns.roots = cfg, proj, [cfg / "projects"]
    ns.a, ns.b = a, b
    return ns


# --------------------------------------------------------------------------- #
# pure helpers                                                                 #
# --------------------------------------------------------------------------- #
def test_slugify_cwd_mirrors_transcripts():
    assert L.slugify_cwd("/home/devuser/code/x") == "-home-devuser-code-x"
    assert L.slugify_cwd(Path("/a/b")) == "-a-b"


def test_slugify_cwd_is_portable():
    """Claude Code replaces EVERY non-[A-Za-z0-9-] char with '-' (verified against
    real on-disk dirs). The portable rule fixes both a Linux miss on dotted/worktree
    paths and the whole of native-Windows path slugging."""
    # Linux worktree path: '/.claude/' → '--claude-' (the leading '/.' collapses).
    assert (L.slugify_cwd("/home/devuser/code/contoso/.claude/worktrees/x")
            == "-home-devuser-code-contoso--claude-worktrees-x")
    # Underscores and dots in a repo name also collapse.
    assert L.slugify_cwd("/home/u/my_repo.v2") == "-home-u-my-repo-v2"
    # Native Windows: backslashes AND the drive colon become '-' (double at 'C:').
    assert L.slugify_cwd(r"C:\Users\dana\code\proj") == "C--Users-dana-code-proj"


def test_resolve_windows_layout(tmp_path, monkeypatch):
    """End-to-end resolve over a simulated NATIVE-WINDOWS on-disk layout
    (%USERPROFILE%\\.claude\\projects\\<win-slug>\\<sid>.jsonl), reproduced on this
    host as plain directory names. Proves the Windows slug resolves a real session."""
    win_cwd = r"C:\Users\dana\code\proj"
    slug = L.slugify_cwd(win_cwd)
    assert slug == "C--Users-dana-code-proj"
    cfg = tmp_path / ".claude"
    proj = cfg / "projects" / slug
    now = time.time()
    sess = _write_session(proj, "win-sess-1", mtime=now)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: False)

    r = L.resolve(cwd=win_cwd, roots=[cfg / "projects"])
    assert r["path"] == str(sess)
    assert r["session_id"] == "win-sess-1"
    assert r["slug"] == slug


def test_live_session_id_reads_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert L.live_session_id() is None
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    assert L.live_session_id() is None  # empty string treated as unset
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-live")
    assert L.live_session_id() == "sess-live"


def test_config_dir_honors_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "myconf"))
    assert L.config_dir() == tmp_path / "myconf"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert L.config_dir() == tmp_path / ".claude"


# --------------------------------------------------------------------------- #
# resolve — resolution strategy                                                #
# --------------------------------------------------------------------------- #
def test_explicit_session_id_wins_over_newest(env):
    r = L.resolve(cwd=CWD, session_id="sess-A", roots=env.roots)
    assert r["session_id"] == "sess-A"
    assert r["path"] == str(env.a)
    # candidates is the full newest-first menu (B newer than A), regardless of pick
    assert [c["session_id"] for c in r["candidates"]] == ["sess-B", "sess-A"]


def test_env_session_id_used_when_no_explicit(env, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-A")
    r = L.resolve(cwd=CWD, roots=env.roots)
    assert r["session_id"] == "sess-A"        # env id, not the newer sess-B
    assert r["path"] == str(env.a)
    assert r["is_live"] is True               # identity: stem == env id
    assert r["live_session_id"] == "sess-A"


def test_newest_first_fallback(env):
    r = L.resolve(cwd=CWD, roots=env.roots)
    assert r["session_id"] == "sess-B"        # newest wins
    assert r["path"] == str(env.b)


def test_fallback_is_live_uses_heuristic(env, monkeypatch):
    # No explicit/env id => no identity to match => is_live falls to the heuristic.
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: False)
    assert L.resolve(cwd=CWD, roots=env.roots)["is_live"] is False
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: True)
    assert L.resolve(cwd=CWD, roots=env.roots)["is_live"] is True


# --------------------------------------------------------------------------- #
# Identity beats the heuristic                                                 #
# --------------------------------------------------------------------------- #
def test_is_live_identity_beats_false_heuristic(env, monkeypatch):
    # The crux: is_being_written says False (per-turn false-negative), yet the
    # session-id IDENTITY says this is the live file. Identity must win — and it
    # must short-circuit so the heuristic is never even consulted.
    def _boom(*a, **k):
        raise AssertionError("is_being_written must NOT be called when identity matches")

    monkeypatch.setattr(L, "is_being_written", _boom)
    r = L.resolve(cwd=CWD, session_id="sess-A", roots=env.roots)
    assert r["is_live"] is True
    assert r["path"] == str(env.a)


# --------------------------------------------------------------------------- #
# account derivation                                                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "dirname, expected",
    [
        (".claude", "work"),
        (".claude-acme", "acme"),
        (".claude-beta", "beta"),
        ("claude-nodot", "custom"),
    ],
)
def test_account_derivation(tmp_path, monkeypatch, dirname, expected):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: False)
    # Nest each account under its own parent so basenames don't collide in tmp.
    cfg = tmp_path / f"acct-{expected}" / dirname
    proj = cfg / "projects" / SLUG
    _write_session(proj, "sess-X", mtime=time.time())

    r = L.resolve(cwd=CWD, session_id="sess-X", roots=[cfg / "projects"])
    assert r["account"] == expected
    assert r["config_dir"] == str(cfg)
    assert r["project_dir"] == str(proj)


# --------------------------------------------------------------------------- #
# roots-wide fallback + graceful empties                                       #
# --------------------------------------------------------------------------- #
def test_explicit_id_found_under_other_slug(env):
    # A session that lives under a DIFFERENT project slug is still resolvable by
    # its (globally-unique) id, even though it's absent from the cwd candidates.
    other = env.cfg / "projects" / "-home-devuser-code-other"
    p = _write_session(other, "sess-Z", mtime=time.time())
    r = L.resolve(cwd=CWD, session_id="sess-Z", roots=env.roots)
    assert r["path"] == str(p)
    assert r["project_dir"] == str(other)
    assert r["is_live"] is True  # identity
    assert "sess-Z" not in [c["session_id"] for c in r["candidates"]]


def test_graceful_empty_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(L, "is_being_written", lambda *a, **k: False)
    missing = tmp_path / "nope" / ".claude" / "projects"  # never created
    r = L.resolve(cwd=CWD, roots=[missing])
    assert r["path"] is None
    assert r["candidates"] == []
    assert r["is_live"] is False
    assert r["project_dir"] is None


def test_graceful_empty_unknown_session_id(env):
    # Explicit id that exists nowhere => path None, but the cwd menu still returns.
    r = L.resolve(cwd=CWD, session_id="sess-does-not-exist", roots=env.roots)
    assert r["path"] is None
    assert [c["session_id"] for c in r["candidates"]] == ["sess-B", "sess-A"]


# --------------------------------------------------------------------------- #
# CLI smoke                                                                    #
# --------------------------------------------------------------------------- #
def test_cli_resolve_emits_json(env, capsys):
    rc = L.main(["resolve", "--cwd", CWD, "--session-id", "sess-A"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["session_id"] == "sess-A"
    assert out["path"] == str(env.a)


def test_cli_live_and_config_dir(env, capsys, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-live")
    assert L.main(["live"]) == 0
    assert capsys.readouterr().out.strip() == "sess-live"
    assert L.main(["config-dir"]) == 0
    assert capsys.readouterr().out.strip() == str(env.cfg)
