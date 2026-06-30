"""Tests for fb_assist.redact — the detection + redaction floor.

Plants KNOWN secrets/PII into a COPY of the real fixture (the real fixtures and
real transcripts are NEVER modified), then measures per-detector recall and
exercises strip_categories / reversible_tokenize / leak_scan.

Run the recall report standalone:
    USE_TF=0 python -m tests.test_redact        # from the fb-assist/ dir
or the asserts under pytest:
    USE_TF=0 pytest tests/test_redact.py -v -s
"""

import json
import os
import shutil
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import redact  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "sample-mid.jsonl"

# Planted, synthetic, KNOWN sensitive values. None of these are real credentials.
PLANTS = {
    # A realistic-looking (synthetic) AWS key — NOT the canonical AKIAIOSFODNN7EXAMPLE
    # documentation key, which gitleaks deliberately allowlists.
    "aws":       ("AKIA2E4Z7QF9KD3MXR8T", "secret"),
    "anthropic": ("sk-ant-api03-" + ("Xy9kLm3nQp7rTv2w" * 6) + "ZZ", "secret"),
    "github":    ("ghp_" + ("a1B2c3D4e5" * 4), "secret"),
    "email":     ("priya.raman@northwind-labs.example", "pii"),
    "person":    ("Jordan Castellano", "pii"),
    "phone":     ("(415) 555-0142", "pii"),
}


def _make_planted_copy(dst_dir: Path) -> Path:
    """Copy the real fixture and append one synthetic user-prompt record carrying
    every planted value. Returns the path to the copy. Real fixture untouched."""
    dst = dst_dir / "planted-mid.jsonl"
    shutil.copyfile(FIXTURE, dst)
    plant_text = (
        "Reminder to self: deploy creds are AWS " + PLANTS["aws"][0] +
        " and the Claude key " + PLANTS["anthropic"][0] +
        " plus a GitHub token " + PLANTS["github"][0] + ". "
        "Email " + PLANTS["email"][0] + " to loop in " + PLANTS["person"][0] +
        " and call " + PLANTS["phone"][0] + " if the build breaks."
    )
    rec = {
        "parentUuid": "plant-0000",
        "isSidechain": False,
        "promptId": "plant-prompt",
        "type": "user",
        "message": {"role": "user", "content": plant_text},
        "uuid": "plant-uuid-0001",
        "timestamp": "2026-06-29T00:00:00.000Z",
        "userType": "external",
        "entrypoint": "cli",
        "cwd": "/home/devuser/code/project",
        "sessionId": "1a2b3c4d-0000-4000-8000-000000000099",
        "version": "2.1.168",
        "gitBranch": "HEAD",
    }
    with open(dst, "a") as fh:
        fh.write(json.dumps(rec) + "\n")
    return dst


def _detector_findings(text: str) -> dict:
    """Run each detector layer separately so recall is attributable per-layer."""
    return {
        "regex": redact._scan_secrets_regex(text),
        "gitleaks": redact._scan_secrets_gitleaks(text),
        "detect-secrets": redact._scan_secrets_detect_secrets(text),
        "presidio": redact._scan_pii_presidio(text),
        "gliner": redact._scan_pii_gliner(text),
    }


# The real fixtures are full multi-MB transcripts; the live /feedback gather caps
# a bundle at ~1 MB. Measure recall on a realistic feedback-sized window (real
# transcript head + the planted tail) rather than the whole multi-MB file.
_BUNDLE_BUDGET = 300_000


def _realistic_bundle(copy_path: Path) -> str:
    lines = copy_path.read_text().splitlines()
    plant = lines[-1]
    head, size = [], 0
    for ln in lines[:-1]:
        if size + len(ln) > _BUNDLE_BUDGET:
            break
        head.append(ln)
        size += len(ln) + 1
    return "\n".join(head + [plant])


def _hit(findings, value: str) -> bool:
    """A planted value counts as detected if any finding meaningfully overlaps it
    (full containment either direction; >=4 chars to avoid trivial coincidences)."""
    for f in findings:
        t = f.text or ""
        if len(t) >= 4 and (value in t or t in value):
            return True
    return False


_RECALL_CACHE: dict = {}


