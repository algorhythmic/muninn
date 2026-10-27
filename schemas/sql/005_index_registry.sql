-- Additive canonical index registry. Corpus identity survives derived rebuilds.
CREATE TABLE index_corpus (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    corpus_id TEXT NOT NULL UNIQUE,
    source_epoch INTEGER NOT NULL DEFAULT 0 CHECK (source_epoch >= 0),
    active_generation_id TEXT REFERENCES index_generations (generation_id),
    lease_token TEXT,
    lease_expires_at INTEGER
) STRICT;
CREATE TABLE index_generations (
    generation_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL REFERENCES index_corpus (corpus_id),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json)),
    source_epoch INTEGER NOT NULL CHECK (source_epoch >= 0),
    state TEXT NOT NULL CHECK (state IN ('staging', 'ready', 'failed')),
    manifest_path TEXT,
    manifest_digest TEXT,
    failure_reason TEXT,
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    ready_at INTEGER,
    CHECK (state <> 'ready' OR (manifest_path IS NOT NULL AND manifest_digest IS NOT NULL))
) STRICT;
INSERT INTO index_corpus (singleton, corpus_id) VALUES (1, 'muninn-corpus:' || lower(hex(randomblob(16))));

-- Content/visibility mutations invalidate inside the writer's transaction,
-- including older writers. A no-op update does not invalidate. Status/audit,
-- usage_records, sync_checkpoints and deep_pass_requested are excluded.
CREATE TRIGGER index_epoch_bookmarks_insert
AFTER INSERT ON bookmarks
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_bookmarks_update
AFTER UPDATE ON bookmarks
WHEN OLD.bookmark_id IS NOT NEW.bookmark_id
  OR OLD.source IS NOT NEW.source
  OR OLD.source_id IS NOT NEW.source_id
  OR OLD.captured_at IS NOT NEW.captured_at
  OR OLD.title IS NOT NEW.title
  OR OLD.url IS NOT NEW.url
  OR OLD.folder_path IS NOT NEW.folder_path
  OR OLD.era_label IS NOT NEW.era_label
  OR OLD.domain IS NOT NEW.domain
  OR OLD.source_metadata IS NOT NEW.source_metadata
  OR OLD.redacted_param_count IS NOT NEW.redacted_param_count
  OR OLD.redacted_param_names IS NOT NEW.redacted_param_names
  OR OLD.path_redacted IS NOT NEW.path_redacted
  OR OLD.content_visible IS NOT NEW.content_visible
  OR OLD.enrichment_source IS NOT NEW.enrichment_source
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_bookmarks_delete
AFTER DELETE ON bookmarks
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_results_insert
AFTER INSERT ON scrape_results
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_results_update
AFTER UPDATE ON scrape_results
WHEN OLD.scrape_result_id IS NOT NEW.scrape_result_id
  OR OLD.bookmark_id IS NOT NEW.bookmark_id
  OR OLD.pass IS NOT NEW.pass
  OR OLD.fetched_at IS NOT NEW.fetched_at
  OR OLD.target_timestamp IS NOT NEW.target_timestamp
  OR OLD.actual_snapshot_at IS NOT NEW.actual_snapshot_at
  OR OLD.archive_url IS NOT NEW.archive_url
  OR OLD.final_url IS NOT NEW.final_url
  OR OLD.http_status IS NOT NEW.http_status
  OR OLD.scrape_status IS NOT NEW.scrape_status
  OR OLD.extraction_quality IS NOT NEW.extraction_quality
  OR OLD.content_text IS NOT NEW.content_text
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_results_delete
AFTER DELETE ON scrape_results
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_history_insert
AFTER INSERT ON scrape_history
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_history_update
AFTER UPDATE ON scrape_history
WHEN OLD.scrape_history_id IS NOT NEW.scrape_history_id
  OR OLD.revision_ulid IS NOT NEW.revision_ulid
  OR OLD.bookmark_id IS NOT NEW.bookmark_id
  OR OLD.item_ref IS NOT NEW.item_ref
  OR OLD.content_sha256 IS NOT NEW.content_sha256
  OR OLD.normalization IS NOT NEW.normalization
  OR OLD.char_count IS NOT NEW.char_count
  OR OLD.content_text IS NOT NEW.content_text
  OR OLD.coverage IS NOT NEW.coverage
  OR OLD.retrieval_method IS NOT NEW.retrieval_method
  OR OLD.scrape_pass IS NOT NEW.scrape_pass
  OR OLD.extractor_name IS NOT NEW.extractor_name
  OR OLD.extractor_version IS NOT NEW.extractor_version
  OR OLD.http_status IS NOT NEW.http_status
  OR OLD.final_locator IS NOT NEW.final_locator
  OR OLD.archive_snapshot_at IS NOT NEW.archive_snapshot_at
  OR OLD.published_at IS NOT NEW.published_at
  OR OLD.retrieved_at IS NOT NEW.retrieved_at
  OR OLD.observed_at IS NOT NEW.observed_at
  OR OLD.withdrawn_at IS NOT NEW.withdrawn_at
  OR OLD.withdrawal_reason IS NOT NEW.withdrawal_reason
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_scrape_history_delete
AFTER DELETE ON scrape_history
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_enriched_insert
AFTER INSERT ON enriched
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_enriched_update
AFTER UPDATE ON enriched
WHEN OLD.bookmark_id IS NOT NEW.bookmark_id
  OR OLD.summary IS NOT NEW.summary
  OR OLD.tags IS NOT NEW.tags
  OR OLD.entities IS NOT NEW.entities
  OR OLD.content_type IS NOT NEW.content_type
  OR OLD.language IS NOT NEW.language
  OR OLD.word_count IS NOT NEW.word_count
  OR OLD.enrichment_model IS NOT NEW.enrichment_model
  OR OLD.enrichment_prompt_version IS NOT NEW.enrichment_prompt_version
  OR OLD.content_hash IS NOT NEW.content_hash
  OR OLD.enriched_at IS NOT NEW.enriched_at
  OR OLD.key_quotes IS NOT NEW.key_quotes
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_enriched_delete
AFTER DELETE ON enriched
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_item_links_insert
AFTER INSERT ON item_links
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_item_links_update
AFTER UPDATE ON item_links
WHEN OLD.item_link_id IS NOT NEW.item_link_id
  OR OLD.parent_bookmark_id IS NOT NEW.parent_bookmark_id
  OR OLD.child_bookmark_id IS NOT NEW.child_bookmark_id
  OR OLD.parent_ref IS NOT NEW.parent_ref
  OR OLD.child_ref IS NOT NEW.child_ref
  OR OLD.relation IS NOT NEW.relation
  OR OLD.position IS NOT NEW.position
  OR OLD.locator_resolution IS NOT NEW.locator_resolution
  OR OLD.entity_status IS NOT NEW.entity_status
  OR OLD.first_seen_at IS NOT NEW.first_seen_at
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_item_links_delete
AFTER DELETE ON item_links
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_cross_references_insert
AFTER INSERT ON cross_references
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_cross_references_update
AFTER UPDATE ON cross_references
WHEN OLD.cross_reference_id IS NOT NEW.cross_reference_id
  OR OLD.source_bookmark_id IS NOT NEW.source_bookmark_id
  OR OLD.target_bookmark_id IS NOT NEW.target_bookmark_id
  OR OLD.relationship IS NOT NEW.relationship
  OR OLD.rationale IS NOT NEW.rationale
  OR OLD.created_by IS NOT NEW.created_by
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_cross_references_delete
AFTER DELETE ON cross_references
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_deep_pass_results_insert
AFTER INSERT ON deep_pass_results
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_pair_checks_insert
AFTER INSERT ON lint_pair_checks
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_pair_checks_update
AFTER UPDATE ON lint_pair_checks
WHEN OLD.lint_pair_check_id IS NOT NEW.lint_pair_check_id
  OR OLD.bookmark_id_a IS NOT NEW.bookmark_id_a
  OR OLD.bookmark_id_b IS NOT NEW.bookmark_id_b
  OR OLD.content_hash_a IS NOT NEW.content_hash_a
  OR OLD.content_hash_b IS NOT NEW.content_hash_b
  OR OLD.model IS NOT NEW.model
  OR OLD.prompt_version IS NOT NEW.prompt_version
  OR OLD.task_id IS NOT NEW.task_id
  OR OLD.tag IS NOT NEW.tag
  OR OLD.checked_at IS NOT NEW.checked_at
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_pair_checks_delete
AFTER DELETE ON lint_pair_checks
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_findings_insert
AFTER INSERT ON lint_findings
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_findings_update
AFTER UPDATE ON lint_findings
WHEN OLD.lint_finding_id IS NOT NEW.lint_finding_id
  OR OLD.relation IS NOT NEW.relation
  OR OLD.bookmark_id IS NOT NEW.bookmark_id
  OR OLD.other_bookmark_id IS NOT NEW.other_bookmark_id
  OR OLD.content_hash IS NOT NEW.content_hash
  OR OLD.other_content_hash IS NOT NEW.other_content_hash
  OR OLD.rationale IS NOT NEW.rationale
  OR OLD.evidence_json IS NOT NEW.evidence_json
  OR OLD.model IS NOT NEW.model
  OR OLD.prompt_version IS NOT NEW.prompt_version
  OR OLD.task_id IS NOT NEW.task_id
  OR OLD.tag IS NOT NEW.tag
  OR OLD.created_at IS NOT NEW.created_at
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

CREATE TRIGGER index_epoch_lint_findings_delete
AFTER DELETE ON lint_findings
BEGIN
    UPDATE index_corpus SET source_epoch = source_epoch + 1 WHERE singleton = 1;
END;

