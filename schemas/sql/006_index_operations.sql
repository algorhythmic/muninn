-- The active pointer remains the sole activation authority. Journals record intent
-- and outcome, never a competing serving pointer.
ALTER TABLE index_corpus ADD COLUMN previous_generation_id TEXT REFERENCES index_generations(generation_id);
ALTER TABLE index_corpus ADD COLUMN activation_id TEXT;
CREATE TABLE index_activations (
    activation_id TEXT PRIMARY KEY,
    generation_id TEXT NOT NULL REFERENCES index_generations(generation_id),
    previous_generation_id TEXT REFERENCES index_generations(generation_id),
    expected_epoch INTEGER NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('prepared','committed','superseded')),
    created_at INTEGER NOT NULL DEFAULT (unixepoch()),
    committed_at INTEGER
) STRICT;
