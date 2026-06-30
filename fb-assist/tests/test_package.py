"""Tests for fb_assist.package — the packaging + safe-submit primitives.

Safety-critical focus: swap_restore must restore the user's real transcript
byte-for-byte (sha256) under every failure mode, including a hard process kill
mid-swap. All swap tests run on COPIES in a tmp dir — the real fixtures and any
real ~/.claude transcript are never touched.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

# Make the package importable when run directly (pytest also handles this).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fb_assist import package as P  # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


# --------------------------------------------------------------------------- #
# Fixtures: always a COPY                                                      #
# --------------------------------------------------------------------------- #
@pytest.fixture
def transcript_copy(tmp_path):
    """A byte-for-byte copy of the small real fixture, plus a richer synthetic one."""
    src = FIXTURES / "sample-small.jsonl"
    dst = tmp_path / "session-A.jsonl"
    dst.write_bytes(src.read_bytes())
    return dst


@pytest.fixture
def backup_root(tmp_path):
    return tmp_path / "backups"


def make_records(n=3, session="sess-1", secret="dana@example.com"):
    """Synthetic but schema-faithful records (envelope + message.content)."""
    recs = []
    for i in range(n):
        recs.append({
            "uuid": f"u{i}",
            "type": "user" if i % 2 == 0 else "assistant",
            "sessionId": session,
            "cwd": "/home/devuser/code/secret-proj",
            "gitBranch": "feature/x",
            "timestamp": f"2026-06-08T21:3{i}:00.000Z",
            "version": "2.1.195",
            "message": {"role": "user", "content": f"line {i} contact {secret} ok"},
        })
    return recs


# --------------------------------------------------------------------------- #
# serialize / parse round-trip                                                #
# --------------------------------------------------------------------------- #
def test_serialize_parse_roundtrip():
    recs = make_records(5)
    data = P.serialize_records(recs)
    assert data.endswith(b"\n")
    assert P.parse_jsonl(data) == recs


def test_serialize_empty():
    assert P.serialize_records([]) == b""


def test_parse_jsonl_skips_blank_raises_malformed():
    assert P.parse_jsonl("\n\n") == []
    with pytest.raises(ValueError):
        P.parse_jsonl('{"a":1}\nNOT JSON\n')


# --------------------------------------------------------------------------- #
# atomic write                                                                #
# --------------------------------------------------------------------------- #
def test_atomic_write_sets_content_and_mtime(tmp_path):
    p = tmp_path / "f.bin"
    P._atomic_write(p, b"hello", mtime=1_000_000_000.0)
    assert p.read_bytes() == b"hello"
    assert abs(os.stat(p).st_mtime - 1_000_000_000.0) < 1e-6
    # no stray temp files left behind
    assert not list(tmp_path.glob(".fbassist-tmp-*"))


def test_atomic_write_works_without_o_directory(tmp_path, monkeypatch):
    """Windows has no os.O_DIRECTORY (and can't fsync a dir handle); the write must
    still succeed by skipping the best-effort directory fsync, not raise."""
    monkeypatch.delattr(os, "O_DIRECTORY", raising=False)
    p = tmp_path / "win.bin"
    P._atomic_write(p, b"on-windows")
    assert p.read_bytes() == b"on-windows"
    assert not list(tmp_path.glob(".fbassist-tmp-*"))


# --------------------------------------------------------------------------- #
# swap_restore — the load-bearing safety core                                 #
# --------------------------------------------------------------------------- #
def test_swap_restore_byte_exact(transcript_copy, backup_root):
    """Happy path: sanitized bytes visible inside the with; original byte-exact after."""
    original = transcript_copy.read_bytes()
    original_hash = sha(original)
    original_mtime = os.stat(transcript_copy).st_mtime
    sanitized = b'{"type":"redacted"}\n'

    with P.swap_restore({transcript_copy: sanitized}, backup_root=backup_root) as handle:
        # Inside: the on-disk file IS the sanitized version (this is what /feedback reads).
        assert transcript_copy.read_bytes() == sanitized
        assert handle.entries[0].original_sha256 == original_hash
        assert Path(handle.journal_path).exists()

    # After: original restored byte-for-byte AND mtime preserved.
    restored = transcript_copy.read_bytes()
    assert restored == original
    assert sha(restored) == original_hash
    assert abs(os.stat(transcript_copy).st_mtime - original_mtime) < 1e-6
    # Clean success removes backups + journal.
    assert not Path(handle.journal_path).exists()


def test_swap_restore_restores_on_body_exception(transcript_copy, backup_root):
    """A failure mid-operation (body raises) still restores the original cleanly."""
    original = transcript_copy.read_bytes()
    original_hash = sha(original)

    class BoomError(RuntimeError):
        pass

    with pytest.raises(BoomError):
        with P.swap_restore({transcript_copy: b"SANITIZED\n"}, backup_root=backup_root):
            assert transcript_copy.read_bytes() == b"SANITIZED\n"
            raise BoomError("simulated /feedback failure mid-swap")

    restored = transcript_copy.read_bytes()
    assert restored == original
    assert sha(restored) == original_hash


def test_swap_restore_multi_file(tmp_path, backup_root):
    """Multiple targets all restore byte-exact."""
    files = {}
    for i in range(4):
        p = tmp_path / f"s{i}.jsonl"
        p.write_bytes(P.serialize_records(make_records(3, session=f"s{i}")))
        files[p] = (p.read_bytes(), sha(p.read_bytes()))

    targets = {p: b'{"type":"redacted","i":%d}\n' % i for i, p in enumerate(files)}
    with P.swap_restore(targets, backup_root=backup_root):
        for i, p in enumerate(files):
            assert b"redacted" in p.read_bytes()

    for p, (orig, h) in files.items():
        assert p.read_bytes() == orig
        assert sha(p.read_bytes()) == h


def test_swap_restore_empty_bytes_truncate_exclude(transcript_copy, backup_root):
    """Swapping to b'' (truncate-to-exclude) is non-destructive: original comes back."""
    original = transcript_copy.read_bytes()
    with P.swap_restore({transcript_copy: b""}, backup_root=backup_root):
        assert transcript_copy.read_bytes() == b""
        assert os.path.getsize(transcript_copy) == 0  # /feedback skips size-0 files
    assert transcript_copy.read_bytes() == original


def test_swap_restore_rejects_missing_file(tmp_path, backup_root):
    with pytest.raises(FileNotFoundError):
        with P.swap_restore({tmp_path / "nope.jsonl": b"x"}, backup_root=backup_root):
            pass


def test_swap_restore_rejects_nonbytes(transcript_copy, backup_root):
    with pytest.raises(TypeError):
        with P.swap_restore({transcript_copy: "i am a str not bytes"}, backup_root=backup_root):  # type: ignore
            pass


def test_swap_restore_empty_targets():
    with pytest.raises(ValueError):
        with P.swap_restore({}):
            pass


def test_swap_restore_live_detection(transcript_copy, backup_root, monkeypatch):
    """A target detected as actively-written is refused unless allow_live=True."""
    monkeypatch.setattr(P, "is_being_written", lambda *a, **k: True)
    with pytest.raises(P.LiveTranscriptError):
        with P.swap_restore({transcript_copy: b"x\n"}, backup_root=backup_root):
            pass
    # allow_live bypasses, still restores
    original = transcript_copy.read_bytes()
    with P.swap_restore({transcript_copy: b"x\n"}, backup_root=backup_root, allow_live=True):
        assert transcript_copy.read_bytes() == b"x\n"
    assert transcript_copy.read_bytes() == original


def test_swap_restore_corrupt_backup_keeps_journal(transcript_copy, backup_root):
    """If restoration can't be verified, backups+journal are KEPT and RestoreError raised."""
    original = transcript_copy.read_bytes()
    with pytest.raises(P.RestoreError) as ei:
        with P.swap_restore({transcript_copy: b"SAN\n"}, backup_root=backup_root) as handle:
            # Corrupt the backup so restore can't verify against original hash.
            Path(handle.entries[0].backup_path).write_bytes(b"corrupted backup")
    err = ei.value
    assert Path(err.journal_path).exists()  # journal preserved for recovery
    assert "recover" in err.recover_hint
    # The real fixture is never lost: recover() won't help here (backup corrupt),
    # but the on-disk file was already restored-attempted; assert we did not delete data.
    # The original content may be the sanitized version since verify failed — that's
    # exactly why the journal is kept. We assert the journal still references the file.
    data = json.loads(Path(err.journal_path).read_text())
    assert data["entries"][0]["real_path"] == str(transcript_copy)
    assert data["entries"][0]["original_sha256"] == sha(original)


# --------------------------------------------------------------------------- #
# Crash recovery — true process death via subprocess + os._exit                #
# --------------------------------------------------------------------------- #
def test_recover_after_hard_process_kill(tmp_path):
    """Simulate a process killed mid-swap (os._exit skips finally); recover() heals it.

    This is the ultimate non-destructiveness proof: even if the swapping process
    dies *while the sanitized file is on disk*, the durable journal + backups let a
    later recover() put the original back, byte-for-byte.
    """
    target = tmp_path / "session.jsonl"
    original = P.serialize_records(make_records(4, session="crash-test"))
    target.write_bytes(original)
    original_hash = sha(original)
    backup_root = tmp_path / "backups"

    pkg_dir = str(Path(P.__file__).resolve().parents[1])  # fb-assist/ (so `import fb_assist`)
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, {pkg_dir!r})
        from fb_assist import package as P
        cm = P.swap_restore({{{str(target)!r}: b"SANITIZED-AND-CRASHED\\n"}},
                            backup_root={str(backup_root)!r})
        cm.__enter__()                       # swap happens; backups+journal written
        assert open({str(target)!r}, "rb").read() == b"SANITIZED-AND-CRASHED\\n"
        os._exit(137)                        # hard kill — finally never runs
    """)
    res = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert res.returncode == 137, res.stderr

    # The file is still the sanitized version (crash left it swapped).
    assert target.read_bytes() == b"SANITIZED-AND-CRASHED\n"

    # Dry-run reports the pending recovery without touching disk.
    dry = P.recover(backup_root, dry_run=True)
    assert dry and dry[0]["status"] == "would-restore"
    assert target.read_bytes() == b"SANITIZED-AND-CRASHED\n"

    # Real recover restores byte-for-byte and clears the journal.
    out = P.recover(backup_root)
    assert out and out[0]["status"] == "restored"
    assert not out[0]["failed"]
    restored = target.read_bytes()
    assert restored == original
    assert sha(restored) == original_hash
    assert not list(backup_root.glob("**/journal-*.json"))


def test_recover_noop_when_empty(tmp_path):
    assert P.recover(tmp_path / "does-not-exist") == []


def test_recover_only_paths_leaves_other_sessions(tmp_path):
    """A scoped recover restores only journals that swap a given path — it must not
    un-swap a concurrent session's still-staged journal."""
    shared_bk = tmp_path / "bk"
    a = tmp_path / "sessA.jsonl"; a.write_bytes(b"ORIG-A\n")
    b = tmp_path / "sessB.jsonl"; b.write_bytes(b"ORIG-B\n")
    P.begin_swap({str(a): b"SAN-A\n"}, backup_root=shared_bk)   # "crashed" — never finished
    P.begin_swap({str(b): b"SAN-B\n"}, backup_root=shared_bk)   # concurrent, still staged
    assert a.read_bytes() == b"SAN-A\n" and b.read_bytes() == b"SAN-B\n"

    res = P.recover(shared_bk, only_paths={str(a)})
    assert any(r["status"] == "restored" for r in res)
    assert a.read_bytes() == b"ORIG-A\n"   # A restored
    assert b.read_bytes() == b"SAN-B\n"    # B left intact for its own owner

    P.recover(shared_bk, only_paths={str(b)})
    assert b.read_bytes() == b"ORIG-B\n"


