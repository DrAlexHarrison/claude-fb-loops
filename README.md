# claude-fb-loops

A privacy-preserving feedback co-author for Claude, plus the per-surface and
org-side pieces around it.

Claude Code's `/feedback` can attach the actual session — prompts, thinking, tool
calls, file contents — to a bug report. That transcript is also what stops people
from sending it: it can contain secrets, PII, customer data, and proprietary code.
The built-in redaction strips API keys only; everything else uploads verbatim and is
retained for five years.

`fb-assist` works inside the session you're already in. It reads the transcript,
helps you describe the bug, lets you decide what's private, and ships only what you
confirm — through Claude Code's real `/feedback` intake, non-destructively: it swaps a
sanitized copy onto disk for the submit, then restores your original byte-for-byte.

---

## Demo (download-free, offline, ~2 seconds)

```bash
make demo
```

It plants a live-looking `sk-ant-…` key, an `AKIA…` key, a GitHub token, an email, an
SSN, an IP, and an absolute path into a schema-faithful session — across a human
prompt, a Read file body, and a Bash stdout — then runs the flow:

```
[ BEFORE ]  the human turn as it sits on disk (secrets visible):
    I'm Marlene Vasquez and I build the Contoso API. While debugging I pasted my key
    sk-ant-api03-AAAA1111BBBB2222CCCC3333DDDD4444 and AWS AKIAZZ44QQ55WW66EE77 ...
    SSN 123-45-6789. The real bug: the /feedback flow keeps FREEZING on submit.

[ PREVIEW ]  the gate the user confirms before anything ships:
      INCLUDED : 8 records  (3,210 bytes)
      STRIPPED : 6 records redacted
        redacted : 1×ANTHROPIC_KEY, 1×AWS_ACCESS_KEY, 1×EMAIL_ADDRESS, 1×IP_ADDRESS, 1×US_SSN, 1×PERSON …

[ AFTER ]  the same turn in the sanitized bundle (values gone, meaning kept):
    I'm ‹PERSON› and I build the Contoso API. While debugging I pasted my key
    ‹ANTHROPIC_KEY› and ‹ORGANIZATION› ‹AWS_ACCESS_KEY› ... ‹US_SSN›. The real bug:
    the /feedback flow keeps FREEZING on submit.

[ RESTORE ]  original transcript back on disk after submit:
    during-swap on disk == sanitized bytes : True
    restored byte-exact (sha256 matches)   : True

RESULT: GREEN — fb-assist redacted the session end-to-end.
```

Every planted secret/PII/path is absent from the actual upload bytes, and the original
is restored byte-exact. Those guarantees come from a deterministic floor — regex +
structural strips + a crash-safe swap-restore — so the demo runs on a bare interpreter
with no model downloads and no network. With the optional NER stack installed it also
masks the person name; that pass is additive and never the gate.

Each surface has its own one-command demo (all offline, off built-in fixtures):
`make demo-api`, or `make demo-all` for every surface at once.

---

## Activate `/fb` in your own sessions

```bash
make setup      # one-time: install the packages + NER stack (heavy; see banner)
make install    # copies the /fb skill + registers the fb-assist MCP server (idempotent)
# restart Claude Code, then type /fb in any session
```

`make install` computes its own interpreter path and merges the `fb-assist` server into
`~/.claude.json` (backing it up first) — no hand-editing. `make uninstall` reverses it.
Details + the IDE/JetBrains story: [`fb-assist/RUNTIME.md`](fb-assist/RUNTIME.md).

---

## How the integration works

The mechanism rests on one verified fact: `/feedback` reads the on-disk transcript at
submit time, so rewriting that file before you submit changes what gets received. This
was confirmed three ways against the real, shipping command (full method in
[`docs/verification.md`](docs/verification.md)):

- **Filesystem** (decisive): `inotify` caught `/feedback` `OPEN→ACCESS→CLOSE` on the
  on-disk past-session `.jsonl`; after redacting that same file in place, the next
  gather read the redacted bytes from the same path. Same path, two different contents,
  both pulled into the bundle.
