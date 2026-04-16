"""ad_hoc_analysis task: cross-corpus query, append to `analyses`.

Materials passed to the container:
  - prompt
  - filter_query (the SQL/JSON filter that selected the corpus subset)
  - bookmarks: [{bookmark_id, title, url, summary?, tags?}, ...]
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from muninn.models import SynthesisRun


def prepare_input(
    conn: sqlite3.Connection,
    prompt: str,
    *,
    filter_query: str | None = None,
    bookmark_ids: list[int] | None = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Build materials for an ad_hoc_analysis task.

    If `bookmark_ids` is provided, that set is exactly what's surfaced. Otherwise
    a generic bounded sample is used; callers usually compose their own subset
    via `filter_query` interpretation upstream.
    """
    cur = conn.cursor()
    if bookmark_ids:
        placeholders = ",".join("?" * len(bookmark_ids))
        cur.execute(
            f"SELECT b.bookmark_id, b.title, b.url, b.captured_at, b.domain, "
            f"       e.summary, e.tags "
            f"FROM bookmarks b "
            f"LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
            f"WHERE b.bookmark_id IN ({placeholders}) AND b.content_visible = 1 "
            f"LIMIT ?",
            [*bookmark_ids, limit],
        )
    else:
        cur.execute(
            "SELECT b.bookmark_id, b.title, b.url, b.captured_at, b.domain, "
            "       e.summary, e.tags "
            "FROM bookmarks b "
            "LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
            "WHERE b.content_visible = 1 "
            "ORDER BY b.captured_at DESC LIMIT ?",
            (limit,),
        )

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
        for r in cur.fetchall()
    ]

    return {
        "prompt": prompt,
        "filter_query": filter_query,
        "bookmarks": bookmarks,
    }


def write_output(
    conn: sqlite3.Connection,
    output: dict[str, Any],
    run: SynthesisRun,
    *,
    prompt: str,
    filter_query: str | None = None,
    title: str | None = None,
    materials: dict[str, Any] | None = None,
) -> None:
    """Append a row to `analyses` (append-only)."""
    meta = output.get("synthesis_metadata", {}) or {}
    cur = conn.cursor()
    cur.execute(
        """
        INSERT INTO analyses (
            title, prompt, filter_query, narrative,
            enrichment_model, enrichment_prompt_version
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            title or output.get("title") or _derive_title(prompt),
            prompt,
            filter_query,
            output.get("narrative"),
            run.enrichment_model or meta.get("model"),
            run.enrichment_prompt_version or meta.get("prompt_version"),
        ),
    )


def _derive_title(prompt: str) -> str:
    snippet = prompt.strip().splitlines()[0] if prompt.strip() else "ad-hoc analysis"
    return snippet[:200]
