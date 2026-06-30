"""pps capture edge — locate + copy the candidate's Claude Code .jsonl into a bundle.

THE THIN EDGE. A candidate's work session IS a Claude Code transcript; this copies
it into ``<bundle_dir>/session.jsonl`` so the packager can parse it with
``fb_assist.transcripts``. Discovery reuses ``fb_assist.transcripts.find_transcripts``.

Usage:
    python -m pps_pipeline.capture.ccode_jsonl <bundle_dir> [--cwd <candidate_cwd>]
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys


def copy_latest_session(bundle_dir: str, cwd: str | None = None,
                        window_hours: float | None = 24.0) -> str | None:
    """Copy the most-recent CC transcript (for ``cwd``) into the bundle. Returns
    the destination path, or None if none found."""
    from fb_assist import transcripts as tx

    rows = tx.find_transcripts(cwd=cwd, window_hours=window_hours)
    if not rows:
        return None
    src = rows[0]["path"]
    os.makedirs(bundle_dir, exist_ok=True)
    dst = os.path.join(bundle_dir, "session.jsonl")
    shutil.copy2(src, dst)
    return dst


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - edge CLI
    ap = argparse.ArgumentParser(prog="pps-capture-ccode")
    ap.add_argument("bundle_dir")
    ap.add_argument("--cwd", default=None, help="candidate working dir to match")
    ap.add_argument("--window-hours", type=float, default=24.0)
    args = ap.parse_args(argv)
    dst = copy_latest_session(args.bundle_dir, cwd=args.cwd,
                              window_hours=args.window_hours)
    if not dst:
        print("no recent Claude Code transcript found", file=sys.stderr)
        return 1
    print(f"copied candidate session -> {dst}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
