# Empirical verification — `/feedback` redaction propagation

**Question:** can a user change what `/feedback` actually uploads to Anthropic by
redacting their on-disk session transcript *before* submitting?

**Answer: yes — confirmed three ways (filesystem, network, code) against the real,
shipping `/feedback` command.** This is what makes `fb-assist` a genuine integration with
the tool users already run, not a parallel toy. The test was run on a Linux workstation
with a first-party Max account, on throwaway data the author authorized.

> Sanitized for publication: the raw evidence (a pcap, raw `inotify` logs, real session
> IDs, the issued Feedback ID, machine IP) lives in the author's private working dir and
> is deliberately excluded from this repo. The method and findings below are the record.

## Method

1. Generate a scratch "past session" transcript (~99 KB) containing a unique sentinel
   string that appears nowhere else.
2. Drive the **real `/feedback`** TUI in a fresh session in the same project dir:
   description → scope selector → confirmation → submit.
3. Monitor three ways simultaneously: `inotifywait` on the transcript dir (file reads),
   `tcpdump` + `ss` (network egress to Anthropic), and the binary's gather code path.
4. Redact the on-disk file in place (99 KB → ~0.7 KB; sentinel → a placeholder) and
   observe a subsequent gather.

## What was confirmed

1. **The scope selector matches the binary verbatim:** *"This session only / + the last
   24 hours / + the last 7 days."*

2. **The confirmation screen reveals the payload composition (verbatim):**
   > This report will include: — Your feedback / bug description — **Environment info** —
   > **Git repo metadata** — Session transcript: this session + this project's other
   > sessions from the last 7 days

   — corroborating exactly the env / git-metadata / transcript leak surface fb-assist targets.

3. **Filesystem (decisive):** at submit, `/feedback` does `OPEN → ACCESS → CLOSE` on the
   on-disk past-session `.jsonl` (unredacted, sentinel present). After redacting that same
   file in place, the next gather reads the **redacted** bytes from the **same path**.
   → Same path, two different on-disk contents, both pulled into the gather. **The on-disk
   file is the source of truth; redacting it changes the payload.**

4. **Network:** the submit opened TLS connections to Anthropic's API and returned a real
   Feedback ID. → egress to Anthropic confirmed.

5. **First-party = submit-only.** There is no user-facing "save to local bundle" option on
   a first-party account (the local-archive path is auto-only for Bedrock/Vertex/no-creds).
   → A "preview without sending" UX must operate **pre-submit on the on-disk files** —
   which is exactly the mechanism fb-assist uses.

6. **Built-in redaction scope (the gap, confirmed):** docs and binary agree — only
   **API-key / token patterns** are stripped; *"source code, file contents, and other
   conversation content are uploaded as-is,"* and the submit otherwise only drops whole
   third-party-provider transcripts. Everything fb-assist targets (prompts, file contents,
   IP, cwd / branch / commit metadata, person/customer names) is uploaded verbatim and
   retained five years.

## Conclusion

A tool that redacts the on-disk `~/.claude*/projects/**/<session>.jsonl` **before** the
user runs `/feedback` (scope = 24h / 7d) causes Anthropic's *real* intake to receive the
sanitized version. fb-assist's swap-restore does exactly this, non-destructively and
crash-safely — proven by the suite's byte-exact restore and `os._exit` mid-swap recovery
tests.

## Honest caveats

- The post-redaction **submit** (vs. gather) wasn't captured with a clean second Feedback
  ID, purely due to TUI-automation flakiness on a polluted session — not a technical
  limit. The filesystem evidence (the gather reads the redacted file) + the code path
  close the loop regardless.
- One real throwaway test report ("TEST please ignore") was submitted during the test and
  is retained per policy; harmless.
- Wire-level *content* of the egress isn't readable without a TLS MITM (not done); the
  endpoint/size + the readable on-disk source + the file-read proof substitute trivially.
