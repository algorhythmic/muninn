# Proposed contract amendment — revision 1

Package A: https://github.com/algorhythmic/muninn/blob/8bdda8b/docs/braid-integration/A-EMBEDDING-COMPATIBILITY.md

Owner source inspection: Braid base `7e24af358ca822b70f6238f77b7dca498e03d4eb`.
Proposal for executive transfer into the Muninn contract; Muninn is unchanged.
Consumer confirmation is pending. Implementation revision/binary identity and
check evidence are supplied in the companion receipt.

Contract revision: 1 (owner proposed amendment).

Protocol remains version 1. Capability `query_embedding` is advertised by hello;
callers negotiate it before supplying the optional field. Older binaries route
through the consumer's existing fallback.

Field shape: `query.query_embedding` on query/explain;
`export.query.query_embedding` on export_context:

```json
{"model":"example-space-v1","vector":[0.12,-0.04]}
```

Go shape: `Query.QueryEmbedding *QueryEmbedding`, containing `Model string` and
`Vector []float32`. Omitted or null query_embedding means absent. Inside an
object, omitted/null vector is empty and omitted model is empty. A null model
or null vector element is a strict schema error. Exact field spelling,
unknown-field rejection and duplicate-key rejection apply recursively. Wrong
types or float32 overflow are `invalid_request`; invalid JSON, including
NaN/Infinity tokens, is `parse_error` before engine validation.

The model is a nonempty complete embedding-space ID, exactly equal to configured
`embed.model`. Supplied vectors are authoritative for fused and raw dense
retrieval without a Provider call, including with embed.provider=none. The
absent-vector behavior is unchanged. Even with dense weight zero, supplied
vectors receive all validation. All-zero means every float32 element is zero;
native Go NaN/Inf values receive typed nonfinite errors.

Dimension comes from a stored embedding of the configured model in the pinned
dataset snapshot, without eligibility filters. There is no dimension field in
the existing config or dataset schema. An empty model space fails closed with
`embedding_dimension_unavailable`; dimension is never inferred from the query.
Deleting the final stored vector therefore makes the dimension unavailable.

Anchor ambiguity is resolved as follows: supplied vector plus nonempty anchors
and blank/whitespace text is rejected (the preexisting anchor-averaging form).
Nonblank text plus supplied vector and anchors is allowed; the supplied vector
drives dense retrieval and anchors remain graph seeds. A vector without text or
anchors is allowed.

Semantic errors have Go type `*braid.EmbeddingIncompatibilityError` with exported
`Code` and `Message` fields; `errors.As` works through wrappers. Protocol error
code equals the typed error's Code:

| Condition | Code |
| --- | --- |
| Empty/missing/null vector in a supplied object | embedding_empty_vector |
| All-zero vector | embedding_zero_vector |
| Nonfinite element (native Go) | embedding_nonfinite_vector |
| Missing/empty model, empty configured model, or exact model mismatch | embedding_model_mismatch |
| Dimension differs from unfiltered published model | embedding_dimension_mismatch |
| Supplied vector with anchors and blank text | embedding_ambiguous_anchors |
| No stored model dimension | embedding_dimension_unavailable |

Model and dimension validation apply even if filters allow no nodes. Storage or
provider failures retain existing error semantics. No schema/default/ADR changes.

Protocol guide: `docs/protocol.md#package-a-query-embedding-extension`.

Absent-vector source path: `queryOutput` and raw `engineRetriever.retrieve`
validate the optional extension then use the existing retrieval paths.
`dense` retains its existing none-provider early return when the optional field
is absent; nonblank text calls Provider.Embed and validates its output, while
blank text deduplicates anchors and averages their stored vectors. Supplied
vectors select the new branch ahead of those paths.
