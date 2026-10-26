# B — Index publishing, full-text chunks and authored notes

Status: planned, 2026-10-25. Parent: [integration references](README.md).
Repository: Muninn; consumes Braid's existing dataset/batch APIs.
Dependencies: A for dense embeddings. Projection work can begin independently.
Consumers: C/D; E completes operational coordination.

## Objective and current boundary

Publish rebuildable generations containing visible captured sources, their
full-text chunks and permitted authored notes. Preserve source identity,
revision, coverage and exact spans. A partial build leaves the active generation
intact. Canonical stores and the existing FTS/Qdrant writers continue operating.

Current semantic embedding inputs are title/summary/tags; full scrape text is
available to FTS. Authored namespaces are scanned separately by MCP.

Affected source includes [revisions.py](../../src/muninn/revisions.py),
[intake_records.py](../../src/muninn/intake_records.py),
[vault_fs.py](../../src/muninn/consumers/mcp/vault_fs.py),
[MCP hydration](../../src/muninn/consumers/mcp/tools.py),
[enrichment](../../src/muninn/enrich/pipeline.py),
[Qdrant reconciliation](../../src/muninn/vector/reconcile.py) and
[database migrations](../../src/muninn/db.py).

Proposed modules: index/projection.py, sources.py, chunking.py, manifest.py,
publisher.py, braid_client.py and consumers/cli/index.py. Names are adjustable;
their boundaries and contracts are the handoff.

## Shared visible-source projection

Extract a reusable source-access layer from current serving rules. It supplies
both publishing and response hydration; do not create weaker index-only
visibility logic. Keep heavy dependencies lazy.

| Input | Identity/revision | Selection and ownership |
| --- | --- | --- |
| Captured metadata | Corpus + bookmark ID; projection digest | Current visible title, summary, tags and safe metadata |
| Captured body | Exact content/capture reference, digest and coverage | Reuse canonical source selection; record live/archive pass explicitly |
| Authored Markdown | Corpus + vault-relative path; exact file digest | concepts, infra, people and reference projects only |
| Relationships | Endpoint identities and source relation identity | item_links and persisted cross_references; visible endpoints only |

Keep publication, bookmark-observation, capture and modification times distinct.
Do not concatenate live and historical captures into a supposedly current body.
For legacy captures without an exported revision ULID, retain an exact local
record identity plus content digest; do not invent archival provenance.

Use existing vault path, symlink, size and personal-vault guards. Enumerate
authored namespaces directly rather than using a query-ranked or bounded scan
as a complete inventory. Report every bound/omission. Never infer deletion from
a failed or truncated enumeration. Exclude compiled bookmarks from authored
indexing to prevent duplicates. Authored files remain read-only.

Apply existing hidden-link and derived-material rules before embedding. For a
derived output whose inputs became hidden, withhold the affected output as the
current source rules require; stripping a URL alone does not erase paraphrases.

## Documents, chunks and edges

Publish a document metadata node plus body chunk nodes. A chunk's attributes
contain document ID, namespace, exact source reference/revision, normalized
character start/end, text digest, ordinal and chunker version. Retain a mapping
from embedding input (title/heading prefix plus body) to the exact body span.

Initial chunk settings: target 512 document tokens, maximum 768 including the
prefix and prompt overhead, overlap about 64 tokens for split long sections.
Use the pinned model tokenizer; prefer paragraph/heading boundaries, retain
code fences where practical and split oversized sections explicitly. These are
starting settings, not measured optimums. A title/heading prefix must not be
reported as if it occurred inside the cited body span.

Chunk IDs bind document identity, source projection revision, chunker version
and ordinal. Exact text digests provide reuse evidence; do not identify chunks
by ordinal alone. Renames initially remove/add authored documents. Empty or
unavailable bodies may retain metadata nodes with explicit coverage.

Add contains/belongs_to and source-derived relationship edges with timestamps
and provenance. Graph edges do not imply required context. C initially disables
automatic graph/temporal expansion.

## Generation and persistence contract

Use one stable corpus/scope dataset identity and a unique generation ID. Store
private generations outside the checkout. A manifest includes schema version,
source snapshot/epoch, authored inventory digest, profiles, source/chunk counts,
omissions, Braid identity/revision/digest and creation time. Chunk manifests may
contain private source locators and text; protect them like the corpus.

Own the next additive numbered migration, currently 005 at the source snapshot;
reinspect migration numbering before implementation. Add corpus identity,
captured-source epoch, generation rows and an active-generation reference.
Coordinate these fields with E rather than creating a second registry. Keep
embedding vectors in A's derived cache. Do not edit schema.sql's frozen 001
mirror or the evidence-packet contract.

Captured-source epoch changes must be transactional with relevant canonical
mutations. Define trigger coverage for bookmarks/visibility, selected capture
content, enrichment, item links, cross-references and relevant derived-result
visibility. Existing and older writers should invalidate an index without
knowing the new publisher. Avoid treating usage/status writes as content changes.

## Publication algorithm

1. Acquire the corpus publisher lease; allocate a staging generation durably.
2. Read canonical source projection and epoch in one SQLite read transaction.
   Materialize bounded private spool files, then release the transaction before
   model inference. Read authored files into a digest-bound inventory.
3. Build deterministic nodes/chunks/edges; obtain A's compatible cached or newly
   generated document vectors. Record coverage and omissions.
4. Initialize a fresh Braid database with the bound dataset ID and embedding
   profile. Negotiate capabilities. Apply node/vector batches by encoded byte
   size, below the advertised request limit, chaining expected revisions. Apply
   edges after endpoints exist. Fresh staging files need no deletion pass.
5. Finish all writes and persist the final manifest and Braid identity. Set the
   generation to ready only after its files are complete. Readiness is a build
   state, not a test or retrieval-quality verdict.
6. On explicit activation, compare the current captured epoch and authored
   inventory with the manifest. Update the active reference in a short canonical
   transaction, including the epoch comparison. A changed source stays stale;
   C's request-time hydration remains necessary for concurrent file edits.

All CLI names are proposed: index sync builds; index activate switches readers.
Provide no implicit activation in the first implementation. E defines process
handover and retention. Account for SQLite WAL files: never publish by copying
only an open database's main file or deleting sidecars used by readers.

## Failure behavior and handoff

Unknown mutation outcomes require reading the staging dataset and journal before
retry, or abandoning that disposable generation. Cached complete embeddings can
be reused on restart. A stale epoch, changed authored inventory, incomplete scan,
profile mismatch or interrupted build cannot overwrite the active reference.
Missing sources and unavailable source scans are distinct states.

Completion artifacts: shared projection and resolvers, deterministic chunk
contract, migration, publisher/client, staging journal, manifest and build/status
commands. Give C the generation-open API, exact source resolver and freshness
contract; give D the span/prefix mapping; give E registry and lease ownership.
Record source/build/runtime evidence separately under the
[current operator policy](README.md#evidence-and-current-operator-policy).
