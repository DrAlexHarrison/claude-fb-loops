"""End-to-end integration test: the three fb-assist modules COMPOSE into the real
privacy-preserving feedback flow on a transcript carrying a PLANTED secret + PII.

This is the proof that the locator extractor (``transcripts.py``), the redaction
toolbox (``redact.py``), and the packager (``package.py``) form one working
pipeline — and it is the co-author's playbook: the exact, validated call-sequence.

The flow validated here:

  1. parse           transcripts.parse(path)                 -> records
  2. detect          transcripts.redaction_map(...)          -> WHERE each category lives
                     redact.scan_secrets / redact.scan_pii   -> WHAT is sensitive
  3. redact          redact.strip_categories(...)            -> bulk structural strip
                     + char-precise narrative mask via the   -> keep meaning, drop values
                       locator<->finding BRIDGE (below)
  4. assemble        package.assemble_payload(desc, {path:recs}, limit=1MB) -> Payload
  5. preview         package.diff_preview(orig, san, redaction_map=bridge)  -> gate text
  6. swap            with package.swap_restore(payload.targets): ...        -> non-destructive
  7. verify          redact.leak_scan(bundle_text)           -> adversarial egress gate

THE LOCATOR <-> REDACTION_MAP BRIDGE (the one real seam between the modules):
  transcripts.py emits a *locator* per sensitive region:
      {category, line, uuid, field, path, start, end, text, ...}   (Span.locator())
  package.py's diff_preview consumes a *redaction_map* of:
      {uuid, category, original, replacement, count}
  They don't line up: a locator says "human_prompts lives at message.content of
  record U"; it does NOT say "an sk-ant key sits at chars 40..78 inside it". The
  bridge closes that gap: for each located narrative region we run the detectors
  on that region's *text* (giving char-offset Findings), mask them in place with
  ``redact.apply_redactions`` + ``transcripts.replace_span`` (locator -> mutation),
  and emit one diff_preview entry per chosen Finding (locator.uuid + Finding ->
  {uuid, category, original, replacement, count}). See ``_mask_narrative``.

LOCAL ONLY. Everything runs on a scratch .jsonl built in a tmp dir; no real
transcript is ever touched, and ``/feedback`` is never invoked.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

# Force the torch-only path for transformers/gliner BEFORE importing redact.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import transcripts as T  # noqa: E402
from fb_assist import redact as R  # noqa: E402
from fb_assist import package as P  # noqa: E402


# --------------------------------------------------------------------------- #
# The planted sentinels — fake but pattern-valid secrets + real-shaped PII.    #
# --------------------------------------------------------------------------- #
PLANTED = {
    "anthropic_key": "sk-ant-api03-AAAA1111BBBB2222CCCC3333DDDD4444",
    "aws_key_prompt": "AKIAIOSFODNN7EXAMPLE",          # canonical AWS-docs example key
    "email_prompt": "dana.canary@secret-corp.example",
    "person": "Marlene Vasquez",
    "ip": "10.77.88.99",
    "ssn": "123-45-6789",
    "email_assistant": "support.canary@secret-corp.example",
    "aws_key_file": "AKIAZZ44QQ55WW66EE77",            # buried in a Read file-content
    "email_file": "db-admin@secret-corp.example",
    "github_token": "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789AB",  # buried in Bash stdout
    "secret_path": "/home/devuser/code/secret-proj",       # leaks via cwd + a Read path
}

# Every literal that MUST be absent from the sanitized bundle. (The person name is
# NER-detected; see _ner_available — kept in the hard list because models load here.)
SENTINELS = [
    PLANTED["anthropic_key"], PLANTED["aws_key_prompt"], PLANTED["email_prompt"],
    PLANTED["person"], PLANTED["ip"], PLANTED["ssn"], PLANTED["email_assistant"],
    PLANTED["aws_key_file"], PLANTED["email_file"], PLANTED["github_token"],
    PLANTED["secret_path"],
]

SESSION_ID = "11111111-2222-3333-4444-555555555555"


def _env(uuid, parent, ts, **extra):
    base = {
        "uuid": uuid,
        "parentUuid": parent,
        "isSidechain": False,
        "sessionId": SESSION_ID,
        "timestamp": ts,
        "cwd": PLANTED["secret_path"],
        "gitBranch": "feature/secret-x",
        "version": "2.1.195",
        "userType": "external",
    }
    base.update(extra)
    return base


def build_planted_transcript() -> list[dict]:
    """A small but schema-faithful transcript: meta records + a real bug-report turn
    whose human prompt accidentally pastes a key/PII, plus a Read and a Bash whose
    tool output buries more secrets. Mirrors the shapes in tests/fixtures/."""
    recs: list[dict] = [
        # --- lightweight meta records (must survive parsing untouched) ---
        {"type": "agent-color", "agentColor": "red", "sessionId": SESSION_ID},
        {"type": "ai-title", "aiTitle": "feedback flow freezes on submit", "sessionId": SESSION_ID},

        # --- turn: human bug report with pasted secret + PII (NARRATIVE we KEEP) ---
        _env("u-prompt", None, "2026-06-29T18:00:00.000Z",
             type="user", promptSource="typed", entrypoint="cli",
             message={"role": "user", "content": (
                 f"I'm {PLANTED['person']} and I build the Contoso API. "
                 f"While debugging I pasted my key {PLANTED['anthropic_key']} and AWS "
                 f"{PLANTED['aws_key_prompt']} into the repl. Reach me at "
                 f"{PLANTED['email_prompt']} or {PLANTED['ip']}, SSN {PLANTED['ssn']}. "
                 f"The real bug: the /feedback flow keeps FREEZING on submit every time."
             )}),

        # --- assistant text reply (NARRATIVE we KEEP; carries one planted email) ---
        _env("a-text", "u-prompt", "2026-06-29T18:00:05.000Z",
             type="assistant", requestId="req_abc",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "thinking", "thinking": "User pasted a live key; note to self.",
                  "signature": "AAAABBBBCCCCDDDD=="},
                 {"type": "text", "text": (
                     "Got it — the submit freeze is the real issue. If you need our "
                     f"support team, that's {PLANTED['email_assistant']}. Let me check the config."
                 )},
             ]}),

        # --- assistant Read call ---
        _env("a-read", "a-text", "2026-06-29T18:00:06.000Z",
             type="assistant",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "tool_use", "id": "toolu_read1", "name": "Read",
                  "input": {"file_path": f"{PLANTED['secret_path']}/config.py"}},
             ]}),

        # --- Read result: file content buries an AWS key + an email (STRIP wholesale) ---
        _env("u-read", "a-read", "2026-06-29T18:00:07.000Z",
             type="user", sourceToolAssistantUUID="a-read",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_read1", "content": (
                     f'AWS_SECRET = "{PLANTED["aws_key_file"]}"\n'
                     f'OWNER_EMAIL = "{PLANTED["email_file"]}"\n'
                 )},
             ]},
             toolUseResult={"file": {
                 "content": (
                     f'AWS_SECRET = "{PLANTED["aws_key_file"]}"\n'
                     f'OWNER_EMAIL = "{PLANTED["email_file"]}"\n'
                 ),
                 "filePath": f"{PLANTED['secret_path']}/config.py",
                 "numLines": 2, "startLine": 1, "totalLines": 2,
             }}),

        # --- assistant Bash call ---
        _env("a-bash", "u-read", "2026-06-29T18:00:08.000Z",
             type="assistant",
             message={"role": "assistant", "model": "claude-opus-4-8", "content": [
                 {"type": "tool_use", "id": "toolu_bash1", "name": "Bash",
                  "input": {"command": "cat .env | grep TOKEN"}},
             ]}),

        # --- Bash result: stdout buries a GitHub token (STRIP wholesale) ---
        _env("u-bash", "a-bash", "2026-06-29T18:00:09.000Z",
             type="user",
             message={"role": "user", "content": [
                 {"type": "tool_result", "tool_use_id": "toolu_bash1",
                  "content": f"GITHUB_TOKEN={PLANTED['github_token']}\n"},
             ]},
             toolUseResult={"stdout": f"GITHUB_TOKEN={PLANTED['github_token']}\n",
                            "stderr": "", "interrupted": False, "isImage": False}),
    ]
    return recs


# --------------------------------------------------------------------------- #
# THE BRIDGE: locator (transcripts.py) -> finding (redact) -> diff_preview.    #
# --------------------------------------------------------------------------- #
# Categories we STRIP wholesale (bulk content the user doesn't want shipped at all).
STRIP_CATEGORIES = [
    "file_contents", "bash_output", "tool_calls", "websearch",
    "thinking_blocks", "hook_output", "injected_memory",
    "env_metadata", "paths",
]
# Narrative categories we KEEP but scrub char-precise (meaning survives; values don't).
KEEP_BUT_MASK = ["human_prompts", "assistant_text"]


def _mask_narrative(raws: list[dict]) -> list[dict]:
    """In-place char-precise mask of secrets/PII inside the KEPT narrative fields.

    For each located narrative Span, run the detectors on its text (redact), mask
    in place via the Span's path (transcripts.replace_span), and return
    diff_preview-shaped redaction_map entries. This is the locator<->redaction_map
    bridge made concrete.
    """
    redaction_map: list[dict] = []
    for i, raw in enumerate(raws):
        rec = T.Record(line=i + 1, raw=raw, type=str(raw.get("type", "")))
        # Span objects carry the locator shape (.path/.start/.end/.uuid).
        spans = list(T.human_prompts([rec])) + list(T.assistant_text([rec]))
        for sp in spans:
            findings = R.scan_secrets(sp.text) + R.scan_pii(sp.text)
            chosen = R.merge_redaction_spans(findings)
            if not chosen:
                continue
            masked, _ = R.apply_redactions(sp.text, findings, style="mask")
            if masked == sp.text:
                continue
            T.replace_span(raw, sp, masked)            # locator -> in-place mutation
            for f in chosen:                            # Finding -> diff_preview entry
                redaction_map.append({
                    "uuid": sp.uuid,
                    "category": f.entity,               # e.g. ANTHROPIC_KEY / PERSON / EMAIL_ADDRESS
                    "original": f.text,
                    "replacement": f"‹{R._token_label(f.entity)}›",
                    "count": 1,
                })
    return redaction_map


# --------------------------------------------------------------------------- #
# THE FLOW: the exact validated call-sequence (the co-author's playbook).      #
# --------------------------------------------------------------------------- #
def run_privacy_flow(path: Path, backup_root: Path) -> dict:
    """Run all seven steps on ``path`` and return every artifact for assertion."""
    art: dict = {}

    # 1) PARSE — on-disk .jsonl -> records (Record objects; .raw is the dict).
    records = list(T.parse(str(path)))
    original_raws = [r.raw for r in records]
    art["records"] = records
    art["original_raws"] = original_raws

    # 2) DETECT — WHERE (locators) + WHAT (findings).
    art["location_map"] = T.redaction_map(records)                # where each category lives
    full_text = P.serialize_records(original_raws).decode("utf-8")
    art["pre_secrets"] = R.scan_secrets(full_text)
    art["pre_pii"] = R.scan_pii(full_text)

    # 3) REDACT — bulk structural strip, then char-precise narrative mask (bridge).
    sanitized_raws = R.strip_categories(original_raws, STRIP_CATEGORIES, mode="replace")
    bridge_map = _mask_narrative(sanitized_raws)                  # mutates sanitized_raws in place
    art["sanitized_raws"] = sanitized_raws
    art["bridge_map"] = bridge_map

    # 4) ASSEMBLE — {real_path: sanitized records} -> the on-disk layout /feedback reads.
    description = (
        "The /feedback submit flow freezes every time. Reproduced after a Read+Bash; "
        "secrets and PII in this session were redacted by fb-assist before sending."
    )
    payload = P.assemble_payload(
        description,
        {str(path): sanitized_raws},
        limit=1_000_000,
        effort_signal={"redaction": "surgical", "quality": 4, "alignment_confidence": 5},
    )
    art["payload"] = payload

    # 5) PREVIEW — the concise included/stripped gate summary.
    preview = P.diff_preview(original_raws, sanitized_raws, redaction_map=bridge_map)
    art["preview"] = preview

    # The ACTUAL upload = description (+ effort footer) + the sanitized JSONL bytes.
    sanitized_bytes = payload.targets[str(path)]
    art["sanitized_bytes"] = sanitized_bytes
    upload_text = payload.description + "\n" + sanitized_bytes.decode("utf-8")
    art["upload_text"] = upload_text
    art["bundle_text"] = upload_text  # alias used by the literal-absence assertions

    # The CONTENT surface = description + the human-meaningful narrative, rendered via
    # rendered from the *sanitized* records. This — NOT the raw JSONL — is the right
    # input for the NER egress gate (see step 7 note).
    san_recs = [T.Record(line=i + 1, raw=r, type=str(r.get("type", "")))
                for i, r in enumerate(sanitized_raws)]
    narrative = list(T.human_prompts(san_recs)) + list(T.assistant_text(san_recs))
    content_text = payload.description + "\n" + "\n".join(s.text for s in narrative)
    art["content_text"] = content_text

    # 6) SWAP-RESTORE — non-destructive on-disk swap around the (simulated) submit.
    art["original_disk_bytes"] = path.read_bytes()
    art["original_disk_sha"] = hashlib.sha256(art["original_disk_bytes"]).hexdigest()
    with P.swap_restore(payload.targets, backup_root=str(backup_root), settle_s=0.05) as handle:
        art["on_disk_during_swap"] = path.read_bytes()
        art["journal_existed"] = Path(handle.journal_path).exists()
    art["on_disk_after_swap"] = path.read_bytes()
    art["on_disk_after_sha"] = hashlib.sha256(art["on_disk_after_swap"]).hexdigest()

    # 7) LEAK-SCAN — the adversarial egress gate, in TWO layers:
    #   (a) DETERMINISTIC FLOOR over the actual upload bytes — machine-decidable,
    #       zero false positives (regex secret + regex PII). THIS is the hard gate.
    #   (b) SEMANTIC NER (leak_scan) over the rendered CONTENT surface — a recall
    #       layer for the co-author. Run over raw JSONL it hallucinates PII from
    #       structural tokens (UUID->"credit card", model-name->"person") and from
    #       the placeholder labels themselves; its hits are CANDIDATES to adjudicate,
    #       never a boolean. We assert only that no REAL planted value resurfaces.
    art["upload_secrets"] = R.scan_secrets(upload_text)        # deterministic; must be []
    art["upload_pii_floor"] = R._scan_pii_regex(upload_text)   # deterministic; must be []
    art["leak"] = R.leak_scan(content_text)                    # semantic recall (candidates)
    art["leak_summary"] = R.summarize_findings(art["leak"])
    art["leak_real_values"] = [f for f in art["leak"] if f.text in set(SENTINELS)]
    return art


# --------------------------------------------------------------------------- #
# pytest fixtures + tests                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def flow(tmp_path_factory):
    d = tmp_path_factory.mktemp("integration")
    path = d / f"{SESSION_ID}.jsonl"
    path.write_bytes(P.serialize_records(build_planted_transcript()))
    backup_root = d / "backups"
    return run_privacy_flow(path, backup_root), path


def test_parse_found_planted_secrets(flow):
    """Sanity: the planted sentinels really ARE in the original (so removal is proven)."""
    art, _ = flow
    full = P.serialize_records(art["original_raws"]).decode("utf-8")
    for s in SENTINELS:
        assert s in full, f"planted sentinel missing from original: {s}"
    pre = {f.entity for f in art["pre_secrets"]}
    assert "ANTHROPIC_KEY" in pre and "AWS_ACCESS_KEY" in pre
    pre_pii = {f.entity for f in art["pre_pii"]}
    assert "EMAIL_ADDRESS" in pre_pii and "US_SSN" in pre_pii


def test_location_map_locates_categories(flow):
    """redaction_map locates every planted category (the WHERE handoff)."""
    art, _ = flow
    summ = art["location_map"]["summary"]
    for cat in ("human_prompts", "assistant_text", "file_contents", "bash_output"):
        assert summ[cat]["count"] > 0, f"redaction_map failed to locate {cat}"


def test_planted_secrets_gone_from_sanitized(flow):
    """HARD: not one planted sentinel survives anywhere in the sanitized bundle."""
    art, _ = flow
    san = P.serialize_records(art["sanitized_raws"]).decode("utf-8")
    for s in SENTINELS:
        assert s not in san, f"LEAK: sentinel survived in sanitized records: {s}"
        assert s not in art["bundle_text"], f"LEAK: sentinel survived in bundle: {s}"


def test_meaning_preserved(flow):
    """The bug report's MEANING survives the char-precise mask."""
    art, _ = flow
    bundle = art["bundle_text"]
    assert "FREEZING" in bundle and "/feedback" in bundle
    # The masked narrative kept structure: placeholder markers replaced the values.
    assert "‹ANTHROPIC_KEY›" in bundle
    assert "‹PERSON›" in bundle


