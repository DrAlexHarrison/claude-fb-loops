# Cowork edge — `cowork.py`

The Cowork surface adapter. Claude Desktop now bundles Cowork, which writes a
local-agent-mode session as `audit.jsonl` (snake_case records: `{_audit_timestamp,
message:{content,role}, parent_tool_use_id, session_id, type, uuid}`, bwrap-sandboxed).
`cowork.py` reads that shape, locates and strips its blocks, and runs the same
deterministic floor + genericize verifier the other surfaces use.

## Run it

```bash
make demo-cowork          # locate the categories in the bundled audit.jsonl fixture
# or directly:
python -m fb_assist.cowork {find|map|redact|feedback}
```

All four subcommands default to a built-in synthetic fixture, so they run offline with
no input. `redact` prints the gate verdict over the sanitized slice; `map` prints the
category locators.

## What's proven vs. open

- **Proven:** the strip handles the audit shape — a Claude Code strip leaves tool output
  in place, a Cowork strip removes it (the shapes differ, and the adapter accounts for it).
- **Open (empirical):** whether the bundled `claude`'s `/feedback` gather root includes
  `~/Library/Application Support/Claude/local-agent-mode-sessions/**/audit.jsonl` — that
  pins whether swap-restore reaches the Cowork slice end-to-end. The intake wire itself is
  undocumented (a closed door), so the Cowork→Anthropic adapter is a reference against the
  `coworkFeedback` / `FeedbackWindow` shape we *can* inspect locally, not a deployed path.
  See [`../GAPS.md`](../GAPS.md) (H6, E2/E3) and MCPB cross-platform packaging (C1/C2).
