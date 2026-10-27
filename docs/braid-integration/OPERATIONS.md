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
* `muninn index worker --once`: observe changes and process at most one build. Without
  `--once`, poll in the foreground under external supervision. It builds only;
  it never activates. Explicit sync remains the primary publishing path.

Captured-source epoch triggers apply inside the canonical writer transaction,
including older writers. Authored inventory is bounded and digest-bound; an
incomplete/missing scan is unavailable, not a deletion or an empty corpus.
Retrieval rechecks source digests and visibility before returning text.

The optional worker observes authored files before and after each build through
the same bounded inventory used by explicit sync. Only a complete scan replaces
the stored scope/digest baseline. Missing or failed scans preserve the previous
baseline, report an unavailable observation and leave work pending. Edits during
inference coalesce into the existing job slot. A verified ready manifest can seed
the initial baseline without a redundant build. The current shared authored
digest includes every authored namespace, so a change outside a narrower authored
scope may conservatively request a rebuild. No observer activates a generation.

Each search request pins the canonical generation before opening its runtime
session. Every process monitors the same canonical active pointer at a 250 ms
interval. A changed or unavailable pointer stops admission to old sessions and
drains them for at most 10 seconds; process closure adds its bounded transport
shutdown wait (up to 4 seconds). Request deadlines remain 10 seconds including
connection queue wait. There is no independent serving pointer or activation RPC.

Retention is explicit through `publisher.cleanup_generations(db_path, limit=100)`;
`retention_candidates` remains an advisory report. Cleanup holds the publisher OS
lock, preserves the canonical active and previous generations, and closes local
sessions before attempting an exclusive generation lock. Every runtime session
holds a shared lock across opening, requests, idle time and verified process exit.
Other processes therefore prevent cleanup until their SQLite handles close.
Publisher children inherit the same shared ownership descriptor; a publisher
parent crash cannot authorize deleting staging while its child still runs.
Stable lock files under `data/index/leases` are never unlinked.

Cleanup only removes registry-owned, unreferenced generation/staging directories,
including their SQLite sidecars. It refuses symlink escapes and overlap with
configured canonical databases, raw captures, caches or authored vaults. It never
cleans embedding caches, authored Markdown or canonical captures. Interrupted
staging is marked failed before cleanup. A partial unlink can be retried safely;
only after directory fsync does the registry record `derived_files_removed`.
Audit rows and activation journals remain intact, while cleaned generations are
excluded from future ready-build selection. No worker or activation invokes GC.
