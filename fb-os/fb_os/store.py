"""fb_os.store — the local relational + vector store for the Feedback OS.

Tables: ``artifacts``, ``clusters``, ``triage``, ``questions``. SQLite is the
zero-infra default (``make demo`` needs no server). Embeddings are stored as JSON
text and cosine similarity runs in pure Python (:mod:`fb_os.cluster`), so **no new
dependency is required to run** — when the optional ``sqlite-vec`` extension is
present we use it for ANN, otherwise the JSON+cosine path is identical in result.

The Postgres + pgvector backend (the "real" pitch) is gated behind the
``$FB_OS_STORE_BACKEND=postgres`` flag + the ``[postgres]`` extra and is documented
as a v-next switch; the CORE is 100% stdlib ``sqlite3``.

Local only. No network.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable, Optional

PathLike = os.PathLike | str

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id          TEXT PRIMARY KEY,
    surface              TEXT NOT NULL DEFAULT 'cli',
    created_at           TEXT,
    description          TEXT NOT NULL DEFAULT '',
    transcript_path      TEXT,
    report_only          INTEGER NOT NULL DEFAULT 0,
    answers_question_id  TEXT,
    effort_signal        TEXT NOT NULL DEFAULT '{}',   -- json
    embedding            TEXT,                          -- json list[float]
    cluster_id           TEXT,
    triaged_at           TEXT,
    quarantined          INTEGER NOT NULL DEFAULT 0,
    quarantine_reason    TEXT,
    ingested_at          TEXT
);

CREATE TABLE IF NOT EXISTS clusters (
    cluster_id   TEXT PRIMARY KEY,
    label        TEXT NOT NULL DEFAULT '',
    summary      TEXT NOT NULL DEFAULT '',
    centroid     TEXT,            -- json list[float]
    size         INTEGER NOT NULL DEFAULT 0,
    suppressed   INTEGER NOT NULL DEFAULT 0,
    keywords     TEXT NOT NULL DEFAULT '[]',  -- json list[str]
    updated_at   TEXT
);

CREATE TABLE IF NOT EXISTS triage (
    artifact_id          TEXT PRIMARY KEY,
    category             TEXT,
    route                TEXT,
    priority             REAL,
    dup_of               TEXT,
    answers_question_id  TEXT,
    cluster_id           TEXT,
    triaged_at           TEXT
);

CREATE TABLE IF NOT EXISTS questions (
    id       TEXT PRIMARY KEY,
    payload  TEXT NOT NULL          -- json OpenQuestion.to_dict()
);

CREATE INDEX IF NOT EXISTS idx_artifacts_cluster ON artifacts(cluster_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_quar    ON artifacts(quarantined);
"""

