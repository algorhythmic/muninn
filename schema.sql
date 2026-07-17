-- Muninn canonical schema (v1).
-- Authoritative source: SPEC.md "Schema — concrete CREATE TABLE statements".
-- Applied at init by scripts/init-db.py against a fresh SQLite file.
-- Conventions: INTEGER PKs, epoch-second timestamps, JSON arrays in TEXT
-- columns, booleans as 0/1 INTEGER, enums as TEXT with CHECK.

PRAGMA foreign_keys = ON;

-- ── bookmarks ──────────────────────────────────────────────────────
-- Source-agnostic normalized row. Future sources (Evernote, YouTube, etc.)
-- upsert via (source, source_id) — bookmark_id is stable across re-runs.
CREATE TABLE bookmarks (
    bookmark_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT    NOT NULL,
    source_id               TEXT    NOT NULL,
    captured_at             INTEGER NOT NULL,
    title                   TEXT,
    url                     TEXT,
    folder_path             TEXT    CHECK (folder_path IS NULL OR json_valid(folder_path)),
    era_label               TEXT,
    domain                  TEXT,
    source_metadata         TEXT    CHECK (source_metadata IS NULL OR json_valid(source_metadata)),
    redacted_param_count    INTEGER NOT NULL DEFAULT 0,
    redacted_param_names    TEXT    CHECK (redacted_param_names IS NULL OR json_valid(redacted_param_names)),
    path_redacted           INTEGER NOT NULL DEFAULT 0 CHECK (path_redacted IN (0, 1)),
    content_visible         INTEGER NOT NULL DEFAULT 1 CHECK (content_visible IN (0, 1)),
    enrichment_source       TEXT,
    ingested_at             INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (source, source_id),
    CHECK (enrichment_source IS NULL OR enrichment_source IN
           ('at_capture', 'recent_archive', 'live_fallback', 'none'))
);

CREATE INDEX idx_bookmarks_captured_at ON bookmarks (captured_at);
CREATE INDEX idx_bookmarks_domain      ON bookmarks (domain);
CREATE INDEX idx_bookmarks_era_label   ON bookmarks (era_label);
CREATE INDEX idx_bookmarks_source      ON bookmarks (source);

-- ── scrape_results ─────────────────────────────────────────────────
-- One row per (bookmark_id, pass). Re-running mutates in place.
CREATE TABLE scrape_results (
    scrape_result_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bookmark_id         INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    pass                TEXT    NOT NULL,
    fetched_at          INTEGER NOT NULL,
    target_timestamp    INTEGER,
    actual_snapshot_at  INTEGER,
    archive_url         TEXT,
    final_url           TEXT,
    http_status         INTEGER,
    scrape_status       TEXT    NOT NULL,
    extraction_quality  TEXT,
    content_text        TEXT,
    content_html        TEXT,
    raw_html_path       TEXT,
    error_detail        TEXT,
    UNIQUE (bookmark_id, pass),
    CHECK (pass IN ('live', 'at_capture', 'recent_archive', 'playwright', 'manual')),
    CHECK (scrape_status IN
           ('ok', 'partial', 'failed', 'js_required', 'paywall',
            'robots_disallowed', 'no_archive', 'network_error',
            'timeout', 'auth_required')),
    CHECK (extraction_quality IS NULL OR extraction_quality IN ('ok', 'partial', 'failed'))
);

CREATE INDEX idx_scrape_results_bookmark ON scrape_results (bookmark_id);
CREATE INDEX idx_scrape_results_status   ON scrape_results (scrape_status);

