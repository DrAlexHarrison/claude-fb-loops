"""fb_os.metrics — visibility for the loop (the thin reporting tier).

Computes the metrics the JD cares about — **time-to-triage**, **signal quality**
(effort-weighted), cluster-size distribution, and **question turnover** — and renders
a committed **static-HTML dashboard** (``--html``) so the public demo runs without
standing up Metabase. (Metabase is the internal/"real" path — see
``dashboards/metabase/docker-compose.yml``; it is AGPL and intentionally NOT required
by the core, plan §8.)

``effort_weight`` / ``cluster_priority`` are re-exported from :mod:`fb_os.triager` so
"signal quality" is computed the *same* way the triager weights question priority.
"""

from __future__ import annotations

import html
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .triager import cluster_priority, effort_weight  # re-export: one definition of "signal quality"

__all__ = ["compute_metrics", "render_html", "effort_weight", "cluster_priority", "signal_quality"]


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _hours_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    da, db = _parse_iso(a), _parse_iso(b)
    if da is None or db is None:
        return None
    return (db - da).total_seconds() / 3600.0


def signal_quality(artifacts: list[dict]) -> float:
    """Effort-weighted signal quality across artifacts, in ``[0.3, 1.5]`` (1.0 = neutral)."""
    sigs = [a.get("effort_signal", {}) or {} for a in artifacts]
    return effort_weight(sigs)


def compute_metrics(store) -> dict:
    """Roll the store up into the dashboard metric set."""
    arts = store.artifacts(include_quarantined=True)
    live = [a for a in arts if not a.get("quarantined")]
    quarantined = [a for a in arts if a.get("quarantined")]
    clusters = store.clusters(include_suppressed=True)
    questions = store.load_questions()

    # time-to-triage (created_at -> triaged_at), hours
    ttt = [h for a in live if (h := _hours_between(a.get("created_at"), a.get("triaged_at"))) is not None]
    triaged = [a for a in live if a.get("triaged_at")]

    # question turnover
    by_status = {"open": 0, "answered": 0, "retired": 0}
    for q in questions:
        by_status[q.get("status", "open")] = by_status.get(q.get("status", "open"), 0) + 1

    # per-cluster signal quality + priority (effort-weighted)
    cluster_rows = []
    for c in clusters:
        members = store.cluster_members(c["cluster_id"]) if not c.get("suppressed") else []
        sigs = [m.get("effort_signal", {}) or {} for m in members]
        cluster_rows.append({
            "cluster_id": c["cluster_id"], "label": c.get("label"), "size": c.get("size", 0),
            "suppressed": c.get("suppressed", False),
            "signal_quality": round(effort_weight(sigs), 4) if sigs else None,
            "priority": cluster_priority(c.get("size", 0), sigs) if sigs else None,
            "summary": c.get("summary", ""),
        })

    sizes = [c.get("size", 0) for c in clusters]
    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifacts": {
            "total": len(arts), "ingested": len(live), "quarantined": len(quarantined),
            "triaged": len(triaged),
            "triaged_pct": round(100 * len(triaged) / len(live), 1) if live else 0.0,
        },
        "time_to_triage_hours": {
            "count": len(ttt),
            "mean": round(statistics.fmean(ttt), 3) if ttt else None,
            "median": round(statistics.median(ttt), 3) if ttt else None,
            "max": round(max(ttt), 3) if ttt else None,
        },
        "signal_quality": {
            "overall": round(signal_quality(live), 4) if live else None,
            "by_cluster": cluster_rows,
        },
        "clusters": {
            "total": len(clusters),
            "suppressed": sum(1 for c in clusters if c.get("suppressed")),
            "sizes": sizes,
            "mean_size": round(statistics.fmean(sizes), 2) if sizes else 0.0,
        },
        "questions": {
            "total": len(questions),
            "by_status": by_status,
        },
    }


# --------------------------------------------------------------------------- #
# Static-HTML dashboard (no Metabase, no network)                              #
# --------------------------------------------------------------------------- #
def _bar(value: float, vmax: float, width: int = 24) -> str:
    if vmax <= 0:
        return ""
    n = int(round(width * value / vmax))
    return "█" * n + "░" * (width - n)