_ARTIFACT_COLS = (
    "artifact_id", "surface", "created_at", "description", "transcript_path",
    "report_only", "answers_question_id", "effort_signal", "embedding",
    "cluster_id", "triaged_at", "quarantined", "quarantine_reason", "ingested_at",
)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Store:
    """A thin, explicit SQLite store. Pass ``":memory:"`` for tests."""

    def __init__(self, path: PathLike = ":memory:"):
        backend = os.environ.get("FB_OS_STORE_BACKEND", "sqlite")
        if backend not in ("sqlite", ""):
            # Documented v-next: postgres+pgvector. The core only ships sqlite.
            raise NotImplementedError(
                f"store backend {backend!r} is a documented v-next upgrade; "
                "the runnable core is sqlite only (unset $FB_OS_STORE_BACKEND)."
            )
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.init_schema()

    # -- lifecycle -------------------------------------------------------------
    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- artifacts -------------------------------------------------------------
    def upsert_artifact(self, art: dict) -> None:
        row = {
            "artifact_id": art["artifact_id"],
            "surface": art.get("surface", "cli"),
            "created_at": art.get("created_at"),
            "description": art.get("description", ""),
            "transcript_path": art.get("transcript_path"),
            "report_only": int(bool(art.get("report_only", False))),
            "answers_question_id": art.get("answers_question_id"),
            "effort_signal": json.dumps(art.get("effort_signal", {}) or {}),
            "embedding": json.dumps(art["embedding"]) if art.get("embedding") is not None else None,
            "cluster_id": art.get("cluster_id"),
            "triaged_at": art.get("triaged_at"),
            "quarantined": int(bool(art.get("quarantined", False))),
            "quarantine_reason": art.get("quarantine_reason"),
            "ingested_at": art.get("ingested_at") or _now_iso(),
        }
        cols = ",".join(_ARTIFACT_COLS)
        placeholders = ",".join(f":{c}" for c in _ARTIFACT_COLS)
        updates = ",".join(f"{c}=excluded.{c}" for c in _ARTIFACT_COLS if c != "artifact_id")
        self.conn.execute(
            f"INSERT INTO artifacts ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(artifact_id) DO UPDATE SET {updates}",
            row,
        )
        self.conn.commit()

    def get_artifact(self, artifact_id: str) -> Optional[dict]:
        cur = self.conn.execute("SELECT * FROM artifacts WHERE artifact_id=?", (artifact_id,))
        r = cur.fetchone()
        return _artifact_row_to_dict(r) if r else None

    def artifacts(self, *, include_quarantined: bool = False,
                  clustered_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM artifacts"
        conds = []
        if not include_quarantined:
            conds.append("quarantined=0")
        if clustered_only:
            conds.append("cluster_id IS NOT NULL")
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY artifact_id"
        return [_artifact_row_to_dict(r) for r in self.conn.execute(sql)]

    def set_embedding(self, artifact_id: str, vector: list[float]) -> None:
        self.conn.execute("UPDATE artifacts SET embedding=? WHERE artifact_id=?",
                          (json.dumps(vector), artifact_id))
        self.conn.commit()

    def set_cluster(self, artifact_id: str, cluster_id: Optional[str]) -> None:
        self.conn.execute("UPDATE artifacts SET cluster_id=? WHERE artifact_id=?",
                          (cluster_id, artifact_id))
        self.conn.commit()

    def mark_triaged(self, artifact_id: str, when: Optional[str] = None) -> None:
        self.conn.execute("UPDATE artifacts SET triaged_at=? WHERE artifact_id=?",
                          (when or _now_iso(), artifact_id))
        self.conn.commit()

    def quarantine(self, artifact_id: str, reason: str) -> None:
        self.conn.execute(
            "UPDATE artifacts SET quarantined=1, quarantine_reason=?, cluster_id=NULL "
            "WHERE artifact_id=?", (reason, artifact_id))
        self.conn.commit()

    # -- clusters --------------------------------------------------------------
    def upsert_cluster(self, cluster: dict) -> None:
        self.conn.execute(
            "INSERT INTO clusters (cluster_id,label,summary,centroid,size,suppressed,keywords,updated_at) "
            "VALUES (:cluster_id,:label,:summary,:centroid,:size,:suppressed,:keywords,:updated_at) "
            "ON CONFLICT(cluster_id) DO UPDATE SET "
            "label=excluded.label, summary=excluded.summary, centroid=excluded.centroid, "
            "size=excluded.size, suppressed=excluded.suppressed, keywords=excluded.keywords, "
            "updated_at=excluded.updated_at",
            {
                "cluster_id": cluster["cluster_id"],
                "label": cluster.get("label", ""),
                "summary": cluster.get("summary", ""),
                "centroid": json.dumps(cluster["centroid"]) if cluster.get("centroid") is not None else None,
                "size": int(cluster.get("size", 0)),
                "suppressed": int(bool(cluster.get("suppressed", False))),
                "keywords": json.dumps(cluster.get("keywords", []) or []),
                "updated_at": cluster.get("updated_at") or _now_iso(),
            },
        )
        self.conn.commit()

    def clusters(self, *, include_suppressed: bool = True) -> list[dict]:
        sql = "SELECT * FROM clusters"
        if not include_suppressed:
            sql += " WHERE suppressed=0"
        sql += " ORDER BY cluster_id"
        return [_cluster_row_to_dict(r) for r in self.conn.execute(sql)]

    def get_cluster(self, cluster_id: str) -> Optional[dict]:
        r = self.conn.execute("SELECT * FROM clusters WHERE cluster_id=?", (cluster_id,)).fetchone()
        return _cluster_row_to_dict(r) if r else None

    def clear_clusters(self) -> None:
        """Reset cluster assignments before a fresh clustering pass (idempotent)."""
        self.conn.execute("DELETE FROM clusters")
        self.conn.execute("UPDATE artifacts SET cluster_id=NULL")
        self.conn.commit()

    def cluster_members(self, cluster_id: str) -> list[dict]:
        return [_artifact_row_to_dict(r) for r in self.conn.execute(
            "SELECT * FROM artifacts WHERE cluster_id=? AND quarantined=0 ORDER BY artifact_id",
            (cluster_id,))]

    # -- triage ----------------------------------------------------------------
    def upsert_triage(self, rec: dict) -> None:
        self.conn.execute(
            "INSERT INTO triage (artifact_id,category,route,priority,dup_of,answers_question_id,cluster_id,triaged_at) "
            "VALUES (:artifact_id,:category,:route,:priority,:dup_of,:answers_question_id,:cluster_id,:triaged_at) "
            "ON CONFLICT(artifact_id) DO UPDATE SET "
            "category=excluded.category, route=excluded.route, priority=excluded.priority, "
            "dup_of=excluded.dup_of, answers_question_id=excluded.answers_question_id, "
            "cluster_id=excluded.cluster_id, triaged_at=excluded.triaged_at",
            {
                "artifact_id": rec["artifact_id"],
                "category": rec.get("category"),
                "route": rec.get("route"),
                "priority": rec.get("priority"),
                "dup_of": rec.get("dup_of"),
                "answers_question_id": rec.get("answers_question_id"),
                "cluster_id": rec.get("cluster_id"),
                "triaged_at": rec.get("triaged_at") or _now_iso(),
            },
        )
        self.conn.commit()

    def triage_records(self) -> list[dict]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM triage ORDER BY artifact_id")]

    # -- questions (mirror of the published file, for metrics/SoR) -------------
    def save_questions(self, questions: Iterable[dict]) -> None:
        self.conn.execute("DELETE FROM questions")
        self.conn.executemany(
            "INSERT INTO questions (id,payload) VALUES (?,?)",
            [(q["id"], json.dumps(q)) for q in questions],
        )
        self.conn.commit()

    def load_questions(self) -> list[dict]:
        return [json.loads(r["payload"]) for r in self.conn.execute("SELECT payload FROM questions")]


def _artifact_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["report_only"] = bool(d.get("report_only"))
    d["quarantined"] = bool(d.get("quarantined"))
    d["effort_signal"] = json.loads(d["effort_signal"]) if d.get("effort_signal") else {}
    d["embedding"] = json.loads(d["embedding"]) if d.get("embedding") else None
    return d


def _cluster_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["suppressed"] = bool(d.get("suppressed"))
    d["centroid"] = json.loads(d["centroid"]) if d.get("centroid") else None
    d["keywords"] = json.loads(d["keywords"]) if d.get("keywords") else []
    return d
