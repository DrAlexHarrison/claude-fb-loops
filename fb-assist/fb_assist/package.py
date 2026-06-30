"""Packaging + safe-submit primitives for fb-assist (Build 3).

This module is the *non-destructive mechanism* that makes co-authored, redacted
feedback safe to ship through Claude Code's real ``/feedback`` command.

Why it exists (the empirically-verified integration — see ../verification-evidence/RESULTS.md):
``/feedback`` does NOT read an in-memory copy of your past sessions; at submit
time it ``readdir``s the current project's transcript dir, filters ``*.jsonl`` by
mtime within the chosen window (this-session / +24h / +7d), and reads each file
*from disk*, newest-first, up to a 1 MB total budget. The on-disk file is the
source of truth. So the way to sanitize what Anthropic receives is to transiently
swap the on-disk transcript for a redacted version *around* the submit, then put
the original back — byte-for-byte, mtime and all.

The cardinal rule (spec §15): **giving feedback must never degrade the user's own
resumable history.** A destructive in-place overwrite is forbidden. Everything in
:func:`swap_restore` is built to guarantee the original is restored even if the
body raises, even if the process is killed mid-swap (durable journal + backups +
:func:`recover`).

Capabilities (all importable; CLI wraps them — see :func:`main`):

* :func:`swap_restore`  — context manager: back up → atomically write sanitized →
  yield (caller runs ``/feedback``) → restore + verify byte-exact. The load-bearing
  safety core.
* :func:`recover`       — crash recovery: restore any orphaned swaps from journals.
* :func:`diff_preview`  — concise "included / stripped" summary for the gate.
* :func:`budget_pack`   — relevance-ranked selection under the 1 MB cap; reports drops.
* mtime helpers         — :func:`move_into_window` / :func:`move_out_of_window` /
  :func:`windowed_mtimes` so the user controls exactly which sessions ``/feedback``
  gathers (it only offers a coarse time window natively).
* :func:`assemble_payload` — turn ``{description, redacted transcripts}`` into the
  on-disk ``{path: bytes}`` layout ``/feedback`` will read.

Sibling module ``fb_assist.transcripts`` (built in parallel) owns the JSONL parser,
the per-category extractors, and the ``redaction_map``. Here a *record* is just a
parsed JSON dict (one per JSONL line); see :data:`TRANSCRIPTS_CONTRACT` for the
exact seam. This module is pure-stdlib (plus optional psutil/lsof for live-write
detection) so it runs and tests standalone.

Local only. No network. No paid software.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional, Sequence, Union

PathLike = Union[str, os.PathLike]

# /feedback's hard gather constraints, named once (binary-confirmed, 2.1.195).
FEEDBACK_BUDGET_BYTES = 1_000_000  # total across all gathered transcripts
WINDOWS: "OrderedDict[str, float]" = OrderedDict(
    # name -> seconds back from "now" that /feedback's mtime filter reaches
    [("session", 0.0), ("day", 24 * 3600.0), ("week", 7 * 24 * 3600.0)]
)

# Where backups + the durable recovery journal live by default. A *stable* known
# location (not a random tempdir) is deliberate: it's what makes recover() work
# after a crash. Tests override this.
DEFAULT_BACKUP_ROOT = Path(
    os.environ.get("FB_ASSIST_BACKUP_ROOT", str(Path.home() / ".cache" / "fb-assist" / "swap-backups"))
)

# Integration seam with fb_assist.transcripts (ToolExtract). Documented, not enforced.
TRANSCRIPTS_CONTRACT = """
A *record* is one parsed JSON dict per JSONL line. Records carry an envelope
(uuid, type, timestamp, cwd, gitBranch, sessionId, version, ...) and, for
type in {user, assistant, system, attachment}, a 'message'/'content' payload.

diff_preview() optionally consumes a redaction_map: a sequence of dict entries
    {"uuid": <str|None>, "category": <str>, "original": <str>,
     "replacement": <str>, "count": <int, optional>}
