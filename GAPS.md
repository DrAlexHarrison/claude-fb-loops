# fb-assist — parity & gap ledger (the unflinching version)

The CLI keystone (Build 3) meets the full spec and is proven. Every *other* surface falls short of CLI parity. This ledger classifies each gap and drives it:

- **❌❌ HARD (architecturally impossible client-side)** → we build the **extensible seam** (a drop-in reference Anthropic adopts). The gap can only close on Anthropic's side; our job is to hand them the buildable fix.
- **❌ CLOSEABLE** → we **max-build to close it** in this repo.
- **⚠️ UNPROVEN** → closeable, but gated on an **empirical check** (some need Alex's hand).
- **✅ DONE** · **🔨 BUILDING** · **📋 QUEUED**

The CLI parity bar (10 powers): in-session morph · read any past on-disk session · non-destructive swap-restore around the real `/feedback` · structural strip + char-mask + detectors · genericize by live Opus · two-layer gate over actual upload bytes · ships through Anthropic's proven intake · profile/learn · watcher · effort-signal (+ question-loop).

---

## ❌❌ HARD — architecturally impossible client-side → build the EXTENSIBLE seam

| # | Surface · gap | Why it can't close client-side | Extensible seam we build | Status |
|---|---|---|---|---|
| H1 | **claude.ai live thumbs-down redaction** | feedback POST is referenced-not-inlined (HAR: `{type,reason}`, 97 B); the conversation never travels in the request | **`server_side.py`** — consent-genericize-before-attach reference, 3 adapter ports, fail-closed gate | ✅ DONE |
| H2 | **VS Code Extension per-message 👍/👎** | `messageRated({messageUuid,sentiment})` — referenced UUID, no transcript text | covered by the same **`server_side.py`** reference | ✅ DONE |
| H3 | **API: no feedback intake endpoint exists** | Anthropic ships no public `/v1/feedback`; nothing to submit *to* | (a) `claude-repro` draft + request-id anchor ✅; (b) **reference `/v1/feedback` intake endpoint** (mirror the server-side-ref pattern) | a ✅ / b 📋 QUEUED |
| H4 | **Bedrock/Vertex request-id absent** | the Anthropic `request-id` header may not survive the gateway | deterministic fallback anchor `{provider,model,usage}` flagged `verifiable:false` (honest) | ✅ DONE |
| H5 | **IDE native panel: `mcp__ide__getDiagnostics` absent** | Anthropic hasn't exposed it to the panel tool list (#40766) | universal `settings.json` hook signal covers the panel; enriched signal is terminal-only until Anthropic closes parity | ✅ DONE |
| H6 | **Cowork→Anthropic intake wire** | the server-side intake wire is undocumented (closed door) | **reference Cowork-intake adapter** built against the bundle's `coworkFeedback`/`FeedbackWindow` shape we *can* inspect (Claude Desktop now bundles Cowork — readable locally) | 📋 QUEUED (gated on Desktop inspect) |
| H7 | **claude.ai/Desktop in-session morph** | no skill system in the web/Electron chat UI | the **export co-pilot** is the user-side answer (Build F); server-side ref is the Anthropic-side answer | 🔨 F BUILDING |

> The honest line: H1–H7 are *recommendations wearing working-code costumes* — runnable references, not deployed capabilities. That's the strongest form the no-can-do's can take.

---

## ❌ CLOSEABLE — max-build to close in this repo

| # | Surface · gap | The fix | Status |
|---|---|---|---|
| C1 | **Cowork: `strip_categories` strips nothing on the server slice** (fix 7) | write a Cowork `strip_blocks` (like the API edge) | 📋 QUEUED (Build E) |
| C2 | **Cowork: MCPB cross-platform packaging (incl. win32)** | vendored runtime + `darwin`/`win32`/`linux` manifest | 📋 QUEUED (Build E) |
| C3 | **Question-loop not closed end-to-end** | Build 1 triager produces `open-questions.json` + **CLI `open_questions` reader tool** consumes it | 🔨 Build 1 BUILDING / reader 📋 (me) |
| C4 | **Reputation token (§13): opaque stub** | real pseudonymous signed-token scheme (issuance/storage/sync/revocation) | 📋 OPEN (scope → interview) |
| C5 | **Multi-session bundling: runtime wires one session** | wire `extra_sessions` through `assemble` (budget_pack already supports N) | 📋 QUEUED (me) |
| C6 | **Profile cross-machine sync unspecced** | ride rc/bus config-sync or a thin syncer | 📋 QUEUED |
| C7 | **NER over-redaction** (`JSON→PERSON`, `API→ORG`) | profile allow/deny (✅ partial) + genericize layer; recall/precision tuning | ⚠️ PARTIAL |
| C8 | **API genericize degraded (no live Claude)** | pluggable LLM-genericize hook + optional local-Ollama pass | 📋 QUEUED |
| C9 | **Production repo + FIX-4 fixture sanitization** | synthetic-fixture generator, scrub-gate, README+STRATEGY, CI, `make demo`, local `git init` | 📋 QUEUED (Build C) |
| C10 | **Windows platform: all code is POSIX-assumed** | Windows-aware paths (`%APPDATA%`, `%USERPROFILE%\.claude`), config-dir resolution, atomic-rename + live-detect portability | 📋 OPEN (scope → interview) |
| C11 | **Langfuse/Helicone ingest schemas pinned from docs** | pin from a real captured export | ⚠️ needs a sample |

---

## ⚠️ UNPROVEN — closeable, gated on an empirical check (some need Alex)

| # | Check | Who | Closes |
|---|---|---|---|
| E1 | **Live billed `response._request_id` success path** | needs **API credits** (account is $0) | the "ungameable anchor" claim (header-on-error + SDK-source + mocks already in hand) |
| E2 | **Cowork bundled-`claude` `/feedback` gather root includes `local-agent-mode-sessions`** | I run via tailscale `amac` (authorize) | "swap-restore works on Cowork slice-1" |
| E3 | **Real `read_transcript` payload shape** | amac capture / Desktop bundled Cowork | pins `cowork_conversation.py` |
| E4 | **VS Code Extension GUI "Give feedback" — attaches transcript or description-only?** | human at the panel + capture (HAR-analog) | finalizes the GUI swap-restore handoff |
| E5 | **Panel custom-skill slash-list; JetBrains zero-code claim** | human 30-sec IDE checks | confirms IDE invocation lanes |

---

## Coverage check (nothing left un-addressed)
- Every **❌❌** has an extensible seam (built or queued). Every **❌** has a build (in flight or queued). Every **⚠️** has a named check.
- The application's strength = the proven keystone **+ this ledger**. Handing Anthropic an honest parity ledger with a buildable plan for every gap is more convincing than a uniform-success story.
