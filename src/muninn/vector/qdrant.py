"""Qdrant client wrapper: collection management + upserts keyed by ``bookmark_id``.

Configuration (URL, collection name, vector dim) lives in
:mod:`muninn.config`. All point IDs are the ``bookmark_id`` integer from
the canonical schema, so the SQL store is the source of truth and Qdrant
is rebuildable from it at any time via ``scripts/reconcile-vector-index.py``.

If Qdrant is unreachable, ``get_client`` returns ``None`` rather than
raising — callers are expected to log-and-continue and rely on reconcile
to catch up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from muninn.config import QDRANT_COLLECTION, QDRANT_URL, QDRANT_VECTOR_DIM

if TYPE_CHECKING:  # pragma: no cover
    from qdrant_client import QdrantClient

logger = logging.getLogger(__name__)


@dataclass
class QdrantStatus:
    available: bool
    point_count: int = 0
    error: str | None = None


def get_client(url: str = QDRANT_URL, timeout: int = 5) -> "QdrantClient | None":
    """Return a Qdrant client, or ``None`` if the server is unreachable."""
    try:
        from qdrant_client import QdrantClient

        client = QdrantClient(url=url, timeout=timeout)
        client.get_collections()  # cheap connectivity probe
        return client
    except Exception as exc:  # noqa: BLE001 — explicit availability gate
        logger.warning("Qdrant unavailable at %s: %s", url, exc)
        return None


def ensure_collection(
    client: "QdrantClient",
    collection_name: str = QDRANT_COLLECTION,
    vector_dim: int = QDRANT_VECTOR_DIM,
) -> None:
    """Create the collection if it does not already exist."""
    from qdrant_client.models import Distance, VectorParams

    existing = {c.name for c in client.get_collections().collections}
    if collection_name in existing:
        return
    client.create_collection(
        collection_name=collection_name,
        vectors_config=VectorParams(size=vector_dim, distance=Distance.COSINE),
    )
    logger.info("Created Qdrant collection '%s'", collection_name)


def upsert_point(
    client: "QdrantClient",
    bookmark_id: int,
    vector: list[float],
    payload: dict | None = None,
    collection_name: str = QDRANT_COLLECTION,
) -> bool:
    """Upsert a single point keyed by ``bookmark_id``. Returns True on success."""
    from qdrant_client.models import PointStruct

    try:
        client.upsert(
            collection_name=collection_name,
            points=[
                PointStruct(id=bookmark_id, vector=vector, payload=payload or {}),
            ],
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to upsert point %d: %s", bookmark_id, exc)
        return False


def upsert_points_batch(
    client: "QdrantClient",
    points: list[tuple[int, list[float], dict]],
    collection_name: str = QDRANT_COLLECTION,
) -> int:
    """Batch upsert. Returns the count of successfully upserted points."""
    if not points:
        return 0
    from qdrant_client.models import PointStruct

    try:
        structs = [
            PointStruct(id=bid, vector=vec, payload=payload)
            for bid, vec, payload in points
        ]
        client.upsert(collection_name=collection_name, points=structs)
        return len(structs)
    except Exception as exc:  # noqa: BLE001
        logger.error("Batch upsert failed: %s", exc)
        return 0


def get_point_ids(
    client: "QdrantClient",
    collection_name: str = QDRANT_COLLECTION,
    page_size: int = 1000,
) -> set[int]:
    """Return all integer point IDs in the collection via ``scroll``."""
    ids: set[int] = set()
    offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=page_size,
            offset=offset,
            with_payload=False,
            with_vectors=False,
        )
        for p in points:
            ids.add(p.id)
        if next_offset is None:
            break
        offset = next_offset
    return ids


def get_collection_count(
    client: "QdrantClient",
    collection_name: str = QDRANT_COLLECTION,
) -> int:
    """Return the total point count for the collection (0 on error)."""
    try:
        info = client.get_collection(collection_name)
        return info.points_count or 0
    except Exception:  # noqa: BLE001
        return 0


def get_status(
    client: "QdrantClient | None" = None,
    collection_name: str = QDRANT_COLLECTION,
) -> QdrantStatus:
    """Convenience: return availability + point-count snapshot."""
    if client is None:
        client = get_client()
    if client is None:
        return QdrantStatus(available=False, error="Qdrant not reachable")
    try:
        return QdrantStatus(
            available=True,
            point_count=get_collection_count(client, collection_name),
        )
    except Exception as exc:  # noqa: BLE001
        return QdrantStatus(available=False, error=str(exc))
