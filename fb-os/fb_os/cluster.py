"""fb_os.cluster — the local "Clio repro" clustering pass (lightweight core).

The intended production engine is **BERTopic** (SBERT -> UMAP -> HDBSCAN -> c-TF-IDF),
the OSS reproduction of Anthropic's Clio. BERTopic + UMAP + HDBSCAN are heavy
downloads, so the **CORE ships a deterministic, dependency-free reproduction of the
same *shape*** that runs anywhere with zero new packages:

  * **embed**      -> :mod:`fb_os.embed` (hashing vectoriser; SBERT if present),
  * **cluster**    -> single-pass **threshold agglomeration** over cosine similarity
                      (deterministic; a stand-in for UMAP+HDBSCAN), pinned by a stable
                      artifact ordering rather than a random seed,
  * **label**      -> a **c-TF-IDF-style** term ranking (a term that is frequent in
                      this cluster but rare across all clusters), the same labelling
                      idea BERTopic uses.

Two privacy/quality mechanisms from Clio are kept because they are load-bearing,
not optional:

  * **dedup pre-pass** — near-identical descriptions are merged (one MinHash-free,
    cosine-threshold dedup) so a brigaded/duplicated report can't inflate a theme.
  * **min-cluster-size suppression** — any cluster with fewer than ``min_cluster_size``
    members is marked ``suppressed`` and is NEVER surfaced to the triager or a quote.
    This is the documented Clio 39%-reID defence: rare == potentially identifying.

BERTopic/UMAP/HDBSCAN remain the documented production upgrade (``[cluster]`` extra).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional, Sequence

from .embed import tokenize

# Clustering defaults (the "pinned" config — deterministic, no RNG).
DEFAULT_SIM_THRESHOLD = 0.30   # cosine >= this joins an existing cluster
DEFAULT_DEDUP_THRESHOLD = 0.97  # cosine >= this == a duplicate
DEFAULT_MIN_CLUSTER_SIZE = 2    # Clio privacy floor; suppress smaller clusters
SUPPRESSED_LABEL = "‹suppressed: rare/identifying topic›"

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two vectors (pure Python). Vectors from :mod:`fb_os.embed`
    are already L2-normalised, so this is just a dot product, but we normalise
    defensively to tolerate any backend."""
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _centroid(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return []
    dim = len(vectors[0])
    acc = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            acc[i] += x
    n = len(vectors)
    cent = [x / n for x in acc]
    norm = math.sqrt(sum(x * x for x in cent))
    if norm > 0:
        cent = [x / norm for x in cent]
    return cent


def _slug(text: str, maxlen: int = 40) -> str:
    s = _SLUG_RE.sub("_", text.lower()).strip("_")
    return s[:maxlen] or "topic"


def dedup(items: list[dict], *, threshold: float = DEFAULT_DEDUP_THRESHOLD) -> tuple[list[dict], dict]:
    """Collapse near-identical descriptions (cosine >= ``threshold``). Returns
    ``(representatives, dup_map)`` where ``dup_map[artifact_id] = canonical_id``.
    Every artifact keeps its row; only the canonical representative is clustered,
    so a duplicated/brigaded report cannot inflate a theme's evidence_count."""
    reps: list[dict] = []
    dup_map: dict = {}
    for it in items:
        vec = it.get("embedding") or []
        canonical = None
        for rep in reps:
            if cosine(vec, rep.get("embedding") or []) >= threshold:
                canonical = rep
                break
        if canonical is None:
            reps.append(it)
            dup_map[it["artifact_id"]] = it["artifact_id"]
        else:
            dup_map[it["artifact_id"]] = canonical["artifact_id"]
    return reps, dup_map


def _ctfidf_labels(members_tokens: list[list[str]], all_tokens: Counter,
                   total_docs: int, top_n: int = 6) -> list[str]:
    """c-TF-IDF-style top terms: frequent in this cluster, rare across the corpus."""
    cluster_tf = Counter()
    for toks in members_tokens:
        cluster_tf.update(set(toks))  # doc-frequency within the cluster
    scored: list[tuple[float, str]] = []
    for term, tf in cluster_tf.items():
        df = all_tokens.get(term, 1)
        idf = math.log((1 + total_docs) / (1 + df)) + 1.0
        scored.append((tf * idf, term))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [t for _, t in scored[:top_n]]


def cluster_artifacts(
    artifacts: list[dict],
    *,
    sim_threshold: float = DEFAULT_SIM_THRESHOLD,
    dedup_threshold: float = DEFAULT_DEDUP_THRESHOLD,
    min_cluster_size: int = DEFAULT_MIN_CLUSTER_SIZE,
    random_state: int = 42,  # accepted for API/BERTopic parity; this core is RNG-free
) -> list[dict]:
    """Cluster artifacts (each a dict with ``artifact_id`` + ``embedding``).

    Deterministic: artifacts are processed in a stable ``artifact_id`` order, so the
    same input always yields the same clusters — a stand-in for a pinned random
    seed, achieved without an RNG. Returns a list of cluster dicts::

        {cluster_id, label, keywords, centroid, size, suppressed, members:[ids],
         dup_map:{id:canonical}}

    Clusters below ``min_cluster_size`` are flagged ``suppressed`` (Clio privacy floor)
    and carry the :data:`SUPPRESSED_LABEL`; the triager skips them entirely.
    """
    usable = [a for a in artifacts if a.get("embedding")]
    usable.sort(key=lambda a: a["artifact_id"])  # the pin

    reps, dup_map = dedup(usable, threshold=dedup_threshold)

    # Single-pass threshold agglomeration over cosine similarity to cluster centroids.
    groups: list[dict] = []  # {vectors, members}
    for it in reps:
        vec = it["embedding"]
        best_i, best_sim = -1, sim_threshold
        for i, g in enumerate(groups):
            sim = cosine(vec, g["centroid"])
            if sim >= best_sim:
                best_i, best_sim = i, sim
        if best_i < 0:
            groups.append({"vectors": [vec], "members": [it["artifact_id"]],
                          "centroid": _centroid([vec])})
        else:
            g = groups[best_i]
            g["vectors"].append(vec)
            g["members"].append(it["artifact_id"])
            g["centroid"] = _centroid(g["vectors"])

    # Fold duplicates back into their canonical representative's group (so
    # evidence_count reflects real, distinct artifacts but the dup doesn't form
    # its own singleton cluster).
    canonical_to_group: dict[str, dict] = {}
    for g in groups:
        for cid in g["members"]:
            canonical_to_group[cid] = g
    for art_id, canonical in dup_map.items():
        if art_id == canonical:
            continue
        g = canonical_to_group.get(canonical)
        if g is not None and art_id not in g["members"]:
            g["members"].append(art_id)

    # Corpus token stats (for c-TF-IDF labelling), keyed by canonical artifact.
    desc_by_id = {a["artifact_id"]: a.get("description", "") for a in usable}
    all_tokens = Counter()
    for a in reps:
        all_tokens.update(set(tokenize(desc_by_id.get(a["artifact_id"], ""))))
    total_docs = max(1, len(reps))

    out: list[dict] = []
    used_ids: set[str] = set()
    for g in groups:
        members = sorted(set(g["members"]))
        members_tokens = [tokenize(desc_by_id.get(m, "")) for m in members]
        keywords = _ctfidf_labels(members_tokens, all_tokens, total_docs)
        suppressed = len(members) < min_cluster_size
        base = _slug("_".join(keywords[:3])) if keywords else "topic"
        cid = f"clu_{base}"
        n = 1
        while cid in used_ids:
            n += 1
            cid = f"clu_{base}_{n}"
        used_ids.add(cid)
        label = SUPPRESSED_LABEL if suppressed else (
            " / ".join(keywords[:3]).replace("_", " ") if keywords else "misc")
        out.append({
            "cluster_id": cid,
            "label": label,
            "keywords": keywords,
            "centroid": g["centroid"],
            "size": len(members),
            "suppressed": suppressed,
            "members": members,
            "dup_map": {k: v for k, v in dup_map.items() if v in members or k in members},
        })
    out.sort(key=lambda c: c["cluster_id"])
    return out
