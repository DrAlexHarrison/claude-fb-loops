# The `request-id` provenance anchor

`claude_repro` ties each reported API interaction to a real, metered call by recording
the Anthropic `request-id` (`req_…`) the SDK exposes per response. That id is the
verifiable anchor: it can be correlated to Anthropic's own server-side log without any
extra user content. This note records how the anchor is sourced and what's verified.

## What's verified

1. **Live header evidence (zero cost).** Even a `400` response carries a `request-id`
   header — captured live as `req_011CcYmw6apDW1kzQt8gvW6x`, confirmed `req_`-shaped
   (`verification-evidence/request-id-live.json`). The header mechanism is real and
   `req_`-prefixed on actual HTTP calls.
2. **SDK-source proof of attribute population.** In `anthropic 0.76.0`,
   `_models.py add_request_id(obj, request_id)` sets `obj._request_id = request_id`
   from the response plumbing on the top-level object. `_request_id` is **absent from
   `Message.model_fields`** (`['id','content','model','role','stop_reason',
   'stop_sequence','type','usage']`) — which is exactly why a type check is insufficient
   and a live attribute read is the honest check.
3. **Runnable verification script.** `scripts/verify_request_id.py` completes the live
   success-path assertion in one command once the account has credits:
   ```
   python scripts/verify_request_id.py
   ```
   It asserts `create()._request_id.startswith("req_")`, probes the streaming header,
   and records `messages.parse` availability.
4. **Unit tests against mocks** (`tests/test_claude_repro.py`) cover the extraction
   logic for: a `Message`-like object exposing `_request_id`; a plain dict; a stream
   object exposing `.request_id`; and the **Bedrock/Vertex deterministic fallback**
   when the header is absent.

## Streaming path — a real finding, baked into the wrapper

`stream.get_final_message()` returns an **accumulated snapshot** built from SSE
events; `add_request_id()` is NOT guaranteed to run on it, so
`final_message._request_id` may be `None`. The reliable streaming anchor is
**`stream.request_id`** — a property that reads the `request-id` **response
header** off the underlying raw stream (`anthropic/lib/streaming/_messages.py`).
`ReportingClient`'s streaming recorder therefore reads `stream.request_id` (header)
and falls back to `getattr(final, "_request_id", None)`.

## `messages.parse` — does NOT exist on `anthropic 0.76.0`

`hasattr(client.messages, "parse")` is `False` here. When present in a newer SDK it
routes through the same `add_request_id()` path as `create`, so
`extract_request_id()` (which reads `getattr(obj, "_request_id", None)` first) covers
it with no special-casing. The extractor is robust to its absence.

## Bedrock / Vertex (`AnthropicBedrock` / `AnthropicVertex`)

Both client classes exist in 0.76.0. The Anthropic `request-id` header may be
ABSENT there (the provider returns its own id). `anchor_for()` detects a missing /
non-`req_` id on a non-anthropic provider and falls back to a **deterministic
anchor** `{provider, provider_id (message id), model, usage, fingerprint}` —
`verifiable: False`, clearly flagged. Not live-verified (no AWS/GCP creds wired);
the fallback branch is unit-tested.

**Bottom line:** the header mechanism is live-confirmed; attribute population is
SDK-source-confirmed + mock-tested; the full live success assertion is one
`scripts/verify_request_id.py` run away once the account has credits.
