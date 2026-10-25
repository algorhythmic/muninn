-- Immutable paid deep-pass outputs, independent of the mutable enriched projection.
-- No backfill: existing enriched rows cannot establish historical provenance.
-- Retain input digests only, never source text or raw material locators.
-- Bookmark ids deliberately have no FK: deleting a bookmark keeps history,
-- while read helpers withhold records without a current visible bookmark.
CREATE TABLE deep_pass_results (
    deep_pass_result_id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL CHECK (attempt > 0),
    bookmark_id INTEGER NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    source_pass TEXT,
    source_content_hash TEXT NOT NULL,
    materials_digest TEXT NOT NULL,
    material_bookmark_ids_json TEXT NOT NULL CHECK (json_valid(material_bookmark_ids_json)),
    output_json TEXT NOT NULL CHECK (json_valid(output_json)),
    output_digest TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    completed_at INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (task_id, attempt),
    CHECK (length(source_content_hash) = 71 AND source_content_hash GLOB 'sha256:*'),
    CHECK (length(materials_digest) = 71 AND materials_digest GLOB 'sha256:*'),
    CHECK (length(output_digest) = 71 AND output_digest GLOB 'sha256:*')
) STRICT;

CREATE INDEX idx_deep_pass_results_bookmark ON deep_pass_results (bookmark_id, deep_pass_result_id);

CREATE TRIGGER deep_pass_results_no_update
BEFORE UPDATE ON deep_pass_results
BEGIN
    SELECT RAISE(ABORT, 'deep_pass_results is immutable');
END;

CREATE TRIGGER deep_pass_results_no_delete
BEFORE DELETE ON deep_pass_results
BEGIN
    SELECT RAISE(ABORT, 'deep_pass_results is immutable');
END;

-- REPLACE deletes conflicting rows without DELETE triggers unless recursive
-- triggers are enabled. Reject replacement identities before that can happen.
CREATE TRIGGER deep_pass_results_no_replace
BEFORE INSERT ON deep_pass_results
WHEN EXISTS (
    SELECT 1 FROM deep_pass_results
    WHERE (task_id = NEW.task_id AND attempt = NEW.attempt)
       OR deep_pass_result_id = NEW.deep_pass_result_id
)
BEGIN
    SELECT RAISE(ABORT, 'deep_pass_results identity already exists');
END;