describing each redaction transcripts.py applied. If absent, diff_preview falls
back to a structural record-level + placeholder-token comparison.
"""


# --------------------------------------------------------------------------- #
# Serialization                                                               #
# --------------------------------------------------------------------------- #
def serialize_records(records: Iterable[Mapping[str, Any]]) -> bytes:
    """Serialize parsed records back to JSONL bytes (one compact object per line).

    The output must be *whole-file readable* — ``/feedback`` skips files it can
    only partially read — so we emit strict UTF-8 JSONL with a trailing newline.
    """
    out = []
    for rec in records:
        out.append(json.dumps(rec, ensure_ascii=False, separators=(",", ":")))
    text = "\n".join(out)
    if text:
        text += "\n"
    return text.encode("utf-8")


def parse_jsonl(data: Union[bytes, str]) -> list[dict]:
    """Convenience JSONL reader (the real parser lives in transcripts.py).

    Tolerant of blank lines; raises on a malformed non-blank line so we never
    silently drop content.
    """
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    records: list[dict] = []
    for i, line in enumerate(data.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            raise ValueError(f"malformed JSONL at line {i + 1}: {e}") from e
    return records


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- #
# Atomic write — the primitive every disk mutation here goes through          #
# --------------------------------------------------------------------------- #
def _atomic_write(path: PathLike, data: bytes, *, mtime: Optional[float] = None) -> None:
    """Write ``data`` to ``path`` atomically (tmp in same dir → fsync → os.replace).

    Same-directory temp guarantees ``os.replace`` is a same-filesystem atomic
    rename: a reader of ``path`` sees either the entire old file or the entire
    new one, never a torn write. ``mtime`` (if given) is applied to the final
    file so we can preserve the original's timestamp on restore.
    """
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".fbassist-tmp-", dir=directory)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if mtime is not None:
            os.utime(tmp, (mtime, mtime))
        os.replace(tmp, path)
        # Best-effort directory fsync so the rename itself is durable. POSIX-only:
        # Windows has no os.O_DIRECTORY (and can't fsync a directory handle), while
        # os.replace is already atomic there — so skip rather than raise AttributeError.
        if hasattr(os, "O_DIRECTORY"):
            with contextlib.suppress(OSError):
                dfd = os.open(directory, os.O_DIRECTORY)
                try:
                    os.fsync(dfd)
                finally:
                    os.close(dfd)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


# --------------------------------------------------------------------------- #
# Live-write detection                                                         #
# --------------------------------------------------------------------------- #
class LiveTranscriptError(RuntimeError):
    """Raised when asked to swap a transcript that is being actively written.

    The current (live) session's file is owned by Claude Code's writer; rewriting
    it under the writer risks interleaving/corruption. Past, closed sessions are
    the safe, deterministic target (spec §15).
    """


def _has_open_writer(path: str) -> Optional[bool]:
    """True/False if we can determine an open writer, else None (unknown).

    Uses psutil if importable, else lsof if on PATH. Both are optional.
    """
    real = os.path.realpath(path)
    try:
        import psutil  # type: ignore

        for proc in psutil.process_iter(["open_files"]):
            try:
                for of in proc.info["open_files"] or []:
                    mode = getattr(of, "mode", "") or ""
                    if os.path.realpath(of.path) == real and ("w" in mode or "a" in mode or "+" in mode):
                        return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
    except Exception:
        pass

    lsof = shutil.which("lsof")
    if lsof:
        import subprocess

        try:
            # -F fn prints field-tagged: 'f' = fd/mode, 'n' = name. A trailing
            # 'w'/'u' on the fd field marks a write/read-write handle.
            res = subprocess.run([lsof, "-F", "fn", "--", real], capture_output=True, text=True, timeout=4)
            writer = False
            for ln in res.stdout.splitlines():
                if ln[:1] == "f":
                    writer = ln.rstrip().endswith(("w", "u", "W", "U"))
            return writer
        except Exception:
            return None
    return None


def is_being_written(path: PathLike, *, settle_s: float = 0.15, use_open_check: bool = True) -> bool:
    """Heuristically detect whether ``path`` is being appended to right now.

    Two independent signals, either is sufficient:
      1. An open write/append handle (psutil/lsof) — definitive when available.
      2. A settle sample: stat, wait ``settle_s``, stat again; size or mtime
         changing means a writer is active.
    Conservative by design — a false "live" just makes the caller pass
    ``allow_live=True`` consciously; a false "not live" is the dangerous miss,
    so we use two signals.
    """
    path = os.fspath(path)
    if use_open_check:
        writer = _has_open_writer(path)
        if writer:
            return True

    if settle_s and settle_s > 0:
        try:
            s1 = os.stat(path)
            time.sleep(settle_s)
            s2 = os.stat(path)
        except FileNotFoundError:
            return False
        if (s1.st_size, s1.st_mtime_ns) != (s2.st_size, s2.st_mtime_ns):
            return True
    return False


# --------------------------------------------------------------------------- #
# mtime / windowing — control exactly which sessions /feedback gathers         #
# --------------------------------------------------------------------------- #
def _window_seconds(window: str) -> float:
    try:
        return WINDOWS[window]
    except KeyError:
        raise ValueError(f"unknown window {window!r}; choose from {list(WINDOWS)}") from None


def get_mtime(path: PathLike) -> float:
    return os.stat(os.fspath(path)).st_mtime


def set_mtime(path: PathLike, when: float) -> float:
    """Set ``path``'s mtime to ``when``; return the *previous* mtime (for undo)."""
    path = os.fspath(path)
    prev = os.stat(path).st_mtime
    os.utime(path, (when, when))
    return prev


def move_into_window(path: PathLike, window: str = "week", *, now: Optional[float] = None, margin_s: float = 60.0) -> float:
    """Bump mtime so ``/feedback`` *will* gather this file in ``window``.

    Sets mtime to ``now - margin_s`` (just inside the boundary; the small margin
    avoids a future-timestamp). Returns the previous mtime so the caller can
    restore it. For ``window='session'`` mtime is irrelevant (only the current
    session is gathered), but we still freshen it harmlessly.
    """
    now = time.time() if now is None else now
    return set_mtime(path, now - margin_s)


def move_out_of_window(path: PathLike, window: str = "week", *, now: Optional[float] = None, margin_s: float = 3600.0) -> float:
    """Age mtime so ``/feedback`` will *not* gather this file in ``window``.

    Sets mtime to ``now - window_seconds - margin_s``. Returns previous mtime.
    (To exclude a file regardless of window, prefer swapping its content to b""
    via :func:`swap_restore`, which is non-destructive — see truncate note.)
    """
    now = time.time() if now is None else now
    secs = _window_seconds(window)
    return set_mtime(path, now - secs - margin_s)


@contextlib.contextmanager
def windowed_mtimes(
    into: Sequence[PathLike] = (),
    out_of: Sequence[PathLike] = (),
    *,
    window: str = "week",
    now: Optional[float] = None,
) -> Iterator[None]:
    """Temporarily push ``into`` files in-window and ``out_of`` files out-of-window.

    Restores every touched file's original mtime on exit (even on error) — purely
    metadata, fully reversible. Pair with :func:`swap_restore` to make
    ``/feedback``'s native gather match a :func:`budget_pack` decision exactly.
    """
    saved: list[tuple[str, float]] = []
    try:
        for p in into:
            p = os.fspath(p)
            saved.append((p, move_into_window(p, window, now=now)))
        for p in out_of:
            p = os.fspath(p)
            saved.append((p, move_out_of_window(p, window, now=now)))
        yield
    finally:
        for p, prev in reversed(saved):
            with contextlib.suppress(FileNotFoundError):
                os.utime(p, (prev, prev))


