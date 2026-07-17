#!/usr/bin/env python3
"""Reconcile enriched rows with the Qdrant vector index.

Workflow:

1. Read every ``enriched.bookmark_id`` from the canonical SQL store.
2. Scroll every point ID out of the Qdrant collection.
3. The set difference ``enriched - qdrant`` is the backfill list.
4. For each missing row, recompute the embedding from
   ``(title, summary, tags)`` (the canonical schema does not persist
   embedding text — see :mod:`muninn.vector.embed`) and batch-upsert.

Idempotent: re-running once everything is in sync is a no-op. Safe to
run on a cron, after a migration, or when the homelab Qdrant comes
back online after an outage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muninn.config import load_paths  # noqa: E402
from muninn.db import connect  # noqa: E402
from muninn.enrich.haiku import build_embedding_text  # noqa: E402
from muninn.vector.embed import embed_document  # noqa: E402
from muninn.vector.qdrant import (  # noqa: E402
    ensure_collection,
    get_client,
    get_point_ids,
    upsert_points_batch,
)

logger = logging.getLogger(__name__)


def get_all_enriched_rows(conn: sqlite3.Connection) -> dict[int, dict]:
    """Return ``{bookmark_id: {title, summary, tags, content_type, language}}``.

    Joins ``bookmarks`` for the title since the embedding text is
    derived from ``(title, summary, tags)`` and the schema doesn't
    persist a precomputed embedding text column.
    """
    rows = conn.execute(
        """
        SELECT e.bookmark_id,
               b.title,
               e.summary,
               e.tags,
               e.content_type,
               e.language
        FROM   enriched e
        JOIN   bookmarks b ON b.bookmark_id = e.bookmark_id
        """
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        out[r["bookmark_id"]] = {
            "title": r["title"] or "",
            "summary": r["summary"] or "",
            "tags": r["tags"],
            "content_type": r["content_type"],
            "language": r["language"],
        }
    return out


def reconcile(
    db_path: str | Path,
    *,
    dry_run: bool = False,
    batch_size: int = 100,
) -> dict:
    """Identify enriched rows missing from Qdrant and backfill them."""
    conn = connect(Path(db_path))
    try:
        enriched = get_all_enriched_rows(conn)
    finally:
        # Close the connection as soon as we have what we need; Qdrant calls
        # don't touch SQL.
        conn.close()
    enriched_ids = set(enriched.keys())

    client = get_client()
    if client is None:
        logger.error("Qdrant unavailable — cannot reconcile")
        return {"error": "qdrant_unavailable", "enriched_count": len(enriched_ids)}

    ensure_collection(client)
    qdrant_ids = get_point_ids(client)

    missing_ids = enriched_ids - qdrant_ids
    extra_ids = qdrant_ids - enriched_ids

    stats = {
        "enriched_count": len(enriched_ids),
        "qdrant_count": len(qdrant_ids),
        "missing_from_qdrant": len(missing_ids),
        "extra_in_qdrant": len(extra_ids),
        "backfilled": 0,
        "errors": 0,
    }

    logger.info(
        "Reconciliation: %d enriched, %d in Qdrant, %d missing, %d extra",
        len(enriched_ids),
        len(qdrant_ids),
        len(missing_ids),
        len(extra_ids),
    )

    if dry_run:
        logger.info("DRY RUN — would backfill %d points", len(missing_ids))
        return stats

    batch: list[tuple[int, list[float], dict]] = []
    for bid in sorted(missing_ids):
        row = enriched[bid]
        try:
            tags = json.loads(row["tags"]) if row["tags"] else []
        except json.JSONDecodeError:
            tags = []
        embedding_text = build_embedding_text(row["title"], row["summary"], tags)
        vector = embed_document(embedding_text)
        payload = {
            "bookmark_id": bid,
            "title": row["title"],
            "summary": row["summary"],
            "tags": tags,
            "content_type": row["content_type"],
            "language": row["language"],
        }
        batch.append((bid, vector, payload))

        if len(batch) >= batch_size:
            written = upsert_points_batch(client, batch)
            stats["backfilled"] += written
            stats["errors"] += len(batch) - written
            batch = []

    if batch:
        written = upsert_points_batch(client, batch)
        stats["backfilled"] += written
        stats["errors"] += len(batch) - written

    logger.info("Reconciliation complete: %s", stats)
    return stats


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Reconcile enriched rows with the Qdrant vector index"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="Path to SQLite DB (defaults to MUNINN_DB_PATH).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report only; do not write to Qdrant.",
    )
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("-v", "--verbose", action="store_true")

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    db_path = args.db or load_paths().db_path
    stats = reconcile(db_path, dry_run=args.dry_run, batch_size=args.batch_size)
    print(json.dumps(stats, indent=2))
    return 0 if stats.get("errors", 0) == 0 and "error" not in stats else 1


if __name__ == "__main__":
    raise SystemExit(main())
