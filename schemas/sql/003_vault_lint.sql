-- Muninn migration 003: model-tier vault lint (roadmap Phase 6).
-- Docs: docs/vault-lint.md and docs/synthesis-contract.md ("lint").
--
-- Migration body only. The migrator wraps it in one immediate transaction
-- together with the user_version bump and runs it with foreign_keys OFF,
-- then checks foreign_key_check before COMMIT and integrity_check after.
-- Every statement ends at a line-final semicolon and no semicolon appears
-- anywhere else, so a statement splitter (sqlite3.complete_statement) and
-- executescript both read it the same way.
--
-- Two parts:
--
-- 1. synthesis_runs gains the task type 'lint'. Its CHECK enum is part of
--    the table definition, so the table is rebuilt the documented SQLite
--    way: create the new shape under a temporary name, copy every row
--    (ids included), drop the old table, rename the new one into place
--    and recreate its index. No other table, view or trigger references
--    synthesis_runs. The AUTOINCREMENT high-water mark is carried over,
--    so a new run never reuses an old synthesis_run_id. fts_bookmarks and
--    every other 001/002 table are untouched.
--
-- 2. Two new append-only tables for the lint task (task_type 'lint'):
--    * lint_pair_checks: one row per pair of compiled bookmark pages a
--      lint run examined, with or without a finding. It is the
--      idempotency record: a pair whose two content hashes were already
--      checked under the same prompt version (and model) is not sent to a
--      model again.
--    * lint_findings: what the model found for a pair: 'contradicts', or
--      'supersedes' (bookmark_id's claim supersedes other_bookmark_id's).
--      The vault compiler renders a finding only while both pages are
--      visible, both content hashes still match, and the finding belongs
--      to the latest check of that pair.
--    Pairs in lint_pair_checks are stored ordered (bookmark_id_a <
--    bookmark_id_b), while a finding keeps the model's orientation. Bookmark
--    links cascade: a deleted bookmark takes its checks and findings with
--    it. task_id is the synthesis run id (synthesis_runs.task_id).

CREATE TABLE synthesis_runs_new (
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
    CHECK (task_type IN ('era_narrative', 'deep_pass', 'ad_hoc_analysis', 'lint')),
    CHECK (status IN ('running', 'completed', 'validation_failed', 'container_failed', 'cap_hit'))
);

INSERT INTO synthesis_runs_new (
    synthesis_run_id, task_id, task_type, attempt, started_at, completed_at, status,
    enrichment_model, enrichment_prompt_version, input_token_count, output_token_count,
    validation_errors, container_id
)
SELECT
    synthesis_run_id, task_id, task_type, attempt, started_at, completed_at, status,
    enrichment_model, enrichment_prompt_version, input_token_count, output_token_count,
    validation_errors, container_id
FROM synthesis_runs
ORDER BY synthesis_run_id;

-- Carry the AUTOINCREMENT high-water mark over (it can exceed max(id)).
INSERT INTO sqlite_sequence (name, seq)
SELECT 'synthesis_runs_new', seq FROM sqlite_sequence
WHERE name = 'synthesis_runs'
  AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'synthesis_runs_new');

UPDATE sqlite_sequence
SET seq = max(seq, COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'synthesis_runs'), 0))
WHERE name = 'synthesis_runs_new';

DROP TABLE synthesis_runs;

ALTER TABLE synthesis_runs_new RENAME TO synthesis_runs;

CREATE INDEX idx_synthesis_runs_status ON synthesis_runs (status);

-- ── lint_pair_checks ────────────────────────────────────────────────
CREATE TABLE lint_pair_checks (
    lint_pair_check_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    bookmark_id_a       INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    bookmark_id_b       INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    content_hash_a      TEXT    NOT NULL,
    content_hash_b      TEXT    NOT NULL,
    model               TEXT    NOT NULL,
    prompt_version      TEXT    NOT NULL,
    task_id             TEXT    NOT NULL,
    tag                 TEXT,
    checked_at          INTEGER NOT NULL,
    UNIQUE (task_id, bookmark_id_a, bookmark_id_b),
    CHECK (bookmark_id_a < bookmark_id_b),
    CHECK (content_hash_a GLOB 'sha256:*' AND content_hash_b GLOB 'sha256:*')
) STRICT;

CREATE INDEX idx_lint_pair_checks_pair ON lint_pair_checks (bookmark_id_a, bookmark_id_b);
CREATE INDEX idx_lint_pair_checks_b ON lint_pair_checks (bookmark_id_b);

-- ── lint_findings ───────────────────────────────────────────────────
CREATE TABLE lint_findings (
    lint_finding_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    relation            TEXT    NOT NULL,
    bookmark_id         INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    other_bookmark_id   INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    content_hash        TEXT    NOT NULL,
    other_content_hash  TEXT    NOT NULL,
    rationale           TEXT    NOT NULL,
    evidence_json       TEXT    NOT NULL CHECK (json_valid(evidence_json)),
    model               TEXT    NOT NULL,
    prompt_version      TEXT    NOT NULL,
    task_id             TEXT    NOT NULL,
    tag                 TEXT,
    created_at          INTEGER NOT NULL,
    UNIQUE (task_id, relation, bookmark_id, other_bookmark_id),
    CHECK (relation IN ('contradicts', 'supersedes')),
    CHECK (bookmark_id <> other_bookmark_id),
    CHECK (content_hash GLOB 'sha256:*' AND other_content_hash GLOB 'sha256:*')
) STRICT;

CREATE INDEX idx_lint_findings_bookmark ON lint_findings (bookmark_id);
CREATE INDEX idx_lint_findings_other ON lint_findings (other_bookmark_id);
