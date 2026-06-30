# fb-assist across IDEs (VS Code · Cursor & forks · JetBrains)

**The one fact that governs this surface: the IDEs *are* the Claude Code CLI.** `/fb`, the `fb-assist` MCP server, the co-author prompt, and swap-restore are surface-agnostic core — every IDE reads the same `~/.claude` (skills, hooks, MCP servers, permissions, and the `~/.claude*/projects/<slug>/<sessionId>.jsonl` transcripts). So the IDE "edge" is **not a rebuild** — it's only *how `/fb` is invoked, how the gate is shown, and how the watcher signals*. Full rationale: `../plans/ide-edge-plan.md`.

## Two runtime locations inside one IDE

| Location | What runs | Parity | fb-assist |
|---|---|---|---|
| **Integrated terminal** (`claude` in the IDE terminal) | the standalone `claude`, auto-connected to the `ide` MCP | **1:1 with bare CLI** — all skills/commands/hooks/MCP, plus `mcp__ide__getDiagnostics` | **works today, zero new code** |
| **Native chat panel** (the sidebar/tab) | a bundled private `claude` | **subset** (`/`-menu is a subset; MCP "partial"; no `!` bash; `getDiagnostics` absent) | needs the documented fallbacks below |

**The integrated terminal is the demo lane and the universal escape hatch** — it's identical to the proven CLI build, so the IDE story is *true today* with only the shared core. The panel is the polish lane.

## Per-IDE

- **JetBrains** (IntelliJ/PyCharm/GoLand/Android Studio): the plugin runs the externally-installed `claude` in the integrated terminal and uses `~/.claude/` as home — *no separate JetBrains config*. So `/fb` + the MCP + swap-restore run **exactly as on bare CLI. Zero JetBrains-specific code.** Bonus: the IDE's native diff viewer is available for the optional single-span confirm drill-down. *(Un-run check: install PyCharm CE → `claude` → `/fb`; ~5 min — see below.)*
- **VS Code / Cursor / forks** (Windsurf, Kiro, VSCodium via Open VSX): same extension everywhere.
  - **Lane 1 — integrated terminal (default, 1:1):** `/fb` works unchanged; the co-author can additionally call `mcp__ide__getDiagnostics` and drive the native side-by-side diff viewer. **This already works the moment the core ships.**
  - **Lane 2 — native panel (richer UI, parity-gapped):** invocation degrades gracefully — `disable-model-invocation: false` means typing *"give feedback on this session"* triggers `/fb` via the model even if the slash list omits it; the terminal and a URI-resume tab are further fallbacks. **The slash-list question is a non-blocker** because the feature degrades to "still works," never "blocked."

## The confirm gate, rendered in-IDE
- **Primary (panel AND terminal):** the co-author writes the concise **"included / stripped" summary** (`package.diff_preview(...).render()`) as a **markdown doc the IDE auto-opens** (the same affordance Plan mode uses), then a **numbered in-chat confirm** (`1 = ship · 2 = edit · 3 = cancel`). Universal; matches "make confirming a tap." **Never diff the raw 1 MB JSONL at a human.**
- **Optional drill-down (terminal lane):** a true before/after of a *single* redacted span via the IDE diff viewer.
- **Hard floor unchanged:** the deterministic `scan_secrets` + PII-regex gate over the actual upload bytes runs regardless of IDE.

## The watcher, in-IDE
- **Universal signal (all IDEs + CLI):** the shared `~/.claude/settings.json` hook (`PostToolUse`/`UserPromptSubmit`/`SessionEnd`) fires in every IDE's integrated terminal with no per-IDE code — non-blocking nudge, offer-once, disable-able.
- **IDE-enriched signal (terminal lane):** fold `mcp__ide__getDiagnostics` error-storms into the detector — a report-worthy signal the bare CLI lacks. *Honest gap:* that tool is panel-absent, so the diagnostics signal is integrated-terminal-only until the parity bug closes; the universal hook signals cover the panel.
- **Privacy bonus:** a `Read` deny-rule on a path (e.g. `.env`) stops both the selection text and the open-file notice from reaching the model as IDE context — fb-assist's privacy posture extends *upstream* of the transcript for free.

## Isolation levels (honestly distinct, not conflated)
- **In-session, same window** (default) — `/fb` morphs the live Claude.
- **"Separate tab, same thread"** — `vscode://anthropic.claude-code/open?prompt=<urlenc '/fb'>&session=<id>` (prompt pre-filled, not auto-submitted; `session=` *resumes*, it is **not** a true fork).
- **"Fully isolated"** — integrated-terminal `claude --resume <id> --fork-session --append-system-prompt-file co-author.md --mcp-config fb-assist-mcp.json`.

## Distribution: one unit, every IDE
Ship as a **Claude Code plugin** bundling the `/fb` skill + the `fb-assist` MCP registration + the watcher hooks — `/plugins` installs identically across CLI, the VS Code panel, and JetBrains. Alt: a repo-committed `.mcp.json` + `.claude/skills/fb/` + `.claude/settings.json` for team-shareable per-repo install; or user-scope (what this repo wires today).

