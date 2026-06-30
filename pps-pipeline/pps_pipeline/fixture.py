"""pps_pipeline.fixture — the synthetic SessionBundle generator.

Emits a complete, capture-free ``SessionBundle`` so the WHOLE pipeline runs with
**zero recording, zero network, zero paid software**: pre-baked frame captions, a
faster-whisper-shaped transcript, a HAR, and a candidate ``session.jsonl`` (a real
Claude Code transcript shape) with **planted secrets/PII on every text surface**.

The planted sentinels (exposed as :data:`SENTINELS`) let the tests prove
byte-absence after :mod:`pps_pipeline.redact_pass` — a secret in the prompt, in a
tool result, in a HAR auth header, and an email in a caption must all be gone from
the assembled package, and the leak-scan floor must read clean.

Timestamps are deterministic (a fixed ``t0`` + fixed offsets) so the mock
assessor's evidence citations land on real timeline events.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

SESSION_ID = "pps_2026-06-29_cand07"
T0_EPOCH = 1782000000.0
DURATION_S = 200.0

# --- planted sentinels (one per surface) ----------------------------------- #
ANT_KEY = "sk-ant-api03-FIXTUREKEYAAAA1111BBBB2222"   # in a typed prompt
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"                        # in a tool_result (cat .env)
BEARER = "fixturebearertoken1234567890ABCDEF"          # in a HAR auth header
EMAIL = "jane.doe@example.com"                          # in a frame caption
SENTINELS = [ANT_KEY, AWS_KEY, BEARER, EMAIL]


def _iso(offset: float) -> str:
    return datetime.fromtimestamp(T0_EPOCH + offset, tz=timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Stream builders
# --------------------------------------------------------------------------- #
def _transcript_rows() -> list[dict]:
    return [
        {"start": 14.0, "end": 18.0,
         "text": "ok the token util signature changed, let me read the file first"},
        {"start": 50.0, "end": 54.0,
         "text": "three tests failing, the signature must have changed"},
        {"start": 73.0, "end": 76.0,
         "text": "oh I shouldn't have catted the env, anyway moving on"},
        {"start": 112.0, "end": 116.0,
         "text": "good, green now, let me run the whole suite to be sure"},
    ]


def _caption_rows() -> list[dict]:
    return [
        {"t": 13.0, "text": "VS Code open on auth.ts, terminal split to the right"},
        {"t": 49.0, "text": "terminal shows 3 failing tests in red"},
        {"t": 71.0, "text": f"terminal shows .env contents; profile tab open with email {EMAIL}"},
        {"t": 159.0, "text": "terminal shows all tests passing in green"},
    ]


def _har() -> dict:
    return {"log": {
        "version": "1.2",
        "creator": {"name": "pps-fixture", "version": "0.1.0"},
        "entries": [
            {
                "startedDateTime": _iso(22.0), "time": 31,
                "request": {"method": "GET", "url": "https://api.example.com/health",
                            "headers": [{"name": "Authorization", "value": f"Bearer {BEARER}"}]},
                "response": {"status": 200, "headers": [],
                             "content": {"mimeType": "application/json", "text": "{\"status\":\"ok\"}"}},
            },
            {
                "startedDateTime": _iso(145.0), "time": 12,
                "request": {"method": "GET", "url": "https://api.example.com/health", "headers": []},
                "response": {"status": 200, "headers": [],
                             "content": {"mimeType": "application/json", "text": "{\"status\":\"ok\"}"}},
            },
        ],
    }}


def _ccode_rows() -> list[dict]:
    """Claude Code transcript shape (parsed downstream by fb_assist.transcripts)."""
    def env(uuid, parent, off, **extra):
        b = {"uuid": uuid, "parentUuid": parent, "isSidechain": False,
             "sessionId": SESSION_ID, "timestamp": _iso(off),
             "cwd": "/home/cand/proj", "gitBranch": "main", "version": "2.1.195",
             "userType": "external"}
        b.update(extra)
        return b

    return [
        env("u1", None, 10.0, type="user", promptSource="typed",
            message={"role": "user", "content": (
                "Refactor the auth middleware to use the new token util. "
                f"oops I pasted my key {ANT_KEY} by mistake, ignore that.")}),
        env("a1", "u1", 12.0, type="assistant",
            message={"role": "assistant", "model": "claude-sonnet-4-5", "content": [
                {"type": "tool_use", "id": "t1", "name": "Read",
                 "input": {"file_path": "src/middleware/auth.ts"}}]}),
        env("u2", "a1", 20.0, type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": "read 84 lines of auth.ts"}]},
            toolUseResult={"file": {"filePath": "src/middleware/auth.ts",
                                    "content": "…84 lines…", "numLines": 84}}),
        env("a2", "u2", 35.0, type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t2", "name": "Edit",
                 "input": {"file_path": "src/middleware/auth.ts",
                           "old_string": "verifyToken(x)", "new_string": "verifyToken(x, opts)"}}]}),
        env("u3", "a2", 48.0, type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": "tests: 3 failed, 18 passed"}]},
            toolUseResult={"stdout": "tests: 3 failed, 18 passed", "stderr": ""}),
        env("a3", "u3", 70.0, type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "cat .env"}}]}),
        env("u4", "a3", 72.0, type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t3", "content": f"AWS_SECRET={AWS_KEY}"}]},
            toolUseResult={"stdout": f"AWS_SECRET={AWS_KEY}", "stderr": ""}),
        env("a4", "u4", 95.0, type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t4", "name": "Edit",
                 "input": {"file_path": "src/middleware/auth.ts",
                           "old_string": "opts", "new_string": "opts || {}"}}]}),
        env("u5", "a4", 110.0, type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t4", "content": "tests: 21 passed"}]},
            toolUseResult={"stdout": "tests: 21 passed", "stderr": ""}),
        env("a5", "u5", 140.0, type="assistant",
            message={"role": "assistant", "content": [
                {"type": "tool_use", "id": "t5", "name": "Bash", "input": {"command": "npm test"}}]}),
        env("u6", "a5", 158.0, type="user",
            message={"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t5", "content": "tests: 21 passed"}]},
            toolUseResult={"stdout": "tests: 21 passed", "stderr": ""}),
    ]


def _manifest() -> dict:
    return {
        "schema_version": "1.0",
        "session_id": SESSION_ID,
        "t0_epoch": T0_EPOCH,
        "duration_s": DURATION_S,
        "consent": {"obtained": True, "scope": "work-observation", "ref": "consent.txt"},
        "streams": {
            "video":         {"ref": "video.mkv", "fps": 30, "present": False},
            "transcript":    {"ref": "transcript.jsonl", "engine": "faster-whisper",
                              "present": True, "time_base": "offset"},
            "captions":      {"ref": "captions.jsonl", "present": True, "time_base": "offset"},
            "network":       {"ref": "network.har", "present": True, "time_base": "iso"},
            "ccode_session": {"ref": "session.jsonl", "present": True, "time_base": "iso"},
            "events":        {"ref": "events.jsonl", "present": False, "time_base": "offset"},
        },
        "redaction": {"applied": False},
    }


def _write_jsonl(path: str, rows: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def generate(out_dir: str) -> str:
    """Write the full synthetic bundle to ``out_dir``. Returns ``out_dir``."""
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(_manifest(), fh, indent=2)
    _write_jsonl(os.path.join(out_dir, "transcript.jsonl"), _transcript_rows())
    _write_jsonl(os.path.join(out_dir, "captions.jsonl"), _caption_rows())
    _write_jsonl(os.path.join(out_dir, "session.jsonl"), _ccode_rows())
    with open(os.path.join(out_dir, "network.har"), "w", encoding="utf-8") as fh:
        json.dump(_har(), fh, indent=2)
    return out_dir


def default_fixture_dir() -> str:
    """The committed demo fixture dir (``pps-pipeline/fixtures/session-demo``)."""
    return os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "fixtures", "session-demo"))


if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else default_fixture_dir()
    print(generate(target))
