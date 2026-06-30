"""pps_pipeline.capture — the thin, swappable front-end (explicitly the edge).

The pipeline's contract is the **bundle, not the capture tool**. Any recorder
that drops a valid ``SessionBundle`` (manifest + streams) into a directory works.
These are reference front-ends, smoke-noted (not unit-pinned), built last:

* ``obs_wfrecorder.sh`` — screen+audio capture via OBS / wf-recorder.
* ``mitm_har.py``       — a mitmproxy addon that exports network as HAR.
* ``ccode_jsonl.py``    — copy the candidate's Claude Code ``.jsonl`` into the
  bundle and register it in the manifest.

None of these are imported by the core; swap any of them without touching the
packager.
"""
