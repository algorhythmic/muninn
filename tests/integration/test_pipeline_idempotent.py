"""Pipeline-level integration tests over the canonical schema.

Pinned behaviors:

1. ``enrich_all`` enriches every eligible bookmark (content_visible=1
   AND enrichment_source NOT NULL/'none').
2. Re-running on unchanged content makes zero API calls — the
   ``(model, prompt_version, content_hash)`` triple gate fires for every row.
3. NOT NULL columns (model, prompt_version, content_hash, enriched_at)
   are populated on every row.
4. Cache hit rate ≥ 80% after the first call in a bulk pass — the PRD
   target for prompt caching.
5. The canonical scrape pass per bookmark is the row whose
   ``scrape_results.pass`` matches ``bookmarks.enrichment_source``
   ('live_fallback' maps to the 'live' pass).
"""

from __future__ import annotations

import sqlite3
import time
from unittest.mock import MagicMock, patch

import pytest

from muninn.config import HAIKU_MODEL, PER_BOOKMARK_PROMPT_VERSION
from muninn.enrich.haiku import EnrichmentResult
from muninn.enrich.pipeline import EnrichmentStats, enrich_all


# ── Fixtures ─────────────────────────────────────────────────────────


def _insert_bookmark(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    title: str,
    enrichment_source: str | None,
    content_visible: int = 1,
) -> None:
    conn.execute(
        """
        INSERT INTO bookmarks (
            bookmark_id, source, source_id, captured_at,
            title, url, content_visible, enrichment_source
        ) VALUES (?, 'netscape', ?, ?, ?, ?, ?, ?)
        """,
        (
            bookmark_id,
            f"src-{bookmark_id}",
            int(time.time()) - 86400,
            title,
            f"https://example.com/{bookmark_id}",
            content_visible,
            enrichment_source,
        ),
    )


def _insert_scrape(
    conn: sqlite3.Connection,
    *,
    bookmark_id: int,
    pass_: str,
    content_text: str,
    scrape_status: str = "ok",
) -> None:
    conn.execute(
        """
        INSERT INTO scrape_results (
            bookmark_id, pass, fetched_at, scrape_status,
            extraction_quality, content_text, final_url
        ) VALUES (?, ?, ?, ?, 'ok', ?, ?)
        """,
        (
            bookmark_id,
            pass_,
            int(time.time()),
            scrape_status,
            content_text,
            f"https://example.com/{bookmark_id}",
        ),
    )


@pytest.fixture
def seeded_db(fresh_db: sqlite3.Connection) -> sqlite3.Connection:
    """5 eligible bookmarks + 2 ineligible (hidden, source=none).

    Coverage of the pass-mapping:
      - bookmarks 1-3 use 'at_capture'
      - bookmark 4 uses 'recent_archive'
      - bookmark 5 uses 'live_fallback' → joins to the 'live' pass
    """
    # Eligible: at_capture
    for i in (1, 2, 3):
        _insert_bookmark(
            fresh_db,
            bookmark_id=i,
            title=f"Title {i}",
            enrichment_source="at_capture",
        )
        _insert_scrape(
            fresh_db,
            bookmark_id=i,
            pass_="at_capture",
            content_text=f"Content about topic {i}",
        )

    # Eligible: recent_archive
    _insert_bookmark(
        fresh_db,
        bookmark_id=4,
        title="Title 4",
        enrichment_source="recent_archive",
    )
    _insert_scrape(
        fresh_db,
        bookmark_id=4,
        pass_="recent_archive",
        content_text="Content about topic 4",
    )

    # Eligible: live_fallback → joins on pass='live'
    _insert_bookmark(
        fresh_db,
        bookmark_id=5,
        title="Title 5",
        enrichment_source="live_fallback",
    )
    _insert_scrape(
        fresh_db,
        bookmark_id=5,
        pass_="live",
        content_text="Content about topic 5",
    )

    # Ineligible: content_visible=0
    _insert_bookmark(
        fresh_db,
        bookmark_id=6,
        title="Hidden",
        enrichment_source="at_capture",
        content_visible=0,
    )
    _insert_scrape(
        fresh_db,
        bookmark_id=6,
        pass_="at_capture",
        content_text="Hidden content",
    )

    # Ineligible: enrichment_source = 'none'
    _insert_bookmark(
        fresh_db,
        bookmark_id=7,
        title="No source",
        enrichment_source="none",
    )
    # No scrape row for #7 — eligibility filter excludes it before the join.

    fresh_db.commit()
    return fresh_db


