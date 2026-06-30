"""fb_os.triager — THE keystone: the producer of ``open-questions.json``.

Reads each (non-suppressed) cluster's representative artifacts plus the running
open-questions and emits:

  (a) a **per-artifact triage record** (category / route / priority / dup_of),
  (b) a per-cluster **theme summary**, and
  (c) a candidate **open-question** that, merged into the prior set with stable ids,
      becomes the living ``open-questions.json`` the CLI consumes.

The LLM backend is **pluggable**:

  * :class:`MockTriagerBackend` — **record/replay** from a small canned-response file
    (keyed by a stable cluster *signature*), with a deterministic heuristic fallback
    so the loop closes on freshly-generated clusters. **Free + deterministic** — this
    is what tests and ``make demo`` use. No network, no paid software.
  * :class:`ClaudeHeadlessBackend` — the real producer: headless Claude via Max auth
    (``claude -p``), the same pattern Build 3 uses (**no metered spend**). Documented
    + pluggable; never invoked by tests/demo.

Whatever a backend returns, the triager **validates against the fixed label sets**
(``CATEGORIES`` / ``ROUTES``) and coerces anything out-of-set — the "never invent
labels" guarantee holds regardless of backend behaviour.

Effort-weighting (the JD's "signal quality" made mechanical) lives here too:
:func:`cluster_priority` raises a cluster's question-priority when its members carry
high ``quality`` / ``alignment_confidence`` and a ``reputation_token``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence

from . import questions as Q
from .embed import tokenize
from .questions import OpenQuestion, OpenQuestionSet, QuestionMatch

PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "triager.md"

# Fixed label sets — the contract the triager enforces (never-invent-labels).
CATEGORIES = ("bug", "feature_request", "ux_friction", "performance",
              "docs_gap", "praise", "question", "other")
ROUTES = ("product", "research", "engineering", "design", "docs", "growth", "none")

_CATEGORY_TO_ROUTE = {
    "bug": "engineering", "feature_request": "product", "ux_friction": "design",
    "performance": "engineering", "docs_gap": "docs", "praise": "growth",
    "question": "research", "other": "none",
}

# Heuristic keyword -> category cues for the deterministic fallback backend.
_CATEGORY_CUES = (
    ("bug", ("bug", "error", "crash", "broken", "fail", "wrong", "regression", "exception")),
    ("performance", ("slow", "lag", "perf", "latency", "freeze", "hang", "timeout", "memory")),
    ("docs_gap", ("doc", "docs", "unclear", "confus", "example", "tutorial", "explain")),
    ("praise", ("love", "great", "awesome", "thanks", "delight", "amazing", "perfect")),
    ("question", ("how", "why", "what", "unsure", "wondering")),
    ("feature_request", ("wish", "want", "add", "support", "feature", "able", "could", "request", "allow")),
)


# --------------------------------------------------------------------------- #
# Effort-weighting (signal quality) — shared by triager + metrics              #
# --------------------------------------------------------------------------- #
def _num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def effort_weight(effort_signals: Sequence[dict]) -> float:
    """Map a set of effort-signals to a multiplier in ``[0.3, 1.5]``.

    High ``quality`` / ``alignment_confidence`` (rated ~1..5) and the presence of a
    ``reputation_token`` (a careful, trusted filterer) push the weight up; their
    absence pushes it toward the floor. This is the JD's "signal quality" metric
    made mechanical (plan §4.3)."""
    if not effort_signals:
        return 1.0
    q_vals, a_vals, rep = [], [], 0
    for s in effort_signals:
        q = _num(s.get("quality"))
        a = _num(s.get("alignment_confidence"))
        if q is not None:
            q_vals.append(min(1.0, q / 5.0))
        if a is not None:
            a_vals.append(min(1.0, a / 5.0))
        if s.get("reputation_token"):
            rep += 1
    parts = []
    if q_vals:
        parts.append(sum(q_vals) / len(q_vals))
    if a_vals:
        parts.append(sum(a_vals) / len(a_vals))
    base = (sum(parts) / len(parts)) if parts else 0.5
    rep_boost = 0.2 * (rep / len(effort_signals))
    weight = 0.5 + base + rep_boost          # base in [0,1] -> weight in [0.5, 1.7]
    return max(0.3, min(1.5, round(weight, 4)))


def cluster_priority(evidence_count: int, effort_signals: Sequence[dict]) -> float:
    """Base priority (driven by how many distinct artifacts back the theme) times the
    effort weight, clamped to ``[0,1]``. Equal-size clusters are separated by signal
    quality — the whole point of the effort signal."""
    base = min(1.0, 0.25 + evidence_count / 8.0)
    return round(max(0.0, min(1.0, base * effort_weight(effort_signals))), 4)


# --------------------------------------------------------------------------- #
# Cluster signature (stable replay key)                                        #
# --------------------------------------------------------------------------- #
def cluster_signature(cluster: dict) -> str:
    """A stable, content-based key for record/replay: the cluster's top terms,
    sorted. Independent of the generated ``cluster_id`` so a replay file survives
    re-clustering as long as the topic's vocabulary is stable."""
    kws = [k.lower() for k in (cluster.get("keywords") or [])][:4]
    return "|".join(sorted(kws))