# --------------------------------------------------------------------------- #
# Two-phase swap: begin_swap / finish_swap (the in-session /feedback straddle)  #
# --------------------------------------------------------------------------- #
def test_begin_finish_straddle_byte_exact(tmp_path):
    """begin_swap leaves sanitized bytes live on disk + a journal; a LATER turn
    (the user's /feedback) reads them; finish_swap restores byte-exact. This is the
    keystone: the swap straddles a turn boundary the context manager can't cross."""
    target = tmp_path / "past-session.jsonl"
    original = P.serialize_records(make_records(5, session="past"))
    target.write_bytes(original)
    original_hash = sha(original)
    original_mtime = os.stat(target).st_mtime
    backup_root = tmp_path / "bk"

    handle = P.begin_swap({target: b'{"type":"redacted"}\n'}, backup_root=backup_root)
    # --- the model's turn has ENDED here; sanitized file is live on disk ---
    assert target.read_bytes() == b'{"type":"redacted"}\n'
    assert Path(handle.journal_path).exists()
    # simulate the user's /feedback gather reading the on-disk (sanitized) file
    assert b"redacted" in target.read_bytes()

    # --- a later turn: the user says "done" ---
    report = P.finish_swap(handle.journal_path)
    assert report.ok and not report.already_done
    assert report.restored == [str(target)]
    restored = target.read_bytes()
    assert restored == original and sha(restored) == original_hash
    assert abs(os.stat(target).st_mtime - original_mtime) < 1e-6
    assert not Path(handle.journal_path).exists()  # clean success drops the journal


