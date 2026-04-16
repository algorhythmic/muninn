"""Embedding generation for the Qdrant vector index.

v1 uses a deterministic SHA-256-derived embedding so the pipeline is
runnable without any external embedding API. Same input ⇒ same vector,
which is enough for upsert idempotency and reconcile correctness; the
*search quality* of these vectors is intentionally weak — swap in a
real embedding model (Voyage AI, etc.) before exposing semantic search
to consumers.

The output dimension is fixed at ``QDRANT_VECTOR_DIM`` (1024) so
swapping in a real model later doesn't require recreating the
collection if the model's dim happens to match.
"""

from __future__ import annotations

import hashlib
import math

from muninn.config import QDRANT_VECTOR_DIM

VECTOR_DIM = QDRANT_VECTOR_DIM


def text_to_vector(text: str) -> list[float]:
    """Deterministic L2-normalized embedding for ``text``.

    Identical inputs yield identical outputs, which guarantees that
    re-running enrichment produces the same Qdrant point and the
    reconcile script's set-difference is meaningful.
    """
    values: list[float] = []
    # Each SHA-256 yields 32 bytes ⇒ 8 floats (4 bytes each).
    chunks_needed = math.ceil(VECTOR_DIM / 8)
    for i in range(chunks_needed):
        h = hashlib.sha256(f"{text}::{i}".encode("utf-8")).digest()
        for j in range(0, 32, 4):
            val = int.from_bytes(h[j : j + 4], "little", signed=True)
            values.append(val / (2**31))

    vec = values[:VECTOR_DIM]

    magnitude = math.sqrt(sum(v * v for v in vec))
    if magnitude > 0:
        vec = [v / magnitude for v in vec]
    return vec