# --------------------------------------------------------------------------- #
# Backends                                                                     #
# --------------------------------------------------------------------------- #
class TriagerBackend:
    """Interface: given a cluster + its member artifacts + the running questions,
    return a raw decision dict (validated by the orchestrator)."""

    def triage_cluster(self, cluster: dict, members: list[dict],
                       open_questions: OpenQuestionSet) -> dict:
        raise NotImplementedError


def _heuristic_decision(cluster: dict, members: list[dict]) -> dict:
    """Deterministic, model-free triage — the fallback that lets the loop close on
    freshly-generated clusters without a recorded response. Rule-based off the
    cluster's c-TF-IDF keywords (no RNG, fully reproducible)."""
    kws = [k.lower() for k in (cluster.get("keywords") or [])]
    blob = " ".join(kws) + " " + " ".join(m.get("description", "") for m in members).lower()
    category = "ux_friction"
    for cat, cues in _CATEGORY_CUES:
        if any(cue in blob for cue in cues):
            category = cat
            break
    route = _CATEGORY_TO_ROUTE.get(category, "none")
    signals = [m.get("effort_signal", {}) or {} for m in members]
    prio = cluster_priority(cluster.get("size", len(members)), signals)
    topic = ", ".join(k.replace("_", " ") for k in kws[:3]) or "this area"
    size = cluster.get("size", len(members))
    uncertainty = round(max(0.25, min(0.9, 1.0 - size / 12.0)), 4)
    question = {
        "question": f"You mentioned {topic} — is that the part you'd most want improved next?",
        "hypothesis": f"There is a shared, actionable concern around {topic}.",
        "keywords": kws[:8],
        "surfaces": sorted({m.get("surface", "cli") for m in members}) or ["cli"],
        "uncertainty": uncertainty,
    }
    return {
        "theme": {"summary": f"Shared concern: {topic} ({size} artifact(s))."},
        "artifact_triage": {"category": category, "route": route, "priority": prio},
        "per_artifact": {},
        "question": question,
    }