def test_finish_swap_idempotent(tmp_path):
    """finish_swap is a no-op the second time (already restored / crash-healed)."""
    target = tmp_path / "s.jsonl"
    target.write_bytes(b'{"orig":1}\n')
    handle = P.begin_swap({target: b"SAN\n"}, backup_root=tmp_path / "bk")
    first = P.finish_swap(handle.journal_path)
    assert first.ok and not first.already_done
    second = P.finish_swap(handle.journal_path)
    assert second.already_done and second.ok and not second.restored


def test_begin_swap_orphan_recovered_by_recover(tmp_path):
    """A swap begun but never finished (crash/exit between turns) is healed by
    recover() — the across-turns analogue of the hard-kill test."""
    target = tmp_path / "session.jsonl"
    original = P.serialize_records(make_records(4, session="orphan"))
    target.write_bytes(original)
    backup_root = tmp_path / "bk"

    P.begin_swap({target: b"SANITIZED-ORPHAN\n"}, backup_root=backup_root)  # never finished
    assert target.read_bytes() == b"SANITIZED-ORPHAN\n"
    out = P.recover(backup_root)
    assert out and out[0]["status"] == "restored" and not out[0]["failed"]
    assert target.read_bytes() == original
    assert not list(backup_root.glob("**/journal-*.json"))


