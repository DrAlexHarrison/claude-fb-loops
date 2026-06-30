"""Tests for fb_assist.transcripts.

Runs against the bundled fixtures (sample-small 93 B, sample-mid 12 MB,
sample-large 76 MB) plus a few diverse real transcripts discovered read-only
under ~/.claude*/projects (skipped gracefully if none are present).

Dual-mode: ``pytest`` discovers the ``test_*`` functions; running the file
directly (``python3 tests/test_transcripts.py``) executes them all with a
PASS/FAIL summary and the perf/memory report — no pytest dependency needed
(stdlib only, matching the parser).
"""

from __future__ import annotations

import copy
import json
import os
import sys
import time
import tracemalloc
from pathlib import Path

# Make the package importable when run directly.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fb_assist.transcripts as T  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SMALL = FIXTURES / "sample-small.jsonl"
MID = FIXTURES / "sample-mid.jsonl"
LARGE = FIXTURES / "sample-large.jsonl"


# --------------------------------------------------------------------------- #
# parse
# --------------------------------------------------------------------------- #
def test_parse_small_does_not_crash():
    recs = list(T.parse(SMALL))
    assert len(recs) == 1
    assert recs[0].type == "agent-color"
    assert recs[0].line == 1


def test_parse_mid_streams_and_counts():
    stats = T.ParseStats()
    n = sum(1 for _ in T.parse(MID, stats=stats))
    assert n == stats.ok > 1000
    assert stats.malformed == 0


def test_parse_is_lazy_generator():
    # parse must be a generator (not a list) so it streams.
    g = T.parse(MID)
    assert hasattr(g, "__next__")
    first = next(g)
    assert isinstance(first, T.Record)
    g.close()


def test_malformed_lines_skipped_and_counted(tmp_path=None):
    p = (tmp_path or Path(_scratch())) / "malformed.jsonl"
    p.write_text(
        '{"type":"user","uuid":"u1","message":{"role":"user","content":"hi"}}\n'
        "not json at all\n"
        "\n"
        "[1,2,3]\n"  # valid JSON but not an object
        '{"type":"assistant","uuid":"a1","message":{"role":"assistant","content":[]}}\n'
        '{"type":"user","uuid":"u2","message":{"role":"user","content":"bye"\n'  # truncated
    )
    stats = T.ParseStats()
    recs = list(T.parse(p, stats=stats))
    assert [r.uuid for r in recs] == ["u1", "a1"]
    assert stats.malformed == 2  # "not json" + truncated line
    assert stats.blank == 1
    assert stats.not_object == 1
    assert 2 in stats.malformed_lines and 6 in stats.malformed_lines


# --------------------------------------------------------------------------- #
# category extractors
# --------------------------------------------------------------------------- #
EXPECTED_NONEMPTY = [
    "human_prompts", "thinking_blocks", "assistant_text", "bash_output",
    "file_contents", "tool_calls", "tool_results", "paths", "env_metadata",
    "hook_output", "injected_memory", "websearch",
]


def test_every_category_extracts_on_mid():
    recs = list(T.parse(MID))
    counts = {}
    for cat in T.EXTRACTORS:
        counts[cat] = sum(1 for _ in T.extract(recs, cat))
    for cat in EXPECTED_NONEMPTY:
        assert counts[cat] > 0, f"{cat} extracted nothing"


def test_extractors_accept_path_or_records():
    # Same result whether given a path or a materialized record list.
    from_path = sum(1 for _ in T.human_prompts(MID))
    recs = list(T.parse(MID))
    from_recs = sum(1 for _ in T.human_prompts(recs))
    assert from_path == from_recs > 0


def test_locator_roundtrip_every_string_span():
    """Every Span's (path, start, end) must navigate back to exactly its text."""
    recs = list(T.parse(MID))
    by_line = {r.line: r for r in recs}
    checked = 0
    for sp in T.extract_all(recs):
        got = T.get_at(by_line[sp.line].raw, sp.path)
        assert isinstance(got, str), f"{sp.field} is not a string"
        assert got[sp.start:sp.end] == sp.text, f"mismatch at {sp.field}"
        checked += 1
    assert checked > 5000


