# fb-assist

The keystone package of [claude-fb-loops](../README.md): a privacy-preserving feedback
co-author for Claude. It reads a Claude Code session transcript, helps the user say the
bug precisely, redacts secrets / PII / company IP under the user's control, and ships
only the confirmed bundle through Anthropic's real `/feedback` intake — **non-destructively**
(swap a sanitized copy onto disk for the submit, then restore the original byte-for-byte).

See the [top-level README](../README.md) for the story, the empirical proof, and the
download-free demo (`make demo`).

## Modules

| Module | Role |
|---|---|
| `transcripts.py` | streaming parser + 12 category extractors + char-precise locators + `redaction_map` + `relevant_slice` + `size_estimate` |
| `redact.py` | secret detectors (regex / gitleaks / detect-secrets) + PII (Presidio / GLiNER) + `strip_categories` + `reversible_tokenize` + `leak_scan` |
| `package.py` | `assemble_payload` (<1 MB budget) + `diff_preview` (the gate) + crash-safe `swap_restore` / `recover` + two-phase `begin_swap` / `finish_swap` |
| `locate.py` | self-locator: which on-disk `.jsonl` is *this* session (identity-first) |
| `profile.py` | set-once privacy policy: precedence engine + hard floors + learn |
| `genericize.py` | re-identification guardrail for the genericize layer |
| `watcher.py` | frustration / delight signal hook |
| `mcp_server.py` | the in-session `mcp__fb-assist__*` tool server the `/fb` skill wields |
| `claude_repro.py` | the API-surface SDK: Messages request/response → redacted repro + `request-id` anchor |
| `desktop_chat.py` | claude.ai export co-pilot (genericize an exported `conversations.json`) |
| `server_side.py` | reference consent-genericize gate for referenced-not-inlined feedback |
| `cowork.py` | Cowork-surface adapter (locate/strip/redact the bundled `audit.jsonl` shape) |
| `reputation.py` | pseudonymous careful-filterer trust token (Ed25519; weights an opted-in contributor's signal) |

`prompts/co-author.md` is the co-author "how to be"; `skill/fb/` (here under `fb-assist/`)
is the shipped `/fb` skill + its mirror of that prompt; `voice/` is the optional
push-to-talk confirm + the summon/quick-panel helpers; `INTEGRATION.md` is the validated
call-sequence playbook; `RUNTIME.md` is the in-session wiring.

## Install / run

```bash
# Download-free demo — no install needed:
make -C .. demo

# Full install (heavy NER stack; see banner) to run the suite:
pip install -e .
python -m spacy download en_core_web_sm
USE_TF=0 pytest -q            # 377 tests
```

Heavy detectors are function-local and guarded — with nothing extra installed the package
degrades to a **stdlib regex floor** (which is what makes the demo offline). `USE_TF=0` is
required (transformers' TensorFlow path breaks under Keras 3); the modules set it
defensively and the suite/CI export it.

## Console scripts

Core: `fb-transcripts`, `fb-redact`, `fb-package`, `fb-assist-mcp`. Per-surface (each with
a built-in `demo` / default fixture): `claude-repro`, `fb-desktop-chat`, `fb-server-side`,
`fb-cowork`. Each is the argparse CLI over the matching module.

Apache-2.0. **Best-effort redaction aid, not a guarantee** — always review the preview
before sending.