def test_begin_swap_refuses_live_session_by_id(tmp_path):
    """The target whose filename stem == the live session_id is refused
    outright (Claude Code writes per-turn, so is_being_written false-negatives
    BETWEEN turns). The heuristic stays as a secondary guard."""
    sid = "abcd1234-live-session"
    live = tmp_path / f"{sid}.jsonl"
    live.write_bytes(b'{"live":1}\n')
    with pytest.raises(P.LiveTranscriptError):
        P.begin_swap({live: b"x\n"}, backup_root=tmp_path / "bk", live_session_id=sid)
    assert live.read_bytes() == b'{"live":1}\n'  # untouched
    # A DIFFERENT (past) session with the same live_session_id set is allowed.
    past = tmp_path / "other-past-session.jsonl"
    past.write_bytes(b'{"past":1}\n')
    handle = P.begin_swap({past: b"SAN\n"}, backup_root=tmp_path / "bk2", live_session_id=sid)
    P.finish_swap(handle.journal_path)
    assert past.read_bytes() == b'{"past":1}\n'


def test_begin_swap_journals_and_restores_window_mtimes(tmp_path):
    """Windowing OTHER transcripts out of /feedback's gather is journaled, so
    finish_swap/recover undo the mtime edits too — the crash-self-healing
    guarantee extends to windowing as well."""
    target = tmp_path / "target.jsonl"
    target.write_bytes(P.serialize_records(make_records(3, session="t")))
    other = tmp_path / "other-recent.jsonl"
    other.write_bytes(P.serialize_records(make_records(3, session="o")))
    other_mtime = os.stat(other).st_mtime
    backup_root = tmp_path / "bk"

    handle = P.begin_swap(
        {target: b"SAN\n"}, backup_root=backup_root,
        window_out=[other], window="week",
    )
    # 'other' is now aged out of the week window...
    assert os.stat(other).st_mtime < time.time() - P.WINDOWS["week"]
    # ...and that edit is journaled (so a crash here is fully recoverable).
    jdata = json.loads(Path(handle.journal_path).read_text())
    assert any(m["path"] == str(other) for m in jdata["mtime_edits"])

    P.finish_swap(handle.journal_path)
    assert abs(os.stat(other).st_mtime - other_mtime) < 1e-6  # mtime restored
    assert os.stat(other).st_mtime == pytest.approx(other_mtime, abs=1e-6)


