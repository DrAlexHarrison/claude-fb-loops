# Server-side consent-genericize — a drop-in reference for the claude.ai feedback flow

> A drop-in reference implementation for the claude.ai thumbs-down flow. The caller
> implements **three methods** (the adapter ports); the genericize-before-attach core,
> the two-pass re-identification verify, the fail-closed egress gate, and the audit
> record are provided.

Module: [`fb_assist/server_side.py`](../fb_assist/server_side.py) · Tests:
[`tests/test_server_side.py`](../tests/test_server_side.py) · Runnable demo:
`python -m fb_assist.server_side`.

---

## 1. The problem, and why it can only be fixed server-side

The claude.ai feedback contract (observed in an authenticated HAR of **my own**
account — a public API surface) is:

```
POST /api/organizations/<org>/chat_conversations/<conv>/chat_messages/<msg>/chat_feedback
body: { "type": "...", "reason": "<text>" }          (~97 bytes, 201 Created)
```

The conversation is identified **purely by the URL UUIDs** and is **never sent in
the request body** — the server attaches its own stored copy. That single fact is
decisive:

- There is **no client-side seam** to redact a conversation that doesn't travel in
  the request. A browser interceptor has nothing to intercept. (It is also
  ToS-hostile post-Apr-2026 — explicitly out.)
- So the privacy step has to run **where the conversation actually is: server-side**,
  at the moment feedback is captured, *before* the stored copy is attached to a
  feedback record an engineer will later read.

The referenced **message UUID** is the verifiable anchor here — exactly analogous to
the Messages-API `request-id`. It ties the report to a real stored message with zero
extra user content.

This is also **Anthropic's own stated commitment.** The Sept-2025 postmortem:
*"we'll develop infrastructure and tooling to better debug community-sourced feedback
without sacrificing user privacy,"* with the named blocker being that privacy
controls prevent engineers from examining problematic interactions. A
**user-consented, genericized** snapshot is exactly that unlock. And claude.ai's
**"Share" already genericizes server-side** (it strips MCP raw data + files at
snapshot time) — so this is a natural extension of a *shipping pattern*, not a new
capability class.

---

## 2. The principled boundary (this is load-bearing)

This reference is built **only** against two **publicly-observable, ToS-clean facts**,
and uses **ZERO Anthropic-internal knowledge**:

1. **The feedback contract** above (from my own authenticated HAR — public API surface).
2. **The conversation schema** from the **public export feature**
   (Settings → Privacy → Export — the same shape the stored copy uses):
   `conversation { uuid, name, account, created_at, chat_messages[] }`;
   `message { uuid, sender ("human"|"assistant"), text, content[], ... }`;
   `content[] { type, text, ... }`. Message text is EITHER `content[].text` joined OR
   the bare top-level `text` — both handled.

**Everything I cannot know about your internals — where conversations are stored,
how consent is captured, how feedback is persisted — is an adapter PORT.** I make
**zero claims** about your real databases or services. You implement three small
interfaces; the privacy-bearing core is done.

---

## 3. The three adapter ports (what you implement)

All three are `typing.Protocol`s — structural, no base class to inherit.

| Port | Method | The seam it abstracts |
|---|---|---|
| `ConversationStore` | `fetch(org_id, conversation_id, message_id) -> Conversation \| None` | Wherever your conversations actually live. Return the stored conversation **in the public export schema** (`fb_assist.desktop_chat.Conversation`). Return `None` if unavailable → the handler fails closed. |
| `ConsentPolicy` | `decision(user_id, org_id, conversation_id) -> ConsentDecision` | The **NEW product surface**: the user's consent + genericize preference (`none` / `genericized` / `raw`). Implement against your real consent capture. |
| `FeedbackSink` | `attach(feedback_id, artifact, audit) -> None` | Wherever the feedback record is persisted. The artifact is already privacy-clean; you only durably write it. |

`ConsentDecision` fields: `attach` (`"none"` / `"genericized"` / `"raw"`), `scope`
(opaque to this reference, e.g. `"message"` vs `"conversation"`), plus reference
conveniences carried into the audit — `basis`, `reason`, and `genericize_terms`
(org/profile-named codenames or IP strings that MUST NOT survive; masked literally
**and** verified absent).

The repo ships **working reference adapters** of all three so the whole thing runs
today: `InMemoryConversationStore.from_export(path)`, `StaticConsentPolicy(decision)`,
`InMemoryFeedbackSink`. Swap each for your real implementation.

---

## 4. What's already done for you (the core)

`handle_feedback(event, *, store, consent, sink, ...)` is the whole step:

1. **Parse** the feedback `event` (`{org, conversation, message, type, reason}`).
2. **Consent** — `consent.decision(...)`:
   - `none` → attach only `{type, reason}` — **today's behavior**, no conversation
     text leaves. The privacy-safe default.
   - `genericized` → `store.fetch(...)` → **genericize-before-attach**: reuse the
     `redact` deterministic floor (`scan_secrets`, `scan_pii`, paths) **+** literal
     codename masking **+** the two-pass `verify_genericization` (re-identification
     bar). Attach the **sanitized** conversation + a redaction/effort summary + the
     **message-UUID anchor**.
   - `raw` → explicit power-user opt-in; attach raw, loudly flagged.
3. **Audit** — emit an `AuditRecord`: which categories were redacted, the re-id
   verify verdict, the consent basis, the anchor. It carries **only categories +
   verdicts**, never a raw value, so the audit itself is safe to store.
