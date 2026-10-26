# Braid query-vector extension — interface contract (draft)

Date: 2026-10-26. Status: draft interface for package A; not implemented.
Parent: [integration references](README.md), [package A](A-EMBEDDING-COMPATIBILITY.md).

## Ownership

- **Owner: the Braid orchestrator** (Odin project Braid). It owns this contract,
  the generic Braid implementation and Braid's protocol guide. Every Braid change
  links package A.
- **Consumer: the Muninn orchestrator** (Odin project Muninn). It owns the
  embedding profile, query-vector production, publishing and fallback routing.
- Amendments: the owner proposes a revision here (bump `Contract revision`);
  the consumer confirms or objects in its checkpoint; the Odin executive
  arbitrates unresolved conflicts. Braid stays generic: no Muninn-specific
  names or behaviour in Braid.

Contract revision: 0 (draft from package A; the owner confirms or amends after
reinspecting Braid source at or after `7e24af3`).

## Negotiation

- Protocol version 1; capability name `query_embedding` is advertised in the
  capability list. Callers send the field only after seeing the capability.
- An older binary without the capability is incompatible with the integration;
  Muninn routes through package C's legacy fallback.
- No protocol major-version change for this optional capability.

## Request field

Accepted on `query`, `explain` and `export_context`:

```json
{ "query_embedding": { "model": "<embedding space id>", "vector": [0.12, -0.04] } }
```

- `model` names the complete embedding space (for Muninn,
  `muninn-embedding-profile:<digest>`), compared exactly with the configured
  `embed.model` of the published dataset; `embed.provider` is `none`.
- When present, the vector is authoritative for the dense stage; no Provider
  call occurs. Applies to fused queries and raw dense retrieval.
- When absent, existing provider-based text retrieval and anchor-vector
  averaging are unchanged.
- If dense retrieval is disabled, the field is still validated and never
  invokes a provider.

## Validation and errors

Reject with a typed embedding-incompatibility error and a structured protocol
error code (names fixed by the owner in revision 1):

| Condition | Required behaviour |
| --- | --- |
| empty vector, all-zero vector, NaN/Inf element | reject |
| `model` differs from the dataset's embedding space | reject |
| dimension differs from the published space | reject, even when filters yield no candidates |
| vector plus anchor-vector averaging in one request | reject as ambiguous |
| explicit graph anchors with a text/vector query | allowed for the graph stage |

An empty filtered result never hides a model or dimension mismatch.

## Handoff the owner returns (revision 1)

1. Braid revision and binary identity that implements the capability.
2. Capability name and field shape as implemented (strict-decoder behaviour).
3. Error type and protocol error codes, one per condition above.
4. Protocol guide section path.
5. The absent-vector compatibility path explained at source level.

Muninn's package A side (profile schema, cache, query helper) consumes this
handoff; packages B and C depend on both.
