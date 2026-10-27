-- Complete authored observations share the existing coalesced publisher slot.
-- A failed observation never overwrites the last complete digest/scope.
ALTER TABLE index_corpus ADD COLUMN publisher_inventory_scope TEXT;
ALTER TABLE index_corpus ADD COLUMN publisher_inventory_digest TEXT;
ALTER TABLE index_corpus ADD COLUMN publisher_inventory_observed_at INTEGER;
ALTER TABLE index_corpus ADD COLUMN publisher_inventory_failure_category TEXT;
