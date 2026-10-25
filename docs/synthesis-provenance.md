# Deep-pass synthesis history

Migration `004_synthesis_provenance.sql` adds immutable captured-lane
`deep_pass_results`. Each newly accepted deep pass stores its full result
before updating `enriched`, FTS, or cross-references, in the caller's existing
success transaction. Ordinary enrichment continues to update the current
summary and tags; it cannot erase the separate deep-pass record. Existing
display behavior and the bulk enrichment idempotency triple stay unchanged.

This is a source-only migration increment. It uses the existing explicit
migration mechanism; it does not migrate personal state automatically. The
initial schema mirror `schema.sql` remains a mirror of migration 001 only.

## Recorded evidence

Each record contains:

- The unique `(task_id, attempt)` and subject bookmark ID.
- The synthesis model and prompt version, separate from bulk enrichment.
- The selected source pass and SHA-256 of the exact UTF-8 `source_text` the
  task supplied, including the digest of empty text when no content exists.
- A digest of the complete task materials and the IDs of material and
  referenced bookmarks. These describe the accepted input, independently of
  later mutable scrape results or enrichment hashes.
- Canonical JSON of the complete model output and its SHA-256, including
  summary, tags, entities, quotes, references and synthesis metadata.
- Run start, acceptance completion and recording times. Completion here is
  captured when the writer accepts the result, just before the orchestrator
  finalizes the run audit row.

The source text, neighbor summaries and locators from task materials are
represented by digests rather than duplicated in this table. Full output is
still private corpus data. A materials digest is distinct from the task
contract's `task_digest`: it does not hash the prompt, schema or envelope.
Retain private emitted task files when the exact original input is needed.

SQL triggers reject updates, deletes and identity replacement. Identical
replays return the original identity and leave current projections alone;
different evidence under the same task/attempt raises `DeepPassReplayError`
before projection writes. A new task identity appends a separate result even
when its text matches an earlier result. The writer never commits on its own.
Direct callers must provide the original materials, including matching
`bookmark_id` and string `source_text`; missing evidence is rejected rather
than inferred from current state. Both orchestrated execution and result-file
ingestion already supply those materials.

## Reading recovery evidence

`muninn.synthesis.provenance` provides a read path using an existing SQLite
connection, including one opened with `muninn.db.connect_readonly()`:

```python
from muninn.synthesis.provenance import get_deep_pass, list_deep_passes

record = get_deep_pass(conn, task_id, attempt)
page = list_deep_passes(conn, bookmark_id, limit=100, after_id=0)
# Continue with after_id=page[-1]["deep_pass_result_id"] when page is nonempty.
```

`get_deep_pass` returns a dictionary or `None`. `list_deep_passes` returns
dictionaries in ascending acceptance ID order, with a default limit of 100
and a maximum of 1000. Each dictionary includes parsed `output` and
`material_bookmark_ids`, plus provenance columns. These helpers only read;
they do not restore a result to `enriched`, FTS, Qdrant or the vault, and do
not select a summary for display. Callers can inspect or retain the original
output when recovering useful synthesis after re-enrichment.

The subject and all recorded input/reference bookmarks must currently exist
and be visible. Otherwise reads withhold the whole record, since the model
could have paraphrased any input. Newly accepted results are rejected before
writes if an input has become hidden or disappeared since task preparation.
Explicit neighbor selection also filters hidden rows before task emission.
This policy can withhold an otherwise useful history record when an unrelated
candidate neighbor becomes hidden.

## Limits and integration

- Only newly accepted deep passes gain history. Existing `enriched` rows
  cannot establish old model/source provenance; there is no invented
  backfill or recovery of already overwritten output.
- History retains outputs when a bookmark is deleted, without a cascading
  foreign key. Read helpers withhold those records. The SQLite database and
  its backups remain private; direct SQL access can still read retained data.
- The normal enriched upsert still allows later bulk enrichment to replace
  the displayed summary and tags. Display precedence remains undecided.
- Era narratives, ad-hoc analyses and lint retain their existing storage.
- The existing orchestrator already forwards run and materials to the
  deep-pass writer, so no orchestrator, compiler, template or CLI hook is
  required. Any future CLI exposure should call the read helpers and preserve
  visibility filtering. The compiler remains the sole vault writer.

No tests or runtime migration were run for this increment. Checks:
**SKIPPED (YOLO MODE)**.
