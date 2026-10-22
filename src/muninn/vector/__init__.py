"""Vector index — embedding generation and Qdrant client wrapper.

The canonical SQLite store does not persist embeddings; they live in
Qdrant only and are regenerated deterministically from the
``(title, summary, tags)`` triple at enrich time and at reconcile time.

Public API:

- ``text_to_vector`` — deterministic embedding for a string
  (see :mod:`muninn.vector.embed`).
- ``get_client`` / ``ensure_collection`` / ``upsert_point`` /
  ``upsert_points_batch`` / ``get_point_ids`` / ``get_point_payloads`` /
  ``delete_points`` / ``get_collection_count``
  (see :mod:`muninn.vector.qdrant`).
- :mod:`muninn.vector.reconcile` — backfills missing points, refreshes
  stale ones and drops hidden ones (``scripts/reconcile-vector-index.py``).
  Not imported here, to keep this package free of the enrich and DB code.
"""

from muninn.vector.embed import text_to_vector
from muninn.vector.qdrant import (
    delete_points,
    ensure_collection,
    get_client,
    get_collection_count,
    get_point_ids,
    get_point_payloads,
    upsert_point,
    upsert_points_batch,
)

__all__ = [
    "delete_points",
    "ensure_collection",
    "get_client",
    "get_collection_count",
    "get_point_ids",
    "get_point_payloads",
    "text_to_vector",
    "upsert_point",
    "upsert_points_batch",
]
