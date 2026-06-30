# fb-assist runtime — quickstart

`/fb` is an **in-session** feedback co-author for Claude Code (same window, multi-turn, like `/interview`). Invoking it loads the co-author "how to be" and gives the live Claude a privacy toolbox (the `fb-assist` MCP server) so it can redact secrets / PII / company-IP out of your transcript and ship clean feedback through Anthropic's **real** `/feedback` intake — **non-destructively** (your originals come back byte-exact, even across a crash).

## What you get
- **`/fb` skill** — `~/.claude/skills/fb/SKILL.md` (+ `co-author.md`, the brain). Mirrored in the repo at `fb-assist/skill/fb/`.
- **`fb-assist` MCP server** — `fb_assist/mcp_server.py`, 20 model-invocable tools (`mcp__fb-assist__*`): locate, see, detect, redact, genericize-verify, distill, assemble, preview, the two-layer leak gate, the two-phase submit handoff, the set-once profile, and the open-questions reader.
- **watcher hook** — `fb_assist/watcher.py`, offers a one-tap `/fb` on high-precision frustration/delight moments.

## Install (clean clone)
```bash
git clone <repo> && cd fb-assist
python -m venv .venv && . .venv/bin/activate
pip install -e .[all]                 # presidio, gliner, detect-secrets, mcp, faster-whisper, anthropic
python -m spacy download en_core_web_sm
# gitleaks is a Go binary (optional but recommended — the secret floor uses it if present):
#   https://github.com/gitleaks/gitleaks  →  put on PATH (e.g. ~/.local/bin/gitleaks)
USE_TF=0 python -m pytest -q            # expect all green
```
> **Note (GLiNER / TensorFlow):** export `USE_TF=0` before running anything — transformers' TF path breaks under Keras 3. The modules set it defensively at import, and CI exports it too.

## Register the MCP server (one entry in `~/.claude.json`)
Add under the project (or top-level `mcpServers` for global / always-on):
```jsonc
"fb-assist": {
  "type": "stdio",
  "command": "/abs/path/to/fb-assist/.venv/bin/python",
  "args": ["-m", "fb_assist.mcp_server"],
  "env": { "USE_TF": "0", "USE_FLAX": "0", "TOKENIZERS_PARALLELISM": "false" }
}
```
**Scope:** project-scoped to the repo dir → `/fb` works when you're in this repo (no context bloat elsewhere). Move it to `mcpServers` (top-level) or the `/home/<you>` project for **always-on** `/fb` in every session. Cheap to change.

## Wire the watcher hook (optional — `~/.claude/settings.json`)
```jsonc
"hooks": {
  "UserPromptSubmit": [{ "hooks": [{ "type": "command",
    "command": "/abs/path/to/fb-assist/.venv/bin/python -m fb_assist.watcher UserPromptSubmit" }] }],
  "PostToolUse":      [{ "hooks": [{ "type": "command",
    "command": "/abs/path/to/fb-assist/.venv/bin/python -m fb_assist.watcher PostToolUse" }] }],
  "SessionEnd":       [{ "hooks": [{ "type": "command",
    "command": "/abs/path/to/fb-assist/.venv/bin/python -m fb_assist.watcher SessionEnd" }] }]
}
```
Disable anytime: `touch ~/.config/fb-assist/watcher.off`.

## Use it
In any Claude Code session: **`/fb`** (or `/fb express` for a fast hard-send, or `/fb <what you hit>`). The co-author reads first, proposes back, redacts to the level you want, shows the concise "included / stripped" gate, and on your OK stages the sanitized bytes and tells you the exact `/feedback` + scope to run. After you submit and say "done", it restores your originals byte-exact.

## The non-destructive handoff (why it's safe)
`/feedback` reads your **on-disk** transcript at submit time. fb-assist:
1. backs up the real file + writes a durable journal (fsync'd) **before** touching anything,
2. atomically swaps in the sanitized bytes (`submit_begin`),
3. you run `/feedback` (a separate turn) — it reads the sanitized file,
4. `submit_finish` restores the original byte-exact (sha256-verified).

If the process is killed between 2 and 4, `/fb`'s startup `recover_orphans()` (or `python -m fb_assist.package recover`) restores it. **Your resumable history is never degraded** — that's the cardinal rule.

## Library CLIs (no MCP needed)
```bash
USE_TF=0 python -m fb_assist.transcripts ...   # parse / extract / scope / redaction-map / size
USE_TF=0 python -m fb_assist.redact ...         # detect / strip / leak-scan
python -m fb_assist.package recover             # heal an orphaned swap
python -m fb_assist.locate resolve              # which session am I?
python -m fb_assist.profile resolve --cwd .     # effective privacy policy
```