# ── Mock helpers ─────────────────────────────────────────────────────


def _mock_enrich_factory(*, cache_hit: bool = True):
    """Build a side-effect that returns deterministic EnrichmentResults."""

    def _impl(title, content, client=None):
        return EnrichmentResult(
            summary=f"Summary of {title}",
            tags=["tag1", "tag2"],
            entities=["Entity1"],
            content_type="article",
            language="en",
            cache_hit=cache_hit,
            input_tokens=100,
            cache_read_tokens=80 if cache_hit else 0,
            cache_creation_tokens=0 if cache_hit else 50,
            output_tokens=120,
        )

    return _impl


# ── Tests ────────────────────────────────────────────────────────────


class TestPipelineEligibility:
    """Eligibility filter and pass-mapping."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_only_eligible_enriched(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        stats = enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        assert stats.total == 5
        assert stats.enriched == 5
        # Hidden + source=none bookmarks must not be touched.
        for bid in (6, 7):
            row = seeded_db.execute(
                "SELECT 1 FROM enriched WHERE bookmark_id = ?", (bid,)
            ).fetchone()
            assert row is None

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_live_fallback_pulls_from_live_pass(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        # The mock records the (title, content) it was called with.
        captured = {}

        def _capture(title, content, client=None):
            captured[title] = content
            return _mock_enrich_factory()(title, content, client)

        mock_enrich.side_effect = _capture
        enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        # bookmark 5 is enrichment_source='live_fallback' but its scrape
        # row is pass='live'. Verify the join wired up correctly.
        assert captured["Title 5"] == "Content about topic 5"


class TestIdempotentRerun:
    """SC: re-run = no-op (zero API calls)."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_second_run_zero_api_calls(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()

        # First run — all 5 enriched.
        stats1 = enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        assert stats1.enriched == 5
        assert stats1.api_calls == 5

        # Second run — same content, same model, same prompt version.
        mock_enrich.reset_mock()
        stats2 = enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        assert stats2.api_calls == 0
        assert stats2.skipped_idempotent == 5
        mock_enrich.assert_not_called()

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_content_change_invalidates_skip(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        enrich_all(seeded_db, client=MagicMock(), qdrant=None)

        # Mutate one bookmark's canonical scrape text.
        seeded_db.execute(
            "UPDATE scrape_results SET content_text = 'NEW CONTENT' "
            "WHERE bookmark_id = 1 AND pass = 'at_capture'"
        )
        seeded_db.commit()

        mock_enrich.reset_mock()
        stats = enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        # Only bookmark 1 should re-enrich; rest skip.
        assert stats.api_calls == 1
        assert stats.skipped_idempotent == 4


class TestNotNullEnforcement:
    """SC: enrichment_model, enrichment_prompt_version, content_hash NOT NULL."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_all_required_columns_populated(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        enrich_all(seeded_db, client=MagicMock(), qdrant=None)

        rows = seeded_db.execute(
            """
            SELECT bookmark_id, enrichment_model, enrichment_prompt_version,
                   content_hash, enriched_at, language, word_count, deep_pass_requested
            FROM   enriched
            """
        ).fetchall()
        assert len(rows) == 5
        for row in rows:
            assert row["enrichment_model"] == HAIKU_MODEL
            assert row["enrichment_prompt_version"] == PER_BOOKMARK_PROMPT_VERSION
            assert row["content_hash"] is not None
            assert len(row["content_hash"]) == 64  # SHA-256 hex
            assert row["enriched_at"] is not None
            assert row["enriched_at"] > 0
            assert row["language"] == "en"
            assert row["word_count"] is not None
            assert row["deep_pass_requested"] == 0

    def test_schema_rejects_null_via_direct_insert(
        self, fresh_db: sqlite3.Connection
    ) -> None:
        """Belt-and-braces: schema itself enforces NOT NULL."""
        _insert_bookmark(
            fresh_db,
            bookmark_id=1,
            title="t",
            enrichment_source="at_capture",
        )
        fresh_db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            fresh_db.execute(
                """
                INSERT INTO enriched (
                    bookmark_id, enrichment_model, enrichment_prompt_version,
                    content_hash, enriched_at
                ) VALUES (1, NULL, 'v', 'h', 1)
                """
            )


class TestCacheHitRateScenario:
    """Cache hit rate ≥ 80% on a bulk pass after warmup."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_warmup_then_cached_calls(
        self, mock_enrich, fresh_db: sqlite3.Connection
    ) -> None:
        # Seed 100 eligible bookmarks for a credible bulk-pass scenario.
        for i in range(1, 101):
            _insert_bookmark(
                fresh_db,
                bookmark_id=i,
                title=f"Title {i}",
                enrichment_source="at_capture",
            )
            _insert_scrape(
                fresh_db,
                bookmark_id=i,
                pass_="at_capture",
                content_text=f"Body {i}",
            )
        fresh_db.commit()

        # First call is a cache miss (prompt write); rest hit.
        call_count = {"n": 0}

        def _warmup(title, content, client=None):
            call_count["n"] += 1
            is_first = call_count["n"] == 1
            return EnrichmentResult(
                summary=f"Summary of {title}",
                tags=["t"],
                entities=["E"],
                content_type="article",
                language="en",
                cache_hit=not is_first,
                input_tokens=100,
                cache_read_tokens=0 if is_first else 80,
                cache_creation_tokens=50 if is_first else 0,
                output_tokens=120,
            )

        mock_enrich.side_effect = _warmup
        stats = enrich_all(fresh_db, client=MagicMock(), qdrant=None)

        assert stats.api_calls == 100
        assert stats.cache_misses == 1
        assert stats.cache_hits == 99
        # 99/100 = 0.99 — well above the 80% PRD threshold.
        assert stats.cache_hit_rate >= 0.80

    def test_cache_hit_rate_property(self) -> None:
        s = EnrichmentStats()
        s.api_calls = 100
        s.cache_hits = 85
        assert s.cache_hit_rate == 0.85
        assert s.summary()["cache_hit_rate"] == 0.85

    def test_cache_hit_rate_zero_when_no_calls(self) -> None:
        s = EnrichmentStats()
        assert s.cache_hit_rate == 0.0


