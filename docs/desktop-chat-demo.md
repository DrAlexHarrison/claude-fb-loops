# Build F — claude.ai / Desktop-chat surface: export-JSON co-pilot

**Surface:** the plain-chat surface of claude.ai (and the Claude Desktop webview, same data model).
**Module:** `fb_assist/desktop_chat.py` · **Tests:** `tests/test_desktop_chat.py` (20) · **Fixture:** `tests/fixtures/sample-export.json` (synthetic).

The hero: take an official claude.ai **Settings → Export Data** archive (`conversations.json`),
turn ONE chosen conversation into a privacy-safe, genericized feedback artifact, and show a
**before/after** view + an **effort signal**. 100% local, zero ToS exposure (operates only on data the
user pulled through Anthropic's own export channel; no automated access to claude.ai).

## Public API
```python
message_text(msg) -> str                       # robust: content[].text join OR bare text; dict OR Message; never raises
parse_export(source) -> list[Conversation]     # array OR jsonl twin OR loaded list; lenient, format-sniffed
iter_conversations(source) -> Iterator[Conversation]   # streaming (ijson if present, else json.load fallback)
select_conversation(source, *, uuid=None, needle=None, index=None) -> Conversation | None
redact_conversation(conv, *, level="genericize", use_gliner=False, quality=4,
                    alignment_confidence=5, reputation_token=None, verify=True) -> ConversationFeedback
genericized_conversation(conv, feedback) -> dict        # round-trippable deep copy; input never mutated
render_before_after(feedback, *, max_turns=None, only_changed=True, width=100, reveal=False) -> str
render_included_stripped(feedback) -> str               # built DIRECTLY from redaction_map
render_effort_signal(feedback) -> str                   # reuses package._render_effort_footer + the gate proof
render_report(feedback, *, max_turns=None, reveal=False) -> str
main(argv=None) -> int                                  # CLI; defaults to the synthetic fixture
```
`Message(uuid, sender, created_at, attachments, files, raw)` — `.role` (human→user), `.text` (both shapes).
`Conversation(uuid, name, created_at, updated_at, messages, raw, summary, account)`.
`ConversationFeedback(conversation_uuid, name, level, turns, redaction_map, effort_signal, floor_clean,
floor_residual, leak_candidates, genericize_ok, meaning_risk_flags, counts, rendered_after)`.

**Reuse (no module surgery):** every detector from `redact.py` (`scan_secrets`, `scan_pii`,
`apply_redactions`, `merge_redaction_spans`, `_token_label`, `leak_scan`, `_scan_pii_regex`,
`_scan_paths_text`); the genericize verification bar `genericize.verify_genericization`; the effort-signal
footer `package._render_effort_footer`. New = only the small lenient parser + the thin redaction driver.

## The two-layer egress gate (per INTEGRATION.md)
- **HARD floor (the gate, machine-decidable):** `scan_secrets` + the PII regex floor over the **actual
  rendered output bytes**. Empty ⇒ ship-able. The driver re-asserts this and exits non-zero if dirty.
- **SOFT layer (advisory):** `leak_scan` (incl. NER) over the rendered output yields *candidates* the
  co-author adjudicates / self-repairs. **Never a veto** — surfaced as `leak_candidates`.

The before/after BEFORE column is **short-masked by default** (`sk-…OO`), so the local preview itself
never prints a live secret while still proving the surrounding narrative is preserved. `--reveal` shows raw.

---

## Ran against the REAL export (read-only, no egress)
`~/claude-ai-export/conversations.json` — read locally, **never copied into the repo, never
committed, never sent anywhere**. The CLI default is the synthetic fixture; the real export is opt-in via
`--export`. Below: **structure + counts only** (no personal content).

**Export structure:** 87 conversations · 1,547 messages · ~1.52 M narrative chars · 17 conversations have
empty message bodies (a real export quirk — exported shells with no text). Message text appears in BOTH
shapes (`content[]` text-block array AND a bare top-level `text`); `message_text` reads either.

**Deterministic floor over the whole export's narrative: 0 secrets / 0 emails / 0 SSNs / 0 IPs / 0 paths.**
Consistent with a deliberately ToS-clean export — the visible human/assistant prose carries no credential
patterns. The semantic (NER) layer still finds names/orgs/locations/phones.

`python -m fb_assist.desktop_chat --export <real> --conversation <N> --json` (Presidio on, GLiNER off):

| conv | msgs (H/A) | turns redacted | redactions | entity TYPES found (counts only) | HARD floor | soft candidates |
|------|-----------|----------------|-----------|-----------------------------------|-----------|-----------------|
| `0` (`255e6a0b…`) | 25 (13/12) | 0 | **0** | — (empty-body conversation) | ✅ CLEAN | 0 |
| `1` (`a6a8aca9…`) | 2 (1/1) | 1 | **18** | PERSON×11, DATE_TIME×3, ORGANIZATION×3, NRP×1 | ✅ CLEAN | 11 |
| `4` (`d15f4fe8…`) | 6 (3/3) | 5 | **160** | LOCATION×115, ORGANIZATION×22, DATE_TIME×13, PERSON×8, NRP×2 | ✅ CLEAN | 45 |

**Reading it:** conv `0` proves graceful handling of the empty-body edge case (0 turns, gate clean). conv
`1`/`4` are the real hero — on real conversations the driver genericized **18** and **160** semantic
identifiers (names, orgs, locations…) and the **HARD deterministic floor over the output bytes was clean in
every case**. The SOFT NER layer still flagged candidates (e.g. a name detected in one position survives in
another the model didn't tag) — `genericize_verified=false` for `1`/`4`. That is **the design working, not
a failure**: the machine-decidable gate passes; the residual semantic candidates are exactly what a
co-author Opus (holding full context) would adjudicate before the user consents. The effort-signal
`quality`/`alignment_confidence` are co-author self-ratings (placeholder defaults in the offline lib).

### Sanitized before/after snippet (from the SYNTHETIC fixture — safe to show)
The real-export AFTER is real prose, so the illustrative snippet below is the synthetic fixture
(`--conversation 0`, default short-masked BEFORE). Same code path, planted FAKE sentinels:
```
BEFORE / AFTER  —  "Feedback flow keeps freezing on submit"  (level=genericize)

┌─ turn 0 · Human · redacted: ANTHROPIC_KEY, EMAIL_ADDRESS, PHONE_NUMBER
│ BEFORE: The /feedback flow keeps FREEZING when I hit submit. I'm logged in with my key sk-…OO and my email …
│ AFTER : The /feedback flow keeps FREEZING when I hit submit. I'm logged in with my key ‹ANTHROPIC_KEY› and …
└─
┌─ turn 2 · Human · redacted: FS_PATH, IP_ADDRESS, ORGANIZATION, US_SSN
│ BEFORE: Rotated. My S… for the support ticket is 123…89 and the box is at 10.…42. The repo lives at /ho…g.
│ AFTER : Rotated. My ‹ORGANIZATION› for the support ticket is ‹US_SSN› and the box is at ‹IP_ADDRESS›. The r…
└─
Genericized feedback artifact — what it contains:
  INCLUDED : 4 turns (2 human, 2 assistant)  — 750 bytes
  STRIPPED : 10 values across 4 turns
    by category : 3×ORGANIZATION, 1×ANTHROPIC_KEY, 1×EMAIL_ADDRESS, 1×PHONE_NUMBER, 1×AWS_ACCESS_KEY, 1×US_SSN, 1×IP_ADDRESS, 1×FS_PATH

[fb-assist effort signal] redaction=genericize; quality=4; alignment_confidence=5
[egress gate] deterministic floor over 750 output bytes: ✅ CLEAN (0 residual secrets/PII)
[egress gate] soft NER leak_scan candidates: 1 (advisory — co-author adjudicates; not a veto)
[genericize]  verification bar: PASS
```
The meaning survives ("the /feedback flow keeps FREEZING") while every value becomes a categorized
`‹MARKER›`; the deterministic floor over the shipped artifact is **CLEAN**. Same effort-signal schema as the
CLI and API edges = the literal "one platform, every surface plugs into."

---

## Product recommendation — a server-side, consent-gated genericize step (Anthropic-only)

**The architectural fact that forces it.** A captured-and-analyzed HAR of the claude.ai thumbs-down flow
confirms the feedback request is `POST …/chat_messages/<msg>/chat_feedback` with a body of just
`{type, reason}` — **97 bytes, HTTP 201 — and the conversation text is never in the request body.** The
conversation is referenced only by the UUIDs in the URL; Anthropic's server attaches its own stored copy
server-side. Because the feedback request only *references* the conversation and never *carries* it, redaction
**must** happen server-side — **there is no client-side seam to redact at.** This is also why a browser-side
interceptor is not merely against ToS but **architecturally impossible**, and stays out of scope (below).

**The recommendation.** Add a **consent-gated genericize-before-attach step inside Anthropic's own feedback
path**: when a user thumbs-down or Shares-for-research, run the fb-assist core **server-side** to produce a
genericized snapshot plus an **included/stripped consent summary the user OKs** *before* the interaction is
stored against the report or forwarded to an engineer. The precedent that de-risks the ask already ships:
claude.ai's **"Share" already genericizes server-side** (it strips raw MCP data and files at snapshot time),
so this is a natural extension of a shipping pattern, not a new capability class. It also directly fulfils
Anthropic's own Sept-2025 commitment to *"develop infrastructure and tooling to better debug
community-sourced feedback without sacrificing user privacy"* — the stated blocker (privacy controls keep
engineers from examining problematic interactions) is unlocked precisely by a **user-consented, genericized**
repro.

**Why this is the clean two-answer story.** The user-side answer is the **export-JSON co-pilot** in this
build — it gives a user a privacy-safe, genericized artifact today, with zero ToS exposure, that they can
carry into any first-party feedback or `support@` thread. The Anthropic-side answer is the **server-side
consent-genericize step** — the same fb-assist core (the `redact.py` detectors + the genericize verifier +
the effort-signal schema), run where the conversation already lives. The anchor on this surface:
**the server already holds the conversation**, so the user contributes only *consent + the genericized delta
+ the effort signal* — minimal added content. Closing exactly this privacy-vs-feedback
gap is the work the Feedback Loops role exists to do.

## Out of scope — the browser interceptor (definitively)
A client-side extension that reads the 👎 POST and rewrites the conversation **cannot exist**: per the HAR
above, the POST is 97 bytes and references the conversation by UUID only — there is no conversation in the
request to redact. Beyond ToS (the Apr-2026 "no automated means" crackdown), it is architecturally a dead
end. Not built, not recommended. The interceptor PoC is **blocked-by-design**, superseding the earlier
"blocked on a HAR" note.
```
USE_TF=0 python -m pytest -q   # full suite green; tests/test_desktop_chat.py = 20
```