- **Network**: `tcpdump` captured the TLS submit; the submit returned a Feedback ID.
- **Code**: the binary's gather path corroborates both.

fb-assist therefore operates upstream of the real intake: it shapes the input the
shipping tool already consumes. The swap is non-destructive and crash-safe — a durable
journal plus backups restore the original on the next run even after a hard kill
mid-submit.

---

## Tests

```bash
make setup      # one-time: installs the NER stack + spaCy model (HEAVY — banner warns)
make test       # fb-assist + fb-os suites
make scrub-gate # asserts ZERO real personal data in tracked files
```

- The fb-assist tests cover the parser/extractors, the detector floor, the swap-restore
  safety core (including a real `os._exit` mid-swap crash-recovery test), the two-layer
  egress gate over the actual upload bytes, the API SDK, and the reference intake.
- The large fixtures the suite runs on are fully synthetic and deterministic —
  generated at test time by `fb-assist/tests/fixtures/generate_fixtures.py`. No real
  Claude Code session, prompt, path, or credential ships in this repo; the
  `make scrub-gate` check (also a CI gate) enforces it.

---

## Architecture

```
   transcripts.py            redact.py                     package.py
  ┌────────────────┐   ┌────────────────────┐   ┌──────────────────────────┐
  │ parse + 12     │   │ secrets (regex/    │   │ assemble (<1 MB budget)  │
  │ category       │──▶│ gitleaks/detect-   │──▶│ diff_preview (the gate)  │
  │ extractors +   │   │ secrets) + PII     │   │ swap_restore / recover   │
  │ locators       │   │ (presidio/GLiNER)  │   │ (crash-safe, byte-exact) │
  │ relevant_slice │   │ strip + mask +     │   │ begin/finish_swap        │
  │ redaction_map  │   │ leak_scan          │   │ (straddle a turn)        │
  └────────────────┘   └────────────────────┘   └──────────────────────────┘
        WHERE                  WHAT                        SHIP IT SAFELY
                composed by the in-session co-author (the /fb skill)
```

Heavy detectors are function-local and guarded, so the package degrades to a stdlib
regex floor with no heavy deps installed — which is what makes `make demo` download-free
and offline.

---

## Surfaces

One shared core, plugged into per entry point. The factor that picks the mechanism per
surface is **transcript locus** — whether the conversation sits on the user's disk
(rewriteable before send) or is referenced server-side.

| Surface | Module | Mechanism |
|---|---|---|
| **CLI / IDE** (keystone) | `fb-assist/` + `fb-assist/skill/fb/` + `mcp_server.py` | in-session `/fb` morph → swap-restore around the real `/feedback` |
| **API / Console** | `claude_repro.py` + `reference_intake.py` | forward-transform SDK that ties each report to its `request-id`, plus a runnable reference `/v1/feedback` intake |
| **Org-wide loop** | `fb-os/` | ingest distilled artifacts → cluster (Clio-style) → triage → publish `open-questions.json` |

Every surface emits the same effort-signal + artifact schema, so feedback from any
entry point lands in one triage queue with one quality bar.

---

## Repository layout

```
fb-assist/      keystone package — transcripts/redact/package + claude_repro,
                reference_intake, locate, profile, genericize, watcher, reputation,
                mcp_server; the /fb skill + co-author prompt; the voice confirm.
fb-os/          the org-wide ingest/cluster/triage loop.
docs/           design notes, verification (the empirical proof), the per-surface refs.
```

## License

Apache-2.0 (explicit patent grant). fb-assist is a best-effort redaction aid, not a
guarantee — always review the preview before sending. Every dependency is permissive;
AGPL `trufflehog` is invoked only as an optional pre-installed external binary, never
bundled or depended on (see [`NOTICE`](NOTICE)). Authored by Alex Harrison.