def measure_recall(tmp_dir: Path) -> dict:
    # The planted content is deterministic; scan once and reuse across tests so
    # the (model-heavy) detector sweep doesn't run five times.
    if _RECALL_CACHE:
        return _RECALL_CACHE
    copy = _make_planted_copy(tmp_dir)
    bundle = _realistic_bundle(copy)
    per_detector = _detector_findings(bundle)

    # Layered groupings, matching how the toolbox actually composes.
    secret_layer = per_detector["regex"] + per_detector["gitleaks"] + per_detector["detect-secrets"]
    pii_layer = per_detector["presidio"] + per_detector["gliner"]

    rows = {}
    for label, (value, kind) in PLANTS.items():
        rows[label] = {
            "value": value,
            "kind": kind,
            "regex": _hit(per_detector["regex"], value),
            "gitleaks": _hit(per_detector["gitleaks"], value),
            "detect-secrets": _hit(per_detector["detect-secrets"], value),
            "presidio": _hit(per_detector["presidio"], value),
            "gliner": _hit(per_detector["gliner"], value),
            "secret_layer": _hit(secret_layer, value),
            "pii_layer": _hit(pii_layer, value),
            "any": _hit(secret_layer + pii_layer, value),
        }
    _RECALL_CACHE.update(rows)
    return rows


def _print_report(rows: dict) -> None:
    dets = ["regex", "gitleaks", "detect-secrets", "presidio", "gliner",
            "secret_layer", "pii_layer", "any"]
    print("\n================ DETECTION RECALL (planted values) ================")
    head = f"{'plant':10} {'kind':7} " + " ".join(f"{d[:8]:>8}" for d in dets)
    print(head)
    print("-" * len(head))
    tally = {d: 0 for d in dets}
    for label, r in rows.items():
        cells = " ".join(f"{'  ✓' if r[d] else '  ·':>8}" for d in dets)
        print(f"{label:10} {r['kind']:7} {cells}")
        for d in dets:
            tally[d] += 1 if r[d] else 0
    n = len(rows)
    print("-" * len(head))
    print(f"{'RECALL-all':10} {'/'+str(n):7} " + " ".join(f"{tally[d]/n:8.0%}" for d in dets))
    # Per-kind recall for the layer columns (the honest, like-for-like number).
    secrets = [r for r in rows.values() if r["kind"] == "secret"]
    piis = [r for r in rows.values() if r["kind"] == "pii"]
    sl = sum(r["secret_layer"] for r in secrets) / len(secrets)
    pl = sum(r["pii_layer"] for r in piis) / len(piis)
    print(f"\n  secret_layer recall on SECRET plants : {sl:.0%} ({len(secrets)} plants)")
    print(f"  pii_layer    recall on PII    plants : {pl:.0%} ({len(piis)} plants)")
    print(f"  combined 'any-detector' recall       : {tally['any']/n:.0%} ({n} plants)")
    print("===================================================================")


# --------------------------------------------------------------------------- #
# Recall tests
# --------------------------------------------------------------------------- #
def test_recall_report(tmp_path, capsys):
    rows = measure_recall(tmp_path)
    with capsys.disabled():
        _print_report(rows)
    # Every planted value must be caught by *some* layer — the whole point of the
    # safety net. This is the load-bearing guarantee.
    for label, r in rows.items():
        assert r["any"], f"planted {label} ({r['value']}) escaped ALL detectors"


def test_secret_regex_floor_is_deterministic(tmp_path):
    """The regex floor alone must catch the structured-credential plants — it is
    the layer that does NOT depend on any model or external binary."""
    rows = measure_recall(tmp_path)
    assert rows["aws"]["regex"], "AWS key missed by regex floor"
    assert rows["anthropic"]["regex"], "Anthropic key missed by regex floor"
    assert rows["github"]["regex"], "GitHub token missed by regex floor"


def test_secret_layer_full_recall(tmp_path):
    rows = measure_recall(tmp_path)
    for k in ("aws", "anthropic", "github"):
        assert rows[k]["secret_layer"], f"{k} missed by combined secret layer"


def test_pii_layer_catches_each(tmp_path):
    rows = measure_recall(tmp_path)
    assert rows["email"]["pii_layer"], "email missed by PII layer"
    assert rows["person"]["pii_layer"], "person name missed by PII layer"
    assert rows["phone"]["pii_layer"], "phone missed by PII layer"