def test_leak_scan_clean(flow):
    """HARD: the egress gate proves no REAL secret/PII value can be recovered.

    Layer (a) — deterministic floor over the ACTUAL upload bytes — is the
    machine-decidable gate and must be empty. Layer (b) — NER leak_scan over the
    content surface — must surface none of the planted *values* (its remaining hits
    are placeholder-label artifacts the co-author adjudicates)."""
    art, _ = flow
    # (a) Deterministic floor over the real upload bytes — zero false positives.
    assert art["upload_secrets"] == [], \
        f"LEAK: secret in upload: {[(f.entity, f.masked) for f in art['upload_secrets']]}"
    assert art["upload_pii_floor"] == [], \
        f"LEAK: real PII pattern in upload: {[(f.entity, f.masked) for f in art['upload_pii_floor']]}"
    # (b) Semantic NER surfaces NO real planted value.
    assert art["leak_real_values"] == [], \
        f"LEAK: planted value resurfaced via NER: {[(f.entity, f.text) for f in art['leak_real_values']]}"
    # leak_scan still found no `secret`-category hit on the content surface either.
    assert [f for f in art["leak"] if f.category == "secret"] == []


def test_diff_preview_shows_redactions(flow):
    """diff_preview renders the gate summary and reflects real redactions."""
    art, _ = flow
    pv = art["preview"]
    assert pv.bytes_after < pv.bytes_before          # structural strips shrank it
    assert pv.modified_records > 0
    cats = pv.stripped_by_category
    assert cats, "diff_preview saw no per-category redactions"
    # The bridge surfaced the planted secret/PII entities into the gate view.
    assert any(c in cats for c in ("ANTHROPIC_KEY", "AWS_ACCESS_KEY", "EMAIL_ADDRESS", "PERSON"))
    text = pv.render()
    assert "INCLUDED" in text and "STRIPPED" in text


