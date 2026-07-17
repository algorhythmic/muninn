"""Embedding generation for the Qdrant vector index.

Backends, selected by ``$MUNINN_EMBEDDING_BACKEND`` (read at call time):

- ``sentence-transformers`` (default) — local inference with
  ``$MUNINN_EMBEDDING_MODEL`` (default ``google/embeddinggemma-300m``,
  768-dim, Matryoshka-truncatable via ``$MUNINN_EMBEDDING_DIM``).
  Documents and queries use the model's asymmetric prompts so index and
  query vectors share a space. Requires the ``embeddings`` extra:
  ``pip install 'muninn[embeddings]'``.
- ``hash`` — the v1 deterministic SHA-256 placeholder. Same input ⇒ same
  vector, which keeps upserts and reconcile idempotent with zero heavy
  deps; its search quality is intentionally useless. For tests and
  explicit offline runs only — never ship semantic search on it.

All vectors are L2-normalized with ``EMBEDDING_DIM`` components.
"""

from __future__ import annotations

import hashlib
import math
import os
import threading

from muninn.config import EMBEDDING_DIM, EMBEDDING_MODEL

# Kept for callers that size the Qdrant collection off this module.
VECTOR_DIM = EMBEDDING_DIM

_BACKENDS = ("sentence-transformers", "hash")


class EmbeddingBackendError(RuntimeError):
    """The configured embedding backend cannot produce vectors."""


def _backend() -> str:
    return os.environ.get("MUNINN_EMBEDDING_BACKEND", "sentence-transformers")


# ── sentence-transformers backend ──────────────────────────────────

_st_model = None
_st_lock = threading.Lock()


def _get_st_model():
    global _st_model
    if _st_model is None:
        with _st_lock:
            if _st_model is None:
                try:
                    from sentence_transformers import SentenceTransformer
                except ImportError as exc:
                    raise EmbeddingBackendError(
                        "MUNINN_EMBEDDING_BACKEND=sentence-transformers but the "
                        "package is missing — install with "
                        "`pip install 'muninn[embeddings]'`, or set "
                        "MUNINN_EMBEDDING_BACKEND=hash for the offline placeholder."
                    ) from exc
                _st_model = SentenceTransformer(
                    EMBEDDING_MODEL, truncate_dim=EMBEDDING_DIM
                )
    return _st_model


def _st_encode(text: str, prompt_name: str) -> list[float]:
    model = _get_st_model()
    try:
        vec = model.encode(text, prompt_name=prompt_name, normalize_embeddings=True)
    except (KeyError, ValueError):
        # Model config doesn't define this prompt; encode unprompted.
        vec = model.encode(text, normalize_embeddings=True)
    return [float(v) for v in vec]


# ── hash backend ────────────────────────────────────────────────────


def _hash_vector(text: str, dim: int = EMBEDDING_DIM) -> list[float]:
    """Deterministic L2-normalized pseudo-embedding (v1 placeholder)."""
    values: list[float] = []
    # Each SHA-256 yields 32 bytes ⇒ 8 floats (4 bytes each).
    chunks_needed = math.ceil(dim / 8)
    for i in range(chunks_needed):
        h = hashlib.sha256(f"{text}::{i}".encode("utf-8")).digest()
        for j in range(0, 32, 4):
            val = int.from_bytes(h[j : j + 4], "little", signed=True)
            values.append(val / (2**31))

    vec = values[:dim]
    magnitude = math.sqrt(sum(v * v for v in vec))
    if magnitude > 0:
        vec = [v / magnitude for v in vec]
    return vec


# ── public API ──────────────────────────────────────────────────────


def _dispatch(text: str, prompt_name: str) -> list[float]:
    backend = _backend()
    if backend == "hash":
        return _hash_vector(text)
    if backend == "sentence-transformers":
        return _st_encode(text, prompt_name)
    raise EmbeddingBackendError(
        f"Unknown MUNINN_EMBEDDING_BACKEND {backend!r} — expected one of {_BACKENDS}"
    )


def embed_document(text: str) -> list[float]:
    """Embedding for indexed content (bookmark embedding text)."""
    return _dispatch(text, "document")


def embed_query(text: str) -> list[float]:
    """Embedding for search queries — asymmetric prompt, same vector space."""
    return _dispatch(text, "query")


def text_to_vector(text: str) -> list[float]:
    """Legacy alias for :func:`embed_document` (pre query/document split)."""
    return embed_document(text)