# --------------------------------------------------------------------------- #
# swap_restore — the load-bearing non-destructive mechanism                    #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class SwapEntry:
    real_path: str
    backup_path: str
    original_sha256: str
    original_mtime: float
    original_size: int
    sanitized_sha256: str
    sanitized_size: int


@dataclasses.dataclass
class SwapHandle:
    """Yielded by :func:`swap_restore`; lets the caller inspect what is swapped."""
    entries: list[SwapEntry]
    journal_path: str

    @property
    def paths(self) -> list[str]:
        return [e.real_path for e in self.entries]


class RestoreError(RuntimeError):
    """Restore failed or could not be verified. Backups/journal are KEPT.

    Carries ``recover_hint`` — the exact command to recover by hand. This error
    is the only outcome in which the user's original is not provably back on disk,
    so it is loud and the durable journal is preserved.
    """

    def __init__(self, message: str, *, journal_path: str, failures: list[str]):
        self.journal_path = journal_path
        self.failures = failures
        self.recover_hint = f"python -m fb_assist.package recover --backup-root {os.path.dirname(journal_path)}"
        super().__init__(f"{message}\n  backups+journal kept at: {journal_path}\n  recover with: {self.recover_hint}")


def _write_journal(
    backup_root: Path,
    entries: list[SwapEntry],
    mtime_edits: Optional[list[dict]] = None,
) -> Path:
    backup_root.mkdir(parents=True, exist_ok=True)
    jp = backup_root / f"journal-{os.getpid()}-{int(time.time()*1000)}-{os.urandom(3).hex()}.json"
    payload = {
        "version": 2,
        "created": time.time(),
        "pid": os.getpid(),
        "entries": [dataclasses.asdict(e) for e in entries],
        # FIX 2 (mandatory): mtime-only edits to *other* transcripts (windowing) are
        # journaled BEFORE they happen so recover()/finish_swap can undo them too.
        # Without this the crash-self-healing guarantee was false for windowing.
        "mtime_edits": list(mtime_edits or []),
    }
    _atomic_write(jp, json.dumps(payload, indent=2).encode("utf-8"))
    return jp


def _restore_entry(entry: SwapEntry) -> None:
    """Restore one file from its backup and verify byte-exact. Raises on mismatch."""
    with open(entry.backup_path, "rb") as f:
        backup_bytes = f.read()
    if _sha256(backup_bytes) != entry.original_sha256:
        raise RestoreError(
            f"backup for {entry.real_path} is itself corrupt (hash mismatch)",
            journal_path="(in-flight)",
            failures=[entry.real_path],
        )
    _atomic_write(entry.real_path, backup_bytes, mtime=entry.original_mtime)
    with open(entry.real_path, "rb") as f:
        now_bytes = f.read()
    if _sha256(now_bytes) != entry.original_sha256:
        raise RuntimeError(f"post-restore verification failed for {entry.real_path}")


@dataclasses.dataclass
class RestoreReport:
    """Result of :func:`finish_swap` — what a single journal's restore did."""
    journal_path: str
    restored: list[str]              # transcript files put back byte-exact
    mtime_restored: list[str]        # windowed-out files whose mtime was undone
    failures: list[str]              # human-readable per-target failure strings
    already_done: bool = False       # journal absent => nothing to do (idempotent)

    @property
    def ok(self) -> bool:
        return not self.failures


# Keys of SwapEntry that survive a journal round-trip (back-compat tolerant).
_SWAP_ENTRY_KEYS = (
    "real_path", "backup_path", "original_sha256", "original_mtime",
    "original_size", "sanitized_sha256", "sanitized_size",
)


def _restore_journal_payload(data: Mapping[str, Any]) -> tuple[list[str], list[str], list[str]]:
    """Restore every entry + mtime_edit described by a parsed journal payload.

    Best-effort across all items (one failure must not abandon the others).
    Returns ``(restored_paths, mtime_restored_paths, failures)``.
    """
    restored: list[str] = []
    mtime_restored: list[str] = []
    failures: list[str] = []
    for d in data.get("entries", []):
        try:
            entry = SwapEntry(**{k: d[k] for k in _SWAP_ENTRY_KEYS if k in d})
            _restore_entry(entry)
            restored.append(entry.real_path)
        except Exception as e:  # noqa: BLE001 - aggregate
            failures.append(f"{d.get('real_path', '?')}: {e}")
    # FIX 2: undo mtime-only edits to other transcripts (windowing). A windowed
    # file that has since vanished is not a failure — there's nothing to restore.
    for m in data.get("mtime_edits", []):
        p = m.get("path")
        try:
            os.utime(p, (m["original_mtime"], m["original_mtime"]))
            mtime_restored.append(p)
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            failures.append(f"mtime:{p}: {e}")
    return restored, mtime_restored, failures


