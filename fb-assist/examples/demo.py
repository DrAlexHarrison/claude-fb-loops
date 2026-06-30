#!/usr/bin/env python3
"""`make demo` — watch fb-assist redact a planted-secret session end-to-end.

Builds a small, schema-faithful Claude Code transcript whose human turn pastes a
live-looking Anthropic key, an AWS key, a GitHub token, an email, an SSN, an IP,
and an absolute path — plus a Read body and a Bash stdout burying more secrets.
Then it runs the validated co-author call-sequence and prints, plainly:

    BEFORE   the raw transcript excerpt (secrets/PII visible)
    PREVIEW  the concise INCLUDED / STRIPPED gate summary
    AFTER    the sanitized bundle (values gone, meaning intact)
    RESTORE  the original transcript back on disk, byte-exact (sha256)

DOWNLOAD-FREE & OFFLINE. The hard guarantees (every planted secret/PII/path
absent from the actual upload bytes) come from the DETERMINISTIC floor — pure
regex + structural strips + the crash-safe swap-restore — so this runs with a
bare `pip install` and zero model downloads. If the optional NER stack happens
to be installed it additionally masks the person name; that is a bonus, never a
gate. No network. No real `/feedback` is invoked. No real transcript is touched.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

# Importable straight from a source checkout.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import transcripts as T  # noqa: E402
from fb_assist import redact as R  # noqa: E402
from fb_assist import package as P  # noqa: E402

PLANTED = {
    "anthropic_key": "sk-ant-api03-AAAA1111BBBB2222CCCC3333DDDD4444",
    "aws_key": "AKIAZZ44QQ55WW66EE77",
    "github_token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789AB",
    "email": "dana.canary@secret-corp.example",
    "ssn": "123-45-6789",
    "ip": "10.77.88.99",
    "person": "Marlene Vasquez",
    "path": "/home/devuser/code/secret-proj",
}
# Sentinels whose absence is HARD-asserted (all deterministically detectable).
HARD_SENTINELS = [PLANTED[k] for k in
                  ("anthropic_key", "aws_key", "github_token", "email", "ssn", "ip", "path")]
SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _env(uuid, parent, ts, **extra):
    base = {"uuid": uuid, "parentUuid": parent, "isSidechain": False,
            "sessionId": SESSION_ID, "timestamp": ts, "cwd": PLANTED["path"],
            "gitBranch": "feature/secret-x", "version": "2.1.195", "userType": "external"}
    base.update(extra)
    return base


def build_planted_transcript() -> list[dict]:
    return [
        {"type": "agent-color", "agentColor": "red", "sessionId": SESSION_ID},
        {"type": "ai-title", "aiTitle": "feedback flow freezes on submit", "sessionId": SESSION_ID},
        _env("u-prompt", None, "2026-06-29T18:00:00.000Z", type="user",
             promptSource="typed", entrypoint="cli",
             message={"role": "user", "content": (
                 f"I'm {PLANTED['person']} and I build the Contoso API. While debugging I "
                 f"pasted my key {PLANTED['anthropic_key']} and AWS {PLANTED['aws_key']} into "
                 f"the repl. Reach me at {PLANTED['email']} or {PLANTED['ip']}, SSN "
                 f"{PLANTED['ssn']}. The real bug: the /feedback flow keeps FREEZING on submit.")}),
        _env("a-text", "u-prompt", "2026-06-29T18:00:05.000Z", type="assistant", requestId="req_abc",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "thinking", "thinking": "User pasted a live key; note to self.",
                  "signature": "AAAABBBB=="},
                 {"type": "text", "text": "Got it — the submit freeze is the real issue. Let me check the config."}]}),
        _env("a-read", "a-text", "2026-06-29T18:00:06.000Z", type="assistant",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "tool_use", "id": "toolu_read1", "name": "Read",
                  "input": {"file_path": f"{PLANTED['path']}/config.py"}}]}),
        _env("u-read", "a-read", "2026-06-29T18:00:07.000Z", type="user",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_read1",
                  "content": f'AWS_SECRET = "{PLANTED["aws_key"]}"\nOWNER = "{PLANTED["email"]}"\n'}]},
             toolUseResult={"file": {"content": f'AWS_SECRET = "{PLANTED["aws_key"]}"\nOWNER = "{PLANTED["email"]}"\n',
                                     "filePath": f"{PLANTED['path']}/config.py", "numLines": 2}}),
        _env("a-bash", "u-read", "2026-06-29T18:00:08.000Z", type="assistant",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "tool_use", "id": "toolu_bash1", "name": "Bash",
                  "input": {"command": "cat .env | grep TOKEN"}}]}),
        _env("u-bash", "a-bash", "2026-06-29T18:00:09.000Z", type="user",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_bash1",
                  "content": f"GITHUB_TOKEN={PLANTED['github_token']}\n"}]},
             toolUseResult={"stdout": f"GITHUB_TOKEN={PLANTED['github_token']}\n",
                            "stderr": "", "interrupted": False}),
    ]


# Bulk categories stripped wholesale; narrative categories kept but char-masked.
STRIP = ["file_contents", "bash_output", "tool_calls", "thinking_blocks",
         "hook_output", "injected_memory", "env_metadata", "paths"]
KEEP_BUT_MASK = ["human_prompts", "assistant_text"]


def _deterministic_findings(text: str) -> list:
    """Regex secrets + regex PII + filesystem paths — NO models, NO network."""
    out = R.scan_secrets(text) + R._scan_pii_regex(text) + R._scan_paths_text(text)
    try:  # bonus: presidio/gliner if installed (never required)
        out += R.scan_pii(text, use_gliner=False)
    except Exception:
        pass
    return out


def _mask_narrative(raws: list[dict]) -> list[dict]:
    rmap: list[dict] = []
    for i, raw in enumerate(raws):
        rec = T.Record(line=i + 1, raw=raw, type=str(raw.get("type", "")))
        for sp in list(T.human_prompts([rec])) + list(T.assistant_text([rec])):
            findings = _deterministic_findings(sp.text)
            chosen = R.merge_redaction_spans(findings)
            if not chosen:
                continue
            masked, _ = R.apply_redactions(sp.text, findings, style="mask")
            if masked == sp.text:
                continue
            T.replace_span(raw, sp, masked)
            for f in chosen:
                rmap.append({"uuid": sp.uuid, "category": f.entity, "original": f.text,
                             "replacement": f"‹{R._token_label(f.entity)}›", "count": 1})
    return rmap


def main() -> int:
    print("=" * 72)
    print("fb-assist demo — privacy-preserving /feedback flow (download-free, offline)")
    print("=" * 72)

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        path = d / f"{SESSION_ID}.jsonl"
        path.write_bytes(P.serialize_records(build_planted_transcript()))

        # 1) parse + 2) locate.
        records = list(T.parse(str(path)))
        original_raws = [r.raw for r in records]
        loc = T.redaction_map(records)["summary"]

        before = (records[2].raw["message"]["content"])
        print("\n[ BEFORE ]  the human turn as it sits on disk (secrets visible):")
        print("    " + before[:300].replace("\n", "\n    "))

        # 3) redact: bulk strip + char-precise narrative mask.
        sanitized = R.strip_categories(original_raws, STRIP, mode="replace")
        bridge = _mask_narrative(sanitized)

        # 4) assemble (<1 MB) + 5) preview.
        payload = P.assemble_payload(
            "The /feedback submit flow freezes every time; secrets/PII were redacted by fb-assist.",
            {str(path): sanitized}, limit=1_000_000,
            effort_signal={"redaction": "surgical", "quality": 4, "alignment_confidence": 5})
        preview = P.diff_preview(original_raws, sanitized, redaction_map=bridge)
        print("\n[ PREVIEW ]  the gate the user confirms before anything ships:")
        for line in preview.render().splitlines():
            print("    " + line)

        upload_bytes = payload.targets[str(path)]
        upload_text = payload.description + "\n" + upload_bytes.decode("utf-8")

        after = next((T.Record(line=i + 1, raw=r, type="user").raw["message"]["content"]
                      for i, r in enumerate(sanitized) if r.get("uuid") == "u-prompt"), "")
        print("\n[ AFTER ]  the same turn in the sanitized bundle (values gone, meaning kept):")
        print("    " + str(after)[:300].replace("\n", "\n    "))

        # 6) swap-restore around a simulated submit (non-destructive).
        orig_sha = hashlib.sha256(path.read_bytes()).hexdigest()
        with P.swap_restore(payload.targets, backup_root=str(d / "bk"), settle_s=0.02):
            during = path.read_bytes()
        after_sha = hashlib.sha256(path.read_bytes()).hexdigest()

        # 7) egress gate — deterministic floor over the ACTUAL upload bytes.
        upload_secrets = R.scan_secrets(upload_text)
        upload_pii = R._scan_pii_regex(upload_text)
        upload_paths = R._scan_paths_text(upload_text)

        print("\n[ RESTORE ]  original transcript back on disk after submit:")
        print(f"    during-swap on disk == sanitized bytes : {during == upload_bytes}")
        print(f"    restored byte-exact (sha256 matches)   : {after_sha == orig_sha}")

        # Hard checks.
        ok = True
        leaked = [s for s in HARD_SENTINELS if s in upload_text]
        if leaked:
            ok = False
            print(f"\n  !! LEAK: sentinel survived in upload bytes: {leaked}")
        if upload_secrets or upload_pii or upload_paths:
            ok = False
            print(f"  !! egress floor non-empty: secrets={len(upload_secrets)} "
                  f"pii={len(upload_pii)} paths={len(upload_paths)}")
        if after_sha != orig_sha:
            ok = False
            print("  !! original NOT restored byte-exact")
        person_masked = PLANTED["person"] not in upload_text
        print(f"\n    deterministic floor: {len(HARD_SENTINELS)} planted secrets/PII/paths, "
              f"{len(HARD_SENTINELS) - len(leaked)} removed from upload bytes")
        print(f"    person-name (NER, optional): {'masked' if person_masked else 'left (NER not installed)'}")
        print("=" * 72)
        print("RESULT:", "GREEN — fb-assist redacted the session end-to-end." if ok
              else "RED — see !! above.")
        print("=" * 72)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