def render_html(metrics: dict) -> str:
    a = metrics["artifacts"]
    ttt = metrics["time_to_triage_hours"]
    q = metrics["questions"]
    cl = metrics["clusters"]
    rows = metrics["signal_quality"]["by_cluster"]
    e = html.escape

    cluster_html = []
    for r in sorted(rows, key=lambda x: (x.get("suppressed"), -(x.get("size") or 0))):
        sq = r["signal_quality"]
        pr = r["priority"]
        badge = "suppressed" if r["suppressed"] else "active"
        cls = "supp" if r["suppressed"] else "act"
        cluster_html.append(
            f"<tr class='{cls}'><td><code>{e(r['cluster_id'])}</code></td>"
            f"<td>{e(str(r['label']))}</td><td class='num'>{r['size']}</td>"
            f"<td class='num'>{'' if sq is None else f'{sq:.2f}'}</td>"
            f"<td class='num'>{'' if pr is None else f'{pr:.2f}'}</td>"
            f"<td><span class='badge {cls}'>{badge}</span></td>"
            f"<td>{e(str(r['summary']))}</td></tr>"
        )

    qs = q["by_status"]
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Feedback OS — dashboard</title>
<style>
  :root {{ --ink:#1a1a2e; --mut:#6b7280; --line:#e5e7eb; --acc:#c15f3c; --ok:#2f855a; --supp:#9ca3af; }}
  body {{ font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; color:var(--ink); margin:0; background:#faf9f7; }}
  header {{ padding:24px 32px; border-bottom:1px solid var(--line); background:#fff; }}
  h1 {{ margin:0; font-size:20px; }} .sub {{ color:var(--mut); margin-top:4px; }}
  main {{ padding:24px 32px; max-width:1100px; margin:0 auto; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:14px; margin-bottom:28px; }}
  .card {{ background:#fff; border:1px solid var(--line); border-radius:10px; padding:16px; }}
  .card .v {{ font-size:26px; font-weight:650; }} .card .l {{ color:var(--mut); font-size:12px; text-transform:uppercase; letter-spacing:.04em; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
  th,td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ background:#f3f4f6; font-size:12px; text-transform:uppercase; letter-spacing:.03em; color:var(--mut); }}
  td.num {{ text-align:right; font-variant-numeric:tabular-nums; }}
  tr.supp td {{ color:var(--supp); }} code {{ font-size:12px; }}
  .badge {{ font-size:11px; padding:2px 7px; border-radius:99px; }}
  .badge.act {{ background:#e8f3ee; color:var(--ok); }} .badge.supp {{ background:#f1f1f1; color:var(--supp); }}
  h2 {{ font-size:15px; margin:26px 0 10px; }}
  .turn span {{ display:inline-block; margin-right:18px; }} .dot {{ display:inline-block; width:9px; height:9px; border-radius:99px; margin-right:5px; }}
</style></head>
<body>
<header><h1>Feedback OS — the org-wide loop</h1>
  <div class="sub">distilled artifacts → clustered themes → auto-generated open questions · generated {e(metrics['generated_at'])} · no network, no paid software</div>
</header>
<main>
  <div class="cards">
    <div class="card"><div class="l">Artifacts ingested</div><div class="v">{a['ingested']}</div></div>
    <div class="card"><div class="l">Quarantined (leak floor)</div><div class="v">{a['quarantined']}</div></div>
    <div class="card"><div class="l">Triaged</div><div class="v">{a['triaged']} <span style="font-size:14px;color:var(--mut)">({a['triaged_pct']}%)</span></div></div>
    <div class="card"><div class="l">Median time-to-triage</div><div class="v">{'—' if ttt['median'] is None else f"{ttt['median']}h"}</div></div>
    <div class="card"><div class="l">Open questions</div><div class="v">{qs.get('open',0)}</div></div>
    <div class="card"><div class="l">Signal quality (overall)</div><div class="v">{'—' if metrics['signal_quality']['overall'] is None else metrics['signal_quality']['overall']}</div></div>
  </div>

  <h2>Question turnover</h2>
  <div class="turn">
    <span><span class="dot" style="background:#2f855a"></span>open: <b>{qs.get('open',0)}</b></span>
    <span><span class="dot" style="background:#3182ce"></span>answered: <b>{qs.get('answered',0)}</b></span>
    <span><span class="dot" style="background:#9ca3af"></span>retired: <b>{qs.get('retired',0)}</b></span>
  </div>

  <h2>Clusters &amp; signal quality ({cl['total']} total, {cl['suppressed']} suppressed by the privacy floor)</h2>
  <table>
    <thead><tr><th>cluster</th><th>label</th><th>size</th><th>signal&nbsp;qual</th><th>priority</th><th>state</th><th>theme summary</th></tr></thead>
    <tbody>
      {''.join(cluster_html) if cluster_html else '<tr><td colspan="7" style="color:var(--mut)">No clusters yet.</td></tr>'}
    </tbody>
  </table>
  <p style="color:var(--mut);font-size:12px;margin-top:20px">Suppressed clusters fall below the min-cluster-size privacy floor (the Clio 39%-reID defence) and are never surfaced to the triager or shown as quotes.</p>
</main></body></html>"""


def write_html(store, out_path) -> str:
    m = compute_metrics(store)
    p = Path(out_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_html(m), encoding="utf-8")
    return str(p)