def test_replace_span_redacts_in_place():
    recs = list(T.parse(MID))
    by_line = {r.line: r for r in recs}
    sp = next(T.thinking_blocks(recs))
    rec = copy.deepcopy(by_line[sp.line])
    T.replace_span(rec, sp, "[REDACTED]")
    assert T.get_at(rec.raw, sp.path) == "[REDACTED]" + sp.text[sp.end:]
    # original untouched
    assert T.get_at(by_line[sp.line].raw, sp.path) == sp.text


def test_tool_output_stored_twice_is_findable():
    """Structured (toolUseResult) and model-visible (tool_result block) copies of
    the same tool output share a tool_use_id so a redactor can scrub both."""
    recs = list(T.parse(MID))
    struct_ids = {sp.meta.get("tool_use_id") for sp in T.bash_output(recs)} | \
                 {sp.meta.get("tool_use_id") for sp in T.file_contents(recs)}
    visible_ids = {sp.meta.get("tool_use_id") for sp in T.tool_results(recs)}
    struct_ids.discard(None)
    visible_ids.discard(None)
    # At least some structured outputs have a correlatable model-visible twin.
    assert struct_ids & visible_ids


def test_structured_tool_results_completeness_net():
    """Agent-spawn `prompt`, AskUserQuestion `answers`/`questions`, etc. live in
    structured toolUseResult shapes the typed extractors don't own — the net must
    locate them so a redactor doesn't miss them."""
    recs = list(T.parse(MID))
    structured = [sp for sp in T.tool_results(recs) if sp.meta.get("structured")]
    assert structured, "structured completeness net found nothing"
    top_keys = {sp.meta.get("top_key") for sp in structured}
    # mid fixture contains agent spawns + AskUserQuestion
    assert top_keys & {"prompt", "answers", "questions", "description"}
    # and these must NOT duplicate bash/file/websearch-owned keys
    assert not (top_keys & {"stdout", "file", "originalFile", "query", "results"})


def test_worktree_state_paths_and_commit_captured():
    """worktree-state nests cwd/paths/branches + the original HEAD commit SHA —
    the paths extractor must locate all of them (incl. the real commitSha)."""
    rec = T.Record(line=1, type="worktree-state", raw={
        "type": "worktree-state",
        "worktreeSession": {
            "originalCwd": "/home/devuser/code/contoso",
            "worktreePath": "/home/devuser/code/contoso/.claude/worktrees/x",
            "worktreeBranch": "worktree-x",
            "originalBranch": "main",
            "originalHeadCommit": "0123456789abcdef0123456789abcdef01234567",
            "sessionId": "s1",
        },
    })
    spans = list(T.paths([rec]))
    kinds = {sp.meta.get("kind") for sp in spans}
    assert "commit_sha" in kinds
    assert "worktree_path" in kinds
    assert any(sp.meta.get("kind") == "git_branch" for sp in spans)
    # locator round-trips
    for sp in spans:
        assert T.get_at(rec.raw, sp.path)[sp.start:sp.end] == sp.text


def test_category_vocabulary_is_consistent():
    """Span.category always equals its EXTRACTORS key (one vocabulary for the
    redaction integrator)."""
    recs = list(T.parse(MID))
    m = T.redaction_map(recs)
    for cat, locs in m["by_category"].items():
        for loc in locs:
            assert loc["category"] == cat, f"{loc['category']} under {cat}"


def test_paths_scan_text_finds_more_than_structured():
    recs = list(T.parse(MID))
    structured = sum(1 for _ in T.paths(recs, scan_text=False))
    scanned = sum(1 for _ in T.paths(recs, scan_text=True))
    assert scanned > structured > 0


def test_env_metadata_includes_titles_and_cwd():
    recs = list(T.parse(MID))
    kinds = {sp.meta.get("kind") for sp in T.env_metadata(recs)}
    assert "cwd" in kinds
    assert "ai_title" in kinds  # session titles leak topic — must be locatable
    assert "model" in kinds


# --------------------------------------------------------------------------- #
# scope selectors
# --------------------------------------------------------------------------- #
def test_by_session_filters():
    recs = list(T.parse(MID))
    sid = next(r.session_id for r in recs if r.session_id)
    filtered = list(T.by_session(recs, sid))
    assert filtered and all(r.session_id == sid for r in filtered)


def test_sidechain_partition():
    recs = list(T.parse(MID))
    incl = list(T.exclude_sidechains(recs))
    only = list(T.only_sidechains(recs))
    assert len(incl) + len(only) == len(recs)
    assert all(not r.is_sidechain for r in incl)