-- ── enriched ───────────────────────────────────────────────────────
-- 1:1 with bookmarks. Idempotency triple:
-- (enrichment_model, enrichment_prompt_version, content_hash).
-- key_quotes column folded in here (deep-pass writes it).
CREATE TABLE enriched (
    bookmark_id                 INTEGER PRIMARY KEY REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    summary                     TEXT,
    tags                        TEXT    CHECK (tags IS NULL OR json_valid(tags)),
    entities                    TEXT    CHECK (entities IS NULL OR json_valid(entities)),
    content_type                TEXT,
    language                    TEXT,
    word_count                  INTEGER,
    enrichment_model            TEXT    NOT NULL,
    enrichment_prompt_version   TEXT    NOT NULL,
    content_hash                TEXT    NOT NULL,
    enriched_at                 INTEGER NOT NULL,
    deep_pass_requested         INTEGER NOT NULL DEFAULT 0 CHECK (deep_pass_requested IN (0, 1)),
    key_quotes                  TEXT    CHECK (key_quotes IS NULL OR json_valid(key_quotes))
);

CREATE INDEX idx_enriched_content_type ON enriched (content_type);
CREATE INDEX idx_enriched_deep_pass    ON enriched (deep_pass_requested) WHERE deep_pass_requested = 1;

-- ── eras ───────────────────────────────────────────────────────────
-- Derived per-era synthesis. Joined to bookmarks via era_label (no FK —
-- a bookmark's era_label may change if the user re-classifies a folder).
CREATE TABLE eras (
    era_label                   TEXT    PRIMARY KEY,
    inferred_year               INTEGER,
    start_date                  INTEGER,
    end_date                    INTEGER,
    bookmark_count              INTEGER NOT NULL,
    dominant_topics             TEXT    CHECK (dominant_topics IS NULL OR json_valid(dominant_topics)),
    dominant_domains            TEXT    CHECK (dominant_domains IS NULL OR json_valid(dominant_domains)),
    narrative                   TEXT,
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER
);

-- ── cross_references ───────────────────────────────────────────────
-- Bookmark-to-bookmark relationships. Asymmetric by design.
CREATE TABLE cross_references (
    cross_reference_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    target_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    relationship        TEXT,
    rationale           TEXT,
    created_by          TEXT    NOT NULL,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (source_bookmark_id, target_bookmark_id, created_by)
);

CREATE INDEX idx_cross_references_source ON cross_references (source_bookmark_id);
CREATE INDEX idx_cross_references_target ON cross_references (target_bookmark_id);

-- ── analyses ───────────────────────────────────────────────────────
-- Append-only ad-hoc cross-corpus synthesis results.
CREATE TABLE analyses (
    analysis_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title                       TEXT    NOT NULL,
    prompt                      TEXT    NOT NULL,
    filter_query                TEXT,
    narrative                   TEXT,
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER NOT NULL DEFAULT (unixepoch())
);

-- ── synthesis_runs ─────────────────────────────────────────────────
-- Audit log; one row per container launch attempt.
CREATE TABLE synthesis_runs (
    synthesis_run_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                     TEXT    NOT NULL,
    task_type                   TEXT    NOT NULL,
    attempt                     INTEGER NOT NULL,
    started_at                  INTEGER NOT NULL,
    completed_at                INTEGER,
    status                      TEXT    NOT NULL,
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    input_token_count           INTEGER,
    output_token_count          INTEGER,
    validation_errors           TEXT    CHECK (validation_errors IS NULL OR json_valid(validation_errors)),
    container_id                TEXT,
    UNIQUE (task_id, attempt),
    CHECK (task_type IN ('era_narrative', 'deep_pass', 'ad_hoc_analysis')),
    CHECK (status IN ('running', 'completed', 'validation_failed', 'container_failed', 'cap_hit'))
);

CREATE INDEX idx_synthesis_runs_status ON synthesis_runs (status);

-- ── fts_bookmarks ──────────────────────────────────────────────────
-- Regular (content-storing) FTS5 index. Application-layer sync (no
-- triggers); rowid is bookmark_id. NOT contentless: contentless tables
-- reject the DELETE that re-enrichment needs, and the newer
-- contentless_delete=1 option breaks DuckDB's sqlite attach. Duplicated
-- text is an acceptable cost at personal-corpus scale.
CREATE VIRTUAL TABLE fts_bookmarks USING fts5 (
    title,
    summary,
    content_text,
    tags
);
