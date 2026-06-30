"""fb_os.cli — ``fb-os ingest|cluster|triage|publish-questions|metrics|demo``.

``make demo`` drives the whole closed loop through this CLI on a seeded inbox with
**no network and no paid software**: bundles -> ingest (+leak floor) -> embed ->
cluster (+min-size suppression) -> triage (free ``--mock`` backend) -> publish
``open-questions.json`` -> drop an answer bundle -> re-triage -> the question flips to
``answered``. The loop, closed, in one command.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Optional

from . import cluster as cluster_mod
from . import ingest as ingest_mod
from . import metrics as metrics_mod
from . import questions as Q
from .embed import Embedder
from .store import Store
from .triager import ClaudeHeadlessBackend, MockTriagerBackend, Triager


# --------------------------------------------------------------------------- #
# Pipeline steps (importable; the CLI commands are thin wrappers)              #
# --------------------------------------------------------------------------- #
def do_ingest(store: Store, inbox: str, *, backend: str = "hashing") -> list[dict]:
    return ingest_mod.ingest_inbox(store, inbox, Embedder(backend=backend))


def do_cluster(store: Store, *, min_cluster_size: int = cluster_mod.DEFAULT_MIN_CLUSTER_SIZE,
               sim_threshold: float = cluster_mod.DEFAULT_SIM_THRESHOLD) -> list[dict]:
    # Loop-closing answer artifacts (answers_question_id set) answer a question; they
    # do NOT seed a new theme. Exclude them from clustering so an answer can't spawn a
    # duplicate of the very question it resolves (the flip happens in the triager).
    arts = [a for a in store.artifacts(include_quarantined=False) if not a.get("answers_question_id")]
    clusters = cluster_mod.cluster_artifacts(
        arts, min_cluster_size=min_cluster_size, sim_threshold=sim_threshold)
    store.clear_clusters()
    for c in clusters:
        store.upsert_cluster(c)
        for member in c["members"]:
            store.set_cluster(member, c["cluster_id"])
    return clusters


def _backend(mock: Optional[str], kind: str, *, record: bool = False):
    if kind == "claude":
        return ClaudeHeadlessBackend()
    return MockTriagerBackend(mock, record=record)


def do_triage(store: Store, questions_path: Optional[str], *, mock: Optional[str] = None,
              backend: str = "mock", record: bool = False, publish: bool = True) -> dict:
    prior = Q.load(questions_path) if questions_path else Q.OpenQuestionSet()
    be = _backend(mock, backend, record=record)
    triager = Triager(store, be)
    result = triager.run(prior)
    if isinstance(be, MockTriagerBackend) and record:
        be.save()
    if publish and questions_path:
        Q.publish(result["questions"], questions_path)
    return result


# --------------------------------------------------------------------------- #
# Commands                                                                     #
# --------------------------------------------------------------------------- #
def _cmd_ingest(args) -> int:
    with Store(args.db) as store:
        arts = do_ingest(store, args.inbox, backend=args.backend)
    q = sum(1 for a in arts if a["quarantined"])
    print(f"ingested {len(arts)} bundle(s): {len(arts) - q} stored, {q} quarantined (leak floor)")
    for a in arts:
        if a["quarantined"]:
            print(f"  🚫 {a['artifact_id']}: {a['quarantine_reason']}")
    return 0


def _cmd_cluster(args) -> int:
    with Store(args.db) as store:
        clusters = do_cluster(store, min_cluster_size=args.min_cluster_size,
                              sim_threshold=args.sim_threshold)
    active = [c for c in clusters if not c["suppressed"]]
    print(f"{len(clusters)} cluster(s): {len(active)} active, "
          f"{len(clusters) - len(active)} suppressed (privacy floor)")
    for c in clusters:
        flag = "🔒 suppressed" if c["suppressed"] else f"size={c['size']}"
        print(f"  {c['cluster_id']:32s} [{flag}] {c['label']}")
    return 0


def _cmd_triage(args) -> int:
    with Store(args.db) as store:
        result = do_triage(store, args.questions, mock=args.mock, backend=args.backend,
                           record=args.record)
    qs = result["questions"]
    opens = qs.open_questions()
    print(f"triaged {len(result['triage'])} artifact(s) across {len(result['themes'])} theme(s)")
    print(f"open-questions: {len(qs)} total, {len(opens)} open")
    if args.questions:
        print(f"published → {args.questions}")
    for q in sorted(opens, key=lambda x: -x.priority)[:8]:
        print(f"  {q.id}  p={q.priority:.2f} u={q.uncertainty:.2f}  {q.question[:70]}")
    return 0


def _cmd_publish_questions(args) -> int:
    with Store(args.db) as store:
        qs = Q.OpenQuestionSet.from_dict({
            "schema_version": Q.SCHEMA_VERSION,
            "generated_at": Q.now_iso(),
            "questions": store.load_questions(),
        })
    path = Q.publish(qs, args.out)
    print(f"published {len(qs)} question(s) → {path}")
    return 0


def _cmd_metrics(args) -> int:
    with Store(args.db) as store:
        m = metrics_mod.compute_metrics(store)
        if args.html:
            p = metrics_mod.write_html(store, args.html)
            print(f"dashboard → {p}")
    if args.json or not args.html:
        print(json.dumps(m, indent=2))
    return 0


def _cmd_demo(args) -> int:
    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="fb-os-demo-"))
    workdir.mkdir(parents=True, exist_ok=True)
    inbox = workdir / "inbox"
    db = str(workdir / "feedback-os.db")
    qpath = str(workdir / "open-questions.json")
    html = str(workdir / "dashboard.html")
    replay = str(Path(__file__).resolve().parent.parent / "fixtures" / "triager-replay.json")

    from . import fixtures

    print("═" * 64)
    print(" Feedback OS — closed-loop demo (no network, no paid software)")
    print("═" * 64)
    print(f" workdir: {workdir}")

    # 1. seed a synthetic inbox (never real data)
    ids = fixtures.generate_inbox(inbox)
    print(f"\n[1/6] seeded inbox: {len(ids)} synthetic bundles → {inbox}")

    with Store(db) as store:
        # 2. ingest + leak floor + embed
        arts = do_ingest(store, str(inbox))
        quar = [a for a in arts if a["quarantined"]]
        print(f"[2/6] ingest: {len(arts) - len(quar)} stored, {len(quar)} quarantined "
              f"by the leak-scan floor ({', '.join(a['artifact_id'] for a in quar) or 'none'})")

        # 3. cluster + min-size suppression
        clusters = do_cluster(store)
        active = [c for c in clusters if not c["suppressed"]]
        supp = [c for c in clusters if c["suppressed"]]
        print(f"[3/6] cluster: {len(active)} active themes, {len(supp)} suppressed "
              f"(privacy floor){' → ' + ','.join(c['cluster_id'] for c in supp) if supp else ''}")
        for c in active:
            print(f"        • {c['cluster_id']:30s} size={c['size']}  {c['label']}")

        # 4. triage (free mock backend) → open-questions.json
        result = do_triage(store, qpath, mock=replay, backend="mock")
        opens = result["questions"].open_questions()
        print(f"[4/6] triage: generated {len(opens)} open question(s) → {qpath}")
        top = max(opens, key=lambda q: q.priority) if opens else None
        if top:
            print(f"        top: {top.id}  p={top.priority:.2f}  “{top.question[:64]}”")

        # 5. close the loop: a user answers the top question
        if top:
            fixtures.write_answer_bundle(inbox, top.id)
            do_ingest(store, str(inbox))          # ingest the answer bundle
            do_cluster(store)
            result2 = do_triage(store, qpath, mock=replay, backend="mock")
            answered = [q for q in result2["questions"] if q.status == "answered"]
            print(f"[5/6] loop closed: dropped an answer for {top.id} → "
                  f"{len(answered)} question(s) now answered "
                  f"({', '.join(q.id for q in answered) or 'none'})")

        # 6. metrics + static dashboard
        metrics_mod.write_html(store, html)
        m = metrics_mod.compute_metrics(store)
        print(f"[6/6] metrics: {m['artifacts']['ingested']} ingested, "
              f"{m['questions']['by_status']} questions, dashboard → {html}")

    # validate the published file against the seam schema (the contract)
    Q.validate_question_set(json.loads(Path(qpath).read_text()))
    print("\n✅ published open-questions.json is schema-valid (the seam contract).")
    print(f"   canonical CLI path: {Q.default_publish_path()}")
    print(f"   demo published to : {qpath}")
    print("═" * 64)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fb-os", description="Feedback OS — the org-wide feedback loop")
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("ingest", help="ingest an inbox of stage_review bundles (+leak floor +embed)")
    pi.add_argument("--inbox", required=True)
    pi.add_argument("--db", default="feedback-os.db")
    pi.add_argument("--backend", default="hashing", choices=["hashing", "sbert"])
    pi.set_defaults(func=_cmd_ingest)

    pc = sub.add_parser("cluster", help="cluster stored artifacts (+min-size suppression)")
    pc.add_argument("--db", default="feedback-os.db")
    pc.add_argument("--min-cluster-size", type=int, default=cluster_mod.DEFAULT_MIN_CLUSTER_SIZE)
    pc.add_argument("--sim-threshold", type=float, default=cluster_mod.DEFAULT_SIM_THRESHOLD)
    pc.set_defaults(func=_cmd_cluster)

    pt = sub.add_parser("triage", help="run the triager → generate/merge open-questions.json")
    pt.add_argument("--db", default="feedback-os.db")
    pt.add_argument("--questions", default=None, help="path to publish open-questions.json (atomic)")
    pt.add_argument("--backend", default="mock", choices=["mock", "claude"])
    pt.add_argument("--mock", default=None, help="record/replay file for the mock backend")
    pt.add_argument("--record", action="store_true", help="record fresh mock responses to --mock")
    pt.set_defaults(func=_cmd_triage)

    pp = sub.add_parser("publish-questions", help="re-publish the stored question set to a file (atomic)")
    pp.add_argument("--db", default="feedback-os.db")
    pp.add_argument("--out", default=None, help="default: ~/.config/fb-assist/open-questions.json")
    pp.set_defaults(func=_cmd_publish_questions)

    pm = sub.add_parser("metrics", help="compute metrics; --html writes the static dashboard")
    pm.add_argument("--db", default="feedback-os.db")
    pm.add_argument("--html", default=None)
    pm.add_argument("--json", action="store_true")
    pm.set_defaults(func=_cmd_metrics)

    pd = sub.add_parser("demo", help="run the whole closed loop on a seeded inbox (no network/paid)")
    pd.add_argument("--workdir", default=None, help="default: a fresh temp dir")
    pd.set_defaults(func=_cmd_demo)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
