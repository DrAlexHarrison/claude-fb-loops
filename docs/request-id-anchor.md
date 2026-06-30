# FIX-5 — `response._request_id` is the ungameable anchor (verification)

**Binding fix (`plans/00-MANDATORY-FIXES.md` #5):** verify `response._request_id`
on a LIVE instance across `messages.create`, `stream.get_final_message()`, and
`messages.parse()`, plus `AnthropicBedrock`/`AnthropicVertex` (header may be
absent → deterministic fallback). The whole "ungameable anchor" rests on this.

## Which path I took: **documented-script + partial-live + mocks** (honest)

A real `sk-ant-api…` key IS present at `~/.config/anthropic/api_key`, so I tried
the live call. It was **blocked by billing, not by key availability**:

```
anthropic.BadRequestError: 400 - "Your credit balance is too low to access the
Anthropic API." request_id: req_011CcYmw6apDW1kzQt8gvW6x
```

Per FIX-5 ("do NOT spend money or block") I did NOT escalate. What I banked:

1. **Partial LIVE evidence (zero cost).** Even the 400 error carries a
   `request-id` header — captured live: `req_011CcYmw6apDW1kzQt8gvW6x`,
   confirmed `req_`-shaped. This proves the **header mechanism is real and
   `req_`-prefixed on actual HTTP calls** (`verification-evidence/request-id-live.json`).
2. **SDK-source proof of attribute population.** `anthropic 0.76.0`
   `_models.py:764 add_request_id(obj, request_id)` does `obj._request_id =
   request_id`; it is called from the response plumbing (`_legacy_response.py:144`,
   `_response.py`) on the **top-level** object. Confirmed `_request_id` is **absent
   from `Message.model_fields`** (`['id','content','model','role','stop_reason',
   'stop_sequence','type','usage']`) — exactly why a type check is insufficient and
   the live attribute read is the only honest check.
3. **Runnable full-proof script.** `scripts/verify_request_id.py` — one command
   completes the live success-path assertion the moment the account has credits:
   ```
   python scripts/verify_request_id.py
   ```
   It asserts `create()._request_id.startswith("req_")`, probes the streaming
   header, and records `messages.parse` availability.
4. **Unit tests against mocks** (`tests/test_claude_repro.py`) cover the extraction
   logic for: a `Message`-like object exposing `_request_id`; a plain dict; a
   stream object exposing `.request_id`; and the **Bedrock/Vertex deterministic
   fallback** when the header is absent.

## Streaming path — a real finding, baked into the wrapper

`stream.get_final_message()` returns an **accumulated snapshot** built from SSE
events; `add_request_id()` is NOT guaranteed to run on it, so
`final_message._request_id` may be `None`. The reliable streaming anchor is
**`stream.request_id`** — a property that reads the `request-id` **response
header** off the underlying raw stream (`anthropic/lib/streaming/_messages.py:50`).
`ReportingClient`'s streaming recorder therefore reads `stream.request_id` (header)
and falls back to `getattr(final, "_request_id", None)`.

## `messages.parse` — does NOT exist on `anthropic 0.76.0`

`hasattr(client.messages, "parse")` is `False` here. When present in a newer SDK it
routes through the same `add_request_id()` path as `create`, so
`extract_request_id()` (which reads `getattr(obj, "_request_id", None)` first) covers
it with no special-casing. The extractor is robust to its absence.

## Bedrock / Vertex (`AnthropicBedrock` / `AnthropicVertex`)

Both client classes exist in 0.76.0. The Anthropic `request-id` header may be
ABSENT there (provider returns its own id). `anchor_for()` detects a missing /
non-`req_` id on a non-anthropic provider and falls back to a **deterministic
anchor** `{provider, provider_id (message id), model, usage, fingerprint}` —
`verifiable: False`, clearly flagged. Not live-verified (no AWS/GCP creds wired);
the fallback branch is unit-tested.

**Bottom line:** header mechanism is live-confirmed; attribute population is
SDK-source-confirmed + mock-tested; full live success assertion is one
`scripts/verify_request_id.py` run away once the account has credits.
