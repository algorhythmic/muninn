"""era_narrative task: build per-era materials, UPSERT result into `eras`.

Materials passed to the container:
  - era_label
  - bookmarks: [{bookmark_id, title, url, captured_at, summary?, tags?}, ...]
  - neighboring_eras: [{era_label, narrative?}]
  - bookmark_count, start_date, end_date  (host-computed bracket)
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from muninn.models import SynthesisRun


def prepare_input(
    conn: sqlite3.Connection,
    era_label: str,
    *,
    include_neighbors: bool = True,
) -> dict[str, Any]:
    """Build the materials dict for an era_narrative task."""
    cur = conn.cursor()

    cur.execute(
        """
        SELECT b.bookmark_id, b.title, b.url, b.captured_at, b.domain,
               e.summary, e.tags
        FROM bookmarks b
        LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id
        WHERE b.era_label = ? AND b.content_visible = 1
        ORDER BY b.captured_at
        """,
        (era_label,),
    )
    rows = cur.fetchall()
    bookmarks = [
        {
            "bookmark_id": r["bookmark_id"],
            "title": r["title"],
            "url": r["url"],
            "captured_at": r["captured_at"],
            "domain": r["domain"],
            "summary": r["summary"],
            "tags": json.loads(r["tags"]) if r["tags"] else [],
        }
        for r in rows
    ]

    cur.execute(
        "SELECT MIN(captured_at) AS s, MAX(captured_at) AS e, COUNT(*) AS c "
        "FROM bookmarks WHERE era_label = ?",
        (era_label,),
    )
    bracket = cur.fetchone()

    neighbors: list[dict[str, Any]] = []
    if include_neighbors:
        cur.execute(
            """
            SELECT era_label, narrative FROM eras
            WHERE era_label != ? AND narrative IS NOT NULL
            ORDER BY start_date
            """,
            (era_label,),
        )
        neighbors = [
            {"era_label": r["era_label"], "narrative": r["narrative"]}
            for r in cur.fetchall()
        ]

    return {
        "era_label": era_label,
        "bookmarks": bookmarks,
        "neighboring_eras": neighbors,
        "bookmark_count": bracket["c"] if bracket else 0,
        "start_date": bracket["s"] if bracket else None,
        "end_date": bracket["e"] if bracket else None,
    }


def write_output(
    conn: sqlite3.Connection,
    output: dict[str, Any],
    run: SynthesisRun,
    *,
    era_label: str,
    materials: dict[str, Any] | None = None,
) -> None:
    """UPSERT the era row.

    Bracket dates and bookmark_count come from the host-computed materials,
    not from the LLM output (the LLM may not see every row).
    """
    materials = materials or {}
    meta = output.get("synthesis_metadata", {}) or {}
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO eras (
            era_label, inferred_year, start_date, end_date, bookmark_count,
            dominant_topics, dominant_domains, narrative,
            enrichment_model, enrichment_prompt_version, generated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, unixepoch())
        ON CONFLICT(era_label) DO UPDATE SET
            inferred_year             = excluded.inferred_year,
            start_date                = excluded.start_date,
            end_date                  = excluded.end_date,
            bookmark_count            = excluded.bookmark_count,
            dominant_topics           = excluded.dominant_topics,
            dominant_domains          = excluded.dominant_domains,
            narrative                 = excluded.narrative,
            enrichment_model          = excluded.enrichment_model,
            enrichment_prompt_version = excluded.enrichment_prompt_version,
            generated_at              = unixepoch()
        """,
        (
            era_label,
            output.get("inferred_year"),
            materials.get("start_date"),
            materials.get("end_date"),
            materials.get("bookmark_count", 0),
            json.dumps(output.get("dominant_topics") or []),
            json.dumps(output.get("dominant_domains") or []),
            output.get("narrative"),
            run.enrichment_model or meta.get("model"),
            run.enrichment_prompt_version or meta.get("prompt_version"),
        ),
    )