def test_begin_swap_window_out_recovered_after_crash(tmp_path):
    """Under a real crash, recover() restores BOTH the swapped target and the
    windowed-out file's mtime from the durable journal."""
    target = tmp_path / "target.jsonl"
    original = P.serialize_records(make_records(3, session="t"))
    target.write_bytes(original)
    other = tmp_path / "other.jsonl"
    other.write_bytes(b"x")
    other_mtime = os.stat(other).st_mtime
    backup_root = tmp_path / "bk"

    P.begin_swap({target: b"SAN\n"}, backup_root=backup_root, window_out=[other])  # "crash" (no finish)
    out = P.recover(backup_root)
    assert out and out[0]["status"] == "restored"
    assert str(other) in out[0]["mtime_restored"]
    assert target.read_bytes() == original
    assert abs(os.stat(other).st_mtime - other_mtime) < 1e-6


# --------------------------------------------------------------------------- #
# mtime / windowing                                                           #
# --------------------------------------------------------------------------- #
def test_move_into_and_out_of_window(transcript_copy):
    now = time.time()
    P.move_out_of_window(transcript_copy, "week", now=now)
    assert os.stat(transcript_copy).st_mtime < now - P.WINDOWS["week"]
    prev = P.move_into_window(transcript_copy, "week", now=now)
    assert os.stat(transcript_copy).st_mtime > now - P.WINDOWS["week"]
    assert prev < now - P.WINDOWS["week"]  # returned the prior (aged) mtime


def test_windowed_mtimes_restores(tmp_path):
    a = tmp_path / "a.jsonl"; a.write_bytes(b"a")
    b = tmp_path / "b.jsonl"; b.write_bytes(b"b")
    ma, mb = os.stat(a).st_mtime, os.stat(b).st_mtime
    with P.windowed_mtimes(into=[a], out_of=[b], window="week"):
        assert os.stat(b).st_mtime < ma - P.WINDOWS["week"] + 1
    # restored on exit
    assert abs(os.stat(a).st_mtime - ma) < 1e-6
    assert abs(os.stat(b).st_mtime - mb) < 1e-6


