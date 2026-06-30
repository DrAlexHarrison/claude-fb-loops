"""Tests for fb_os.cluster — determinism, min-cluster-size suppression, dedup."""

from fb_os import cluster as C
from fb_os.embed import Embedder


def _arts(texts):
    e = Embedder()
    return [{"artifact_id": f"a_{i:02d}", "description": t, "embedding": e.embed(t),
             "surface": "cli", "effort_signal": {}} for i, t in enumerate(texts)]


THEME_A = [
    "the voice confirm has high latency and slow transcription on super+v",
    "voice confirm latency is slow, transcription takes seconds after super+v",
    "lag in the voice confirm, slow transcription when pressing super+v",
]
THEME_B = [
    "redaction is too aggressive and strips harmless bash output",
    "aggressive redaction masked harmless output and a public url",
    "redaction stripped too much harmless output, less aggressive please",
]
SINGLETON = ["kerning of monospace glyph ligatures looks off on a niche nerd font terminal"]


def test_clustering_is_deterministic():
    arts = _arts(THEME_A + THEME_B + SINGLETON)
    c1 = C.cluster_artifacts(arts)
    c2 = C.cluster_artifacts(arts)
    sig1 = [(c["cluster_id"], tuple(c["members"])) for c in c1]
    sig2 = [(c["cluster_id"], tuple(c["members"])) for c in c2]
    assert sig1 == sig2


def test_two_themes_separate():
    arts = _arts(THEME_A + THEME_B)
    clusters = [c for c in C.cluster_artifacts(arts) if not c["suppressed"]]
    assert len(clusters) == 2
    assert all(c["size"] == 3 for c in clusters)


def test_min_cluster_size_suppresses_singleton():
    arts = _arts(THEME_A + SINGLETON)
    clusters = C.cluster_artifacts(arts, min_cluster_size=2)
    supp = [c for c in clusters if c["suppressed"]]
    assert len(supp) == 1
    assert supp[0]["size"] == 1
    assert supp[0]["label"] == C.SUPPRESSED_LABEL


def test_min_cluster_size_one_suppresses_nothing():
    arts = _arts(THEME_A + SINGLETON)
    clusters = C.cluster_artifacts(arts, min_cluster_size=1)
    assert all(not c["suppressed"] for c in clusters)


def test_dedup_collapses_duplicates():
    dup = "voice confirm latency is slow on super+v"
    arts = _arts([dup, dup, dup])  # identical -> dedup to one representative
    reps, dup_map = C.dedup(arts)
    assert len(reps) == 1
    # all three map to the same canonical id
    assert len(set(dup_map.values())) == 1
    # but the cluster still counts all three distinct artifacts as evidence
    clusters = C.cluster_artifacts(arts, min_cluster_size=1)
    assert sum(c["size"] for c in clusters) == 3


def test_cluster_labels_are_meaningful():
    arts = _arts(THEME_A)
    clusters = C.cluster_artifacts(arts, min_cluster_size=1)
    kws = clusters[0]["keywords"]
    assert any(k in ("voice", "latency", "transcription", "confirm") for k in kws)
