# Security & privacy posture

## fb-assist is a best-effort redaction aid, NOT a guarantee

fb-assist exists to make the privacy decision *cheaper and more legible* before you send
feedback — not to certify that a bundle is safe. Its protection is two layers:

1. A **deterministic floor** — regex secret detection, regex PII (email/SSN/IP), absolute
   paths, and structural strips of whole categories (file contents, tool output, env
   metadata, paths). This layer is machine-decidable and is what the hard guarantees and
   the egress gate rest on.
2. A **semantic recall layer** — NER (Presidio / GLiNER) for names, organizations, and
   novel PII. NER has false negatives on unusual inputs; it is a recall aid, not a proof.

**Therefore: always review the preview before you confirm a send.** The preview (the
`diff_preview` gate) shows exactly what is included and what was redacted. The tool never
sends anything you did not confirm, and the swap-restore is non-destructive — your
original transcript is restored byte-for-byte after the submit, with a crash-safe journal
if the process dies mid-swap.

Do not rely on fb-assist as your only control for highly sensitive material. When in
doubt, strip more, or don't send the transcript at all (you can send the description
alone).

## Reporting a vulnerability

This is a personal open-source project. If you find a security issue — especially a way a
secret or PII value could survive into a sent bundle — please open a GitHub issue marked
**[security]**, or, if it is sensitive, request a private channel in a minimal issue
before sharing details. Please do not include real secrets in a report; the test corpus
shows the synthetic-plant convention to use instead.

## Secret-scanning ourselves

CI runs `gitleaks` against the repository on every push (the same class of tool fb-assist
shells out to). The only allow-listed matches are the deliberately synthetic planted
secrets in the test corpus and the README demo (`.gitleaks.toml`). A `make scrub-gate`
check (also a CI gate) additionally asserts that no real personal data — home paths,
personal identifiers — survives in any tracked file.