def begin_swap(
    targets: Mapping[PathLike, bytes],
    *,
    backup_root: Optional[PathLike] = None,
    allow_live: bool = False,
    set_mtime_now: bool = True,
    settle_s: float = 0.15,
    live_session_id: Optional[str] = None,
    window_out: Sequence[PathLike] = (),
    window: str = "week",
) -> SwapHandle:
    """Phase 1 of the two-phase swap: back up + journal + atomically install the
    sanitized bytes, then **return** — leaving them live on disk for a *later* turn.

    This decomposes the :func:`swap_restore` context manager so the swap can
    straddle the user's interactive ``/feedback`` turn (which the model cannot
    drive). Call :func:`finish_swap` (or :func:`recover`) afterward to restore the
    originals byte-exact. The durable journal makes a crash between begin and finish
    fully recoverable, so the non-destructiveness guarantee holds across turns.

    Guarantees are identical to :func:`swap_restore` (atomic, crash-durable,
    refuses live files), plus:

    * **FIX 3 — live-file refusal by identity, not heuristic.** Claude Code writes
      the current session's transcript per-turn, so the ``is_being_written``
      heuristic false-negatives *between* turns. If ``live_session_id`` is given,
      any target whose filename stem equals it is refused outright (the heuristic
      stays as a secondary check). The safe target is always a *past/closed*
      session, or a checkpointed one (spec §15).
    * **FIX 2 — windowing is journaled.** ``window_out`` files (other transcripts
      aged out of ``/feedback``'s gather window) have their original mtimes recorded
      in the journal *before* any mtime is touched, and are restored by
      :func:`finish_swap`/:func:`recover`.
    """
    backup_root = Path(backup_root) if backup_root is not None else DEFAULT_BACKUP_ROOT
    targets = {os.fspath(p): b for p, b in targets.items()}
    if not targets:
        raise ValueError("begin_swap: no targets given")

    # ---- Phase 1: pre-flight ALL targets. Nothing on disk changes if any fails.
    entries: list[SwapEntry] = []
    staged: list[tuple[str, bytes, float]] = []  # (real_path, sanitized_bytes, mtime_to_apply)
    for path, sanitized in targets.items():
        if not isinstance(sanitized, (bytes, bytearray)):
            raise TypeError(f"sanitized content for {path} must be bytes, got {type(sanitized).__name__}")
        if not os.path.isfile(path):
            raise FileNotFoundError(f"swap target does not exist: {path}")
        # FIX 3: refuse the live session by session-id identity first.
        if not allow_live and live_session_id and Path(path).stem == live_session_id:
            raise LiveTranscriptError(
                f"{path} is the live session ({live_session_id}); refusing to swap it. "
                "Target a past/closed session, or checkpoint (/clear) first (spec §15)."
            )
        if not allow_live and is_being_written(path, settle_s=settle_s):
            raise LiveTranscriptError(
                f"{path} appears to be actively written (live session). "
                "Pass allow_live=True only if you know it is closed."
            )
        with open(path, "rb") as f:
            original = f.read()
        st = os.stat(path)
        entries.append(
            SwapEntry(
                real_path=path,
                backup_path="",  # filled in phase 2
                original_sha256=_sha256(original),
                original_mtime=st.st_mtime,
                original_size=len(original),
                sanitized_sha256=_sha256(bytes(sanitized)),
                sanitized_size=len(sanitized),
            )
        )
        mtime = time.time() if set_mtime_now else st.st_mtime
        staged.append((path, bytes(sanitized), mtime))

    # ---- Phase 1b: pre-flight the windowing targets (record their CURRENT mtimes
    # before we age them out, so the journal can undo it). FIX 2.
    target_set = set(targets)
    mtime_edits: list[dict] = []
    window_plan: list[tuple[str, float]] = []  # (path, aged_mtime_to_apply)
    now = time.time()
    secs = _window_seconds(window)
    for p in window_out:
        p = os.fspath(p)
        if p in target_set or not os.path.isfile(p):
            continue  # never age a swap target; skip vanished files
        st = os.stat(p)
        mtime_edits.append({"path": p, "original_mtime": st.st_mtime})
        window_plan.append((p, now - secs - 3600.0))  # mirrors move_out_of_window margin

    # ---- Phase 2: write durable backups for every target (+fsync), then journal.
    backup_root.mkdir(parents=True, exist_ok=True)
    batch_dir = Path(tempfile.mkdtemp(prefix="swap-", dir=backup_root))
    try:
        for entry in entries:
            with open(entry.real_path, "rb") as f:
                original = f.read()
            bp = batch_dir / (hashlib.sha1(entry.real_path.encode()).hexdigest() + ".bak")
            _atomic_write(bp, original)
            entry.backup_path = str(bp)
        journal_path = _write_journal(batch_dir, entries, mtime_edits=mtime_edits)
    except BaseException:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    # ---- Phase 3: swap each target to its sanitized bytes (atomic); then age out
    # the windowing files. The journal (with backups + mtime_edits) is already
    # durable, so a crash anywhere past this point is fully recoverable.
    try:
        for (path, sanitized, mtime), entry in zip(staged, entries):
            _atomic_write(path, sanitized, mtime=mtime)
        for p, aged in window_plan:
            os.utime(p, (aged, aged))
    except BaseException:
        # Roll back whatever we managed to change, then surface the error. The
        # caller never received a handle, so it cannot call finish_swap itself.
        with contextlib.suppress(Exception):
            data = json.loads(journal_path.read_text())
            _restore_journal_payload(data)
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise

    return SwapHandle(entries=entries, journal_path=str(journal_path))


def finish_swap(journal_path: PathLike, *, raise_on_failure: bool = True) -> RestoreReport:
    """Phase 2 of the two-phase swap: restore the originals named by ``journal_path``
    byte-exact, undo any journaled windowing, verify, and clean up.

    Idempotent: if the journal is already gone (restore happened, or a crash was
    healed by :func:`recover`) it returns ``already_done=True`` and does nothing.
    On a restore/verify failure the backups + journal are KEPT and — if
    ``raise_on_failure`` — :class:`RestoreError` is raised with the recovery command.
    This is the deliberate, single-journal form of :func:`recover`.
    """
    jp = Path(os.fspath(journal_path))
    if not jp.exists():
        return RestoreReport(str(jp), restored=[], mtime_restored=[], failures=[], already_done=True)
    data = json.loads(jp.read_text())
    restored, mtime_restored, failures = _restore_journal_payload(data)
    report = RestoreReport(str(jp), restored, mtime_restored, failures)
    if failures:
        if raise_on_failure:
            raise RestoreError(
                "swap_restore could not verify restoration of: " + "; ".join(failures),
                journal_path=str(jp),
                failures=failures,
            )
        return report
    # clean success — drop backups + journal (the journal lives inside the batch dir)
    shutil.rmtree(jp.parent, ignore_errors=True)
    return report