## The native Extension *GUI* (the `claude-sidebar` webview) — a distinct surface from the TUI
The **Claude VS Code Extension's native sidebar is a webview GUI, not the TUI** — and it has its *own* feedback stack, verified in `extension.js`/`webview/index.js` (v2.1.186). Don't conflate it with the terminal/bundled-binary TUI. Three affordances, three different answers:

1. **"Give feedback" (free-text)** — `submitFeedback(description, {surface:"ide"})` is an **RPC into the same bundled `claude` agent** (`query.submitFeedback(...)`, returns a `feedback_id`), tagged `surface:"ide"`. Because it routes through the same agent that performs the proven on-disk `/feedback` gather, **`/fb` + swap-restore almost certainly cover it** — with a GUI-button straddle identical in shape to the TUI handoff: `submit_begin` stages the sanitized transcript → the user clicks *Give feedback* in the sidebar → `submit_finish` restores. **Open check (the VS-Code analog of the claude.ai HAR):** confirm this RPC *attaches the on-disk transcript* vs sends description-only — un-runnable headlessly (needs a human submitting from the GUI while capturing).
2. **Per-message 👍/👎** — `messageRated({messageUuid, sentiment, surface, cleared})` **references a message UUID and carries no transcript text.** So, exactly like the claude.ai thumbs-down (referenced, not inlined), there is **no client-side seam to redact** — the principled fix is **server-side consent-genericize** (see `server-side-reference.md`). Our server-side reference implementation covers this VS Code thumbs path, not just claude.ai.
3. **Feedback survey** — opens an external **Qualtrics** form (`anthropic.qualtrics.com/jfe/form/...`) and/or the GitHub issues link; carries no transcript → out of scope for redaction.

**Storage is shared (the load-bearing good news):** the GUI extension writes transcripts to the *same* `~/.claude/projects/<sanitized-cwd>/<sessionId>.jsonl` layout the CLI gather reads, so **`locate.py` already resolves the GUI extension's sessions** — no new locator code. The extra `~/.claude/sessions/`, `~/.claude/ide/`, `session-snapshot.json`, `tab-session-map.json` are the GUI's *session-index* metadata (sessions sidebar / reopen-closed-session / tab mapping), **not** separate transcript stores.

**Net for the Extension GUI:** "Give feedback" → swap-restore territory (one HAR-style check to finalize the handoff); per-message thumbs → server-side-reference territory (no client redaction possible); survey → external/out-of-scope. The integrated terminal inside the same extension remains the 1:1 fallback.

## IDE support (verified against the real IDEs)
Confirmed by driving each surface:
- **`/fb` lists in the native panel's `/` menu: YES.** Custom user skills surface in the panel.
- **Check 2 — the panel calls `mcp__fb-assist__*`: YES.** `list_sessions` connected and ran (returned 2,120 sessions). (It also surfaced — and we fixed — an unbounded-output bug: `list_sessions` is now capped.)
- **Check 4 — JetBrains zero-code: YES.** `claude` in Android Studio's integrated terminal, `/fb` → the co-author loaded, identical to bare CLI. The "it *is* the CLI" claim holds end-to-end.
- **Check 5 — `mcp__ide__getDiagnostics` in the panel: NO (no-such-tool).** The `ide` MCP server doesn't appear at all in the panel session → the watcher's diagnostics-enriched signal is integrated-terminal-only; the universal `settings.json` hook is the panel's watcher signal.
- **Dogfooding bonus:** first real cross-surface use found two MCP-integration bugs (slow startup → flaky connect; unbounded `list_sessions`), both fixed same-session — and the co-author flagged a genuine Claude Code rough edge ("a slow MCP server is dropped silently with no surfaced error"), which is itself a real piece of feedback for Anthropic.

## The 30-second checks that need a human at an IDE window (now RUN — see above)
1. VS Code panel: type `/fb` — does the custom skill list? *(High-confidence yes; the one fact a headless agent can't run.)*
2. VS Code panel: can a panel-invoked skill call `mcp__fb-assist__*`? *(MCP is "partial" in the panel — verify the tool calls resolve.)*
3. JetBrains: PyCharm CE → `claude` → `/fb` — confirm the zero-code claim end-to-end.
4. Panel: confirm `mcp__ide__getDiagnostics` is still panel-absent on the current build *(affects only the watcher's richest signal, not the core)*.
5. **Extension GUI "Give feedback":** submit free-text feedback from the `claude-sidebar` while capturing — does the `submitFeedback` RPC **attach the on-disk transcript** (→ swap-restore handoff applies) or send **description-only**? The one fact that finalizes the GUI handoff; the VS-Code-Extension analog of the claude.ai HAR.

**Net:** the IDE edge ships **today** via the integrated terminal (1:1 with the proven CLI), with the panel as graceful polish whose every gap has a working fallback. The only open items are four human-at-the-keyboard confirmations, none of which block the surface.