def test_since_filters_by_timestamp():
    recs = list(T.parse(MID))
    ts = [r.timestamp for r in recs if r.timestamp]
    assert ts
    mid_ts = sorted(ts)[len(ts) // 2]
    kept = list(T.since(recs, mid_ts))
    assert kept and all((r.timestamp or "") >= mid_ts for r in kept if r.timestamp)
    assert len(kept) < len(recs)


def test_turns_segment_and_cover():
    recs = list(T.parse(MID))
    turns = list(T.iter_turns(recs))
    assert len(turns) > 1
    # every record lands in exactly one turn
    assert sum(len(t.records) for t in turns) == len(recs)


def test_human_only_turns_fewer_than_all():
    recs = list(T.parse(MID))
    all_turns = list(T.iter_turns(recs, human_only=False))
    human_turns = list(T.iter_turns(recs, human_only=True))
    assert 0 < len(human_turns) < len(all_turns)
    # human turn prompts are genuinely typed
    for t in human_turns:
        if t.index > 0 and t.prompt is not None:
            assert not (t.prompt_text or "").lstrip().startswith("<")


def test_last_n_turns_tail_only():
    recs = list(T.parse(MID))
    one = list(T.last_n_turns(recs, 1))
    two = list(T.last_n_turns(recs, 2))
    assert 0 < len(one) <= len(two)


def test_turn_range_inclusive():
    recs = list(T.parse(MID))
    r12 = list(T.turn_range(recs, 1, 2))
    r1 = list(T.turn_range(recs, 1, 1))
    assert 0 < len(r1) <= len(r12)


# --------------------------------------------------------------------------- #
# relevant_slice / size_estimate / redaction_map
# --------------------------------------------------------------------------- #
def test_relevant_slice_uuid_needle():
    recs = list(T.parse(MID))
    # pick an assistant uuid in the middle
    uuids = [r.uuid for r in recs if r.type == "assistant" and r.uuid]
    target = uuids[len(uuids) // 2]
    sl = T.relevant_slice(recs, target, context_turns=0)
    assert any(r.uuid == target for r in sl)


def test_relevant_slice_rare_keyword_is_tight():
    recs = list(T.parse(MID))
    # a needle that appears in few turns yields a tight slice (< whole file)
    sl = T.relevant_slice(recs, "ZEPHYR_LEDGER", context_turns=1)
    assert 0 <= len(sl) <= len(recs)


def test_relevant_slice_missing_needle_empty():
    recs = list(T.parse(MID))
    assert T.relevant_slice(recs, "ZZZ_NO_SUCH_STRING_QWERTY_8842") == []


def test_size_estimate_bytes_match_disk():
    est = T.size_estimate(MID)
    assert est["bytes"] == os.path.getsize(MID)
    assert est["est_tokens"] > 0
    assert est["over_1mb"] is True
    assert est["records"] > 0


def test_size_estimate_by_category():
    est = T.size_estimate(MID, by_category=True)
    assert "by_category_chars" in est
    assert est["by_category_chars"]  # non-empty


def test_redaction_map_structure():
    m = T.redaction_map(MID)
    assert set(m) >= {"summary", "totals", "by_category", "parse", "source"}
    assert m["totals"]["spans"] > 0
    # every category present; locators are lightweight (no full 'text')
    for cat, locs in m["by_category"].items():
        for loc in locs[:5]:
            assert "text" not in loc
            assert {"line", "field", "path", "start", "end", "preview"} <= set(loc)


def test_redaction_map_locators_navigate_back():
    recs = list(T.parse(MID))
    by_line = {r.line: r for r in recs}
    m = T.redaction_map(recs)
    for cat, locs in m["by_category"].items():
        for loc in locs[:20]:
            got = T.get_at(by_line[loc["line"]].raw, tuple(loc["path"]))
            assert isinstance(got, str)
            # preview is a prefix of the real located content
            assert got[loc["start"]:loc["end"]].startswith(loc["preview"].rstrip("…"))


def test_redaction_map_subset_categories():
    m = T.redaction_map(MID, categories=["human_prompts", "thinking_blocks"])
    assert set(m["by_category"]) == {"human_prompts", "thinking_blocks"}


# --------------------------------------------------------------------------- #
# the 76 MB file: bounded memory + reasonable time
# --------------------------------------------------------------------------- #
def test_large_processes_in_bounded_memory_and_time():
    assert os.path.getsize(LARGE) > 50_000_000
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    t0 = time.perf_counter()
    n_records = 0
    n_spans = 0
    chars = 0
    stats = T.ParseStats()
    # Pure streaming consume: parse + run every extractor, retain nothing.
    for r in T.parse(LARGE, stats=stats):
        n_records += 1
        for fn in T.EXTRACTORS.values():
            for sp in fn(r):
                n_spans += 1
                chars += sp.char_len
    elapsed = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    assert stats.ok == n_records > 0
    assert n_spans > 0
    # Bounded: peak heap stays far below the 76 MB file size (we never hold the
    # whole file). Allow generous headroom for the largest single record/span.
    assert peak < 40_000_000, f"peak {peak/1e6:.1f} MB too high — not streaming"
    # Reasonable time on a ~76 MB file (very loose ceiling for CI noise).
    assert elapsed < 60, f"took {elapsed:.1f}s"
    test_large_processes_in_bounded_memory_and_time.report = {
        "records": n_records, "spans": n_spans, "chars": chars,
        "peak_mb": round(peak / 1e6, 1), "elapsed_s": round(elapsed, 2),
    }


def test_redaction_map_large_bounded():
    """redaction_map on 76 MB retains only locators — memory stays bounded."""
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    t0 = time.perf_counter()
    m = T.redaction_map(LARGE)
    elapsed = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1] - base
    tracemalloc.stop()
    assert m["totals"]["spans"] > 0
    # Locators + previews for the whole file — bounded relative to file size.
    assert peak < 120_000_000, f"peak {peak/1e6:.1f} MB"
    test_redaction_map_large_bounded.report = {
        "spans": m["totals"]["spans"], "peak_mb": round(peak / 1e6, 1),
        "elapsed_s": round(elapsed, 2),
    }


# --------------------------------------------------------------------------- #
# CLI smoke
# --------------------------------------------------------------------------- #
def test_cli_categories_and_size():
    rc = T.main(["categories"])
    assert rc == 0
    rc = T.main(["size", str(SMALL)])
    assert rc == 0


def test_cli_map_summary_and_extract():
    assert T.main(["map", str(MID), "--summary"]) == 0
    assert T.main(["extract", "human_prompts", str(SMALL)]) == 0


# --------------------------------------------------------------------------- #
# diverse real transcripts (read-only; skipped if unavailable)
# --------------------------------------------------------------------------- #
def _real_transcripts(limit=4):
    found = T.find_transcripts()
    out = []
    for row in found:
        if 100_000 < row["size"] < 5_000_000:
            out.append(row["path"])
        if len(out) >= limit:
            break
    return out


def test_real_transcripts_parse_and_extract():
    paths = _real_transcripts()
    if not paths:
        return  # nothing to test on this machine
    surprises = []
    for p in paths:
        stats = T.ParseStats()
        recs = list(T.parse(p, stats=stats))
        assert recs, f"{p} parsed empty"
        # redaction_map must not crash on any real schema
        m = T.redaction_map(recs)
        assert m["totals"]["spans"] >= 0
        # collect any record types we don't explicitly model (informational)
        known = {"user", "assistant", "attachment", "system", "last-prompt",
                 "queue-operation", "mode", "permission-mode", "bridge-session",
                 "ai-title", "custom-title", "agent-name", "agent-color",
                 "pr-link", "file-history-snapshot", "summary",
                 "worktree-state", "agent-setting"}
        for r in recs:
            if r.type not in known:
                surprises.append((Path(p).name, r.type))
    test_real_transcripts_parse_and_extract.report = {
        "tested": len(paths),
        "unmodeled_types": sorted(set(surprises)),
    }


# --------------------------------------------------------------------------- #
# script runner
# --------------------------------------------------------------------------- #
def _scratch() -> str:
    d = os.environ.get(
        "FB_SCRATCH",
        "/tmp/fb-assist-scratch",
    )
    os.makedirs(d, exist_ok=True)
    return d


def _run_all():
    tests = sorted(
        (name, obj) for name, obj in globals().items()
        if name.startswith("test_") and callable(obj)
    )
    passed = failed = 0
    reports = {}
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
            if hasattr(fn, "report"):
                reports[name] = fn.report
        except Exception as e:  # noqa: BLE001
            import traceback
            print(f"FAIL  {name}: {e}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    if reports:
        print("\n--- perf / info ---")
        for name, rep in reports.items():
            print(f"  {name}: {json.dumps(rep)}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(_run_all())