class TestQdrantBestEffort:
    """Qdrant unavailability never fails the enrich pass."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_qdrant_disabled(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        # qdrant=None — explicitly skip Qdrant writes.
        stats = enrich_all(seeded_db, client=MagicMock(), qdrant=None)
        assert stats.enriched == 5
        assert stats.qdrant_writes == 0
        assert stats.qdrant_skipped == 5

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_qdrant_upsert_failure_does_not_fail_enrich(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        # Qdrant client present but upsert always fails.
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock(
            collections=[MagicMock(name="muninn_bookmarks")]
        )
        mock_qdrant.upsert.side_effect = Exception("Qdrant down")
        mock_qdrant.get_collection.return_value = MagicMock(points_count=0)

        stats = enrich_all(seeded_db, client=MagicMock(), qdrant=mock_qdrant)
        assert stats.enriched == 5
        assert stats.qdrant_writes == 0
        assert stats.qdrant_skipped == 5
        assert stats.errors == 0  # Qdrant failures don't count as errors.

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_qdrant_writes_keyed_by_bookmark_id(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        mock_qdrant = MagicMock()
        mock_qdrant.get_collections.return_value = MagicMock(
            collections=[MagicMock(name="muninn_bookmarks")]
        )
        mock_qdrant.upsert.return_value = True
        mock_qdrant.get_collection.return_value = MagicMock(points_count=5)

        enrich_all(seeded_db, client=MagicMock(), qdrant=mock_qdrant)

        # Every upsert was a single point keyed by bookmark_id.
        ids = set()
        for call in mock_qdrant.upsert.call_args_list:
            points = call.kwargs["points"]
            for p in points:
                ids.add(p.id)
        assert ids == {1, 2, 3, 4, 5}


class TestFtsSync:
    """The contentless FTS5 index is kept in sync with enriched rows."""

    @patch("muninn.enrich.pipeline.enrich_bookmark")
    def test_fts_populated(
        self, mock_enrich, seeded_db: sqlite3.Connection
    ) -> None:
        mock_enrich.side_effect = _mock_enrich_factory()
        enrich_all(seeded_db, client=MagicMock(), qdrant=None)

        rows = seeded_db.execute(
            "SELECT rowid FROM fts_bookmarks WHERE fts_bookmarks MATCH 'Summary'"
        ).fetchall()
        assert len(rows) == 5
