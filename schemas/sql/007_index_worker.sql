-- One coalesced publisher job lives in the existing corpus registry. There is
-- no independent activation pointer or event queue.
ALTER TABLE index_corpus ADD COLUMN publisher_job_id TEXT;
ALTER TABLE index_corpus ADD COLUMN publisher_state TEXT
    CHECK (publisher_state IN ('pending','building','ready','failed','stale','active'));
ALTER TABLE index_corpus ADD COLUMN publisher_pending_epoch INTEGER;
ALTER TABLE index_corpus ADD COLUMN publisher_generation_id TEXT;
ALTER TABLE index_corpus ADD COLUMN publisher_failure_category TEXT;
ALTER TABLE index_corpus ADD COLUMN publisher_updated_at INTEGER;

-- Old and new writers already update source_epoch through migration 005's
-- triggers. Coalesce that observation inside the same canonical transaction.
CREATE TRIGGER index_worker_source_changed AFTER UPDATE OF source_epoch ON index_corpus
WHEN NEW.source_epoch <> OLD.source_epoch
BEGIN
    UPDATE index_corpus SET publisher_pending_epoch=NEW.source_epoch,
        publisher_job_id=coalesce(publisher_job_id,lower(hex(randomblob(16)))),
        publisher_state=CASE WHEN publisher_state='building' THEN 'building'
            WHEN publisher_state IN ('ready','active') THEN 'stale' ELSE 'pending' END,
        publisher_updated_at=unixepoch()
        WHERE singleton=1;
END;
CREATE TRIGGER index_worker_activated AFTER UPDATE OF active_generation_id ON index_corpus
WHEN NEW.active_generation_id=NEW.publisher_generation_id
BEGIN
    UPDATE index_corpus SET publisher_state='active',publisher_updated_at=unixepoch()
        WHERE singleton=1;
END;
