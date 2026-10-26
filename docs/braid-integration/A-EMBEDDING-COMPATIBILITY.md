# A — EmbeddingGemma compatibility

Status: planned, 2026-10-25. Parent: [integration references](README.md).
Repositories: Muninn and Braid. Dependencies: none. Consumers: B, C and D.

## Objective and resulting behavior

Use Muninn's existing EmbeddingGemma implementation for both index inputs and
queries. Braid accepts precomputed vectors bound to an explicit embedding
profile. Index and query inference retain their distinct document/query
prompts. Existing Qdrant behavior continues on its current configuration.

## Current implementation and affected files

Muninn's [vector/embed.py](../../src/muninn/vector/embed.py) dispatches
embed_document and embed_query to sentence-transformers using separate prompt
names. Model and dimension are configured in
[config.py](../../src/muninn/config.py). The hash backend is a placeholder and
must not make a semantic generation eligible for activation.

Braid's current dense retriever requires its own Provider for text queries and
returns no dense results when both provider configuration and runtime provider
are absent. Supplied vectors require changing that branch, not just adding a
JSON field.

| Repository | Existing integration points | Proposed additions |
| --- | --- | --- |
| Muninn | vector/embed.py, config.py | index/embedding_profile.py, index/embedding_cache.py; bounded document batching |
| Braid | pkg/braid/types.go, query.go, retrieve.go; cmd/braid/protocol.go | Optional query_embedding field and negotiated capability |

## Embedding profile

Create a versioned profile object containing resolved model/artifact revision,
output dimension, normalization, query/document prompt identities and prompt
digests, relevant tokenizer behavior and inference adapter version. Hash its
canonical encoding into `muninn-embedding-profile:<digest>`.

Resolve mutable model aliases to an artifact identity before publishing. If
that identity cannot be established, report the profile as incomplete rather
than claiming two builds use the same space. Chunker and projection versions
belong in the generation manifest; cache keys also bind the exact text actually
sent to the document encoder.

Store document vectors in a private rebuildable cache keyed by profile ID plus
embedding-input digest. Retain dimension and numeric encoding metadata. Keep
source text out of cache keys and logs. A corrupt or incompatible entry is a
cache miss, not permission to use a different model. Cache placement is outside
the checkout and outside canonical source tables.

Batch inference is bounded and lazy: importing CLI/search modules must not load
sentence-transformers. Reuse the existing model-loading lock and define whether
inference calls serialize on the loaded model. A cancelled publishing operation
may retain complete cache entries but cannot activate a partial generation.

## Proposed Braid contract

Negotiate protocol version 1 with required capability `query_embedding` before
using this additive field on query, explain or export_context:

```json
{
  "query_embedding": {
    "model": "muninn-embedding-profile:<digest>",
    "vector": [0.12, -0.04]
  }
}
```

The numeric array is illustrative, not an EmbeddingGemma vector. The configured
Braid `embed.model` and every published document embedding use the same profile
ID; `embed.provider` is `none`. Braid's model string denotes the complete space,
not just the upstream model name.

When supplied, the vector is authoritative for the dense stage and causes no
Provider call. Reject empty, zero, nonfinite, wrong-model or wrong-dimension
vectors. Apply the rules to fused queries and raw dense retrieval. Reject
ambiguous vector-plus-anchor-averaging requests; explicit graph anchors may
still accompany a text/vector query for the graph stage.

When absent, retain existing provider-based text retrieval and anchor-vector
averaging. If dense retrieval is disabled, a supplied vector remains subject to
request shape/model/numeric checks; it must not invoke a provider. An older
binary lacking the capability is incompatible with this integration and routes
through C's legacy fallback.

Introduce a typed embedding incompatibility error and a structured protocol
error code. Exact dimension is checked against the published model space even
when eligibility filters yield no candidates; an empty filtered result must not
hide a profile mismatch.

## Compatibility and recovery

Old requests remain valid. New callers negotiate before sending the field, so
old strict decoders never receive it. Do not change the protocol major version
for an optional negotiated capability. Do not alter Qdrant's collection model,
dimension or query/document prompting as part of this work.

A profile change creates a new index generation and cache namespace. It cannot
reuse a same-dimension vector from a different profile. Query inference failure
or incompatibility is reported to C; no silent switch to hash vectors or a
different embedding provider occurs.

## Implementation sequence and handoff

1. Define the profile/cache contract and expose bounded document encoding in
   Muninn without changing existing embedding callers.
2. Extend Braid's types, strict decoding, capability list, dense-stage dispatch
   and structured errors. Document the extension in Braid's protocol guide.
3. Add a Muninn helper that produces a query vector with the generation's exact
   profile, refusing unavailable profiles.
4. Return the Braid revision/binary identity, capability name, field shape,
   error codes and profile schema to B/C.

Completion artifacts: generic Braid support, Muninn profile/cache modules,
updated protocol documentation and a source-level explanation of the absent
vector compatibility path. Runtime and quality claims require separate evidence
when permitted by the [active policy](README.md#evidence-and-current-operator-policy).
