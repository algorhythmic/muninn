# D — Bounded context, excerpts and attribution

Status: planned, 2026-10-25. Parent: [integration references](README.md).
Repository: Muninn. Dependencies: A's profile, B's chunk/source contracts and C's
document retrieval, hydration and fallback. Consumer: MCP clients such as Odin.

## Objective and existing mechanisms

Expose a richer briefing API that selects exact excerpts with citations inside
caller-specified cost and serialized byte bounds. Reuse Braid's required-context
and diversity selection rather than writing a second packing engine in Muninn.

Braid already supports required closures, mandatory nodes, diversity-aware
admission and projected export. Relevant source is
[selection.go](https://github.com/algorhythmic/braid/blob/7e24af358ca822b70f6238f77b7dca498e03d4eb/pkg/braid/selection.go),
[export.go](https://github.com/algorhythmic/braid/blob/7e24af358ca822b70f6238f77b7dca498e03d4eb/pkg/braid/export.go)
and its [retrieval contract](https://github.com/algorhythmic/braid/blob/7e24af358ca822b70f6238f77b7dca498e03d4eb/docs/retrieval-contract.md).

Proposed Muninn additions: search/context.py for assembly and accounting,
typed request/result records in search/documents.py, and a search_context wrapper
in [MCP server.py](../../src/muninn/consumers/mcp/server.py). Reuse B's source
resolver and C's router.

## Proposed request/result

```text
build_context(query, namespaces, budget, max_bytes, anchors,
              required_sources, expected_generation) -> ContextResult
```

Start with bounded defaults: 2,500 estimated text-cost units, 16,384 serialized
context bytes and at most 12 optional chunk roots. Callers can request smaller
limits; define explicit upper bounds in code/config. These defaults are design
starting points, not retrieval-quality findings.

The result has a version, backend, generation/source boundary, context array,
context_bytes, cost accounting, coverage, abstention/omission reasons and
fallback diagnostics. Each context entry identifies an exact source/revision,
namespace, cited span, text digest and excerpt. Safe optional fields include a
title, heading breadcrumb, matched stages and why it was selected.

Resolve anchors and required sources through trusted source mappings. Do not
accept arbitrary private filesystem paths or treat source text as caller scope.
An expected generation is a consistency precondition; if it cannot be served,
return an explicit conflict/unavailable outcome rather than silently answering
against another generation.

## Packing and graph semantics

Publish small attribution nodes as required context for each excerpt. Their
bytes/cost count against the same limit. Shared attribution enters once; a
chunk whose required attribution cannot fit is omitted as a complete bundle.
Mandatory sources that cannot be supplied cause explicit abstention, not a
partial mandatory answer. Prefix text added for embedding is distinct from the
cited body span and must be labelled accordingly.

Use Braid's requires/mandatory_ids for this purpose. Its graph edges merely
describe relationships and never imply that every linked article must be sent.
Do not present these context dependencies as execution/task scheduling.

Support explicit known-document anchors, resolved to nodes in the pinned
generation. Automatic graph seeding remains outside the first ordinary search
path. For explicit graph requests, materialize only permitted sources; output
filters alone do not hide traversal intermediates or IDs in explanations.

Set explicit stage weights and diversity settings. Keep temporal contribution
off by default for a historical archive. A minimum rank-fusion score or
RequireEvidence is a selection policy, not calibrated truth confidence.

## Source hydration and byte accounting

Plan against one generation, then resolve each selected source and span through
the shared current visibility path. Compare revisions and text digests before
returning either excerpts or Braid explanations. Source content changing after
selection invalidates the affected selection; one bounded reassembly is allowed.
Never individually truncate a chunk or citation out of an admitted bundle.

Make Braid's exported projection match the final context representation as
closely as possible. Count UTF-8 JSON bytes of the actual final context array,
including citations, headings, metadata and explanations. If canonical
hydration changes a projected field, recompute the bound and reassemble or omit
complete optional bundles. Do not claim Braid's byte limit automatically covers
a later-expanded Muninn response.

The diagnostic envelope is outside context_bytes and has a separate small cap.
Document that distinction for MCP callers. Text-cost estimates are labelled by
estimator/version. A strict byte bound is not a claim of model-exact token cost.
Callers reserving a model prompt must include their own fixed overhead.

## Legacy fallback

If Braid cannot serve, use C's complete legacy search path to obtain source
candidates. Resolve candidate bodies through B's shared projection and choose
bounded lexical excerpts with their exact references. Label fallback coverage
and limitations, including keyword-only authored-note recall where applicable.

Do not invent Braid graph/diversity explanations for a legacy result. Enforce
the same final byte bound and mandatory attribution rules. If a requested
required-source bundle or consistency precondition cannot be honored through
fallback, return unavailable/abstained with a reason instead of an incomplete
briefing represented as complete.

Existing search, semantic_search and fts_search stay available. search_context
adds a capability; it does not rewrite authored notes, accept project work or
turn excerpts into canonical project decisions.

## Implementation sequence and completion artifacts

1. Define bounded request/result records and attribution projection jointly
   with B/C; document the exact byte-counted field.
2. Map chunks/attribution to Braid requires and export_context. Supply A's query
   vector and explicit profiles/weights.
3. Add canonical rehydration, digest checks and complete-bundle recovery.
4. Add degraded legacy excerpt assembly using the shared source layer.
5. Expose the additive MCP tool and document it in docs/mcp-api.md.

Return the tool contract, exact citation format, cost estimator, byte-accounting
definition, fallback behavior and implementation revision. Keep source,
activation and live-consumer evidence distinct under the
[current operator policy](README.md#evidence-and-current-operator-policy).