4. **HARD FAIL-CLOSED GATE** — the deterministic floor (`scan_secrets` +
   `_scan_pii_regex`) is re-run **authoritatively** over the actual outbound bytes
   (the rendered markdown **and** the round-trip conversation dict). Any residue, or
   a failed re-id verify → **attach `none` + a flag, never the leaky artifact.**

The genericize step is itself a documented seam (`genericize=` parameter, default
`genericize_for_attach`). This is exactly where you plug in a **Claude-powered
semantic rewrite** for open-ended IP that no regex or NER catches — and because the
hard gate is enforced over whatever it returns, a stronger genericizer can only ever
make the output *safer*. The default deterministic core already passes the gate.

---

## 5. Wire it into your service — 6 steps

```python
from fb_assist.server_side import (
    FeedbackEvent, handle_feedback,
    ConversationStore, ConsentPolicy, FeedbackSink,   # the three Protocols
    ConsentDecision,
)
from fb_assist.desktop_chat import Conversation, iter_conversations

# 1. ConversationStore — return YOUR stored conversation in the export schema.
class MyStore:
    def fetch(self, org_id, conversation_id, message_id) -> Conversation | None:
        row = my_db.load_conversation(org_id, conversation_id)      # <-- your storage
        if row is None:
            return None
        # Adapt your row -> the public export shape, then lift to a typed Conversation:
        return next(iter_conversations([row.to_export_dict()]), None)

# 2. ConsentPolicy — model the new toggle (default to genericized).
class MyConsent:
    def decision(self, user_id, org_id, conversation_id) -> ConsentDecision:
        pref = my_consent_service.get(user_id, conversation_id)     # <-- your consent capture
        return ConsentDecision(attach=pref.attach,                  # "none"|"genericized"|"raw"
                               scope=pref.scope,
                               basis=pref.provenance,
                               genericize_terms=my_org_codenames(org_id))

# 3. FeedbackSink — persist the sanitized artifact + audit.
class MySink:
    def attach(self, feedback_id, artifact, audit) -> None:
        my_feedback_store.write(feedback_id, artifact.to_dict(), audit.to_dict())  # <-- your store

# 4. In your existing POST .../chat_feedback handler, build the event from the URL +
#    body (the body is just {type, reason}; org/conv/msg come from the path; the
#    user comes from your auth):
event = FeedbackEvent.from_request(
    org_id=org, conversation_id=conv, message_id=msg, body=request.json, user_id=auth.user_id,
)

# 5. Call the core. Everything privacy-bearing happens here.
result = handle_feedback(event, store=MyStore(), consent=MyConsent(), sink=MySink())

# 6. Return the minimal 201 body (NO conversation text — only status + anchor + audit).
return 201, result.to_public_dict()
```

`fb_assist/server_side.py` also ships an **illustrative** stdlib `http.server`
endpoint (`make_reference_app(store, consent, sink)`) that shows this exact wiring
over a real socket — useful to read, not meant as production HTTP plumbing.

---

## 6. The consent UX (the one new product surface)

The only genuinely new thing to build is the **consent toggle**, surfaced in the
thumbs-down sheet (or account settings):

- **Off / "Just my rating"** → `attach="none"` — exactly today's behavior; nothing
  but `{type, reason}` is stored. This stays the default until the user opts in.
- **"Include a privacy-scrubbed copy of this conversation"** → `attach="genericized"`
  — the recommended middle. The user can be shown the **before/after** preview
  (categories + counts) so they consent to *exactly* what it contains.
- **"Include the full conversation" (advanced)** → `attach="raw"` — explicit
  opt-in, loudly flagged in the audit.

Because the genericize step runs server-side **before** anything is persisted, the
consent decision governs precisely what an engineer can later see.

---

## 7. The fail-closed guarantee

> A `genericized` artifact is attached **only** if the deterministic floor
> (`scan_secrets` + `_scan_pii_regex`) over the **actual outbound bytes** is empty
> **and** the two-pass re-identification verify passed. On any residue — or if a
> custom genericizer misbehaves — the step **fails closed**: it attaches the
> privacy-safe `{type, reason}` artifact + a flag (`fail_closed`,
> `residual_floor_leak` / `reid_verify_failed`), and records why in the audit. **It
> never ships the leaky artifact.** Feedback is never blocked; it just degrades to
> the safe default.

The gate is re-run **inside the handler**, over the real bytes, independent of what
the genericize step self-reports — so a stronger (e.g. Claude-powered) rewrite can
only make the output safer, never bypass the floor. (See
`test_fail_closed_on_residual_floor_leak` and `test_fail_closed_on_reid_verify_failure`.)

---

## 8. Try it

```bash
# Genericized (default consent), on the synthetic fixture — prints the sanitized
# artifact that WOULD be attached + the audit:
python -m fb_assist.server_side --genericize-term "Project Halcyon"

# The privacy-safe default (no conversation text attached):
python -m fb_assist.server_side --consent none

# The illustrative HTTP endpoint (shows the exact POST seam):
python -m fb_assist.server_side --serve

# The tests (force USE_TF=0):
USE_TF=0 pytest tests/test_server_side.py -q
```

Everything runs on a **synthetic** fixture
(`tests/fixtures/sample-feedback-conversation.json`) — no real data, no
Anthropic-internal assumptions, only the public contract + export schema.
