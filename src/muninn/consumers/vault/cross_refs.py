"""Display persisted cross-references between visible compiled bookmarks."""

from __future__ import annotations

import sqlite3


def load_cross_refs(
    conn: sqlite3.Connection, slug_by_id: dict[int, str]
) -> dict[int, list[str]]:
    """Return sorted, bidirectional display links for persisted references.

    The database relationships remain directional. Only endpoints in the
    current compiled slug map are displayed, and stored prose is never read.
    """
    neighbors: dict[int, set[int]] = {}
    for source_id, target_id in conn.execute(
        "SELECT source_bookmark_id, target_bookmark_id FROM cross_references"
    ):
        if (
            source_id == target_id
            or source_id not in slug_by_id
            or target_id not in slug_by_id
        ):
            continue
        neighbors.setdefault(source_id, set()).add(target_id)
        neighbors.setdefault(target_id, set()).add(source_id)

    return {
        bookmark_id: sorted(slug_by_id[target_id] for target_id in target_ids)
        for bookmark_id, target_ids in neighbors.items()
    }