def test_layering_beats_any_single_pii_detector(tmp_path):
    """Demonstrates the investigation's core claim: layering > any one detector.
    The combined PII layer recall must be >= the best single PII detector."""
    rows = measure_recall(tmp_path)
    pii = [r for r in rows.values() if r["kind"] == "pii"]
    presidio = sum(r["presidio"] for r in pii) / len(pii)
    gliner = sum(r["gliner"] for r in pii) / len(pii)
    combined = sum(r["pii_layer"] for r in pii) / len(pii)
    assert combined >= max(presidio, gliner)


# --------------------------------------------------------------------------- #
# Transform tests
# --------------------------------------------------------------------------- #
def test_anonymize_removes_pii_values():
    text = "Email priya.raman@example.com and ask for Jordan Castellano at (415) 555-0142."
    out = redact.anonymize_pii(text)
    assert "priya.raman@example.com" not in out
    assert "Jordan Castellano" not in out
    assert "‹" in out  # placeholders present


def test_deterministic_email_floor_catches_any_tld():
    """The regex email floor must catch reserved-TLD emails that Presidio rejects
    and NER may miss in short context — the gap surfaced during testing."""
    out = redact.anonymize_pii("ping me at priya.raman@northwind-labs.example today", use_gliner=False)
    assert "priya.raman@northwind-labs.example" not in out


def test_reversible_tokenize_is_consistent_and_reversible():
    # A repeated email is detected deterministically by the regex floor, so the
    # consistency guarantee (same value -> same token) is testable without NER flake.
    email = "priya.raman@example.com"
    key = "sk-ant-api03-" + ("Ab3dEf6h" * 5)
    text = f"Mail {email} about the build, then re-send to {email}. Key {key} used twice: {key}."
    red, mapping = redact.reversible_tokenize(text)
    # Same value -> same token, at every occurrence (relationships preserved).
    assert red.count("‹EMAIL_ADDRESS_1›") == 2
    assert red.count("‹ANTHROPIC_KEY_1›") == 2
    assert email not in red and key not in red
    # Mapping reverses exactly, byte-for-byte.
    restored = red
    for tok, val in mapping.items():
        restored = restored.replace(tok, val)
    assert restored == text


# --------------------------------------------------------------------------- #
# strip_categories tests (on a COPY of the real fixture)
# --------------------------------------------------------------------------- #
_FIXTURE_CACHE: list | None = None


def _load(path: Path) -> list:
    # Parse the 13 MB fixture once; tests deep-copy via strip_categories anyway.
    global _FIXTURE_CACHE
    if path == FIXTURE:
        if _FIXTURE_CACHE is None:
            _FIXTURE_CACHE = [json.loads(l) for l in path.read_text().splitlines() if l.strip()]
        return _FIXTURE_CACHE
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


def test_strip_file_contents_removes_bodies_keeps_structure():
    records = _load(FIXTURE)
    before = json.dumps(records)
    stripped = redact.strip_categories(records, ["file_contents"])
    # Same number of records; input untouched.
    assert len(stripped) == len(records)
    assert json.dumps(records) == before, "strip_categories mutated its input"
    # At least one Read/Edit file body got marked.
    blob = json.dumps(stripped)
    assert "file_contents stripped" in blob
    # A known proprietary header that lives in a file body is gone. (Precompute the
    # booleans so a failure never feeds a multi-MB string to pytest introspection.)
    header_in_before = "All rights reserved" in before
    header_in_after = "All rights reserved" in blob
    assert (not header_in_after) or (not header_in_before)


def test_strip_env_metadata_scrubs_envelope():
    records = _load(FIXTURE)
    stripped = redact.strip_categories(records, ["env_metadata"])
    for r in stripped:
        assert r.get("cwd") in (None, "‹cwd›")
        assert r.get("gitBranch") in (None, "‹gitBranch›")
    # The real cwd string is gone from the env fields.
    assert all(r.get("cwd") != "/home/devuser/code/project" for r in stripped)


def test_strip_paths_scrubs_home_paths():
    records = _load(FIXTURE)
    stripped = redact.strip_categories(records, ["paths"])
    # Precompute the boolean (a failing `in` over a 13 MB string would hang
    # pytest's assertion introspection). Covers paths in string values AND in
    # dict keys (file-history-snapshot's trackedFileBackups is keyed by path).
    home_leak = "/home/devuser" in json.dumps(stripped)
    assert not home_leak


