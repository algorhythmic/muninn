# Braid index operations

The default remains `MUNINN_SEARCH_BACKEND=fts5-qdrant`. Building a generation
does not activate it. These commands never install a daemon or change live
configuration.

All disposable index files live beneath `MUNINN_DATA_DIR/index` (by default
`<repository>/data/index`). Set `MUNINN_BRAID_BINARY` to an absolute executable
path. `MUNINN_BRAID_SHA256` optionally pins the binary's lowercase SHA256;
generation manifests additionally bind the negotiated binary/protocol identity.
No binary or embedding model is downloaded by these commands.

* `muninn index sync --backend braid`: explicitly build a ready generation.
* `muninn index status`: inspect configuration intent, latest ready build,
  active pointer, epoch/inventory, profile, Braid identity, publisher state,
  counts and structured freshness reason. `backend_actually_used` is null when
  this process has no request observation; request diagnostics report the
  actual backend used. Configuration intent is never presented as execution.
* `muninn index activate GENERATION`: explicitly switch after current source
  checks. This is an operator deployment decision.
* `muninn index rollback GENERATION`: explicitly select a retained compatible
  previous generation. Source changes can refuse rollback; rollback never
  makes removed content eligible again.
* `muninn index worker --once`: process an already pending change. Without
  `--once`, poll in the foreground under external supervision. It builds only;
  it never activates. Explicit sync remains the primary publishing path.

Captured-source epoch triggers apply inside the canonical writer transaction,
including older writers. Authored inventory is bounded and digest-bound; an
incomplete/missing scan is unavailable, not a deletion or an empty corpus.
Retrieval rechecks source digests and visibility before returning text.

Retention currently reports candidates without deleting files. Destructive
garbage collection is deferred until cross-process session leases can prove
that all readers closed their SQLite handles and sidecars. This conservatively
retains more private derived data and never removes canonical or authored data.
