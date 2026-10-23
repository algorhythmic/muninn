"""Draft migration 002 (Research Intake) against a 001 database.

No runtime path applies 002 yet. These tests apply it the way the migrator
will (statement by statement inside one immediate transaction) and check
that it is additive: 001's tables, indexes and rows are unchanged, and the
integrity and foreign-key checks pass. They also pin the stage-native rules
the contract relies on (revision dedup, append-only keys, SET NULL links,
X post text reachable by the existing enrich query).
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from muninn.consumers.packets import content_digest, link_item_id, locator_source_id, new_ulid

ROOT = Path(__file__).resolve().parents[2]
SQL_001 = ROOT / "schemas" / "sql" / "001_initial.sql"
SQL_002 = ROOT / "schemas" / "sql" / "002_research_intake.sql"
EMPTY = content_digest("")


def statements(sql: str) -> list[str]:
    """Split a migration body the way a non-executescript migrator would."""
    out, buf = [], ""
    for line in sql.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                out.append(buf.strip())
            buf = ""
    assert not buf.strip(), f"trailing partial statement: {buf!r}"
    return out


def apply_002(conn: sqlite3.Connection) -> None:
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        for stmt in statements(SQL_002.read_text()):
            conn.execute(stmt)
        conn.execute("PRAGMA user_version = 2")
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def db_001(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SQL_001.read_text())
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def master(conn: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    return {
        (r["type"], r["name"]): r["sql"]
        for r in conn.execute("SELECT type, name, sql FROM sqlite_master")
    }


def seed_001(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, title, url, domain, enrichment_source) "
        "VALUES ('bookmarks_html', 'h1', 1551734100, 'Synthetic page', 'https://example.org/p', 'example.org', 'at_capture')"
    )
    conn.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, scrape_status, content_text) "
        "VALUES (1, 'at_capture', 1551734200, 'ok', 'synthetic text')"
    )
    conn.execute(
        "INSERT INTO enriched (bookmark_id, summary, enrichment_model, enrichment_prompt_version, content_hash, enriched_at) "
        "VALUES (1, 's', 'm', 'v1', 'h', 1551734300)"
    )
    conn.execute("INSERT INTO fts_bookmarks (rowid, title, summary, content_text, tags) VALUES (1, 'Synthetic page', 's', 'synthetic text', '')")
    conn.commit()


# ── Additivity ───────────────────────────────────────────────────────

def test_002_is_a_bare_migration_body() -> None:
    stmts = statements(SQL_002.read_text())
    assert stmts
    for stmt in stmts:
        body = "\n".join(l for l in stmt.splitlines() if not l.strip().startswith("--")).strip()
        assert body.split()[0].upper() == "CREATE", body[:60]
    text = SQL_002.read_text()
    code = "\n".join(l.split("--", 1)[0] for l in text.splitlines()).upper()
    for word in ("BEGIN", "COMMIT", "ROLLBACK", "END", "PRAGMA", "DROP", "ALTER", "TRIGGER", "VIRTUAL"):
        assert not re.search(rf"\b{word}\b", code), word
    assert "contentless" not in text.lower()
    # Every semicolon ends a line, so naive and complete_statement splitting agree.
    assert all(line.rstrip().endswith(";") for line in text.splitlines() if ";" in line)


def test_002_leaves_001_unchanged_and_checks_pass() -> None:
    conn = db_001()
    seed_001(conn)
    before = master(conn)
    counts = {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
              for t in ("bookmarks", "scrape_results", "enriched", "fts_bookmarks")}
    apply_002(conn)
    after = master(conn)
    for key, sql in before.items():
        assert after[key] == sql, key
    new = {name for (kind, name) in after if (kind, name) not in before and kind == "table"}
    assert new == {"item_links", "scrape_history", "interest_profiles", "assessments",
                   "evidence_packets", "sync_checkpoints", "usage_records"}
    for t, n in counts.items():
        assert conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == n
    assert conn.execute("SELECT * FROM fts_bookmarks WHERE fts_bookmarks MATCH 'synthetic'").fetchone()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 2


def test_executescript_and_statement_paths_agree() -> None:
    a = db_001()
    apply_002(a)
    b = db_001()
    b.executescript("BEGIN IMMEDIATE;\n" + SQL_002.read_text() + "\nCOMMIT;")
    assert master(a) == master(b)


def test_002_does_not_apply_twice() -> None:
    conn = db_001()
    apply_002(conn)
    with pytest.raises(sqlite3.OperationalError):
        apply_002(conn)
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE name = 'scrape_history'").fetchone()[0] == 1


def test_duckdb_attach_reads_the_new_tables(tmp_path: Path) -> None:
    pytest.importorskip("duckdb")
    from muninn.db import duckdb_attached, migrate

    path = tmp_path / "m.db"
    conn = db_001(str(path))
    seed_001(conn)
    apply_002(conn)
    conn.close()
    migrate(path)  # any later migrations, so the reader's version check passes
    con = duckdb_attached(path)
    try:
        assert con.execute("SELECT count(*) FROM sqlite_db.bookmarks").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM sqlite_db.scrape_history").fetchone()[0] == 0
    finally:
        con.close()


# ── Stage-native behaviour ───────────────────────────────────────────

POST_ID = "1900000000000000001"
POST_REF = f"x:post:{POST_ID}"
LINK = "https://example.com/articles/synthetic-retrieval-notes"
LINK_REF = link_item_id(LINK)


@pytest.fixture
def intake() -> sqlite3.Connection:
    conn = db_001()
    apply_002(conn)
    conn.isolation_level = ""
    conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, url, domain, source_metadata, enrichment_source) "
        "VALUES ('x', ?, 1760947200, ?, 'x.com', json('{\"author_id\":\"1900000000000000901\"}'), 'live_fallback')",
        (POST_ID, f"https://x.com/synth_author_a/status/{POST_ID}"),
    )
    conn.execute(
        "INSERT INTO bookmarks (source, source_id, captured_at, url, domain) VALUES ('x_link', ?, 1760947200, ?, 'example.com')",
        (locator_source_id(LINK), LINK),
    )
    conn.execute(
        "INSERT INTO item_links (parent_bookmark_id, child_bookmark_id, parent_ref, child_ref, relation, position, locator_resolution, first_seen_at) "
        "VALUES (1, 2, ?, ?, 'links_to', 0, 'unwound', 1760947200)",
        (POST_REF, LINK_REF),
    )
    conn.commit()
    return conn


def add_revision(conn, item_ref, text, coverage="full", method="live", bookmark_id=2, ignore=False) -> int | None:
    verb = "INSERT OR IGNORE" if ignore else "INSERT"
    cur = conn.execute(
        f"{verb} INTO scrape_history (revision_ulid, bookmark_id, item_ref, content_sha256, normalization, char_count, "
        "content_text, coverage, retrieval_method, extractor_name, extractor_version, retrieved_at) "
        "VALUES (?, ?, ?, ?, 'muninn-text/1', ?, ?, ?, ?, 'synthetic', '1', 1760947300)",
        (new_ulid(), bookmark_id, item_ref, content_digest(text), len(text), text or None, coverage, method),
    )
    return cur.lastrowid if cur.rowcount else None


def add_profile(conn, digest: str) -> None:
    conn.execute(
        "INSERT INTO interest_profiles (profile_sha256, profile_id, body_json, first_seen_at) VALUES (?, 'p', '{}', 1)",
        (digest,),
    )


def add_assessment(conn, rev_id, profile, coverage="full", disposition="candidate", ref=LINK_REF, bookmark_id=2):
    conn.execute(
        "INSERT INTO assessments (assessment_ulid, scrape_history_id, bookmark_id, item_ref, content_sha256, content_coverage, "
        "context_digest, context_json, profile_sha256, policy_version, model, prompt_version, disposition, relevance, "
        "confidence, rationale, assessed_at) "
        "SELECT ?, scrape_history_id, ?, item_ref, content_sha256, ?, ?, '[]', ?, 'intake-policy/1', 'synthetic-model', "
        "'suitability_v1', ?, 50, 50, 'r', 1760947400 "
        "FROM scrape_history WHERE scrape_history_id = ?",
        (new_ulid(), bookmark_id, coverage, "c" * 64, profile, disposition, rev_id),
    )


def test_unchanged_content_never_creates_a_revision(intake) -> None:
    first = add_revision(intake, LINK_REF, "synthetic body")
    assert first is not None
    assert add_revision(intake, LINK_REF, "synthetic body", ignore=True) is None
    with pytest.raises(sqlite3.IntegrityError):
        add_revision(intake, LINK_REF, "synthetic body")
    assert add_revision(intake, LINK_REF, "synthetic body, changed") is not None
    assert intake.execute("SELECT count(*) FROM scrape_history").fetchone()[0] == 2


def test_coverage_rules(intake) -> None:
    assert add_revision(intake, LINK_REF, "", coverage="blocked") is not None
    assert add_revision(intake, LINK_REF, "", coverage="deleted") is not None
    with pytest.raises(sqlite3.IntegrityError):
        add_revision(intake, LINK_REF, "Please log in", coverage="blocked")
    with pytest.raises(sqlite3.IntegrityError):
        add_revision(intake, LINK_REF, "", coverage="full")
    with pytest.raises(sqlite3.IntegrityError):
        add_revision(intake, LINK_REF, "x", method="playwright")


def test_assessments_append_per_profile_and_refuse_blocked_candidates(intake) -> None:
    rev = add_revision(intake, LINK_REF, "synthetic body")
    add_profile(intake, "a" * 64)
    add_profile(intake, "b" * 64)
    add_assessment(intake, rev, "a" * 64)
    add_assessment(intake, rev, "b" * 64)  # changed profile: new row
    with pytest.raises(sqlite3.IntegrityError):
        add_assessment(intake, rev, "a" * 64)  # same key
    with pytest.raises(sqlite3.IntegrityError):
        add_assessment(intake, rev, "c" * 64)  # unknown profile revision
    blocked = add_revision(intake, LINK_REF, "", coverage="blocked")
    with pytest.raises(sqlite3.IntegrityError):
        add_assessment(intake, blocked, "a" * 64, coverage="blocked", disposition="candidate")
    add_assessment(intake, blocked, "a" * 64, coverage="blocked", disposition="needs_evidence")
    with pytest.raises(sqlite3.IntegrityError):
        add_assessment(intake, rev, "a" * 64, disposition="blocked")
    assert intake.execute("SELECT count(*) FROM assessments").fetchone()[0] == 3


def test_history_survives_a_deleted_bookmark(intake) -> None:
    rev = add_revision(intake, LINK_REF, "synthetic body")
    add_profile(intake, "a" * 64)
    add_assessment(intake, rev, "a" * 64)
    intake.commit()
    intake.execute("DELETE FROM bookmarks WHERE bookmark_id = 2")
    intake.commit()
    row = intake.execute("SELECT bookmark_id, item_ref, content_text FROM scrape_history").fetchone()
    assert row["bookmark_id"] is None and row["item_ref"] == LINK_REF and row["content_text"] == "synthetic body"
    assert intake.execute("SELECT bookmark_id FROM assessments").fetchone()[0] is None
    link = intake.execute("SELECT child_bookmark_id, child_ref FROM item_links").fetchone()
    assert link["child_bookmark_id"] is None and link["child_ref"] == LINK_REF
    assert intake.execute("PRAGMA foreign_key_check").fetchall() == []
    # An assessed revision cannot be deleted, only withdrawn.
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute("DELETE FROM scrape_history WHERE scrape_history_id = ?", (rev,))
    intake.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute("UPDATE scrape_history SET withdrawn_at = 1 WHERE scrape_history_id = ?", (rev,))
    intake.rollback()
    intake.execute(
        "UPDATE scrape_history SET content_text = NULL, withdrawn_at = 1, withdrawal_reason = 'operator_request' "
        "WHERE scrape_history_id = ?", (rev,),
    )


def test_links_are_explicit_and_never_self_referential(intake) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute(
            "INSERT INTO item_links (parent_ref, child_ref, relation, position, first_seen_at) VALUES (?, ?, 'links_to', 1, 1)",
            (POST_REF, POST_REF),
        )
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute(
            "INSERT INTO item_links (parent_ref, child_ref, relation, position, first_seen_at) VALUES (?, ?, 'links_to', 0, 1)",
            (POST_REF, "web:" + "0" * 64),
        )


def test_packet_bookkeeping_and_tombstones(intake) -> None:
    ulid = new_ulid()
    intake.execute(
        "INSERT INTO evidence_packets (packet_ulid, revision, packet_sha256, subject_ref, contract_version, body_json, exported_at) "
        "VALUES (?, 1, ?, ?, '0.1.0-draft', '{}', 1)", (ulid, "d" * 64, POST_REF),
    )
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute("UPDATE evidence_packets SET body_json = NULL")
    intake.execute(
        "UPDATE evidence_packets SET body_json = NULL, withdrawn_at = 2, withdrawal_reason = 'retention'"
    )
    row = intake.execute("SELECT packet_sha256, withdrawal_reason FROM evidence_packets").fetchone()
    assert tuple(row) == ("d" * 64, "retention")


def test_sync_checkpoint_and_usage_rows(intake) -> None:
    run = new_ulid()
    intake.execute(
        "INSERT INTO sync_checkpoints (run_ulid, source, account_ref, collection, status, next_token, started_at, updated_at) "
        "VALUES (?, 'x', '1900000000000000901', 'bookmarks', 'paused', 'opaque-cursor', 1, 1)", (run,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute("UPDATE sync_checkpoints SET status = 'completed'")
    intake.execute("UPDATE sync_checkpoints SET status = 'completed', completed_at = 2, next_token = NULL")
    intake.execute(
        "INSERT INTO usage_records (usage_ulid, kind, provider, operation, sync_checkpoint_id, resource_count, cost_micro_usd, cost_basis, occurred_at) "
        "VALUES (?, 'x_api', 'x', 'GET /2/users/:id/bookmarks', 1, 100, 100000, 'estimated', 1)", (new_ulid(),),
    )
    with pytest.raises(sqlite3.IntegrityError):
        intake.execute(
            "INSERT INTO usage_records (usage_ulid, kind, provider, operation, cost_micro_usd, cost_basis, occurred_at) "
            "VALUES (?, 'model', 'anthropic', 'enrich:suitability', 1, 'estimated', 1)", (new_ulid(),),
        )


def test_x_post_text_reaches_the_existing_enrich_query(intake) -> None:
    """Decision: the post's current text is its 'live' scrape_results row and
    enrichment_source is 'live_fallback', so enrich works with no rebuild."""
    from muninn.enrich.pipeline import get_eligible_bookmarks

    intake.execute(
        "INSERT INTO scrape_results (bookmark_id, pass, fetched_at, http_status, scrape_status, extraction_quality, content_text) "
        "VALUES (1, 'live', 1760947205, 200, 'ok', 'ok', 'Synthetic post text')"
    )
    add_revision(intake, POST_REF, "Synthetic post text", method="x_api", bookmark_id=1)
    rows = {r["bookmark_id"]: r for r in get_eligible_bookmarks(intake)}
    assert rows[1]["content_text"] == "Synthetic post text"
    h = intake.execute("SELECT retrieval_method, content_sha256 FROM scrape_history WHERE item_ref = ?", (POST_REF,)).fetchone()
    assert h["retrieval_method"] == "x_api" and h["content_sha256"] == content_digest("Synthetic post text")
