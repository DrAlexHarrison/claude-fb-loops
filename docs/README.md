# docs

| Doc | What it is |
|---|---|
| [verification.md](verification.md) | the empirical proof: `/feedback` reads the on-disk transcript at submit (filesystem + network + code), so redacting it changes the upload |
| [request-id-anchor.md](request-id-anchor.md) | the API `request-id` anchor — tying a report to a real metered call, with a live capture ([request-id-live.json](request-id-live.json)) |
| [server-side-reference.md](server-side-reference.md) | the consent-genericize reference for referenced-not-inlined feedback (claude.ai 👎, VS Code per-message) |
| [desktop-chat-demo.md](desktop-chat-demo.md) | the claude.ai export co-pilot demo (genericize an exported `conversations.json`) |
| [ide-edge.md](ide-edge.md) | the IDE edge: how the keystone runs ~1:1 in VS Code / JetBrains / Cursor |
| [ide-human-checks-RUNBOOK.md](ide-human-checks-RUNBOOK.md) | the short manual IDE checks that confirm the invocation lanes |

The validated call-sequence playbook lives next to the code at
[`../fb-assist/INTEGRATION.md`](../fb-assist/INTEGRATION.md); the in-session wiring is in
[`../fb-assist/RUNTIME.md`](../fb-assist/RUNTIME.md).

See also: [`../GAPS.md`](../GAPS.md) (the parity ledger) and
[`../STRATEGY.md`](../STRATEGY.md).
