"""fb_os.questions — THE seam between Build 1 (Feedback OS) and Build 3 (the CLI).

This module owns ``open-questions.json``: the living, org-authored list of "things
Anthropic currently wants to learn from its users." Build 1's triager produces it;
Build 3's ``/fb`` co-author consumes it and asks **at most one** maximally-relevant
probe per feedback session (co-author.md §44-46, cli-runtime-plan OPEN-3).

Why this file is the keystone
-----------------------------
Both ends of the loop must agree, byte-for-byte, on:

  1. the **schema** of the published file (``schema/open-questions.schema.json``), and
  2. the **selection rule** — which single question to surface for a given report.

If the producer and consumer drifted on either, the loop would silently break. So
the selector lives **here**, in the package both ends import (:func:`rank_for`), and
the published file is validated against the committed JSON Schema on every publish.

Publication is **atomic** — it reuses :func:`fb_assist.package._atomic_write` (the
same tmp+fsync+os.replace primitive Build 3 trusts for transcript swaps) so a
running ``/fb`` never reads a half-written file (plan §8, "publish path collision").

The published path is ``~/.config/fb-assist/open-questions.json`` — the exact path
the CLI reads (overridable via ``$FB_ASSIST_OPEN_QUESTIONS`` for tests/demo).

Pure stdlib + ``fb_assist`` + (optional) ``jsonschema``. No network. No paid software.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence, Union

# Reuse Build 3's proven atomic writer — do NOT reimplement (plan §8).
_FB_ASSIST_DIR = Path(__file__).resolve().parent.parent.parent / "fb-assist"
if _FB_ASSIST_DIR.is_dir() and str(_FB_ASSIST_DIR) not in sys.path:
    sys.path.insert(0, str(_FB_ASSIST_DIR))
from fb_assist.package import _atomic_write  # noqa: E402

PathLike = Union[str, os.PathLike]

SCHEMA_VERSION = "1.0"
SCHEMA_DIR = Path(__file__).resolve().parent / "schema"
OPEN_QUESTIONS_SCHEMA = SCHEMA_DIR / "open-questions.schema.json"
ARTIFACT_SCHEMA = SCHEMA_DIR / "artifact.schema.json"

VALID_STATUS = ("open", "answered", "retired")

# The default surface the CLI runs on (cli-runtime-plan). rank_for filters to it.
DEFAULT_SURFACE = "cli"

# Selection floor: a candidate must clear this *score* (priority x relevance) to be
# surfaced. 0.0 => at least one keyword must overlap and priority must be > 0. The
# CLI imports rank_for so this floor is shared, never re-decided on the edge.
DEFAULT_SCORE_FLOOR = 0.0


# --------------------------------------------------------------------------- #
# Published-path resolution (the exact file Build 3 reads)                     #
# --------------------------------------------------------------------------- #
def default_publish_path() -> Path:
    """``~/.config/fb-assist/open-questions.json`` — the path the CLI reads.

    Overridable via ``$FB_ASSIST_OPEN_QUESTIONS`` (tests + ``make demo`` set it so
    they never clobber a real user's hand-seeded stub)."""
    env = os.environ.get("FB_ASSIST_OPEN_QUESTIONS")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".config" / "fb-assist" / "open-questions.json"


# --------------------------------------------------------------------------- #
# Time helpers                                                                 #
# --------------------------------------------------------------------------- #
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_iso(dt: Optional[datetime] = None) -> str:
    """ISO-8601 in Zulu form (``...Z``), matching the bundle/effort-signal style."""
    dt = dt or _utcnow()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def week_tag(dt: Optional[datetime] = None) -> str:
    """ISO-week tag like ``2026w26`` — the stable-id epoch (survives regeneration)."""
    dt = dt or _utcnow()
    iso = dt.isocalendar()
    return f"{iso[0]}w{iso[1]:02d}"


def next_question_id(existing_ids: Iterable[str], *, when: Optional[datetime] = None) -> str:
    """Mint a stable id ``oq_<isoweek>_<NN>`` not colliding with ``existing_ids``.

    Stable across regenerations: the week tag is fixed by ``when`` and the sequence
    only ever counts up within a week, so an id, once minted, is never reused."""
    tag = week_tag(when)
    prefix = f"oq_{tag}_"
    seq = 0
    for qid in existing_ids:
        if qid.startswith(prefix):
            try:
                seq = max(seq, int(qid[len(prefix):]))
            except ValueError:
                continue
    return f"{prefix}{seq + 1:02d}"


# --------------------------------------------------------------------------- #
# Models                                                                       #
# --------------------------------------------------------------------------- #
@dataclasses.dataclass
class QuestionMatch:
    """How the edge decides relevance — cheap, local, no model needed (plan §4.2)."""
    keywords: list[str] = dataclasses.field(default_factory=list)
    surfaces: list[str] = dataclasses.field(default_factory=lambda: [DEFAULT_SURFACE])
    embedding_ref: Optional[str] = None  # v-next: vector key for semantic match

    def to_dict(self) -> dict:
        d = {"keywords": list(self.keywords), "surfaces": list(self.surfaces)}
        if self.embedding_ref is not None:
            d["embedding_ref"] = self.embedding_ref
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "QuestionMatch":
        return cls(
            keywords=list(d.get("keywords", []) or []),
            surfaces=list(d.get("surfaces", [DEFAULT_SURFACE]) or [DEFAULT_SURFACE]),
            embedding_ref=d.get("embedding_ref"),
        )


@dataclasses.dataclass
class OpenQuestion:
    """One org-authored probe. ``id`` is stable across regenerations."""
    id: str
    question: str
    hypothesis: str = ""
    cluster_id: str = ""
    cluster_label: str = ""
    match: QuestionMatch = dataclasses.field(default_factory=QuestionMatch)
    priority: float = 0.5
    uncertainty: float = 0.5
    evidence_count: int = 0
    status: str = "open"
    created_at: str = dataclasses.field(default_factory=now_iso)
    expires_at: str = ""
    provenance: dict = dataclasses.field(default_factory=lambda: {"artifact_ids": []})

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "question": self.question,
            "hypothesis": self.hypothesis,
            "cluster_id": self.cluster_id,
            "cluster_label": self.cluster_label,
            "match": self.match.to_dict(),
            "priority": round(float(self.priority), 4),
            "uncertainty": round(float(self.uncertainty), 4),
            "evidence_count": int(self.evidence_count),
            "status": self.status,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "provenance": self.provenance,
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OpenQuestion":
        return cls(
            id=d["id"],
            question=d.get("question", ""),
            hypothesis=d.get("hypothesis", ""),
            cluster_id=d.get("cluster_id", ""),
            cluster_label=d.get("cluster_label", ""),
            match=QuestionMatch.from_dict(d.get("match", {}) or {}),
            priority=float(d.get("priority", 0.5)),
            uncertainty=float(d.get("uncertainty", 0.5)),
            evidence_count=int(d.get("evidence_count", 0)),
            status=d.get("status", "open"),
            created_at=d.get("created_at", now_iso()),
            expires_at=d.get("expires_at", ""),
            provenance=dict(d.get("provenance", {"artifact_ids": []}) or {"artifact_ids": []}),
        )

    def is_expired(self, *, now: Optional[datetime] = None) -> bool:
        exp = _parse_iso(self.expires_at)
        if exp is None:
            return False
        return (now or _utcnow()) >= exp


@dataclasses.dataclass
class OpenQuestionSet:
    """The whole published document (``open-questions.json``)."""
    questions: list[OpenQuestion] = dataclasses.field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    generated_at: str = dataclasses.field(default_factory=now_iso)
    generator: str = "fb-os-triager/0.1 (mock, headless/Max)"
    source_window: dict = dataclasses.field(default_factory=dict)

    # -- container conveniences ------------------------------------------------
    def __iter__(self):
        return iter(self.questions)

    def __len__(self) -> int:
        return len(self.questions)

    def by_id(self, qid: str) -> Optional[OpenQuestion]:
        for q in self.questions:
            if q.id == qid:
                return q
        return None

    def open_questions(self, *, now: Optional[datetime] = None) -> list[OpenQuestion]:
        return [q for q in self.questions if q.status == "open" and not q.is_expired(now=now)]

    # -- serialization ---------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generated_at": self.generated_at,
            "generator": self.generator,
            "source_window": self.source_window,
            "questions": [q.to_dict() for q in self.questions],
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "OpenQuestionSet":
        return cls(
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            generated_at=d.get("generated_at", now_iso()),
            generator=d.get("generator", "unknown"),
            source_window=dict(d.get("source_window", {}) or {}),
            questions=[OpenQuestion.from_dict(q) for q in d.get("questions", [])],
        )


# --------------------------------------------------------------------------- #
# Load / persist                                                               #
# --------------------------------------------------------------------------- #
def load(path: Optional[PathLike] = None) -> OpenQuestionSet:
    """Load an ``open-questions.json``. Missing/empty file => an empty set (the
    living list starts empty; the triager fills it). Never raises on absence."""
    p = Path(path).expanduser() if path is not None else default_publish_path()
    if not p.exists():
        return OpenQuestionSet(questions=[])
    raw = p.read_text(encoding="utf-8").strip()
    if not raw:
        return OpenQuestionSet(questions=[])
    return OpenQuestionSet.from_dict(json.loads(raw))


def publish(qset: OpenQuestionSet, path: Optional[PathLike] = None, *, validate: bool = True) -> str:
    """Atomically write ``qset`` to ``path`` (default: the canonical CLI path).

    Reuses ``fb_assist.package._atomic_write`` (tmp+fsync+os.replace) so a reader
    sees the whole old file or the whole new one — never a torn write (plan §8).
    Validates against the committed JSON Schema first unless ``validate=False``."""
    p = Path(path).expanduser() if path is not None else default_publish_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = qset.to_dict()
    if validate:
        validate_question_set(data)  # raises on contract violation BEFORE any write
    payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
    _atomic_write(p, payload)
    return str(p)


# --------------------------------------------------------------------------- #
# Lifecycle: merge / retire / expire (the living-list mechanics)               #
# --------------------------------------------------------------------------- #
def expire(qset: OpenQuestionSet, *, now: Optional[datetime] = None) -> OpenQuestionSet:
    """Mark every past-``expires_at`` open question ``retired`` (in place). Returns
    the same set for chaining. The list is *living*: stale uncertainty auto-retires."""
    now = now or _utcnow()
    for q in qset.questions:
        if q.status == "open" and q.is_expired(now=now):
            q.status = "retired"
    return qset


def retire(qset: OpenQuestionSet, ids: Iterable[str]) -> OpenQuestionSet:
    """Force-retire questions by id (e.g. a human pulled one). In place."""
    idset = set(ids)
    for q in qset.questions:
        if q.id in idset:
            q.status = "retired"
    return qset


def mark_answered(qset: OpenQuestionSet, qid: str, *, uncertainty_drop: float = 0.4) -> bool:
    """Flip a question to ``answered`` and lower its uncertainty (the loop closing:
    a user answered the org's question). Returns True if the id was found+open."""
    q = qset.by_id(qid)
    if q is None:
        return False
    q.status = "answered"
    q.uncertainty = max(0.0, round(q.uncertainty - uncertainty_drop, 4))
    return True


def merge(
    prior: OpenQuestionSet,
    incoming: Sequence[OpenQuestion],
    *,
    now: Optional[datetime] = None,
    generator: Optional[str] = None,
    source_window: Optional[dict] = None,
) -> OpenQuestionSet:
    """Merge freshly-generated ``incoming`` questions into ``prior``, keeping ids
    stable and retiring answered/expired ones (plan §6.5, §7 "Question lifecycle").

    Matching rule (so a regenerated question reuses its id instead of duplicating):
    an incoming question matches a prior one when their ``cluster_id`` is equal (the
    cluster is the durable identity of a theme). On a match we **update in place**
    (evidence_count, priority, uncertainty, label, provenance, expiry) but preserve
    the prior ``id``, ``created_at``, and any ``answered``/``retired`` status. A
    never-before-seen cluster gets a fresh stable id via :func:`next_question_id`.
    """
    now = now or _utcnow()
    result = OpenQuestionSet(
        questions=list(prior.questions),
        generated_at=now_iso(now),
        generator=generator or prior.generator,
        source_window=source_window if source_window is not None else dict(prior.source_window),
    )
    # Index prior questions by their cluster (the durable theme identity).
    by_cluster: dict[str, OpenQuestion] = {}
    for q in result.questions:
        if q.cluster_id:
            by_cluster.setdefault(q.cluster_id, q)

    existing_ids = {q.id for q in result.questions}
    for cand in incoming:
        match = by_cluster.get(cand.cluster_id) if cand.cluster_id else None
        if match is None:
            # Fallback: re-clustering can rename a cluster_id when membership shifts.
            # Match a still-open prior question whose keyword set strongly overlaps
            # (Jaccard >= 0.6), so a theme keeps its stable id instead of duplicating.
            match = _match_by_keywords(result.questions, cand)
        if match is not None:
            # Update the durable record; never resurrect an answered/retired one.
            match.question = cand.question or match.question
            match.hypothesis = cand.hypothesis or match.hypothesis
            match.cluster_label = cand.cluster_label or match.cluster_label
            match.match = cand.match
            match.evidence_count = cand.evidence_count
            match.provenance = cand.provenance
            if match.status == "open":
                match.priority = cand.priority
                match.uncertainty = cand.uncertainty
                if cand.expires_at:
                    match.expires_at = cand.expires_at
        else:
            qid = next_question_id(existing_ids, when=now)
            existing_ids.add(qid)
            new_q = dataclasses.replace(cand, id=qid)
            if not new_q.created_at:
                new_q.created_at = now_iso(now)
            result.questions.append(new_q)
            if new_q.cluster_id:
                by_cluster[new_q.cluster_id] = new_q

    expire(result, now=now)
    return result


def _match_by_keywords(prior: list[OpenQuestion], cand: OpenQuestion,
                       *, jaccard_floor: float = 0.6) -> Optional[OpenQuestion]:
    """Find a still-open prior question whose keyword set overlaps ``cand``'s by at
    least ``jaccard_floor`` (a re-clustering-resilient secondary identity)."""
    cset = {k.lower() for k in cand.match.keywords}
    if not cset:
        return None
    best, best_j = None, jaccard_floor
    for q in prior:
        if q.status != "open":
            continue
        pset = {k.lower() for k in q.match.keywords}
        if not pset:
            continue
        j = len(cset & pset) / len(cset | pset)
        if j >= best_j:
            best, best_j = q, j
    return best


# --------------------------------------------------------------------------- #
# rank_for — THE shared selector (imported by Build 3 so the ends cannot drift) #
# --------------------------------------------------------------------------- #
def _tokenize(text: str) -> set[str]:
    out: set[str] = set()
    token = []
    for ch in (text or "").lower():
        if ch.isalnum() or ch in "/_-.":
            token.append(ch)
        else:
            if token:
                out.add("".join(token))
                token = []
    if token:
        out.add("".join(token))
    return out


def _normalize_report_context(report_context: Any) -> tuple[str, set[str], str]:
    """Accept a raw report string OR a dict ``{text, keywords, surface}``.
    Returns ``(text, token_set, surface)``."""
    if isinstance(report_context, str):
        text = report_context
        keywords: list[str] = []
        surface = DEFAULT_SURFACE
    elif isinstance(report_context, Mapping):
        text = str(report_context.get("text", "") or "")
        keywords = list(report_context.get("keywords", []) or [])
        surface = str(report_context.get("surface", DEFAULT_SURFACE) or DEFAULT_SURFACE)
    else:
        text, keywords, surface = "", [], DEFAULT_SURFACE
    tokens = _tokenize(text)
    for kw in keywords:
        tokens |= _tokenize(kw)
    return text, tokens, surface


def relevance(question: OpenQuestion, report_context: Any) -> float:
    """Cheap, local, model-free relevance in ``[0, 1]``: the fraction of the
    question's match-keywords present in the current report (substring OR token
    overlap). This is the CORE matcher; embedding-cosine is the v-next upgrade
    (``match.embedding_ref`` carries the vector key for it)."""
    text, tokens, _surface = _normalize_report_context(report_context)
    text_l = (text or "").lower()
    kws = question.match.keywords
    if not kws:
        return 0.0
    matched = 0
    for kw in kws:
        kw_l = kw.lower().strip()
        if not kw_l:
            continue
        if kw_l in text_l or (_tokenize(kw_l) & tokens):
            matched += 1
    return matched / len(kws)


def score(question: OpenQuestion, report_context: Any) -> float:
    """``priority x relevance`` — how badly the org wants this signal, weighted by
    how relevant the probe is to what the user is reporting *right now*."""
    return float(question.priority) * relevance(question, report_context)


def candidates(
    qset: OpenQuestionSet,
    *,
    surface: str = DEFAULT_SURFACE,
    now: Optional[datetime] = None,
) -> list[OpenQuestion]:
    """The eligible pool: ``status=="open"`` AND not expired AND ``surface`` applies.
    (A question with no ``surfaces`` declared is treated as applying everywhere.)"""
    out = []
    for q in qset.questions:
        if q.status != "open" or q.is_expired(now=now):
            continue
        surfaces = q.match.surfaces or []
        if surfaces and surface not in surfaces:
            continue
        out.append(q)
    return out


def rank_for(
    report_context: Any,
    qset: Optional[OpenQuestionSet] = None,
    *,
    surface: str = DEFAULT_SURFACE,
    now: Optional[datetime] = None,
    floor: float = DEFAULT_SCORE_FLOOR,
    path: Optional[PathLike] = None,
) -> Optional[OpenQuestion]:
    """Return the single most-relevant open question for ``report_context``, or
    ``None`` if nothing clears the ``floor``. **One. Never a survey** (co-author.md
    §46) — Build 3 imports *this* function so the producer and consumer apply the
    identical rule and cannot drift.

    ``report_context`` is the current feedback report: a string, or a dict
    ``{"text": ..., "keywords": [...], "surface": "cli"}``. ``qset`` defaults to
    loading the published file at ``path`` (default: the canonical CLI path)."""
    if qset is None:
        qset = load(path)
    pool = candidates(qset, surface=surface, now=now)
    best: Optional[OpenQuestion] = None
    best_score = floor
    for q in pool:
        s = score(q, report_context)
        # Strictly greater than the floor; deterministic tie-break by
        # (priority, uncertainty, id) so the choice is stable run-to-run.
        if s > best_score or (
            best is not None
            and s == best_score
            and (q.priority, q.uncertainty, q.id) > (best.priority, best.uncertainty, best.id)
        ):
            if s > floor:
                best, best_score = q, s
    return best


# --------------------------------------------------------------------------- #
# Schema validation (the contract guard — both directions of the seam)         #
# --------------------------------------------------------------------------- #
def _load_schema(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_question_set(data: Union[OpenQuestionSet, Mapping[str, Any]]) -> None:
    """Validate a published ``open-questions.json`` payload against the committed
    JSON Schema. Uses ``jsonschema`` if importable; otherwise a stdlib structural
    floor (so the core never hard-depends on a download). Raises ``ValueError`` on
    a contract violation."""
    if isinstance(data, OpenQuestionSet):
        data = data.to_dict()
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance=data, schema=_load_schema(OPEN_QUESTIONS_SCHEMA))
        return
    except ImportError:
        _structural_validate_question_set(data)
    except Exception as e:  # jsonschema.ValidationError et al.
        raise ValueError(f"open-questions.json failed schema validation: {e}") from e


def _structural_validate_question_set(data: Mapping[str, Any]) -> None:
    if not isinstance(data, Mapping):
        raise ValueError("open-questions payload must be an object")
    for key in ("schema_version", "generated_at", "questions"):
        if key not in data:
            raise ValueError(f"open-questions.json missing required key: {key!r}")
    if not isinstance(data["questions"], list):
        raise ValueError("'questions' must be a list")
    for q in data["questions"]:
        for key in ("id", "question", "priority", "status", "match"):
            if key not in q:
                raise ValueError(f"question missing required key: {key!r}")
        if q["status"] not in VALID_STATUS:
            raise ValueError(f"invalid status {q['status']!r}; valid={VALID_STATUS}")
        if not (0.0 <= float(q["priority"]) <= 1.0):
            raise ValueError(f"priority out of [0,1]: {q['priority']}")


def validate_artifact_manifest(data: Mapping[str, Any]) -> None:
    """Validate an inbound ``artifact.json`` manifest against its JSON Schema."""
    try:
        import jsonschema  # type: ignore

        jsonschema.validate(instance=data, schema=_load_schema(ARTIFACT_SCHEMA))
        return
    except ImportError:
        if "artifact_id" not in data:
            raise ValueError("artifact.json missing required key: 'artifact_id'")
    except Exception as e:
        raise ValueError(f"artifact.json failed schema validation: {e}") from e


# --------------------------------------------------------------------------- #
# CLI (thin — the real driver is fb_os.cli)                                     #
# --------------------------------------------------------------------------- #
def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="fb_os.questions", description="open-questions.json seam tools")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="print the current published open-questions.json")

    pr = sub.add_parser("rank", help="select the single best question for a report string")
    pr.add_argument("report", help="the report text (or - for stdin)")
    pr.add_argument("--surface", default=DEFAULT_SURFACE)

    pv = sub.add_parser("validate", help="validate a file against the open-questions schema")
    pv.add_argument("path")

    args = ap.parse_args(argv)
    if args.cmd == "show":
        print(json.dumps(load().to_dict(), indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "rank":
        report = sys.stdin.read() if args.report == "-" else args.report
        q = rank_for(report, surface=args.surface)
        print(json.dumps(q.to_dict() if q else None, indent=2, ensure_ascii=False))
        return 0
    if args.cmd == "validate":
        validate_question_set(json.loads(Path(args.path).read_text()))
        print("valid ✅")
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