def test_swap_restore_nondestructive(flow):
    """HARD: inside the swap the on-disk file IS the sanitized version with the
    planted secret ABSENT; after, the original is restored byte-exact."""
    art, path = flow
    # During the swap, /feedback would read exactly this:
    assert art["on_disk_during_swap"] == art["sanitized_bytes"]
    during = art["on_disk_during_swap"].decode("utf-8")
    for s in SENTINELS:
        assert s not in during, f"LEAK: sentinel on disk during swap: {s}"
    assert art["journal_existed"] is True
    # After the swap, byte-exact restoration (sha256), resumability intact.
    assert art["on_disk_after_sha"] == art["original_disk_sha"]
    assert art["on_disk_after_swap"] == art["original_disk_bytes"]
    # And the live file on disk right now matches the original too.
    assert path.read_bytes() == art["original_disk_bytes"]


# --------------------------------------------------------------------------- #
# Standalone runner: prints the validated playbook + a PASS/FAIL banner.        #
# --------------------------------------------------------------------------- #
def _main() -> int:
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        path = d / f"{SESSION_ID}.jsonl"
        path.write_bytes(P.serialize_records(build_planted_transcript()))
        art = run_privacy_flow(path, d / "backups")

        print("=" * 72)
        print("fb-assist END-TO-END INTEGRATION — privacy-preserving /feedback flow")
        print("=" * 72)
        print(f"[1] parse           : {len(art['records'])} records")
        loc = art["location_map"]["summary"]
        print(f"[2] detect (where)  : " + ", ".join(
            f"{c}={loc[c]['count']}" for c in ("human_prompts", "assistant_text",
            "file_contents", "bash_output") if loc[c]["count"]))
        print(f"    detect (what)   : {len(art['pre_secrets'])} secret + "
              f"{len(art['pre_pii'])} pii findings pre-redaction")
        print(f"[3] redact          : strip {STRIP_CATEGORIES} ; "
              f"mask {len(art['bridge_map'])} narrative span(s)")
        print(f"[4] assemble        : {len(art['payload'].targets)} target(s), "
              f"{art['payload'].total_bytes:,} B (<1MB)")
        print(f"[5] preview         :")
        for line in art["preview"].render().splitlines():
            print("        " + line)
        print(f"[6] swap-restore    : during-swap on disk == sanitized: "
              f"{art['on_disk_during_swap'] == art['sanitized_bytes']}; "
              f"restored byte-exact: {art['on_disk_after_sha'] == art['original_disk_sha']}")
        print(f"[7] egress gate (a) DETERMINISTIC over upload bytes: "
              f"{len(art['upload_secrets'])} secret + {len(art['upload_pii_floor'])} pii-floor "
              f"(both must be 0)")
        print(f"    egress gate (b) NER leak_scan over CONTENT: "
              f"{art['leak_summary']['total']} candidate hit(s), "
              f"{len(art['leak_real_values'])} of which are REAL planted values (must be 0)")
        if art["leak"]:
            for f in art["leak"][:6]:
                print(f"        candidate: [{f.category}/{f.severity}] {f.entity} = {f.text!r} "
                      f"(placeholder-label artifact)")

        # Hard checks (mirror the pytest asserts) for the standalone banner.
        san = P.serialize_records(art["sanitized_raws"]).decode("utf-8")
        ok = True
        for s in SENTINELS:
            if s in san or s in art["bundle_text"]:
                ok = False; print(f"  !! sentinel survived: {s}")
        if art["upload_secrets"]:
            ok = False; print("  !! secret in actual upload bytes")
        if art["upload_pii_floor"]:
            ok = False; print("  !! real PII pattern in actual upload bytes")
        if art["leak_real_values"]:
            ok = False; print("  !! NER surfaced a real planted value")
        if art["on_disk_after_sha"] != art["original_disk_sha"]:
            ok = False; print("  !! original NOT restored byte-exact")
        print("=" * 72)
        print("RESULT:", "GREEN — toolbox composes end-to-end." if ok else "RED — see !! above.")
        print("=" * 72)
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
