# C — Search integration and FTS/Qdrant fallback

Status: planned, 2026-10-25. Parent: [integration references](README.md).
Repository: Muninn. Dependencies: A's query-vector contract and B's generations,
source resolvers and freshness metadata. Consumers: D and E.

## Objective and affected interfaces

Serve captured-document and authored-note search through Braid while retaining
the current FTS/Qdrant and authored-scan path as a complete fallback. Exact
source hydration remains Muninn-owned. Existing bookmark-only tools and raw FTS
syntax keep their public behavior.

Existing code: [search/base.py](../../src/muninn/search/base.py),
[registry](../../src/muninn/search/__init__.py),
[FTS/Qdrant backend](../../src/muninn/search/fts5_qdrant.py),
[hybrid fusion](../../src/muninn/search/hybrid.py),
[MCP tools](../../src/muninn/consumers/mcp/tools.py) and
[MCP wrappers](../../src/muninn/consumers/mcp/server.py).

Proposed additions: search/documents.py for typed document results,
search/braid.py for Braid access, search/routing.py for request routing and
fallback. B owns the shared process client and source resolution layer; avoid
creating another subprocess implementation here.

## Internal document contract

Add a document-search interface alongside the existing SearchBackend:

```text
search_documents(query, namespaces, limit, options) -> DocumentSearchResult
DocumentHit(document_id, namespace, source_ref, source_revision,
            score, matched_stages, chunk_refs)
DocumentSearchResult(hits, backend, generation, coverage,
                     fallback_reason, warnings)
```

Use typed records and a schema/version identifier for internal serialization.
Generation identifies corpus/scope, generation ID and Braid revision/digest.
Namespace names remain bookmarks, concepts, infra, people and projects.

Keep SearchHit(bookmark_id, score, index) and current SearchBackend callers.
The Braid bookmark adapter resolves document/chunk hits to bookmark IDs and
preserves visibility-aware hydration. Do not encode a Markdown path into an
integer bookmark ID. The unified search API uses the new document contract.

## Query flow

1. Validate query, namespace and limits before choosing a backend. Scope comes
   from the trusted caller, not arbitrary Braid dataset IDs supplied by content.
2. Select and pin one suitable active generation. Compare profile, source epoch
   and authored inventory under B/E's freshness rules. Refuse incomplete coverage
   for the requested scope unless an explicit partial-search option permits it.
3. Use A to encode a nonempty semantic query against that generation's profile.
   Send expected_revision, namespace eligibility and explicit stage weights.
4. For ordinary search, use lexical+dense; graph=0 and temporal=0 initially.
   Request enough chunk candidates to collapse to the requested document count,
   with a bounded candidate cap. Do not promise a globally complete top-k when
   that cap is reached.
5. Collapse chunks by document using strongest-hit score initially, retaining
   supporting chunk references. Do not sum scores in proportion to document length.
6. Rehydrate current canonical metadata and verify source revision/text digests
   before returning excerpts or explanations. Reuse hidden-link and derived-input
   masking. A source changed during the request is stale, not a valid exact hit.
7. Project to the existing MCP output shape, adding optional diagnostic fields.
   Authored hits keep vault-relative paths; captured hits keep bookmark identity.

The existing Braid query path performs budgeted item selection, whereas this
interface requests document limits. Set explicit retrieval bounds and a
document-search cost policy; do not mistake Braid's default 2,500 cost units for
a top-k limit or silently drop long sources because of its text-cost fallback.
Keep the source projection chunk size bounded. A generic raw fused retrieval
API may be introduced only if the existing query path cannot supply sensible
document candidates without becoming a second context packer.

## Public compatibility

| Caller | Behavior with Braid selected |
| --- | --- |
| fts_search | Existing FTS5 path and MATCH syntax; no semantic encoding |
| semantic_search | Captured-only dense Braid search; existing legacy fallback semantics |
| search(query, namespace, top_k) | Unified document ranking through Braid; current fields retained |
| get_bookmark/get_page/list_namespace | Existing canonical access and path rules |
| future search_context | D's richer briefing contract, not a new meaning for SearchHit |

Keep namespaces and maximum result counts bounded as today. A valid empty result
does not imply a backend failure. Do not run a legacy search simply to fill an
empty Braid response or mix scores from separate rankings.

## Fallback policy

The default remains `MUNINN_SEARCH_BACKEND=fts5-qdrant`. Register `braid` as an
explicit alternative. Its routing wrapper uses the legacy implementation
directly; never resolve the configured backend recursively during fallback.

| Condition | Action |
| --- | --- |
| Missing binary, absent generation or unavailable subprocess | Whole request uses legacy search |
| Unsupported protocol/capability, embedding mismatch or query encoding unavailable | Whole request uses legacy search |
| Stale generation, source epoch or authored inventory | Whole request uses legacy search; expose stale_index |
| Read deadline, malformed/truncated process response or process exit | Dispose of that connection; whole request uses legacy search |
| Valid Braid no-match or deliberate evidence abstention | Return that result |
| Invalid query or denied source/scope | Return the error; do not bypass it through fallback |
| Canonical source inaccessible | Preserve existing fail-closed behavior |

Whole-request fallback calls today's captured FTS/Qdrant hybrid path plus the
authored scan and current lane merge, restricted to the requested namespace.
Qdrant/embedding failures continue degrading to FTS through the existing code.
Report authored-scan bounds and unavailable namespaces. Do not claim fallback
has the same full-text semantic coverage as Braid.

On source changes detected during hydration, abort that Braid result and allow
one bounded retry/fallback path. Do not emit a partly stale mixture. Native
protocol/model errors are normalized to short safe reason codes; logs contain
backend/generation/error categories, not raw queries or content.

## Delivery and recovery

Keep the legacy writer/reconciler active and retain its current configuration.
Switching the environment setting back to fts5-qdrant bypasses Braid without
source migration. A rollback to an older Braid binary negotiates capabilities
again; unsupported query vectors cause fallback rather than guessed behavior.

Completion artifacts: the new internal result type, registered backend,
whole-request router, document collapse/hydration path and additive MCP
diagnostics. Report effective backend and fallback reason independently from
requested configuration. C is the first useful integration milestone; D's
briefings and E's automatic publisher need not delay this path. Evidence follows
the [current operator policy](README.md#evidence-and-current-operator-policy).
