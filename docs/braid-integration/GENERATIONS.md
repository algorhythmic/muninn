# Explicit Braid generation commands

These commands publish a disposable retrieval projection. Captured SQLite and
authored Markdown remain canonical; Qdrant stays the default search backend.
The implementation consumes query-vector contract revision 1 and negotiates
`query_embedding` before sending vectors. Source tests use synthetic fixtures
and fake Braid/embedding implementations; they establish no retrieval quality.

Apply the additive migration through the existing explicit migration command
before using the registry:

```sh
muninn db migrate
muninn index status
muninn index sync --binary /path/to/braid
muninn index activate GENERATION_ID
```

`index sync` builds a ready generation without switching readers. By default it
includes bookmarks, concepts, infra, people and projects; repeat `--namespace`
to build a narrower scope. Authored scope requires a configured permitted vault
and a complete inventory. A request outside an active generation's scope uses
whole-request legacy fallback. The serving binary is resolved as `braid` on
the server's PATH; its digest and ranking identity must match the manifest.

Dense publication requires a complete semantic embedding profile and the exact
model tokenizer. The hash placeholder cannot make a generation eligible for
activation. Unresolved model revisions or missing prompts are reported rather
than guessed. A changed profile requires rebuilding the generation.

All default artifacts are beneath `$MUNINN_DATA_DIR/index` (`<repo>/data/index`
when unset): `embedding-cache`, `staging`, `generations` and `braid-build`.
Staging contains the bounded projection spool, mutation journal, Braid database,
configuration and manifest. Treat these files as private corpus material.

The publisher holds a fenced, expiring corpus lease and reads a captured epoch
in one snapshot. It releases that read transaction before embedding inference,
then sends bounded byte batches with chained revisions. The child exits and
its database is checkpointed before completed files are moved and marked ready.
An interrupted or uncertain build is abandoned, preserving the active reference.
No mutation is blindly retried and no open SQLite main file is copied alone.

Activation compares the current captured epoch and authored inventory with the
manifest, then updates the reference in a short canonical transaction. Current
source hydration remains necessary for concurrent edits. Projection version 2
uses explicit unit candidate costs; older projections require a rebuild.
Switching `MUNINN_SEARCH_BACKEND` back to `fts5-qdrant` bypasses Braid.
