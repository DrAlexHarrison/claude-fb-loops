"""pps capture edge — mitmproxy addon that exports the session's network as HAR.

THE THIN EDGE. Run as:  mitmdump -s mitm_har.py --set bundle_dir=<dir>
Captured flows are written to ``<bundle_dir>/network.har`` with absolute
``startedDateTime`` timestamps; the pipeline normalizes them to the bundle t0.

This is a reference addon — smoke-noted, not unit-pinned. Swap for any tool that
emits a HAR (browser devtools export, Charles, etc.).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

try:  # mitmproxy is an edge dep; importing this module must not hard-require it.
    from mitmproxy import ctx  # type: ignore
except Exception:  # pragma: no cover
    ctx = None


class HarExporter:  # pragma: no cover - exercised only under a live mitmdump
    def __init__(self) -> None:
        self.entries: list[dict] = []
        self.bundle_dir = "."

    def load(self, loader) -> None:
        loader.add_option("bundle_dir", str, ".", "bundle output dir")

    def configure(self, updated) -> None:
        if ctx is not None:
            self.bundle_dir = ctx.options.bundle_dir

    def response(self, flow) -> None:
        req, resp = flow.request, flow.response
        self.entries.append({
            "startedDateTime": datetime.now(timezone.utc).isoformat(),
            "time": 0,
            "request": {
                "method": req.method, "url": req.pretty_url,
                "headers": [{"name": k, "value": v} for k, v in req.headers.items()],
            },
            "response": {
                "status": resp.status_code,
                "headers": [{"name": k, "value": v} for k, v in resp.headers.items()],
                "content": {"mimeType": resp.headers.get("content-type", ""),
                            "text": resp.get_text(strict=False) or ""},
            },
        })
        self._flush()

    def _flush(self) -> None:
        os.makedirs(self.bundle_dir, exist_ok=True)
        har = {"log": {"version": "1.2",
                       "creator": {"name": "pps-capture", "version": "0.1.0"},
                       "entries": self.entries}}
        with open(os.path.join(self.bundle_dir, "network.har"), "w") as fh:
            json.dump(har, fh, indent=2)


addons = [HarExporter()] if ctx is not None else []
