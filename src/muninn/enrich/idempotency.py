"""Idempotency gate for per-bookmark enrichment.

Re-running ``enrich_all`` must be a no-op when nothing has changed.
The mechanism is a triple keyed on the ``enriched`` row:

    (enrichment_model, enrichment_prompt_version, content_hash)

If the existing row's triple matches what *would* be written, we skip
the API call entirely. This module computes the hash and the
would-be-skipped check; the pipeline owns the surrounding loop.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class IdempotencyTriple:
    """The three columns that must all match to skip a re-enrichment."""

    enrichment_model: str
    enrichment_prompt_version: str
    content_hash: str


def compute_content_hash(content_text: str | None) -> str:
    """SHA-256 of the canonical scrape ``content_text``.

    ``None`` is treated as empty string so the hash is well-defined even
    for bookmarks whose canonical pass produced no extracted text. The
    pipeline still skips those rows from enrichment via the eligibility
    query, but the hash function itself is total.
    """
    payload = (content_text or "").encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def get_existing_triple(
    conn: sqlite3.Connection, bookmark_id: int
) -> IdempotencyTriple | None:
    """Return the idempotency triple for an existing ``enriched`` row, or None."""
    row = conn.execute(
        """
        SELECT enrichment_model,
               enrichment_prompt_version,
               content_hash
        FROM   enriched
        WHERE  bookmark_id = ?
        """,
        (bookmark_id,),
    ).fetchone()
    if row is None:
        return None
    return IdempotencyTriple(
        enrichment_model=row["enrichment_model"],
        enrichment_prompt_version=row["enrichment_prompt_version"],
        content_hash=row["content_hash"],
    )


def would_skip(
    conn: sqlite3.Connection,
    bookmark_id: int,
    candidate: IdempotencyTriple,
) -> bool:
    """True iff there is already a matching ``enriched`` row.

    Compute ``candidate`` from the inputs you would otherwise send to
    the LLM (model id, prompt version, ``compute_content_hash`` of the
    canonical scrape text), then call this *before* paying for an API
    call.
    """
    existing = get_existing_triple(conn, bookmark_id)
    if existing is None:
        return False
    return existing == candidate
