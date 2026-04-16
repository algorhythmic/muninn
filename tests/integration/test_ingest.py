"""End-to-end ingest tests against the canonical schema.

Covers:
  - parse + sanitize + upsert round-trip
  - idempotency via SHA-256 over `SELECT * FROM bookmarks ORDER BY bookmark_id`
  - dangerous params absent from stored URLs
  - redaction metadata correctly populated
  - domain policy gates `content_visible`
  - era_label derived from the top folder
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from muninn.ingest import ingest_html
from muninn.ingest.pipeline import SOURCE_NAME
from muninn.sanitize.rules import DANGEROUS_PARAM_NAMES
from muninn.scrape.domain_policy import DomainPolicy

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


# ── helpers ─────────────────────────────────────────────────────────────

def _row_signature(conn: sqlite3.Connection) -> str:
    cur = conn.execute("SELECT * FROM bookmarks ORDER BY bookmark_id")
    rows = [tuple(r) for r in cur.fetchall()]
    return hashlib.sha256(repr(rows).encode("utf-8")).hexdigest()


@pytest.fixture
def policy() -> DomainPolicy:
    """Test policy: chase.example matches by domain; *.evil-tracker.example
    matches by domain glob."""
    return DomainPolicy(
        domain_patterns=("chase.example", "*.evil-tracker.example"),
        path_patterns=(),
    )


# ═══════════════════════════════════════════════════════════════════════
# Parse + insert basics
# ═══════════════════════════════════════════════════════════════════════

class TestBasicIngest:
    def test_ingest_returns_expected_count(self, fresh_db, policy):
        stats = ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        # 8 top-folder + 6 Development + 5 Research = 19
        assert stats.parsed == 19
        assert stats.inserted_or_updated == 19

    def test_ingest_populates_canonical_columns(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        cur = fresh_db.execute(
            "SELECT source, source_id, captured_at, title, url, "
            "folder_path, era_label, domain, content_visible "
            "FROM bookmarks WHERE title = 'Example'"
        )
        row = cur.fetchone()
        assert row is not None
        assert row["source"] == SOURCE_NAME
        assert isinstance(row["source_id"], str) and len(row["source_id"]) == 64
        assert row["captured_at"] == 1609459200
        assert row["url"].startswith("https://www.example.com/")
        assert row["era_label"] == "Jan 1"
        folder = json.loads(row["folder_path"])
        assert folder == ["Jan 1"]
        assert row["domain"] == "www.example.com"
        assert row["content_visible"] == 1

    def test_nested_folder_path_recorded(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        cur = fresh_db.execute(
            "SELECT folder_path, era_label FROM bookmarks WHERE title = 'MDN Web Docs'"
        )
        row = cur.fetchone()
        assert json.loads(row["folder_path"]) == ["Jan 1", "Development"]
        assert row["era_label"] == "Jan 1"


# ═══════════════════════════════════════════════════════════════════════
# Idempotency — re-running yields identical bookmark_ids and row content.
# ═══════════════════════════════════════════════════════════════════════

class TestIdempotentIngest:
    def test_re_ingest_same_count(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        c1 = fresh_db.execute("SELECT count(*) FROM bookmarks").fetchone()[0]

        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        c2 = fresh_db.execute("SELECT count(*) FROM bookmarks").fetchone()[0]

        assert c1 == c2 == 19

    def test_re_ingest_identical_row_signature(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        sig1 = _row_signature(fresh_db)

        # Wipe ingested_at non-determinism by freezing it via direct UPDATE,
        # then re-ingest. The upsert leaves `ingested_at` untouched (it's not
        # in the SET clause), so signatures must match.
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        sig2 = _row_signature(fresh_db)
        assert sig1 == sig2

    def test_bookmark_ids_stable_across_runs(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        ids1 = {
            row["source_id"]: row["bookmark_id"]
            for row in fresh_db.execute(
                "SELECT bookmark_id, source_id FROM bookmarks"
            )
        }
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        ids2 = {
            row["source_id"]: row["bookmark_id"]
            for row in fresh_db.execute(
                "SELECT bookmark_id, source_id FROM bookmarks"
            )
        }
        assert ids1 == ids2


# ═══════════════════════════════════════════════════════════════════════
# Leakage gate — no DANGEROUS_PARAM_NAMES survive in stored URLs.
# ═══════════════════════════════════════════════════════════════════════

class TestNoDangerousParams:
    def test_no_dangerous_params_in_main_fixture(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks.html", fresh_db, policy)
        for (url,) in fresh_db.execute("SELECT url FROM bookmarks WHERE url IS NOT NULL"):
            for param in DANGEROUS_PARAM_NAMES:
                assert f"{param}=" not in url.lower(), (
                    f"Dangerous param '{param}' found in URL: {url}"
                )

    def test_no_dangerous_params_in_redaction_fixture(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        for (url,) in fresh_db.execute("SELECT url FROM bookmarks WHERE url IS NOT NULL"):
            for param in DANGEROUS_PARAM_NAMES:
                assert f"{param}=" not in url.lower(), (
                    f"Dangerous param '{param}' found in URL: {url}"
                )


# ═══════════════════════════════════════════════════════════════════════
# Redaction metadata populated correctly.
# ═══════════════════════════════════════════════════════════════════════

class TestRedactionMetadata:
    def test_redacted_param_count_and_names(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT redacted_param_count, redacted_param_names "
            "FROM bookmarks WHERE title = 'Token in query'"
        ).fetchone()
        assert row is not None
        assert row["redacted_param_count"] == 1
        assert json.loads(row["redacted_param_names"]) == ["token"]

    def test_multiple_redacted_params(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT redacted_param_count, redacted_param_names "
            "FROM bookmarks WHERE title = 'Multiple dangerous params'"
        ).fetchone()
        assert row["redacted_param_count"] == 2
        names = json.loads(row["redacted_param_names"])
        assert "api_key" in names
        assert "session_id" in names

    @pytest.mark.parametrize(
        "title", ["Slack webhook", "Discord webhook", "Telegram bot"]
    )
    def test_path_redacted_for_webhooks(self, fresh_db, policy, title):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT path_redacted FROM bookmarks WHERE title = ?", (title,)
        ).fetchone()
        assert row is not None and row["path_redacted"] == 1

    def test_userinfo_recorded_in_source_metadata(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT source_metadata FROM bookmarks WHERE title = 'Userinfo URL'"
        ).fetchone()
        assert row is not None
        meta = json.loads(row["source_metadata"])
        assert meta.get("userinfo_redacted") is True


# ═══════════════════════════════════════════════════════════════════════
# Domain policy → content_visible
# ═══════════════════════════════════════════════════════════════════════

class TestDomainPolicy:
    def test_banned_domain_hidden(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT content_visible FROM bookmarks WHERE title = 'Banned domain'"
        ).fetchone()
        assert row is not None
        assert row["content_visible"] == 0

    def test_evil_tracker_subdomain_hidden(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT content_visible FROM bookmarks WHERE title = 'Evil tracker subdomain'"
        ).fetchone()
        assert row is not None
        assert row["content_visible"] == 0

    def test_normal_domain_visible(self, fresh_db, policy):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, policy)
        row = fresh_db.execute(
            "SELECT content_visible FROM bookmarks WHERE title = 'Clean URL'"
        ).fetchone()
        assert row is not None
        assert row["content_visible"] == 1

    def test_empty_policy_keeps_everything_visible(self, fresh_db):
        ingest_html(FIXTURES / "bookmarks_redaction.html", fresh_db, DomainPolicy.empty())
        cur = fresh_db.execute(
            "SELECT count(*) FROM bookmarks WHERE content_visible = 0"
        ).fetchone()
        assert cur[0] == 0
