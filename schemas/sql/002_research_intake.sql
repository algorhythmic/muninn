-- Muninn migration 002: Research Intake records (DRAFT, RI-03).
-- Contract: docs/contracts/MUNINN-RESEARCH-INTAKE.md (muninn-research-intake 0.1.0-draft).
--
-- Migration body only. The migrator wraps it in one immediate transaction
-- together with the user_version bump, so this file contains no
-- transaction control and no connection settings. Every statement ends at a
-- line-final semicolon and no semicolon appears anywhere else, so a
-- statement splitter (sqlite3.complete_statement) and executescript both
-- read it the same way.
--
-- Purely additive (RI-01 section 4.6). No existing table, index or
-- column is altered or rebuilt. In particular:
--   * bookmarks is untouched. X posts and linked items are ordinary
--     bookmarks rows (source = 'x', 'x_link', 'x_article').
--   * bookmarks.enrichment_source keeps its CHECK enum. API-delivered
--     post text is the item's current 'live' retrieval, so X posts use
--     the existing value 'live_fallback'. Provenance (x_api versus an
--     HTTP fetch) is recorded per revision in scrape_history.
--   * scrape_results keeps its CHECK enum. The current post text sits in
--     the existing 'live' pass row, and its history sits in scrape_history.
--
-- Conventions kept from 001: INTEGER surrogate keys, epoch-second
-- INTEGER times, JSON in TEXT with json_valid CHECKs, TEXT enums with
-- CHECK. New tables are STRICT. Links to bookmarks are nullable with
-- ON DELETE SET NULL, and each history row also carries the stable typed
-- item_ref (the export item_id), so history survives a deleted bookmark.
-- Append-only tables (scrape_history, assessments, interest_profiles,
-- usage_records) are append-only by application rule. The single
-- permitted UPDATE is withdrawal, which clears content and sets
-- withdrawn_at and withdrawal_reason. No FTS table is added.

-- ── item_links ──────────────────────────────────────────────────────
-- Explicit parent/child relations between items: a post and the pages,
-- repositories, articles and posts it links, quotes or replies to.
-- Canonical-locator dedup never merges a post with its linked items,
-- and a linked item shared by two posts is one row with two links.
CREATE TABLE item_links (
    item_link_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    parent_bookmark_id  INTEGER REFERENCES bookmarks (bookmark_id) ON DELETE SET NULL,
    child_bookmark_id   INTEGER REFERENCES bookmarks (bookmark_id) ON DELETE SET NULL,
    parent_ref          TEXT    NOT NULL,
    child_ref           TEXT    NOT NULL,
    relation            TEXT    NOT NULL,
    position            INTEGER NOT NULL CHECK (position >= 0),
    locator_resolution  TEXT,
    entity_status       INTEGER,
    first_seen_at       INTEGER NOT NULL,
    UNIQUE (parent_ref, relation, position),
    CHECK (relation IN ('links_to', 'quotes', 'replies_to', 'article')),
    CHECK (locator_resolution IS NULL OR locator_resolution IN
           ('unwound', 'expanded', 'short', 'api_reference')),
    CHECK (parent_ref <> child_ref)
) STRICT;

CREATE INDEX idx_item_links_parent ON item_links (parent_bookmark_id);
CREATE INDEX idx_item_links_child  ON item_links (child_bookmark_id);
CREATE INDEX idx_item_links_child_ref ON item_links (child_ref);

