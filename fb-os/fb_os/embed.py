"""fb_os.embed — local, deterministic text embeddings (no network, no heavy deps).

The plan's production embedder is a local sentence-transformer (BGE-M3 / all-MiniLM
via ``sentence-transformers``). Those weights are a multi-hundred-MB download, so on
a metered uplink the **CORE falls back to a deterministic stdlib feature-hashing
vectorizer** (the "hashing-trick" TF vectorizer): tokenise -> hash each token into a
fixed-width signed vector -> L2-normalise. It is:

  * **zero-dependency** (``hashlib`` + ``math`` only),
  * **deterministic** (hashing via ``blake2b``, never Python's salted ``hash()``),
  * **good enough** to cluster short distilled feedback descriptions by topic.

When ``sentence-transformers`` IS importable and ``backend="sbert"`` is requested,
we use it (the documented production path). The backend is selected by ``--backend``
/ ``$FB_OS_EMBED_BACKEND``; the default is ``hashing`` so nothing downloads.

Cosine similarity over these vectors lives in :mod:`fb_os.cluster`.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
from typing import Iterable, Optional

DEFAULT_DIM = 256
DEFAULT_BACKEND = os.environ.get("FB_OS_EMBED_BACKEND", "hashing")

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-./]*")

# Common English stopwords + transcript boilerplate that would wash out topic signal.
_STOPWORDS = frozenset("""
a an the and or but if then else of to in on at for with without from by as is are was were be been being
this that these those it its it's i you we they he she them his her our your my me us do does did done have
has had having not no nor so than too very can could should would will shall may might must just about into
over under again further once here there all any both each few more most other some such only own same out up
down off above below want wants wanted need needs would like really thing things get got
""".split())


def tokenize(text: str) -> list[str]:
    """Lowercase alnum tokens (keeping ``/`` ``_`` ``-`` ``.`` so ``/feedback`` and
    ``--mock`` survive), stopwords dropped. Shared by embedding + cluster labelling."""
    toks = _TOKEN_RE.findall((text or "").lower())
    return [t for t in toks if t not in _STOPWORDS and len(t) > 1]


def _hash_bucket(token: str, dim: int) -> tuple[int, int]:
    """Deterministic (bucket, sign) for a token via blake2b (stable across runs/procs)."""
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(h, "big")
    bucket = n % dim
    sign = 1 if (n >> 63) & 1 else -1
    return bucket, sign


def hashing_embed(text: str, dim: int = DEFAULT_DIM) -> list[float]:
    """Feature-hashing TF vector, L2-normalised. Deterministic and dependency-free."""
    vec = [0.0] * dim
    for tok in tokenize(text):
        bucket, sign = _hash_bucket(tok, dim)
        vec[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


# --------------------------------------------------------------------------- #
# Optional production backend (sentence-transformers) — used only if present   #
# --------------------------------------------------------------------------- #
_sbert_model = None
_sbert_failed = False


def _get_sbert(model_name: str = "all-MiniLM-L6-v2"):
    global _sbert_model, _sbert_failed
    if _sbert_model is None and not _sbert_failed:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore

            _sbert_model = SentenceTransformer(model_name)
        except Exception:
            _sbert_failed = True
    return _sbert_model


def sbert_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401  # type: ignore

        return True
    except Exception:
        return False


class Embedder:
    """Pluggable embedder. ``backend="hashing"`` (default, zero-download) or
    ``"sbert"`` (production; requires ``sentence-transformers`` weights)."""

    def __init__(self, backend: str = DEFAULT_BACKEND, dim: int = DEFAULT_DIM):
        if backend == "sbert" and not sbert_available():
            backend = "hashing"  # graceful fallback; the core never blocks on a download
        self.backend = backend
        self.dim = dim
        self._cache: dict[str, list[float]] = {}

    def embed(self, text: str) -> list[float]:
        key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).hexdigest()
        if key in self._cache:
            return self._cache[key]
        if self.backend == "sbert":
            model = _get_sbert()
            if model is not None:
                vec = [float(x) for x in model.encode(text, normalize_embeddings=True)]
                self._cache[key] = vec
                return vec
        vec = hashing_embed(text, self.dim)
        self._cache[key] = vec
        return vec

    def embed_many(self, texts: Iterable[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


def embed_text(text: str, *, backend: Optional[str] = None, dim: int = DEFAULT_DIM) -> list[float]:
    """Convenience one-shot embed."""
    return Embedder(backend=backend or DEFAULT_BACKEND, dim=dim).embed(text)
