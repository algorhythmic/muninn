#!/usr/bin/env python3
"""Reconcile enriched rows with the Qdrant vector index.

Workflow (implemented in :mod:`muninn.vector.reconcile`):

1. Read every ``enriched`` row, with its bookmark's title and visibility,
   from the canonical SQL store.
2. Scroll every point (with payload) out of the Qdrant collection.
3. Upsert visible rows that are missing from Qdrant, and rows whose
   point is stale (its payload no longer matches the row, e.g. after a
   deep pass rewrote the summary). The embedding is recomputed from
   ``(title, summary, tags)``; the schema persists no embedding text.
4. Delete points whose bookmark is now hidden (``content_visible = 0``).

Covers every source (browser bookmarks, X posts, X Articles, linked
items): it reads ``enriched``, which is source-agnostic.

Idempotent: re-running once everything is in sync is a no-op. Safe to
run on a cron, after a migration, after a deep pass, or when the homelab
Qdrant comes back online after an outage.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from muninn.config import load_paths  # noqa: E402
from muninn.vector.reconcile import get_all_enriched_rows, reconcile  # noqa: E402,F401


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