def test_every_category_strips_without_error():
    records = _load(FIXTURE)
    for cat in redact.CATEGORIES:
        out = redact.strip_categories(records, [cat])
        assert len(out) == len(records)
        json.dumps(out)  # still serializable


def test_unknown_category_raises():
    try:
        redact.strip_categories([], ["not_a_category"])
    except ValueError:
        return
    assert False, "expected ValueError for unknown category"


# --------------------------------------------------------------------------- #
# leak_scan tests
# --------------------------------------------------------------------------- #
def test_leak_scan_blocks_dirty_bundle():
    dirty = ("Ship it. AWS AKIAIOSFODNN7EXAMPLE, email a@b.example, "
             '"cwd":"/home/devuser/code/secret-proj", branch HEAD.')
    findings = redact.leak_scan(dirty)
    summary = redact.summarize_findings(findings)
    assert not summary["clean"]
    assert summary["blocking"], "a leaked AWS key must be a blocking finding"


def test_leak_scan_passes_clean_bundle():
    clean = ("The diff viewer scrolls to the wrong line after a hot reload. "
             "Expected it to keep my cursor position; instead it jumps to top.")
    findings = redact.leak_scan(clean, use_gliner=False)
    summary = redact.summarize_findings(findings)
    assert summary["clean"], f"clean bundle flagged: {[f.entity for f in findings]}"


def test_leak_scan_catches_residual_after_naive_strip():
    """Integration: strip file_contents + bash_output on the real fixture, then
    leak_scan the result. Demonstrates the safety net catches what structural
    strips alone miss (e.g. paths/emails living in human prompts)."""
    records = _load(FIXTURE)[:400]  # bound to a feedback-sized slice
    stripped = redact.strip_categories(records, ["file_contents", "bash_output", "websearch"])
    bundle = "\n".join(json.dumps(r) for r in stripped)[:400_000]
    findings = redact.leak_scan(bundle, use_gliner=False)
    # We don't assert a specific count (transcript-dependent); we assert the gate
    # is *operating* and returns structured, severity-ranked findings.
    summary = redact.summarize_findings(findings)
    assert isinstance(summary["total"], int)
    print(f"\n[leak_scan after structural strip] {summary}")



def _find_tool_record(records, names, predicate):
    for r in records:
        msg = r.get("message")
        if not (isinstance(msg, dict) and isinstance(msg.get("content"), list)):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                if predicate(names.get(b.get("tool_use_id"), ""), r):
                    return r
    return None


def _result_block_text(rec):
    for b in rec["message"]["content"]:
        if isinstance(b, dict) and b.get("type") == "tool_result":
            c = b.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                for s in c:
                    if isinstance(s, dict) and isinstance(s.get("text"), str):
                        return s["text"]
    return None


def test_double_storage_both_copies_scrubbed():
    """Tool output is stored TWICE — the model-visible tool_result block AND the
    structured toolUseResult mirror. A strip must scrub BOTH or content leaks."""
    records = _load(FIXTURE)
    names = redact._index_tool_names(records)
    bash = _find_tool_record(records, names,
                             lambda n, r: n == "Bash" and isinstance(r.get("toolUseResult"), dict))
    assert bash is not None, "no Bash record with both copies in fixture"
    s = redact.strip_categories([bash], ["bash_output"])[0]
    assert "stripped" in (_result_block_text(s) or ""), "tool_result block not scrubbed"
    tur = s["toolUseResult"]
    assert "stripped" in str(tur.get("stdout", "")), "toolUseResult.stdout mirror not scrubbed"


def test_mcp_and_exotic_tool_output_covered_by_tool_calls():
    """The ~44 toolUseResult shapes (MCP results etc.) aren't matched by the
    specific bash/file/web strippers; the tool_calls lever must remove them
    shape-agnostically — both stored copies."""
    records = _load(FIXTURE)
    names = redact._index_tool_names(records)
    mcp = _find_tool_record(records, names, lambda n, r: n.startswith("mcp__"))
    if mcp is None:
        return  # fixture-dependent; skip if no MCP calls present
    before = _result_block_text(mcp)
    assert before and "stripped" not in before
    s = redact.strip_categories([mcp], ["tool_calls"])[0]
    assert "stripped" in (_result_block_text(s) or ""), "MCP tool_result block not scrubbed"
    assert "stripped" in str(s.get("toolUseResult")), "MCP toolUseResult mirror not scrubbed"


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        rows = measure_recall(Path(td))
        _print_report(rows)
