#!/usr/bin/env python3
"""FIX-5 live verification — `response._request_id` is the verifiable anchor.

The whole `claude_repro` anchor story rests on a single empirical fact that a
type-check CANNOT confirm: `_request_id` is a *per-response* attribute the SDK
attaches from the HTTP `request-id` header — it is **absent from
`Message.model_fields`** on `anthropic 0.76.0` (verified: model_fields ==
['id','content','model','role','stop_reason','stop_sequence','type','usage']).

So we verify it the only honest way: make ONE minimal real call and read the
attribute off the live object. This script also probes the **streaming** path
(where `get_final_message()` returns an accumulated snapshot that may NOT carry
`_request_id` — the reliable source there is `stream.request_id`, the header).

Run:
    python scripts/verify_request_id.py            # uses ANTHROPIC_API_KEY or ~/.config/anthropic/api_key
    ANTHROPIC_API_KEY=sk-ant-... python scripts/verify_request_id.py

Cost: two `max_tokens=8` calls (~$0.002 total). Set FB_REPRO_VERIFY_STREAM=0 to
skip the streaming probe and spend half that.

Exit 0 == every assertion held; non-zero == the anchor assumption is broken.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("USE_TF", "0")

MODEL = os.environ.get("FB_REPRO_VERIFY_MODEL", "claude-3-5-haiku-latest")


def _load_key() -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key.strip()
    f = Path(os.path.expanduser("~/.config/anthropic/api_key"))
    if f.exists():
        return f.read_text().strip() or None
    return None


def main() -> int:
    import anthropic

    key = _load_key()
    if not key:
        print("NO API KEY available (env ANTHROPIC_API_KEY or ~/.config/anthropic/api_key).")
        print("Cannot live-verify; the unit tests cover the extraction logic against mocks.")
        return 2

    evidence: dict = {
        "anthropic_version": anthropic.__version__,
        "model": MODEL,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message_model_fields": list(anthropic.types.Message.model_fields.keys()),
        "request_id_in_model_fields": "_request_id" in anthropic.types.Message.model_fields,
    }

    client = anthropic.Anthropic(api_key=key)

    # ---- 1) messages.create ------------------------------------------------
    try:
        resp = client.messages.create(
            model=MODEL, max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
    except anthropic.APIStatusError as err:
        # Key present but the account can't be billed (e.g. zero credit balance).
        # The error STILL carries a request-id header — zero-cost partial evidence
        # that the anchor header exists and is `req_…`-shaped on real calls.
        err_rid = getattr(err, "request_id", None)
        evidence["create_error"] = {
            "status_code": getattr(err, "status_code", None),
            "error_request_id": err_rid,
            "error_request_id_starts_with_req_": bool(err_rid and err_rid.startswith("req_")),
            "message": str(err)[:300],
        }
        out = (Path(__file__).resolve().parent.parent / "verification-evidence"
               / "request-id-live.json")
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(evidence, indent=2))
        print(f"[create]  BILLED CALL BLOCKED ({getattr(err, 'status_code', '?')}): {str(err)[:120]}")
        print(f"[create]  error-path request-id = {err_rid}  "
              f"(header present + req_-shaped: {bool(err_rid and err_rid.startswith('req_'))})")
        print(f"\nEvidence written -> {out}")
        print("RESULT: could NOT complete a billed call (no credits). The request-id "
              "HEADER is confirmed present + req_-shaped even on the error path; the "
              "success-path assertion is covered by the unit tests against mocks. "
              "Re-run this script once the account has credits for full live proof.")
        return 3
    rid_create = getattr(resp, "_request_id", None)
    evidence["create"] = {
        "_request_id": rid_create,
        "starts_with_req_": bool(rid_create and rid_create.startswith("req_")),
        "message_id": resp.id,
        "usage": resp.usage.model_dump() if resp.usage else None,
    }
    assert rid_create and rid_create.startswith("req_"), \
        f"create()._request_id not a req_… string: {rid_create!r}"
    print(f"[create]  _request_id = {rid_create}  ✅")

    # ---- 2) messages.stream -> get_final_message ---------------------------
    if os.environ.get("FB_REPRO_VERIFY_STREAM", "1") != "0":
        with client.messages.stream(
            model=MODEL, max_tokens=8,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        ) as stream:
            final = stream.get_final_message()
            rid_stream_hdr = getattr(stream, "request_id", None)
            rid_final_attr = getattr(final, "_request_id", None)
        evidence["stream"] = {
            "stream_request_id_header": rid_stream_hdr,
            "final_message_request_id_attr": rid_final_attr,
            "header_starts_with_req_": bool(rid_stream_hdr and rid_stream_hdr.startswith("req_")),
            "note": ("get_final_message() snapshot may lack _request_id; "
                     "stream.request_id (the response header) is the reliable source"),
        }
        # The reliable streaming anchor is the header; assert THAT.
        assert rid_stream_hdr and rid_stream_hdr.startswith("req_"), \
            f"stream.request_id not a req_… string: {rid_stream_hdr!r}"
        print(f"[stream]  stream.request_id = {rid_stream_hdr}  ✅  "
              f"(final._request_id = {rid_final_attr!r})")

    # ---- 3) messages.parse -------------------------------------------------
    evidence["parse"] = {
        "available": hasattr(client.messages, "parse"),
        "note": ("messages.parse does NOT exist on anthropic 0.76.0; when present "
                 "it routes through the same add_request_id() path as create, so "
                 "getattr(resp, '_request_id', None) covers it. Extractor is robust "
                 "to its absence."),
    }
    print(f"[parse]   available = {evidence['parse']['available']}  "
          "(extractor is robust to absence)")

    out = Path(__file__).resolve().parent.parent / "verification-evidence" / "request-id-live.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2))
    print(f"\nEvidence written -> {out}")
    print("RESULT: anchor verified ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
