"""deep_pass task: per-bookmark deep analysis.

Materials passed to the container:
  - bookmark_id, title, url, captured_at
  - source_text       (canonical pass content; key_quotes verified verbatim)
  - source_pass       (which scrape pass supplied source_text)
  - candidate_neighbors: [{bookmark_id, title, summary?, tags?}, ...]
  - known_bookmark_ids: list of all bookmark IDs the model is allowed to
                        cross-reference (the candidate set + self)

DB writes (in order, single transaction):
  1. UPDATE enriched SET summary, tags, entities, content_type, language,
     word_count, key_quotes, deep_pass_requested=1, enrichment_model,
     enrichment_prompt_version, content_hash, enriched_at
  2. INSERT INTO cross_references with created_by='deep_pass' (skipped on
     UNIQUE conflict).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from typing import Any

from muninn.models import SynthesisRun


CANONICAL_PASSES_FALLBACK = ("at_capture", "recent_archive", "live", "live_fallback")


def prepare_input(
    conn: sqlite3.Connection,
    bookmark_id: int,
    *,
    candidate_neighbor_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Build materials for a deep_pass task.

    `candidate_neighbor_ids`: bookmark IDs the synthesis model is allowed to
    target in cross_references. Defaults to other bookmarks in the same era.
    """
    cur = conn.cursor()

    cur.execute(
        "SELECT bookmark_id, title, url, captured_at, era_label, "
        "       enrichment_source FROM bookmarks WHERE bookmark_id = ?",
        (bookmark_id,),
    )
    bm = cur.fetchone()
    if bm is None:
        raise LookupError(f"bookmark_id {bookmark_id} not found")

    source_text, source_pass = _canonical_content_text(conn, bookmark_id, bm["enrichment_source"])

    if candidate_neighbor_ids is None and bm["era_label"]:
        cur.execute(
            "SELECT bookmark_id FROM bookmarks "
            "WHERE era_label = ? AND bookmark_id != ? AND content_visible = 1 "
            "LIMIT 200",
            (bm["era_label"], bookmark_id),
        )
        candidate_neighbor_ids = [r["bookmark_id"] for r in cur.fetchall()]
    elif candidate_neighbor_ids is None:
        candidate_neighbor_ids = []

    neighbors: list[dict[str, Any]] = []
    if candidate_neighbor_ids:
        placeholders = ",".join("?" * len(candidate_neighbor_ids))
        cur.execute(
            f"SELECT b.bookmark_id, b.title, b.url, e.summary, e.tags "
            f"FROM bookmarks b "
            f"LEFT JOIN enriched e ON e.bookmark_id = b.bookmark_id "
            f"WHERE b.bookmark_id IN ({placeholders})",
            candidate_neighbor_ids,
        )
        for r in cur.fetchall():
            neighbors.append(
                {
                    "bookmark_id": r["bookmark_id"],
                    "title": r["title"],
                    "url": r["url"],
                    "summary": r["summary"],
                    "tags": json.loads(r["tags"]) if r["tags"] else [],
                }
            )

    known_ids = list(set(candidate_neighbor_ids) | {bookmark_id})

    return {
        "bookmark_id": bookmark_id,
        "title": bm["title"],
        "url": bm["url"],
        "captured_at": bm["captured_at"],
        "source_text": source_text or "",
        "source_pass": source_pass,
        "candidate_neighbors": neighbors,
        "known_bookmark_ids": known_ids,
    }


def write_output(
    conn: sqlite3.Connection,
    output: dict[str, Any],
    run: SynthesisRun,
    *,
    bookmark_id: int,
    materials: dict[str, Any] | None = None,
) -> None:
    """Update the enriched row and insert deep_pass cross_references.

    `created_by='deep_pass'` is injected here, never trusted from the model.
    UNIQUE(source_bookmark_id, target_bookmark_id, created_by) makes
    re-running idempotent.
    """
    materials = materials or {}
    meta = output.get("synthesis_metadata", {}) or {}
    enrichment_model = run.enrichment_model or meta.get("model") or "unknown"
    prompt_version = run.enrichment_prompt_version or meta.get("prompt_version") or "deep_pass_v1"

    source_text = materials.get("source_text", "") or ""
    content_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest() if source_text else ""

    cur = conn.cursor()

    # 1. Upsert enriched row. UPDATE the deep-pass-overridable columns.
    cur.execute(
        """
        INSERT INTO enriched (
            bookmark_id, summary, tags, entities, content_type, language,
            word_count, key_quotes, deep_pass_requested,
            enrichment_model, enrichment_prompt_version, content_hash, enriched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(bookmark_id) DO UPDATE SET
            summary                   = excluded.summary,
            tags                      = excluded.tags,
            entities                  = excluded.entities,
            content_type              = excluded.content_type,
            language                  = excluded.language,
            word_count                = excluded.word_count,
            key_quotes                = excluded.key_quotes,
            deep_pass_requested       = 1,
            enrichment_model          = excluded.enrichment_model,
            enrichment_prompt_version = excluded.enrichment_prompt_version,
            content_hash              = excluded.content_hash,
            enriched_at               = excluded.enriched_at
        """,
        (
            bookmark_id,
            output.get("summary"),
            json.dumps(output.get("tags") or []),
            json.dumps(output.get("entities") or []),
            output.get("content_type"),
            output.get("language"),
            output.get("word_count"),
            json.dumps(output.get("key_quotes") or []),
            enrichment_model,
            prompt_version,
            content_hash,
            int(time.time()),
        ),
    )

    # 2. Cross references — inject created_by='deep_pass', dedupe via UNIQUE.
    for ref in output.get("cross_references") or []:
        target_id = ref.get("target_bookmark_id")
        if target_id is None:
            continue
        cur.execute(
            """
            INSERT OR IGNORE INTO cross_references (
                source_bookmark_id, target_bookmark_id, relationship,
                rationale, created_by
            ) VALUES (?, ?, ?, ?, 'deep_pass')
            """,
            (
                bookmark_id,
                target_id,
                ref.get("relationship"),
                ref.get("rationale") or _confidence_to_rationale(ref.get("confidence")),
            ),
        )


def _canonical_content_text(
    conn: sqlite3.Connection,
    bookmark_id: int,
    enrichment_source: str | None,
) -> tuple[str | None, str | None]:
    """Pull content_text from the canonical scrape pass.

    `bookmarks.enrichment_source` records which pass was canonical
    (at_capture, recent_archive, live_fallback). live_fallback maps to the
    'live' pass row in scrape_results.
    """
    cur = conn.cursor()

    pass_priority: list[str] = []
    if enrichment_source == "at_capture":
        pass_priority = ["at_capture"]
    elif enrichment_source == "recent_archive":
        pass_priority = ["recent_archive", "at_capture"]
    elif enrichment_source == "live_fallback":
        pass_priority = ["live", "at_capture", "recent_archive"]
    else:
        pass_priority = list(CANONICAL_PASSES_FALLBACK)

    for p in pass_priority:
        cur.execute(
            "SELECT content_text FROM scrape_results "
            "WHERE bookmark_id = ? AND pass = ? AND content_text IS NOT NULL",
            (bookmark_id, p if p != "live_fallback" else "live"),
        )
        row = cur.fetchone()
        if row and row["content_text"]:
            return row["content_text"], p
    return None, None


def _confidence_to_rationale(confidence: Any) -> str | None:
    if confidence is None:
        return None
    try:
        return f"deep_pass confidence={float(confidence):.2f}"
    except (TypeError, ValueError):
        return None
