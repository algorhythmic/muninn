"""Idempotency triple gate behavior.

Re-runs of ``enrich_all`` must be no-ops on unchanged content. The gate
that enforces this is the
``(enrichment_model, enrichment_prompt_version, content_hash)`` triple.
These tests pin the gate's behavior on each axis independently.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from muninn.config import HAIKU_MODEL, PER_BOOKMARK_PROMPT_VERSION
from muninn.enrich.idempotency import (
    IdempotencyTriple,
    compute_content_hash,
    get_existing_triple,
    would_skip,
)


# ── compute_content_hash ─────────────────────────────────────────────


class TestComputeContentHash:
    def test_known_value(self) -> None:
        # Anchors the hash to SHA-256 of the UTF-8 bytes of the input.
        # If this changes, every cached enrichment row would be invalidated.
        expected = (
            "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
        )
        assert compute_content_hash("hello world") == expected

    def test_deterministic(self) -> None:
        assert compute_content_hash("foo bar") == compute_content_hash("foo bar")

    def test_different_text_different_hash(self) -> None:
        assert compute_content_hash("foo") != compute_content_hash("bar")

    def test_none_treated_as_empty(self) -> None:
        # None is total here so callers can hash without pre-checking.
        assert compute_content_hash(None) == compute_content_hash("")

    def test_unicode_handled(self) -> None:
        # Don't crash on emoji or non-ASCII.
        h = compute_content_hash("héllo 🌐 wörld")
        assert len(h) == 64


# ── DB-backed gate ───────────────────────────────────────────────────


def _seed_bookmark(conn: sqlite3.Connection, bookmark_id: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO bookmarks (
            bookmark_id, source, source_id, captured_at, title, url
        ) VALUES (?, 'netscape', ?, ?, 'Title', 'https://example.com/')
        """,
        (bookmark_id, f"src-{bookmark_id}", int(time.time())),
    )
    conn.commit()


def _insert_enriched(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    model: str = HAIKU_MODEL,
    prompt_version: str = PER_BOOKMARK_PROMPT_VERSION,
    content_hash: str = "abc123",
) -> None:
    conn.execute(
        """
        INSERT INTO enriched (
            bookmark_id, summary, tags, entities, content_type, language,
            word_count, enrichment_model, enrichment_prompt_version,
            content_hash, enriched_at, deep_pass_requested, key_quotes
        ) VALUES (?, 's', '[]', '[]', 'article', 'en', 0, ?, ?, ?, ?, 0, NULL)
        """,
        (bookmark_id, model, prompt_version, content_hash, int(time.time())),
    )
    conn.commit()


class TestGetExistingTriple:
    def test_returns_none_when_no_row(self, fresh_db: sqlite3.Connection) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        assert get_existing_triple(fresh_db, 1) is None

    def test_returns_triple_when_row_exists(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        _insert_enriched(fresh_db, bookmark_id=1, content_hash="hash-abc")
        triple = get_existing_triple(fresh_db, 1)
        assert triple is not None
        assert triple.enrichment_model == HAIKU_MODEL
        assert triple.enrichment_prompt_version == PER_BOOKMARK_PROMPT_VERSION
        assert triple.content_hash == "hash-abc"


class TestWouldSkip:
    """All three axes of the triple must match for ``would_skip`` to be True."""

    def _candidate(self, **overrides) -> IdempotencyTriple:
        defaults = dict(
            enrichment_model=HAIKU_MODEL,
            enrichment_prompt_version=PER_BOOKMARK_PROMPT_VERSION,
            content_hash="hash-abc",
        )
        defaults.update(overrides)
        return IdempotencyTriple(**defaults)

    def test_no_existing_row_does_not_skip(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        assert not would_skip(fresh_db, 1, self._candidate())

    def test_full_match_skips(self, fresh_db: sqlite3.Connection) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        _insert_enriched(fresh_db, bookmark_id=1, content_hash="hash-abc")
        assert would_skip(fresh_db, 1, self._candidate())

    def test_different_model_does_not_skip(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        _insert_enriched(fresh_db, bookmark_id=1, model="claude-opus-4-6")
        assert not would_skip(fresh_db, 1, self._candidate())

    def test_different_prompt_version_does_not_skip(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        _insert_enriched(fresh_db, bookmark_id=1, prompt_version="per_bookmark_v2")
        assert not would_skip(fresh_db, 1, self._candidate())

    def test_different_hash_does_not_skip(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        _seed_bookmark(fresh_db, bookmark_id=1)
        _insert_enriched(fresh_db, bookmark_id=1, content_hash="hash-different")
        assert not would_skip(fresh_db, 1, self._candidate())

    def test_other_bookmark_row_does_not_satisfy_skip(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        # Existing enrichment for bookmark 1 must NOT cause bookmark 2 to skip.
        _seed_bookmark(fresh_db, bookmark_id=1)
        _seed_bookmark(fresh_db, bookmark_id=2)
        _insert_enriched(fresh_db, bookmark_id=1, content_hash="hash-abc")
        assert not would_skip(fresh_db, 2, self._candidate())


class TestIdempotencyTriple:
    def test_equality(self) -> None:
        a = IdempotencyTriple("m", "v", "h")
        b = IdempotencyTriple("m", "v", "h")
        c = IdempotencyTriple("m", "v", "h2")
        assert a == b
        assert a != c

    def test_frozen(self) -> None:
        t = IdempotencyTriple("m", "v", "h")
        with pytest.raises(Exception):
            t.content_hash = "other"  # type: ignore[misc]
