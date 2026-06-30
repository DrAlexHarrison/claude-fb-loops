# STRATEGY — why this repo, for the Feedback Loops role

This document is the argument the code makes, stated plainly. It's written for the
*Product Operations Manager, Feedback Loops* role: the person who owns the loop between
the millions of people using Claude and the handful of people improving it.

## The thesis

**The highest-value feedback Anthropic could receive is the feedback users refuse to
send.** In Claude Code, `/feedback` can attach the literal session — the single most
debuggable artifact there is. But that session holds secrets, PII, customer data, and
proprietary code, and the built-in redaction strips **API keys only**; everything else
uploads verbatim and is retained five years. So a rational user with a real bug in a
real repo stays silent. The loop is broken at its most valuable point, by privacy.

**fb-assist is the unlock.** It makes the privacy decision *cheap and legible* inside
the session, so the user can send the high-value report safely. It is not a parallel
tool — it operates **upstream of the real `/feedback` intake**, which I verified reads
the on-disk transcript at submit (filesystem + network + code; see
`docs/verification.md`). Redact the file, the upload changes. Non-destructively: swap
in a sanitized copy for the submit, restore the original byte-for-byte after.

## Why this maps to the role, not just the product

The role is **operations**: instrument the loop, raise its signal-to-noise, and make the
pipeline from "user hit a thing" to "Claude got better" shorter and more trustworthy.
fb-assist is built as that pipeline, not a single tool:

- **Capture** with consent and privacy → `fb-assist` (the keystone).
- **Normalize** every surface to one schema → the shared *effort-signal + artifact* every
  edge emits (CLI, API, export, server-side). Sameness is the operational win: one
  triage queue, one dashboard, one quality bar, regardless of where feedback entered.
- **Distill and route** at the org → `fb-os` ingests artifacts, clusters them Clio-style,
  triages with an internal Claude, and publishes a living `open-questions.json` that the
  capture side can *read back* — closing the loop both directions.
- **Raise trust in the signal** → the API surface's **`request-id` anchor** ties a report
  to a real metered call Anthropic can verify against its own logs. Feedback you can
  *trust the provenance of* is worth more per unit than feedback you can't.

That is the job: not "a redaction script," but the **operating system for a privacy-safe,
high-trust, every-surface feedback loop.**

## Anthropic has already said it wants this

- **Sept-2025 postmortem:** *"We'll develop infrastructure and tooling to better debug
  community-sourced feedback without sacrificing user privacy,"* and named privacy
  controls as the reason engineers can't examine unreported problematic interactions.
  A repro the user **voluntarily** scrubs and sends is exactly that infrastructure.
- **The feedback-button / training-opt-out controversy** is the same tension from the
  user side; a consent-first, preview-gated co-author is the resolution.
- **claude.ai "Share" already genericizes server-side** — so the server-side
  consent-genericize recommendation in `server_side.py` extends a shipping pattern,
  not a new ask.

## The surfaces, and why each one earns its place

| Surface | The operational role it plays |
|---|---|
| **CLI / IDE keystone** | the proven mechanism — in-session morph + swap-restore around the real intake. Everything else is this core with a different edge. |
| **`claude-repro` (API)** | reaches the privacy-sensitive segment (Bedrock/Vertex, regulated devs) who can't paste a session at all; adds the ungameable `request-id` anchor. |
| **`desktop_chat` export co-pilot** | a ToS-clean way to bring claude.ai conversations into the loop on the *user's* terms (their export), not by scraping. |
| **`server_side.py`** | for surfaces where the conversation is referenced-not-inlined (claude.ai thumbs, VS Code per-message), the only place the gap can close is Anthropic's side — so we hand them a runnable reference, not a wish. |
| **`fb-os`** | the org-side half of the loop: where captured signal becomes prioritized questions and back-propagates to users. |
| **`pps-pipeline`** | the same privacy-preserving capture discipline applied to a different observation problem — evidence of the pattern generalizing. |

## The honesty is a feature

Not every surface can close client-side, and pretending otherwise would be the wrong
instinct for this role. `GAPS.md` is an unflinching ledger: every gap is classed as
**proven**, **closeable (with a build)**, or **architecturally-impossible-client-side
(so we ship the extensible seam Anthropic adopts)**, and every "unproven" item names the
exact empirical check that would close it. Handing Anthropic an honest scorecard with a
buildable plan for each gap is more useful — and more credible — than a uniform success
story. That posture *is* the operational discipline the role needs.

## What I'd do first, in the role

1. Instrument the existing `/feedback` funnel for the privacy-driven drop-off (how much
   high-value feedback is suppressed, where).
2. Ship the consent-genericize preview as the default path, measured against
   send-rate and signal quality — the postmortem promise, operationalized.
3. Stand up the one-schema triage/cluster loop (`fb-os`) so feedback becomes
   prioritized questions with a trustworthy provenance anchor.
4. Extend surface by surface, locus-first, reusing the core — exactly the order in
   `GAPS.md`.