def test_unknown_window_raises(tmp_path):
    f = tmp_path / "f"; f.write_bytes(b"x")
    with pytest.raises(ValueError):
        P.move_out_of_window(f, "fortnight")


# --------------------------------------------------------------------------- #
# diff_preview                                                                 #
# --------------------------------------------------------------------------- #
def test_diff_preview_with_redaction_map():
    original = make_records(4, secret="dana@example.com")
    redacted = make_records(4, secret="[EMAIL]")
    rmap = [
        {"uuid": "u0", "category": "EMAIL", "original": "dana@example.com", "replacement": "[EMAIL]", "count": 1},
        {"uuid": "u2", "category": "EMAIL", "original": "dana@example.com", "replacement": "[EMAIL]", "count": 1},
        {"uuid": "u0", "category": "FILE_PATH", "original": "/home/devuser/code/secret-proj", "replacement": "[PATH]", "count": 1},
    ]
    s = P.diff_preview(original, redacted, redaction_map=rmap)
    assert s.stripped_by_category["EMAIL"] == 2
    assert s.stripped_by_category["FILE_PATH"] == 1
    assert s.modified_records >= 2
    assert s.bytes_after < s.bytes_before  # [EMAIL] shorter than the address
    text = s.render()
    assert "INCLUDED" in text and "STRIPPED" in text and "EMAIL" in text
    # concise, not a wall of diff
    assert len(text.splitlines()) < 25


def test_diff_preview_structural_dropped_records():
    original = make_records(5)
    redacted = original[:3]  # 2 records dropped entirely
    s = P.diff_preview(original, redacted)
    assert s.dropped_records == 2
    assert s.kept_records == 3
    assert sum(s.dropped_by_type.values()) == 2


def test_diff_preview_structural_placeholder_inference():
    original = [{"uuid": "x", "type": "user", "message": {"role": "user", "content": "email me at a@b.com"}}]
    redacted = [{"uuid": "x", "type": "user", "message": {"role": "user", "content": "email me at [EMAIL]"}}]
    s = P.diff_preview(original, redacted)
    assert s.modified_records == 1
    assert s.stripped_by_category.get("EMAIL", 0) == 1


# --------------------------------------------------------------------------- #
# budget_pack                                                                 #
# --------------------------------------------------------------------------- #
def test_budget_pack_relevance_order_and_drop():
    items = [
        P.PackItem("low", size_bytes=600_000, relevance=0.1, label="low"),
        P.PackItem("high", size_bytes=600_000, relevance=0.9, label="high"),
        P.PackItem("mid", size_bytes=600_000, relevance=0.5, label="mid"),
    ]
    res = P.budget_pack(items, limit=1_000_000)
    assert [i.id for i in res.selected] == ["high"]  # only one 600k fits
    dropped_ids = {i.id for i, _ in res.dropped}
    assert dropped_ids == {"mid", "low"}
    assert res.used_bytes == 600_000
    assert "over-budget" in dict((i.id, r) for i, r in res.dropped)["mid"]


def test_budget_pack_tie_break_prefers_smaller():
    items = [
        P.PackItem("big", size_bytes=900_000, relevance=0.5),
        P.PackItem("small", size_bytes=50_000, relevance=0.5),
    ]
    res = P.budget_pack(items, limit=1_000_000)
    # both fit; smaller should be first by tie-break, both selected
    assert {i.id for i in res.selected} == {"big", "small"}
    assert res.selected[0].id == "small"


def test_budget_pack_too_large_single_item():
    res = P.budget_pack([P.PackItem("huge", size_bytes=2_000_000, relevance=1.0)], limit=1_000_000)
    assert not res.selected
    assert res.dropped[0][0].id == "huge"
    assert "too-large" in res.dropped[0][1]


def test_budget_pack_accepts_dicts():
    res = P.budget_pack(
        [{"id": "a", "size_bytes": 100, "relevance": 1.0}, {"path": "/x/b.jsonl", "size": 100, "relevance": 0.2}],
        limit=1000,
    )
    assert len(res.selected) == 2