@contextlib.contextmanager
def swap_restore(
    targets: Mapping[PathLike, bytes],
    *,
    backup_root: Optional[PathLike] = None,
    allow_live: bool = False,
    set_mtime_now: bool = True,
    settle_s: float = 0.15,
) -> Iterator[SwapHandle]:
    """Transiently swap real transcripts for sanitized bytes, then restore them.

    Usage::

        with swap_restore({path: sanitized_bytes}) as handle:
            run_feedback()        # /feedback reads the sanitized files here
        # <-- originals are byte-for-byte back on disk, verified, by now

    Guarantees:
      * **Atomic** — each swap and each restore is a tmp+fsync+os.replace; a reader
        never sees a torn file.
      * **Non-destructive** — the original content *and mtime* are restored exactly
        (verified by sha256). Resumability + ``/feedback``'s mtime windowing are
        unaffected afterward.
      * **Restore-on-any-error** — restore runs in ``finally``; an exception in the
        body still restores.
      * **Crash-durable** — backups + a journal are written (and fsynced) *before*
        any target is swapped, so a hard kill mid-swap is fully recoverable via
        :func:`recover`.
      * **Refuses live files** — a target detected as actively-written raises
        :class:`LiveTranscriptError` unless ``allow_live=True``.

    On a restore/verify failure the backups and journal are KEPT and
    :class:`RestoreError` is raised with the exact recovery command.

    ``set_mtime_now`` (default True) stamps the sanitized file at ~now so it stays
    inside ``/feedback``'s 24h/7d window; the original's mtime is always restored
    regardless.

    This is now a thin wrapper over :func:`begin_swap` / :func:`finish_swap` (the
    two-phase primitives the in-session runtime needs); the single-turn semantics
    here are unchanged.
    """
    handle = begin_swap(
        targets,
        backup_root=backup_root,
        allow_live=allow_live,
        set_mtime_now=set_mtime_now,
        settle_s=settle_s,
    )
    try:
        yield handle
    finally:
        finish_swap(handle.journal_path, raise_on_failure=True)


def recover(backup_root: Optional[PathLike] = None, *, dry_run: bool = False) -> list[dict]:
    """Restore any orphaned swaps left by a crash, from their durable journals.

    Scans ``backup_root`` for ``journal-*.json`` (including in per-batch subdirs),
    restores each entry's original from its backup, verifies byte-exact, undoes any
    journaled windowing mtimes, and on success removes the batch. Idempotent and
    safe to run anytime.

    Returns a list of per-journal result dicts. ``dry_run=True`` reports what
    *would* be restored without touching anything.
    """
    root = Path(backup_root) if backup_root is not None else DEFAULT_BACKUP_ROOT
    results: list[dict] = []
    if not root.exists():
        return results

    journals = sorted(root.glob("**/journal-*.json"))
    for jp in journals:
        try:
            data = json.loads(jp.read_text())
        except Exception as e:  # noqa: BLE001
            results.append({"journal": str(jp), "status": "unreadable", "error": str(e)})
            continue

        if dry_run:
            restored = [d.get("real_path") for d in data.get("entries", [])]
            results.append({
                "journal": str(jp), "status": "would-restore",
                "restored": restored,
                "mtime_restored": [m.get("path") for m in data.get("mtime_edits", [])],
                "failed": [],
            })
            continue

        restored, mtime_restored, failed = _restore_journal_payload(data)
        status = "restored" if not failed else "partial"
        results.append({
            "journal": str(jp), "status": status,
            "restored": restored, "mtime_restored": mtime_restored, "failed": failed,
        })
        if not failed:
            # whole batch good -> remove backup dir (journal lives inside it)
            shutil.rmtree(jp.parent, ignore_errors=True)
    return results


# --------------------------------------------------------------------------- #
# diff_preview — the concise confirmation-gate view                            #
# --------------------------------------------------------------------------- #
def _record_text(rec: Mapping[str, Any]) -> str:
    """Best-effort flatten of a record's human-content into a string (for samples)."""
    msg = rec.get("message")
    if isinstance(msg, Mapping):
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for blk in content:
                if isinstance(blk, Mapping):
                    parts.append(blk.get("text") or blk.get("thinking") or blk.get("content") or "")
                else:
                    parts.append(str(blk))
            return " ".join(p for p in parts if isinstance(p, str))
    if isinstance(rec.get("content"), str):
        return rec["content"]
    return ""


