"""fb_os — the org-wide Feedback OS (the closed loop).

A clonable sibling package to ``fb_assist``. It ingests the distilled,
redacted feedback artifacts that package produces, clusters them locally (a
lightweight Clio reproduction), runs an internal triager that auto-generates
the living ``open-questions.json``, and publishes that file in the **exact
path + shape** the CLI's ``/fb`` already consumes — closing the bidirectional
loop.

The keystone module is :mod:`fb_os.questions` (the seam between the two
packages). Everything else (store, ingest, embed, cluster, triager, metrics)
is scaffolding around that one closed loop.

Reuses ``fb_assist`` (transcripts parser, redaction leak-scan floor, atomic writer,
effort-signal schema) — never reimplements it. Local only. No network. No paid
software. The runnable core needs zero heavy downloads.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling ``fb_assist`` package importable when fb-os is run from a source
# checkout (editable install also works; this is the zero-install fallback so
# ``make demo`` and the tests run straight from a clone).
_FB_ASSIST_DIR = Path(__file__).resolve().parent.parent.parent / "fb-assist"
if _FB_ASSIST_DIR.is_dir() and str(_FB_ASSIST_DIR) not in sys.path:
    sys.path.insert(0, str(_FB_ASSIST_DIR))

__version__ = "0.1.0"
__all__ = ["questions", "store", "ingest", "embed", "cluster", "triager", "metrics"]
