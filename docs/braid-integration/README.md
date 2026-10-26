# Braid integration implementation references

Date: 2026-10-25. Status: planning reference; implementation and activation are
not established by these documents.

These five work packages describe the requested integration of Braid into
Muninn. Muninn owns canonical captures, authored-note access, source resolution,
EmbeddingGemma inference, visibility and MCP. Braid owns a rebuildable retrieval
projection and budgeted selection. FTS/Qdrant publishing and fallback continue.

| Package | Result | Dependencies |
| --- | --- | --- |
| [A — Embedding compatibility](A-EMBEDDING-COMPATIBILITY.md) | Profile-bound document embeddings and precomputed query vectors accepted by Braid | None |
| [B — Index publishing](B-INDEX-PUBLISHING.md) | Captured material, full-text chunks and authored notes in staged generations | A for dense vectors; projection can begin independently |
| [C — Search and fallback](C-SEARCH-AND-FALLBACK.md) | Canonical document search through Braid, with the existing search path as fallback | A and B |
| [D — Context assembly](D-CONTEXT-ASSEMBLY.md) | Byte-bounded excerpts, source references and required attribution | B and C; consumes A's profile |
| [E — Operations and recovery](E-OPERATIONS-AND-RECOVERY.md) | Freshness, publication coordination, process lifecycle, activation and rollback | Basic publication safety is part of B; operational completion follows C/D |

The first useful milestone is C: full-text and authored-note search through
Muninn with fallback intact. D adds the richer briefing interface. E is not a
reason to postpone B's generation ownership, crash safety or source checks.

## Shared decisions

- Captured SQLite records and authored Markdown remain canonical. Indexing
  authored text into a derived Braid database does not transfer ownership.
- Embeddings stay in Muninn's existing Python inference stack. Braid receives
  document vectors and a negotiated query-vector extension; no embedding server
  is introduced.
- Full generations are the initial publication unit. Reusable vector caching
  avoids recomputing unchanged embedding inputs.
- The active generation is pinned for each request. Dataset revision alone is
  insufficient across different generation files.
- Search returns candidates; Muninn resolves current permissions, exact source
  content and revision digests before emitting text or explanations.
- Existing raw FTS syntax remains available. A valid empty result is not an
  infrastructure failure and does not trigger a second search automatically.
- Initial text search uses lexical and dense stages. Recency and automatic
  graph expansion are disabled. Typed relationships are published for explicit
  context anchors and later use.
- Proposed module names, CLI commands and protocol fields below are design
  targets, not currently supported interfaces.

## Common identity and result contract

Use a stable private corpus ID created once in the canonical database. A Braid
dataset ID identifies that corpus and permitted scope; a separate generation ID
identifies a published file set. A manifest binds both to Braid revision/digest,
captured-source epoch, authored inventory digest, projection/chunker versions,
embedding profile and coverage.

Documents use a stable identity: captured bookmark ID within the corpus, or an
authored vault-relative path within the corpus. Revision identity is separate:
captured source references and projection digests, or authored content digests.
Renames are removal plus addition initially. Chunks carry document ID, revision,
ordinal, normalized character span, exact text digest and chunker version.

An internal document hit includes document ID, namespace, source reference,
source revision, score, matched stages and chunk references. A result envelope
includes backend, generation identity, coverage and structured fallback reason.
Scores are meaningful within the returned ranking; do not merge scores from
different engines. Existing bookmark-only SearchHit callers remain supported.

## Repository ownership and source anchors

Muninn owns projection, chunking, embeddings, publishing, hydration, routing and
MCP. Braid owns only the generic query-vector API extension and its retrieval
implementation. Keep the full integration plan here instead of duplicating it
in both repositories. Each Braid change should link its package A reference.

Source basis: Muninn `37fbd0cd31b031a02d8f57c85b355821d39c400b`; Braid
`7e24af358ca822b70f6238f77b7dca498e03d4eb`. Reinspect affected source before
implementation and preserve later unrelated changes.

- [Current search interface](../../src/muninn/search/base.py)
- [Current backend registry](../../src/muninn/search/__init__.py)
- [Current MCP search](../../src/muninn/consumers/mcp/tools.py)
- [Embedding implementation](../../src/muninn/vector/embed.py)
- [Current Qdrant reconciliation](../../src/muninn/vector/reconcile.py)
- [ADR-008/014 and other decisions](../muninn-decisions.md)
- [Existing roadmap](../muninn-roadmap.md)
- [Braid source snapshot](https://github.com/algorhythmic/braid/tree/7e24af358ca822b70f6238f77b7dca498e03d4eb)

ADR-014 currently retains Qdrant as the default and describes a later Braid
comparison. These references record implementation work without changing that
default, declaring adoption or running the historical evaluation backlog.
Record any eventual adoption/default decision in the existing ADR log.

## Evidence and current operator policy

At authoring time the active Odin YOLO decision is enabled. Checks:
**SKIPPED (YOLO MODE)**. Do not create, modify or run tests, validation,
evaluation, review, benchmark, probe, smoke or dry-run passes while that policy
applies. Source/diff inspection is permitted; packaging, builds, startup and
service operations remain usable. Propagate the active policy to any future
workers. Turning YOLO off does not automatically schedule checks.

Before implementation, reread the current runtime YOLO decision (or its tracked
fallback when no runtime reference is supplied). This dated snapshot does not
freeze the mode or override a later operator instruction.

Each package names the artifacts and behavior its implementation should deliver.
These outcomes are implementation references, not a mandatory sequence of
review/evaluator gates. Delivery notes must distinguish source changes, build
receipts, runtime activation and observed behavior. Do not describe an
unexecuted check as passing or claim retrieval-quality improvement from code.