# --------------------------------------------------------------------------- #
# assemble_payload                                                            #
# --------------------------------------------------------------------------- #
def test_assemble_payload_basic(tmp_path):
    p1 = tmp_path / "s1.jsonl"
    p2 = tmp_path / "s2.jsonl"
    recs1 = make_records(3, session="s1")
    recs2 = make_records(3, session="s2")
    payload = P.assemble_payload(
        "Tab completion eats my Enter key on a polluted session.",
        {p1: recs1, p2: recs2},
        effort_signal={"redaction": "surgical(2 overrides)", "quality": "high", "alignment_confidence": 0.9},
    )
    assert set(payload.targets) == {str(p1), str(p2)}
    # targets serialize to valid JSONL round-trip
    assert P.parse_jsonl(payload.targets[str(p1)]) == recs1
    assert "effort signal" in payload.description
    assert payload.sessions == 2
    assert payload.total_bytes == sum(len(b) for b in payload.targets.values())


def test_assemble_payload_drops_over_budget(tmp_path):
    big = tmp_path / "big.jsonl"
    small = tmp_path / "small.jsonl"
    big_recs = make_records(1)
    big_recs[0]["message"]["content"] = "X" * 1_200_000  # alone exceeds 1 MB
    small_recs = make_records(2)
    payload = P.assemble_payload("desc", {big: big_recs, small: small_recs}, limit=P.FEEDBACK_BUDGET_BYTES)
    assert str(small) in payload.targets
    assert str(big) not in payload.targets
    assert any(path == str(big) for path, _ in payload.dropped)


def test_assemble_payload_feeds_swap_restore(tmp_path):
    """End-to-end: assemble_payload's targets plug straight into swap_restore."""
    real = tmp_path / "real.jsonl"
    real.write_bytes(P.serialize_records(make_records(4, secret="dana@example.com")))
    original = real.read_bytes()

    redacted_recs = make_records(4, secret="[EMAIL]")
    payload = P.assemble_payload("redacted feedback", {real: redacted_recs})
    backup_root = tmp_path / "bk"

    with P.swap_restore(payload.targets, backup_root=backup_root):
        on_disk = real.read_bytes()
        assert b"[EMAIL]" in on_disk
        assert b"dana@example.com" not in on_disk  # /feedback would read the redacted version
    assert real.read_bytes() == original  # original restored after submit


def test_payload_stage_is_nondestructive(tmp_path):
    real = tmp_path / "real.jsonl"
    real.write_bytes(b'{"orig":1}\n')
    payload = P.assemble_payload("d", {real: make_records(2)}, effort_signal={"quality": "high"})
    review = tmp_path / "review"
    written = payload.stage(review)
    assert (review / "description.txt").exists()
    assert (review / "effort-signal.json").exists()
    assert real.read_bytes() == b'{"orig":1}\n'  # staging never touches the real path


# --------------------------------------------------------------------------- #
# Real-fixture smoke (read-only copy) — make sure nothing chokes on real data  #
# --------------------------------------------------------------------------- #
def test_real_fixture_roundtrip_copy(tmp_path):
    src = FIXTURES / "sample-mid.jsonl"
    if not src.exists():
        pytest.skip("mid fixture absent")
    copy = tmp_path / "mid.jsonl"
    copy.write_bytes(src.read_bytes())
    records = P.parse_jsonl(copy.read_bytes())
    assert len(records) > 100
    # diff_preview on identical copies => nothing stripped
    s = P.diff_preview(records, records)
    assert s.dropped_records == 0 and s.modified_records == 0
    # swap_restore on the copy restores byte-exact
    orig = copy.read_bytes()
    with P.swap_restore({copy: b'{"type":"redacted"}\n'}, backup_root=tmp_path / "bk"):
        assert copy.read_bytes() != orig
    assert copy.read_bytes() == orig