def _short(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


@dataclasses.dataclass
class PreviewSummary:
    """Structured 'included / stripped' summary. ``render()`` is the gate text."""
    kept_records: int
    dropped_records: int
    modified_records: int
    bytes_before: int
    bytes_after: int
    dropped_by_type: dict
    stripped_by_category: dict
    samples: list  # list of (category, "original -> replacement") short strings
    sessions: int = 1

    @property
    def bytes_saved(self) -> int:
        return self.bytes_before - self.bytes_after

    def render(self, max_samples: int = 5) -> str:
        pct = (100 * self.bytes_saved / self.bytes_before) if self.bytes_before else 0.0
        lines = ["Feedback bundle — what will be sent:"]
        lines.append(
            f"  INCLUDED : {self.kept_records} records"
            + (f" across {self.sessions} sessions" if self.sessions != 1 else "")
            + f"  ({self.bytes_after:,} bytes)"
        )
        strip_bits = []
        if self.dropped_records:
            strip_bits.append(f"{self.dropped_records} records dropped")
        if self.modified_records:
            strip_bits.append(f"{self.modified_records} records redacted")
        lines.append(
            f"  STRIPPED : {', '.join(strip_bits) or 'nothing'}"
            f"  (-{self.bytes_saved:,} bytes, {pct:.0f}% smaller)"
        )
        if self.dropped_by_type:
            by_type = ", ".join(f"{n}×{t}" for t, n in sorted(self.dropped_by_type.items(), key=lambda kv: -kv[1]))
            lines.append(f"    dropped types : {by_type}")
        if self.stripped_by_category:
            by_cat = ", ".join(f"{n}×{c}" for c, n in sorted(self.stripped_by_category.items(), key=lambda kv: -kv[1]))
            lines.append(f"    redacted      : {by_cat}")
        if self.samples:
            lines.append(f"    e.g. (showing {min(len(self.samples), max_samples)} of {len(self.samples)}):")
            for cat, s in self.samples[:max_samples]:
                lines.append(f"        [{cat}] {s}")
        return "\n".join(lines)


_PLACEHOLDER_HINTS = ("[REDACTED", "[EMAIL", "[SECRET", "[PATH", "[NAME", "[IP", "[TOKEN", "[KEY", "█")


def diff_preview(
    original_records: Sequence[Mapping[str, Any]],
    redacted_records: Sequence[Mapping[str, Any]],
    *,
    redaction_map: Optional[Sequence[Mapping[str, Any]]] = None,
    max_samples: int = 5,
) -> PreviewSummary:
    """Build a concise included/stripped summary — never a wall-of-diff.

    Aligns records by ``uuid`` when present (falls back to positional). A record
    in ``original`` but not ``redacted`` is *dropped*; a record present in both
    with differing content is *redacted*. If ``redaction_map`` is supplied (from
    transcripts.py) it drives the per-category counts and the few short samples;
    otherwise categories are inferred structurally from placeholder tokens.
    """
    def index(records):
        idx = OrderedDict()
        for i, r in enumerate(records):
            key = r.get("uuid") or f"__pos_{i}"
            idx[key] = r
        return idx

    oi, ri = index(original_records), index(redacted_records)

    dropped_by_type: Counter = Counter()
    modified = 0
    for key, orec in oi.items():
        if key not in ri:
            dropped_by_type[orec.get("type", "unknown")] += 1
        else:
            if _record_text(orec) != _record_text(ri[key]) or orec != ri[key]:
                modified += 1

    bytes_before = len(serialize_records(original_records))
    bytes_after = len(serialize_records(redacted_records))

    stripped_by_category: Counter = Counter()
    samples: list[tuple[str, str]] = []
    if redaction_map:
        for e in redaction_map:
            cat = str(e.get("category", "REDACTED"))
            stripped_by_category[cat] += int(e.get("count", 1))
            if e.get("original") is not None and e.get("replacement") is not None:
                samples.append((cat, f"{_short(e['original'], 40)} → {_short(e['replacement'], 24)}"))
    else:
        # Structural fallback: count placeholder tokens that appear in the
        # redacted text but not the original, bucketed by their leading hint.
        for key, rrec in ri.items():
            if key not in oi:
                continue
            otext, rtext = _record_text(oi[key]), _record_text(rrec)
            if otext == rtext:
                continue
            for hint in _PLACEHOLDER_HINTS:
                delta = rtext.count(hint) - otext.count(hint)
                if delta > 0:
                    cat = hint.strip("[█") or "REDACTED"
                    stripped_by_category[cat] += delta
                    if len(samples) < max_samples:
                        samples.append((cat, _short(rtext, 60)))

    # de-dup samples while preserving order
    seen, uniq = set(), []
    for s in samples:
        if s not in seen:
            seen.add(s)
            uniq.append(s)

    return PreviewSummary(
        kept_records=len(ri),
        dropped_records=sum(dropped_by_type.values()),
        modified_records=modified,
        bytes_before=bytes_before,
        bytes_after=bytes_after,
        dropped_by_type=dict(dropped_by_type),
        stripped_by_category=dict(stripped_by_category),
        samples=uniq,
        sessions=len({r.get("sessionId") for r in redacted_records if r.get("sessionId")}) or 1,
    )


# --------------------------------------------------------------------------- #
# budget_pack — relevance-ranked selection under the 1 MB cap                  #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class PackItem:
    id: str
    size_bytes: int
    relevance: float = 0.0
    label: Optional[str] = None
    payload: Any = None  # e.g. a path, bytes, or record list the caller cares about


@dataclasses.dataclass
class PackResult:
    selected: list[PackItem]
    dropped: list[tuple[PackItem, str]]  # (item, reason)
    used_bytes: int
    limit: int

    @property
    def free_bytes(self) -> int:
        return self.limit - self.used_bytes

    def render(self) -> str:
        lines = [f"Budget pack — {self.used_bytes:,} / {self.limit:,} bytes used ({len(self.selected)} selected):"]
        for it in self.selected:
            lines.append(f"  ✓ {it.label or it.id}  ({it.size_bytes:,} B, rel={it.relevance:g})")
        for it, reason in self.dropped:
            lines.append(f"  ✗ {it.label or it.id}  ({it.size_bytes:,} B)  — dropped: {reason}")
        return "\n".join(lines)


def _coerce_item(obj: Any, i: int, relevance_key, size_key) -> PackItem:
    if isinstance(obj, PackItem):
        return obj
    if isinstance(obj, Mapping):
        return PackItem(
            id=str(obj.get("id", obj.get("path", i))),
            size_bytes=int(size_key(obj) if size_key else obj.get("size_bytes", obj.get("size", 0))),
            relevance=float(relevance_key(obj) if relevance_key else obj.get("relevance", 0.0)),
            label=obj.get("label") or obj.get("path"),
            payload=obj.get("payload", obj),
        )
    # arbitrary object: pull via keys if provided
    return PackItem(
        id=str(getattr(obj, "id", i)),
        size_bytes=int(size_key(obj) if size_key else getattr(obj, "size_bytes", 0)),
        relevance=float(relevance_key(obj) if relevance_key else getattr(obj, "relevance", 0.0)),
        label=getattr(obj, "label", None),
        payload=obj,
    )


def budget_pack(
    items: Iterable[Any],
    limit: int = FEEDBACK_BUDGET_BYTES,
    *,
    relevance_key: Optional[Callable[[Any], float]] = None,
    size_key: Optional[Callable[[Any], int]] = None,
) -> PackResult:
    """Greedily select the most relevant items that fit under ``limit``.

    Ranks by ``relevance`` desc, tie-broken by *smaller* size (fit more high-value
    items). Greedy because ``/feedback`` reads whole files newest-first and skips
    a partial read — so we select whole items and **never silently truncate**.
    Everything that doesn't make the cut is returned in ``dropped`` with a reason
    (``too-large`` for a single item over budget, ``over-budget`` otherwise), so
    the caller can surface it and, e.g., push those sessions out-of-window.

    Items may be :class:`PackItem`, dicts (``size_bytes``/``size`` +
    ``relevance``), or arbitrary objects via ``size_key``/``relevance_key``.
    """
    coerced = [_coerce_item(o, i, relevance_key, size_key) for i, o in enumerate(items)]
    # Stable sort: relevance desc, then size asc, then original order.
    order = sorted(range(len(coerced)), key=lambda i: (-coerced[i].relevance, coerced[i].size_bytes, i))

    selected: list[PackItem] = []
    dropped: list[tuple[PackItem, str]] = []
    used = 0
    for i in order:
        it = coerced[i]
        if it.size_bytes > limit:
            dropped.append((it, f"too-large ({it.size_bytes:,} B > {limit:,} B budget; slice it finer)"))
            continue
        if used + it.size_bytes <= limit:
            selected.append(it)
            used += it.size_bytes
        else:
            dropped.append((it, f"over-budget (only {limit - used:,} B free)"))
    return PackResult(selected=selected, dropped=dropped, used_bytes=used, limit=limit)


# --------------------------------------------------------------------------- #
# assemble_payload — {description, redacted transcripts} -> on-disk layout      #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class Payload:
    """The on-disk layout ``/feedback`` will read, plus what didn't fit.

    ``targets`` ({real_path: sanitized_bytes}) feeds straight into
    :func:`swap_restore`. ``description`` is the co-author-authored text (with an
    optional effort-signal footer). ``dropped`` lists sessions excluded for budget
    so the caller can age them out-of-window.
    """
    description: str
    targets: "OrderedDict[str, bytes]"
    dropped: list[tuple[str, str]]  # (path, reason)
    total_bytes: int
    effort_signal: Optional[dict] = None
    sessions: int = 0

    def stage(self, review_dir: PathLike) -> list[str]:
        """Write a *non-destructive* reviewable copy of the bundle (no real paths
        touched) — for a '/feedback save'-style human look before the swap.
        Returns the written file paths.
        """
        review_dir = Path(review_dir)
        review_dir.mkdir(parents=True, exist_ok=True)
        written = []
        (review_dir / "description.txt").write_text(self.description, encoding="utf-8")
        written.append(str(review_dir / "description.txt"))
        for path, data in self.targets.items():
            dest = review_dir / (Path(path).name)
            _atomic_write(dest, data)
            written.append(str(dest))
        if self.effort_signal is not None:
            (review_dir / "effort-signal.json").write_text(json.dumps(self.effort_signal, indent=2), encoding="utf-8")
            written.append(str(review_dir / "effort-signal.json"))
        return written


def _normalize_transcripts(transcripts) -> "OrderedDict[str, dict]":
    """Accept {path: records} or [Transcript-like] -> OrderedDict[path] = {records, relevance, mtime}."""
    norm: "OrderedDict[str, dict]" = OrderedDict()
    if isinstance(transcripts, Mapping):
        for path, recs in transcripts.items():
            norm[os.fspath(path)] = {"records": list(recs), "relevance": None, "mtime": None}
        return norm
    for t in transcripts:
        if isinstance(t, Mapping):
            path = os.fspath(t["path"])
            norm[path] = {
                "records": list(t.get("records", [])),
                "relevance": t.get("relevance"),
                "mtime": t.get("mtime"),
            }
        else:  # object with attrs
            path = os.fspath(t.path)
            norm[path] = {
                "records": list(getattr(t, "records", [])),
                "relevance": getattr(t, "relevance", None),
                "mtime": getattr(t, "mtime", None),
            }
    return norm


def _render_effort_footer(sig: Mapping[str, Any]) -> str:
    bits = []
    if sig.get("redaction"):
        bits.append(f"redaction={sig['redaction']}")
    if sig.get("quality") is not None:
        bits.append(f"quality={sig['quality']}")
    if sig.get("alignment_confidence") is not None:
        bits.append(f"alignment_confidence={sig['alignment_confidence']}")
    if sig.get("reputation_token"):
        bits.append(f"rep={sig['reputation_token']}")
    return "[fb-assist effort signal] " + "; ".join(bits) if bits else ""


def assemble_payload(
    description: str,
    transcripts,
    *,
    limit: int = FEEDBACK_BUDGET_BYTES,
    effort_signal: Optional[Mapping[str, Any]] = None,
    include_effort_footer: bool = True,
) -> Payload:
    """Produce the on-disk ``{description, sanitized transcript(s)}`` layout.

    Serializes each session's *redacted* records to JSONL bytes, runs
    :func:`budget_pack` over them (relevance, then size; default relevance is
    recency via ``mtime`` so it mirrors ``/feedback``'s newest-first), and returns
    a :class:`Payload` whose ``targets`` is ready for :func:`swap_restore`.
    Sessions that don't fit the 1 MB budget are reported in ``dropped`` — never
    silently truncated.

    ``transcripts`` may be ``{real_path: redacted_records}`` or a list of objects
    with ``.path`` / ``.records`` (+ optional ``.relevance`` / ``.mtime``).
    """
    norm = _normalize_transcripts(transcripts)

    # Build pack items: size = serialized JSONL length; relevance = explicit or recency.
    serialized: dict[str, bytes] = {}
    items: list[PackItem] = []
    for path, meta in norm.items():
        data = serialize_records(meta["records"])
        serialized[path] = data
        rel = meta["relevance"]
        if rel is None:
            # recency proxy: prefer on-disk mtime, else file's, else 0
            rel = meta["mtime"]
            if rel is None and os.path.exists(path):
                with contextlib.suppress(OSError):
                    rel = os.stat(path).st_mtime
            rel = float(rel or 0.0)
        items.append(PackItem(id=path, size_bytes=len(data), relevance=rel, label=Path(path).name, payload=path))

    packed = budget_pack(items, limit=limit)

    targets: "OrderedDict[str, bytes]" = OrderedDict()
    for it in packed.selected:
        targets[it.id] = serialized[it.id]
    dropped = [(it.id, reason) for it, reason in packed.dropped]

    full_description = description.rstrip()
    if effort_signal and include_effort_footer:
        footer = _render_effort_footer(effort_signal)
        if footer:
            full_description = f"{full_description}\n\n---\n{footer}"

    return Payload(
        description=full_description,
        targets=targets,
        dropped=dropped,
        total_bytes=packed.used_bytes,
        effort_signal=dict(effort_signal) if effort_signal else None,
        sessions=len(targets),
    )


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def _cli_preview(args) -> int:
    original = parse_jsonl(Path(args.original).read_bytes())
    redacted = parse_jsonl(Path(args.redacted).read_bytes())
    rmap = json.loads(Path(args.redaction_map).read_text()) if args.redaction_map else None
    print(diff_preview(original, redacted, redaction_map=rmap).render(max_samples=args.max_samples))
    return 0


def _cli_pack(args) -> int:
    items = []
    for p in args.files:
        size = os.path.getsize(p)
        rel = os.stat(p).st_mtime  # recency proxy
        items.append(PackItem(id=p, size_bytes=size, relevance=rel, label=os.path.basename(p)))
    print(budget_pack(items, limit=args.limit).render())
    return 0


def _cli_window(args) -> int:
    now = time.time()
    for p in args.into:
        prev = move_into_window(p, args.window, now=now)
        print(f"into-window  {p}  (was mtime {prev:.0f})")
    for p in args.out_of:
        prev = move_out_of_window(p, args.window, now=now)
        print(f"out-of-window {p}  (was mtime {prev:.0f})")
    return 0


def _cli_swap(args) -> int:
    """Swap files for sanitized versions, pause for /feedback, then restore.

    Manifest JSON: {"real_path": "sanitized_file_path", ...}
    """
    manifest = json.loads(Path(args.manifest).read_text())
    targets = {real: Path(san).read_bytes() for real, san in manifest.items()}
    print(f"Swapping {len(targets)} file(s) -> sanitized. Originals backed up.")
    with swap_restore(targets, backup_root=args.backup_root, allow_live=args.allow_live) as handle:
        print("Sanitized versions are live on disk. Backups + journal at:")
        print(f"  {handle.journal_path}")
        if args.auto:
            print("--auto: not pausing; restoring immediately (test mode).")
        else:
            input("Run /feedback now in Claude Code, then press Enter to restore originals... ")
    print("Originals restored and verified byte-for-byte. ✅")
    return 0


def _cli_recover(args) -> int:
    results = recover(args.backup_root, dry_run=args.dry_run)
    if not results:
        print("Nothing to recover (no journals found).")
        return 0
    for r in results:
        print(json.dumps(r, indent=2))
    return 0 if all(r["status"] in ("restored", "would-restore") for r in results) else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fb_assist.package", description="fb-assist packaging + safe-submit primitives")
    sub = p.add_subparsers(dest="cmd", required=True)

    pv = sub.add_parser("preview", help="concise included/stripped summary of original vs redacted JSONL")
    pv.add_argument("original")
    pv.add_argument("redacted")
    pv.add_argument("--redaction-map", help="optional redaction_map JSON from transcripts.py")
    pv.add_argument("--max-samples", type=int, default=5)
    pv.set_defaults(func=_cli_preview)

    pk = sub.add_parser("pack", help="relevance/recency-rank files under the 1 MB budget")
    pk.add_argument("files", nargs="+")
    pk.add_argument("--limit", type=int, default=FEEDBACK_BUDGET_BYTES)
    pk.set_defaults(func=_cli_pack)

    wn = sub.add_parser("window", help="move files into/out of /feedback's gather window via mtime")
    wn.add_argument("--into", nargs="*", default=[])
    wn.add_argument("--out-of", nargs="*", default=[])
    wn.add_argument("--window", choices=list(WINDOWS), default="week")
    wn.set_defaults(func=_cli_window)

    sw = sub.add_parser("swap", help="swap real files for sanitized, pause for /feedback, restore")
    sw.add_argument("manifest", help='JSON: {"real_path": "sanitized_file", ...}')
    sw.add_argument("--backup-root", default=None)
    sw.add_argument("--allow-live", action="store_true")
    sw.add_argument("--auto", action="store_true", help="don't pause (test/non-interactive)")
    sw.set_defaults(func=_cli_swap)

    rc = sub.add_parser("recover", help="restore orphaned swaps from journals after a crash")
    rc.add_argument("--backup-root", default=None)
    rc.add_argument("--dry-run", action="store_true")
    rc.set_defaults(func=_cli_recover)

    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