class MockTriagerBackend(TriagerBackend):
    """Free, deterministic record/replay backend (the ``--mock`` path).

    Loads a canned-response file keyed by :func:`cluster_signature`. On a hit it
    **replays** the recorded decision (this is what the golden test asserts on). On a
    miss it synthesizes a deterministic heuristic decision and — if ``record=True`` —
    appends it to the in-memory map so :meth:`save` can persist a fresh recording.
    """

    def __init__(self, replay_path: Optional[os.PathLike | str] = None, *, record: bool = False):
        self.replay_path = Path(replay_path) if replay_path else None
        self.record = record
        self.by_signature: dict = {}
        if self.replay_path and self.replay_path.exists():
            data = json.loads(self.replay_path.read_text(encoding="utf-8"))
            self.by_signature = data.get("by_signature", {})

    def triage_cluster(self, cluster, members, open_questions) -> dict:
        sig = cluster_signature(cluster)
        entry = self.by_signature.get(sig)
        if entry is None:
            entry = _heuristic_decision(cluster, members)
            if self.record:
                self.by_signature[sig] = entry
        return entry

    def save(self) -> None:
        if not self.replay_path:
            return
        self.replay_path.parent.mkdir(parents=True, exist_ok=True)
        self.replay_path.write_text(
            json.dumps({"by_signature": self.by_signature}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


class ClaudeHeadlessBackend(TriagerBackend):
    """Real producer: headless Claude (``claude -p``) under Max auth — no metered
    spend, same pattern Build 3 uses. **Documented + pluggable; never used by tests
    or ``make demo``** (those must be free + deterministic). Wire it via
    ``--backend claude`` in production."""

    def __init__(self, model: str = "claude-opus-4-8", timeout: int = 120):
        self.model = model
        self.timeout = timeout
        self.prompt = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else ""

    def triage_cluster(self, cluster, members, open_questions) -> dict:
        exe = shutil.which("claude")
        if not exe:
            raise RuntimeError("`claude` CLI not found; use --mock for a free, deterministic run.")
        payload = {
            "cluster": {k: cluster.get(k) for k in ("cluster_id", "label", "keywords", "size")},
            "artifacts": [{"artifact_id": m["artifact_id"], "surface": m.get("surface"),
                           "description": m.get("description", ""),
                           "effort_signal": m.get("effort_signal", {})} for m in members],
            "open_questions": [q.to_dict() for q in open_questions.open_questions()],
        }
        full = (f"{self.prompt}\n\n# Input\n```json\n"
                f"{json.dumps(payload, ensure_ascii=False)}\n```\n"
                "Respond with ONLY the strict-JSON output object.")
        res = subprocess.run([exe, "-p", full, "--model", self.model],
                             capture_output=True, text=True, timeout=self.timeout)
        text = res.stdout.strip()
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end < 0:
            raise ValueError(f"triager backend returned no JSON object: {text[:200]!r}")
        return json.loads(text[start:end + 1])


# --------------------------------------------------------------------------- #
# Validation (never-invent-labels)                                             #
# --------------------------------------------------------------------------- #
def _coerce_category(v) -> str:
    return v if v in CATEGORIES else "other"


def _coerce_route(v) -> str:
    return v if v in ROUTES else "none"


def _coerce_priority(v) -> float:
    n = _num(v, 0.5)
    return round(max(0.0, min(1.0, n)), 4)


# --------------------------------------------------------------------------- #
# The orchestrator                                                             #
# --------------------------------------------------------------------------- #
class Triager:
    """Runs the keystone pass over a populated store and produces the merged
    ``OpenQuestionSet`` (+ triage records + theme summaries)."""

    def __init__(self, store, backend: Optional[TriagerBackend] = None, *,
                 generator: str = "fb-os-triager/0.1 (mock, headless/Max)",
                 min_evidence: int = 1, expiry_days: int = 30):
        self.store = store
        self.backend = backend or MockTriagerBackend()
        self.generator = generator
        self.min_evidence = min_evidence
        self.expiry_days = expiry_days

    def _window(self, now: datetime) -> dict:
        arts = self.store.artifacts(include_quarantined=False)
        dates = sorted(a["created_at"] for a in arts if a.get("created_at"))
        return {"artifacts": len(arts),
                "since": dates[0] if dates else now_iso(now),
                "until": dates[-1] if dates else now_iso(now)}

    def _build_question(self, cluster: dict, members: list[dict], q: dict,
                        now: datetime) -> OpenQuestion:
        signals = [m.get("effort_signal", {}) or {} for m in members]
        member_ids = sorted(m["artifact_id"] for m in members)
        surfaces = q.get("surfaces") or sorted({m.get("surface", "cli") for m in members}) or ["cli"]
        keywords = q.get("keywords") or cluster.get("keywords", [])
        priority = cluster_priority(cluster.get("size", len(members)), signals)
        expires = now + timedelta(days=self.expiry_days)
        return OpenQuestion(
            id="",  # assigned at merge (stable, ISO-week sequenced)
            question=q.get("question", "").strip(),
            hypothesis=q.get("hypothesis", "").strip(),
            cluster_id=cluster["cluster_id"],
            cluster_label=cluster.get("label", ""),
            match=QuestionMatch(keywords=list(keywords)[:8], surfaces=list(surfaces)),
            priority=priority,
            uncertainty=_coerce_priority(q.get("uncertainty", 0.5)),
            evidence_count=cluster.get("size", len(members)),
            status="open",
            created_at=now_iso(now),
            expires_at=now_iso(expires),
            provenance={"artifact_ids": member_ids},
        )

    def run(self, prior: Optional[OpenQuestionSet] = None, *,
            now: Optional[datetime] = None) -> dict:
        now = now or datetime.now(timezone.utc)
        prior = prior if prior is not None else OpenQuestionSet()
        clusters = self.store.clusters(include_suppressed=False)
        candidates: list[OpenQuestion] = []
        triage_records: list[dict] = []
        themes: list[dict] = []

        for cluster in clusters:
            members = self.store.cluster_members(cluster["cluster_id"])
            if not members:
                continue
            raw = self.backend.triage_cluster(cluster, members, prior)
            default = raw.get("artifact_triage", {}) or {}
            per_artifact = raw.get("per_artifact", {}) or {}
            dup_map = cluster.get("dup_map", {}) or {}

            # theme
            summary = (raw.get("theme", {}) or {}).get("summary", "")
            self.store.upsert_cluster({**cluster, "summary": summary})
            themes.append({"cluster_id": cluster["cluster_id"], "label": cluster.get("label"),
                           "summary": summary})

            # per-artifact triage (validated against the fixed label sets)
            for m in members:
                aid = m["artifact_id"]
                ov = per_artifact.get(aid, {})
                category = _coerce_category(ov.get("category", default.get("category")))
                route = _coerce_route(ov.get("route", default.get("route")))
                priority = _coerce_priority(ov.get("priority", default.get("priority", 0.5)))
                canonical = dup_map.get(aid)
                dup_of = ov.get("dup_of", canonical if canonical and canonical != aid else None)
                rec = {
                    "artifact_id": aid, "category": category, "route": route,
                    "priority": priority, "dup_of": dup_of,
                    "answers_question_id": m.get("answers_question_id"),
                    "cluster_id": cluster["cluster_id"], "triaged_at": now_iso(now),
                }
                self.store.upsert_triage(rec)
                self.store.mark_triaged(aid, now_iso(now))
                triage_records.append(rec)

            # candidate question
            qspec = raw.get("question")
            if qspec and cluster.get("size", len(members)) >= self.min_evidence:
                candidates.append(self._build_question(cluster, members, qspec, now))

        merged = Q.merge(prior, candidates, now=now, generator=self.generator,
                         source_window=self._window(now))
        self._apply_answers(merged, triage_records)
        self.store.save_questions([q.to_dict() for q in merged.questions])
        return {"questions": merged, "triage": triage_records, "themes": themes}

    def _apply_answers(self, qset: OpenQuestionSet, triage_records: list[dict]) -> None:
        """Close the loop: any non-quarantined artifact that carries
        ``answers_question_id`` flips that question to ``answered`` and lowers its
        uncertainty (plan §3, the LOOP CLOSED step)."""
        for a in self.store.artifacts(include_quarantined=False):
            qid = a.get("answers_question_id")
            if qid:
                Q.mark_answered(qset, qid)


def now_iso(dt: datetime) -> str:
    return Q.now_iso(dt)
