"""Timeline builder — DuckDB attaches the canonical SQLite store and emits
per-era aggregations as JSON.

Schema mapping:
    bookmarks.captured_at   → epoch seconds (INTEGER)
    bookmarks.era_label     → user-assigned label
    eras.era_label          → TEXT PK
    eras.start_date / end_date → epoch seconds (INTEGER)

Counts here are live (computed from `bookmarks`), not the cached
`eras.bookmark_count`. SPEC.md says a bookmark's era_label may change if the
user re-classifies a folder; the cached count would drift.
"""

from __future__ import annotations

import json
from pathlib import Path

from muninn.config import load_paths


def build_timeline(
    db_path: str | Path | None = None,
    out_path: str | Path | None = None,
) -> str:
    """Build the per-era timeline. Returns the JSON string.

    If `out_path` is given, the JSON is also written there.
    """
    import duckdb

    sqlite_path = str(db_path or load_paths().db_path)

    con = duckdb.connect()
    try:
        con.execute(f"ATTACH '{sqlite_path}' AS muninn (TYPE sqlite, READ_ONLY)")

        # Per-era counts pulled live from bookmarks.
        rows = con.execute(
            """
            SELECT
                era_label,
                COUNT(*) AS bookmark_count,
                COUNT(*) FILTER (WHERE content_visible = 1) AS visible_count,
                MIN(captured_at) AS earliest_captured_at,
                MAX(captured_at) AS latest_captured_at
            FROM muninn.bookmarks
            WHERE era_label IS NOT NULL
            GROUP BY era_label
            ORDER BY earliest_captured_at NULLS LAST, era_label
            """
        ).fetchall()

        # Per-era scrape success counts (joined through bookmarks).
        scrape_rows = con.execute(
            """
            SELECT
                b.era_label,
                COUNT(*) FILTER (
                    WHERE sr.scrape_status IN ('ok', 'partial')
                ) AS scraped_ok_count,
                COUNT(*) FILTER (
                    WHERE sr.scrape_status NOT IN ('ok', 'partial')
                ) AS scraped_failed_count
            FROM muninn.bookmarks b
            LEFT JOIN muninn.scrape_results sr
                   ON sr.bookmark_id = b.bookmark_id
            WHERE b.era_label IS NOT NULL
            GROUP BY b.era_label
            """
        ).fetchall()
        scrape_meta = {r[0]: {"scraped_ok": r[1], "scraped_failed": r[2]} for r in scrape_rows}

        # Era narratives + bracketed dates.
        era_rows = con.execute(
            """
            SELECT era_label, narrative, inferred_year,
                   start_date, end_date,
                   dominant_topics, dominant_domains
            FROM muninn.eras
            """
        ).fetchall()
        era_meta = {
            r[0]: {
                "narrative": r[1],
                "inferred_year": r[2],
                "start_date": r[3],
                "end_date": r[4],
                "dominant_topics": _maybe_json(r[5]),
                "dominant_domains": _maybe_json(r[6]),
            }
            for r in era_rows
        }
    finally:
        con.close()

    timeline = []
    for row in rows:
        era_label = row[0]
        em = era_meta.get(era_label, {})
        sm = scrape_meta.get(era_label, {})
        timeline.append(
            {
                "era_label": era_label,
                "bookmark_count": row[1],
                "visible_count": row[2],
                "earliest_captured_at": row[3],
                "latest_captured_at": row[4],
                "scraped_ok_count": sm.get("scraped_ok", 0),
                "scraped_failed_count": sm.get("scraped_failed", 0),
                "narrative": em.get("narrative"),
                "inferred_year": em.get("inferred_year"),
                "start_date": em.get("start_date"),
                "end_date": em.get("end_date"),
                "dominant_topics": em.get("dominant_topics"),
                "dominant_domains": em.get("dominant_domains"),
            }
        )

    payload = json.dumps({"timeline": timeline}, indent=2, default=str)
    if out_path is not None:
        Path(out_path).write_text(payload)
    return payload


def _maybe_json(val):
    if val is None:
        return None
    if isinstance(val, (list, dict)):
        return val
    try:
        return json.loads(val)
    except (TypeError, json.JSONDecodeError):
        return val


__all__ = ["build_timeline"]