-- ── scrape_history ──────────────────────────────────────────────────
-- Append-only content revisions. One row per distinct (item, content,
-- coverage): re-fetching unchanged content hits the UNIQUE key and adds
-- nothing. content_sha256 is sha256 over the normalized extracted text
-- (normalization rule named in the normalization column), and the empty
-- string when a fetch produced no text. Blocked, deleted and empty
-- captures always carry that empty-text digest and char_count 0, and
-- coverage is part of the key so those three stay distinct revisions.
-- scrape_results keeps upserting the current state.
CREATE TABLE scrape_history (
    scrape_history_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_ulid       TEXT    NOT NULL UNIQUE,
    bookmark_id         INTEGER REFERENCES bookmarks (bookmark_id) ON DELETE SET NULL,
    item_ref            TEXT    NOT NULL,
    content_sha256      TEXT    NOT NULL,
    normalization       TEXT    NOT NULL,
    char_count          INTEGER NOT NULL CHECK (char_count >= 0),
    content_text        TEXT,
    coverage            TEXT    NOT NULL,
    retrieval_method    TEXT    NOT NULL,
    scrape_pass         TEXT,
    extractor_name      TEXT    NOT NULL,
    extractor_version   TEXT    NOT NULL,
    http_status         INTEGER,
    final_locator       TEXT,
    archive_snapshot_at INTEGER,
    published_at        INTEGER,
    retrieved_at        INTEGER NOT NULL,
    observed_at         INTEGER,
    withdrawn_at        INTEGER,
    withdrawal_reason   TEXT,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (item_ref, content_sha256, coverage),
    CHECK (length(revision_ulid) = 26 AND revision_ulid NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
    CHECK (length(content_sha256) = 64 AND content_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (coverage IN ('full', 'partial', 'blocked', 'deleted', 'empty')),
    CHECK (retrieval_method IN ('x_api', 'live', 'archive')),
    CHECK (scrape_pass IS NULL OR scrape_pass IN
           ('live', 'at_capture', 'recent_archive', 'playwright', 'manual')),
    CHECK (retrieval_method = 'archive' OR archive_snapshot_at IS NULL),
    CHECK (coverage IN ('full', 'partial') OR (char_count = 0 AND content_sha256 =
           'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')),
    CHECK (coverage NOT IN ('full', 'partial') OR char_count > 0),
    CHECK (withdrawal_reason IS NULL OR withdrawal_reason IN
           ('retention', 'operator_request', 'source_deleted', 'policy_hidden')),
    CHECK ((withdrawn_at IS NULL) = (withdrawal_reason IS NULL)),
    CHECK (withdrawn_at IS NULL OR content_text IS NULL)
) STRICT;

CREATE INDEX idx_scrape_history_bookmark  ON scrape_history (bookmark_id);
CREATE INDEX idx_scrape_history_item_time ON scrape_history (item_ref, retrieved_at);
CREATE INDEX idx_scrape_history_published ON scrape_history (published_at);

-- ── interest_profiles ───────────────────────────────────────────────
-- Append-only store of every interest-profile revision an assessment has
-- used, so old assessments and packets stay resolvable after the YAML
-- file changes. profile_sha256 is sha256 over the canonical JSON of the
-- parsed profile (the profile's revision).
CREATE TABLE interest_profiles (
    profile_sha256      TEXT    PRIMARY KEY,
    profile_id          TEXT    NOT NULL,
    body_json           TEXT    NOT NULL CHECK (json_valid(body_json)),
    first_seen_at       INTEGER NOT NULL,
    CHECK (length(profile_sha256) = 64 AND profile_sha256 NOT GLOB '*[^0-9a-f]*')
) STRICT;

CREATE INDEX idx_interest_profiles_id ON interest_profiles (profile_id);

-- ── assessments ─────────────────────────────────────────────────────
-- Append-only suitability assessments. Idempotency key: content revision
-- + profile revision + policy version + model + prompt version. A
-- changed profile or policy appends a new row and leaves earlier rows in
-- place. Disposition is a judgement, never a retrieval state, and a
-- capture with no readable content cannot be a candidate.
-- evidence_json is the model-cited evidence (the assessment record's
-- evidence array), NULL when no citation survived validation. Every
-- excerpt was checked against the cited revision's stored text before the
-- row was written.
-- scrape_history_id has no ON DELETE action: an assessed revision cannot
-- be deleted, only withdrawn.
CREATE TABLE assessments (
    assessment_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    assessment_ulid     TEXT    NOT NULL UNIQUE,
    scrape_history_id   INTEGER NOT NULL REFERENCES scrape_history (scrape_history_id),
    bookmark_id         INTEGER REFERENCES bookmarks (bookmark_id) ON DELETE SET NULL,
    item_ref            TEXT    NOT NULL,
    content_sha256      TEXT    NOT NULL,
    content_coverage    TEXT    NOT NULL,
    profile_sha256      TEXT    NOT NULL REFERENCES interest_profiles (profile_sha256),
    policy_version      TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    prompt_version      TEXT    NOT NULL,
    disposition         TEXT    NOT NULL,
    relevance           INTEGER NOT NULL CHECK (relevance BETWEEN 0 AND 100),
    confidence          INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    rationale           TEXT    NOT NULL,
    matched_interests   TEXT    NOT NULL DEFAULT '[]' CHECK (json_valid(matched_interests)),
    unresolved_questions TEXT   NOT NULL DEFAULT '[]' CHECK (json_valid(unresolved_questions)),
    evidence_json       TEXT    CHECK (evidence_json IS NULL OR json_valid(evidence_json)),
    prior_assessment_id INTEGER REFERENCES assessments (assessment_id),
    input_token_count   INTEGER,
    output_token_count  INTEGER,
    assessed_at         INTEGER NOT NULL,
    UNIQUE (scrape_history_id, profile_sha256, policy_version, model, prompt_version),
    CHECK (length(assessment_ulid) = 26 AND assessment_ulid NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
    CHECK (content_coverage IN ('full', 'partial', 'blocked', 'deleted', 'empty')),
    CHECK (disposition IN
           ('candidate', 'reference_only', 'needs_evidence', 'deferred', 'rejected')),
    CHECK (disposition <> 'candidate' OR content_coverage IN ('full', 'partial'))
) STRICT;

CREATE INDEX idx_assessments_item        ON assessments (item_ref, assessed_at);
CREATE INDEX idx_assessments_bookmark    ON assessments (bookmark_id);
CREATE INDEX idx_assessments_disposition ON assessments (disposition);

-- ── evidence_packets ────────────────────────────────────────────────
-- Bookkeeping for exported packets so every exported revision stays
-- resolvable. body_json is the exact packet (canonical JSON), and
-- packet_sha256 is sha256 of those bytes. Withdrawal clears body_json and
-- keeps the row as the tombstone source.
CREATE TABLE evidence_packets (
    evidence_packet_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    packet_ulid         TEXT    NOT NULL,
    revision            INTEGER NOT NULL CHECK (revision >= 1),
    packet_sha256       TEXT    NOT NULL UNIQUE,
    subject_ref         TEXT    NOT NULL,
    assessment_id       INTEGER REFERENCES assessments (assessment_id),
    contract_version    TEXT    NOT NULL,
    body_json           TEXT    CHECK (body_json IS NULL OR json_valid(body_json)),
    export_path         TEXT,
    exported_at         INTEGER NOT NULL,
    withdrawn_at        INTEGER,
    withdrawal_reason   TEXT,
    UNIQUE (packet_ulid, revision),
    CHECK (length(packet_ulid) = 26 AND packet_ulid NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
    CHECK (length(packet_sha256) = 64 AND packet_sha256 NOT GLOB '*[^0-9a-f]*'),
    CHECK (withdrawal_reason IS NULL OR withdrawal_reason IN
           ('retention', 'operator_request', 'source_deleted', 'policy_hidden')),
    CHECK ((withdrawn_at IS NULL) = (withdrawal_reason IS NULL)),
    CHECK ((withdrawn_at IS NULL) = (body_json IS NOT NULL))
) STRICT;

CREATE INDEX idx_evidence_packets_subject ON evidence_packets (subject_ref);

-- ── sync_checkpoints ────────────────────────────────────────────────
-- One row per source sync run (the X adapter first), mutable while the
-- run is open. Resume reads the newest non-completed row for (source,
-- account_ref, collection). next_token is the opaque pagination cursor.
-- No credential is ever stored here: OAuth tokens live outside the DB.
CREATE TABLE sync_checkpoints (
    sync_checkpoint_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ulid            TEXT    NOT NULL UNIQUE,
    source              TEXT    NOT NULL,
    account_ref         TEXT    NOT NULL,
    collection          TEXT    NOT NULL,
    status              TEXT    NOT NULL,
    next_token          TEXT,
    pages_fetched       INTEGER NOT NULL DEFAULT 0 CHECK (pages_fetched >= 0),
    items_seen          INTEGER NOT NULL DEFAULT 0 CHECK (items_seen >= 0),
    items_new           INTEGER NOT NULL DEFAULT 0 CHECK (items_new >= 0),
    resume_after        INTEGER,
    error_code          TEXT,
    error_detail        TEXT,
    config_json         TEXT    CHECK (config_json IS NULL OR json_valid(config_json)),
    started_at          INTEGER NOT NULL,
    updated_at          INTEGER NOT NULL,
    completed_at        INTEGER,
    CHECK (length(run_ulid) = 26 AND run_ulid NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
    CHECK (status IN ('running', 'paused', 'exhausted', 'failed', 'completed')),
    CHECK ((status = 'completed') = (completed_at IS NOT NULL))
) STRICT;

CREATE INDEX idx_sync_checkpoints_resume
    ON sync_checkpoints (source, account_ref, collection, status);

-- ── usage_records ───────────────────────────────────────────────────
-- Append-only usage and cost ledger for paid calls: X API requests and
-- model calls. Cost is integer micro-USD so exports stay integer-only.
CREATE TABLE usage_records (
    usage_record_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    usage_ulid          TEXT    NOT NULL UNIQUE,
    kind                TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    operation           TEXT    NOT NULL,
    model               TEXT,
    sync_checkpoint_id  INTEGER REFERENCES sync_checkpoints (sync_checkpoint_id) ON DELETE SET NULL,
    assessment_id       INTEGER REFERENCES assessments (assessment_id) ON DELETE SET NULL,
    request_count       INTEGER NOT NULL DEFAULT 1 CHECK (request_count >= 0),
    resource_count      INTEGER CHECK (resource_count IS NULL OR resource_count >= 0),
    input_token_count   INTEGER CHECK (input_token_count IS NULL OR input_token_count >= 0),
    output_token_count  INTEGER CHECK (output_token_count IS NULL OR output_token_count >= 0),
    cache_read_token_count INTEGER CHECK (cache_read_token_count IS NULL OR cache_read_token_count >= 0),
    cost_micro_usd      INTEGER NOT NULL CHECK (cost_micro_usd >= 0),
    cost_basis          TEXT    NOT NULL,
    occurred_at         INTEGER NOT NULL,
    CHECK (length(usage_ulid) = 26 AND usage_ulid NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
    CHECK (kind IN ('x_api', 'model')),
    CHECK (cost_basis IN ('estimated', 'reported')),
    CHECK (kind <> 'model' OR model IS NOT NULL)
) STRICT;

CREATE INDEX idx_usage_records_time ON usage_records (occurred_at);
