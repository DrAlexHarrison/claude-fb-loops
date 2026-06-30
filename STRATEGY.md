# STRATEGY — how this repo maps to the Feedback Loops role

This document explains the design choices behind the code, written for the *Product
Operations Manager, Feedback Loops* role: the person who owns the loop between the
people using Claude and the people improving it.

## The problem

In Claude Code, `/feedback` can attach the literal session — one of the most debuggable
artifacts available. But that session can hold secrets, PII, customer data, and
proprietary code, and the built-in redaction strips API keys only; everything else
uploads verbatim and is retained five years. A user with a real bug in a real repo often
declines to send it. The result is that the most useful reports are the ones least
likely to arrive, and the reason is privacy.

`fb-assist` addresses that by making the privacy decision cheap and legible inside the
session, so the user can send the report safely. It is not a parallel tool — it operates
upstream of the real `/feedback` intake, which reads the on-disk transcript at submit
(verified three ways: filesystem + network + code; see `docs/verification.md`). Redact
the file, the upload changes. Non-destructively: swap in a sanitized copy for the
submit, restore the original byte-for-byte after.

## Why this maps to the role, not just the product

The role is operations: instrument the loop, raise its signal-to-noise, and shorten the
path from "user hit a thing" to "Claude got better." The repo is built as that pipeline,
not a single tool:

- **Capture** with consent and privacy → `fb-assist` (the keystone).
- **Normalize** every surface to one schema → the shared effort-signal + artifact every
  edge emits (CLI, API, export, server-side). One schema means one triage queue, one
  dashboard, and one quality bar regardless of where feedback entered.
- **Distill and route** at the org → `fb-os` ingests artifacts, clusters them Clio-style,
  triages with an internal Claude, and publishes a living `open-questions.json` that the
  capture side reads back — closing the loop in both directions.
- **Track provenance** → the API surface's `request-id` anchor ties a report to a real
  metered call Anthropic can verify against its own logs, so the report's origin is
  checkable rather than asserted.

## Prior signals that this is wanted

- **Sept-2025 postmortem:** *"We'll develop infrastructure and tooling to better debug
  community-sourced feedback without sacrificing user privacy,"* and named privacy
  controls as the reason engineers can't examine unreported problematic interactions. A
  repro the user voluntarily scrubs and sends fits that description.
- **The feedback-button / training-opt-out discussion** is the same tension from the
  user side; a consent-first, preview-gated co-author is one resolution.
- **claude.ai "Share" already genericizes server-side** — so the server-side
  consent-genericize reference in `server_side.py` extends a shipping pattern rather than
  proposing a new one.

## The surfaces, and the role each plays

| Surface | The role it plays |
|---|---|
| **CLI / IDE keystone** | the proven mechanism — in-session morph + swap-restore around the real intake. The other surfaces are this core with a different edge. |
| **`claude-repro` (API)** | reaches the segment (Bedrock/Vertex, regulated devs) that can't paste a session at all; adds the `request-id` provenance anchor. |
| **`desktop_chat` export co-pilot** | brings claude.ai conversations into the loop via the user's own export, not by scraping. |
| **`server_side.py`** | for surfaces where the conversation is referenced-not-inlined (claude.ai thumbs, VS Code per-message), the gap can only close on Anthropic's side, so this is a runnable reference rather than a deployed feature. |
| **`cowork.py`** | the Cowork edge — the same strip + gate against the bundled local-agent-mode `audit.jsonl` shape; reference until the intake wire is known. |
| **`fb-os`** | the org-side half of the loop: captured signal becomes prioritized questions and back-propagates to users. |
| **`pps-pipeline`** | the same privacy-preserving capture discipline applied to a different observation problem. |

## On the gaps

Not every surface can close client-side. `GAPS.md` classes each gap as **proven**,
**closeable (with a build)**, or **architecturally-impossible-client-side** (where the
repo ships an extensible seam Anthropic could adopt), and every "unproven" item names the
exact empirical check that would close it. The ledger is part of the deliverable: a
buildable plan for each gap is more useful to act on than a uniform success story.

## What I'd do first, in the role

1. Instrument the existing `/feedback` funnel for the privacy-driven drop-off (how much
   high-value feedback is suppressed, and where).
2. Ship the consent-genericize preview as the default path, measured against send-rate
   and signal quality — the postmortem promise, operationalized.
3. Stand up the one-schema triage/cluster loop (`fb-os`) so feedback becomes prioritized
   questions with a provenance anchor.
4. Extend surface by surface, locus-first, reusing the core — the order in `GAPS.md`.
