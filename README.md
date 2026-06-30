# claude-fb-loops

**A privacy-preserving feedback co-author for Claude — and the platform around it.**

Claude Code's `/feedback` is the highest-bandwidth signal a user can send Anthropic:
the *actual session* — prompts, thinking, tool calls, file contents — attached to a
bug report. It is also the feedback people **don't** send, because that session
contains secrets, PII, customer data, and proprietary code. The built-in redaction
strips **API keys only**; everything else uploads verbatim and is retained for five
years. So the most valuable feedback is the feedback that never gets sent.

`fb-assist` closes that gap **inside the session you're already in**. It reads the
transcript, helps you say the bug precisely, lets you decide exactly what's private,
and ships only what you confirmed — through Anthropic's **real** `/feedback` intake,
**non-destructively**: it swaps a sanitized copy onto disk for the submit, then
restores your original byte-for-byte.

> This repo is a working reference built for Anthropic's *Product Operations Manager,
> Feedback Loops* role. The code is the argument; the strategy is in
> [`STRATEGY.md`](STRATEGY.md); the honest scorecard is in [`GAPS.md`](GAPS.md).

---

## ▶ See it work (download-free, offline, ~2 seconds)

```bash
make demo
```

It plants a live-looking `sk-ant-…` key, an `AKIA…` key, a GitHub token, an email, an
SSN, an IP, and an absolute path into a schema-faithful session — across a human
prompt, a Read file body, and a Bash stdout — then runs the validated flow:

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

The hard guarantees (every planted secret/PII/path absent from the **actual upload
bytes**, original restored byte-exact) come from a **deterministic floor** — regex +
structural strips + a crash-safe swap-restore — so the demo runs on a bare interpreter
with **no model downloads and no network**. If the optional NER stack is installed it
additionally masks the person name; that's a bonus, never the gate.

---

## Why this is a real integration, not a mock

The mechanism rests on one **empirically verified** fact: `/feedback` reads the
**on-disk** transcript at submit time, so rewriting that file before you submit changes
what Anthropic receives. This was confirmed three ways against the real, shipping
command (full method in [`docs/verification.md`](docs/verification.md)):

- **Filesystem** (decisive): `inotify` caught `/feedback` `OPEN→ACCESS→CLOSE` on the
  on-disk past-session `.jsonl`; after redacting that same file in place, the next
  gather read the redacted bytes from the same path. Same path, two different
  contents, both pulled into the bundle — **the on-disk file is the source of truth.**
- **Network**: `tcpdump` captured the TLS submit to `api.anthropic.com`; the submit
  returned a real Feedback ID.
- **Code**: the binary's gather path (`fDl → Akf → Tkf`) corroborates both.

And the confirmation screen states the payload verbatim — *"Environment info … Git
repo metadata … Session transcript: this session + this project's other sessions from
the last 7 days"* — confirming exactly the leak surface fb-assist targets.

So fb-assist is **upstream of the real intake**: it shapes the input the shipping tool
already consumes. The swap is **non-destructive and crash-safe** — a durable journal +
backups mean even a hard kill mid-submit restores your original on the next run.

---

## The proof floor: tests

```bash
make setup      # one-time: installs the NER stack + spaCy model (HEAVY — banner warns)
make test       # 264 (fb-assist) + 46 (fb-os) + 48 (pps-pipeline) — all green
make scrub-gate # asserts ZERO real personal data in tracked files
```

- **264 fb-assist tests** cover the parser/extractors, the detector recall floor, the
  swap-restore safety core (including a real `os._exit` mid-swap crash-recovery test),
  the two-layer egress gate over the actual upload bytes, the API SDK, and the
  server-side reference.
- The large fixtures the suite runs on are **fully synthetic and deterministic** —
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

Heavy detectors are function-local and guarded, so the package **degrades to a
stdlib regex floor** with zero heavy deps installed — which is what makes `make demo`
download-free and offline.

---

## One platform, every surface

The job is "one shared platform every surface plugs into." The principle that picks the
mechanism is **transcript locus** — is the conversation on the user's disk
(rewriteable before send) or server-side (not)? One surface-agnostic core, a thin
per-surface edge. What's in this repo:

| Surface | Module | Mechanism |
|---|---|---|
| **CLI / IDE** (keystone) | `fb_assist/` + `skill/fb/` + `mcp_server.py` | in-session `/fb` morph → swap-restore around the real `/feedback` |
| **API / Console** | `claude_repro.py` | forward-transform SDK; ties each report to its real `request-id` (the ungameable anchor) |
| **claude.ai export** | `desktop_chat.py` | co-pilot over an exported `conversations.json` — genericize + effort-signal, ToS-clean |
| **claude.ai / VS Code thumbs** | `server_side.py` | reference consent-genericize gate for the *referenced* (not inlined) feedback POST |
| **Org-wide loop** | `fb-os/` | ingest distilled artifacts → cluster (Clio-style) → triage → publish `open-questions.json` |
| **Work-observation** | `pps-pipeline/` | recorded session → interleaved, redacted, text-only package + cited assessment |

Every surface emits the **same effort-signal + artifact schema** — that sameness *is*
the "one platform" claim. The surfaces that **can't** close client-side are built as
**extensible reference seams** Anthropic can adopt, and are catalogued honestly in
[`GAPS.md`](GAPS.md).

---

## The pitch, in three anchors

1. **Anthropic already committed to building this.** The Sept-2025 postmortem: *"We'll
   develop infrastructure and tooling to better debug community-sourced feedback
   without sacrificing user privacy"* — and named privacy controls as the blocker to
   examining problematic interactions. A repro the user **voluntarily** scrubs and
   sends is that unlock, in Anthropic's own words.
2. **The `request-id` is an ungameable anchor.** `claude-repro` ties every API report
   to a real metered call Anthropic can verify against its own 7-day server log — high
   signal, zero extra user content.
3. **One effort-signal + artifact schema across every surface** is the literal
   "one platform every surface plugs into" the role asks for.

---

## Quickstart

```bash
git clone <this-repo> && cd claude-fb-loops
make demo            # offline, no install, no downloads — watch redaction happen
make setup           # install the full NER stack (heavy; see banner) to run the suite
make test            # 264 + 46 + 48 tests
make scrub-gate      # prove no personal data ships
```

Requires Python ≥ 3.10. `make demo` needs nothing but the standard library.

---

## Repository layout

```
fb-assist/      keystone package — transcripts/redact/package + claude_repro,
                desktop_chat, server_side, locate, profile, genericize, watcher,
                mcp_server; the /fb skill + co-author prompt; the voice confirm.
fb-os/          Feedback OS (org-wide ingest/cluster/triage loop).
pps-pipeline/   work-observation interview packager + assessment.
docs/           DESIGN, verification (the empirical proof), the per-surface refs.
GAPS.md         the honest parity ledger — what's proven, closeable, or a seam.
STRATEGY.md     why each surface ties to the role + the mission.
```

## License & posture

Apache-2.0 (explicit patent grant). fb-assist is a **best-effort redaction aid, not a
guarantee** — always review the preview before sending. Every dependency is permissive;
AGPL `trufflehog` is invoked only as an optional pre-installed external binary, never
bundled or depended on (see [`NOTICE`](NOTICE)). Personal project by Alex Harrison.
