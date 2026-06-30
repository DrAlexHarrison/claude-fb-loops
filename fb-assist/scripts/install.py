#!/usr/bin/env python3
"""Activate fb-assist for Claude Code — one command instead of seven manual steps.

Installs the `/fb` skill into your Claude config dir AND registers the `fb-assist` MCP
server in `~/.claude.json`, computing its own paths so there is nothing to hand-edit.
Idempotent: safe to re-run (it overwrites only the `fb-assist` entries it owns and backs
up `~/.claude.json` first). Restart Claude Code afterward for `/fb` to appear.

    python scripts/install.py            # install skill + register MCP server
    python scripts/install.py --print-hooks   # also print the optional watcher hooks
    python scripts/install.py --uninstall      # remove what this installed

Honors $CLAUDE_CONFIG_DIR (else ~/.claude). Local only; touches just your Claude config.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]          # the fb-assist/ package root
SKILL_SRC = REPO / "skill" / "fb"
SERVER_NAME = "fb-assist"


def config_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")).expanduser()


def config_json() -> Path:
    """The global config file Claude Code reads ``mcpServers`` from.

    Claude Code's rule (verified empirically across accounts): when
    ``$CLAUDE_CONFIG_DIR`` is set it reads ``$CLAUDE_CONFIG_DIR/.claude.json``;
    when unset (the normal single-account case) it reads ``~/.claude.json``.
    Honoring it here means a multi-account user (``CLAUDE_CONFIG_DIR`` set per
    account) registers the server in the account they ran the installer from —
    and a single-account machine is byte-identical to before (still
    ``~/.claude.json``)."""
    cd = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(cd).expanduser() / ".claude.json") if cd else (Path.home() / ".claude.json")


def _interpreter() -> str:
    """Prefer the repo's venv (it has the NER stack); else the interpreter running us."""
    venv = REPO / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def install_skill() -> Path:
    dst = config_dir() / "skills" / "fb"
    dst.mkdir(parents=True, exist_ok=True)
    for f in SKILL_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, dst / f.name)
    return dst


def _load_claude_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def register_mcp() -> Path:
    path = config_json()
    data = _load_claude_json(path)
    if path.exists():
        shutil.copy2(path, path.with_suffix(".json.fb-assist.bak"))
    servers = data.setdefault("mcpServers", {})
    servers[SERVER_NAME] = {
        "type": "stdio",
        "command": _interpreter(),
        "args": ["-m", "fb_assist.mcp_server"],
        # PYTHONPATH so `-m fb_assist.mcp_server` resolves without an editable install.
        "env": {"USE_TF": "0", "USE_FLAX": "0", "TOKENIZERS_PARALLELISM": "false",
                "PYTHONPATH": str(REPO)},
    }
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def uninstall() -> None:
    skill = config_dir() / "skills" / "fb"
    if skill.is_dir():
        shutil.rmtree(skill, ignore_errors=True)
        print(f"  removed skill: {skill}")
    path = config_json()
    data = _load_claude_json(path)
    if data.get("mcpServers", {}).pop(SERVER_NAME, None) is not None:
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"  unregistered MCP server: {SERVER_NAME}")
    print("Uninstalled. Restart Claude Code.")


HOOKS_SNIPPET = """\
Optional — the proactive watcher (offers /fb when it senses a frustration/delight moment).
Add to your Claude settings.json "hooks" (see fb-assist/RUNTIME.md for the exact handler):
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
     "command": "%s -m fb_assist.watcher hook" }] }]
""" % _interpreter()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="fb-assist-install", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--print-hooks", action="store_true", help="also print the optional watcher hook snippet")
    ap.add_argument("--uninstall", action="store_true", help="remove the skill + MCP registration")
    args = ap.parse_args(argv)

    if args.uninstall:
        uninstall()
        return 0

    skill = install_skill()
    cfg = register_mcp()
    print("✅ fb-assist activated.")
    print(f"  • /fb skill   → {skill}")
    print(f"  • MCP server  → registered as '{SERVER_NAME}' in {cfg} (backup alongside)")
    print(f"  • interpreter → {_interpreter()}")
    if args.print_hooks:
        print("\n" + HOOKS_SNIPPET)
    print("\nRestart Claude Code, then type /fb in any session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
