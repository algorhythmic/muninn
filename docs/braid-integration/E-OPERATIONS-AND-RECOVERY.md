# E — Freshness, lifecycle, activation and recovery

Status: planned, 2026-10-25. Parent: [integration references](README.md).
Repository: Muninn; deploy a pinned Braid binary with A's capability.
Dependencies: B's safe publisher and registry, C's routing and D's context
contract where installed. B already owns basic crash-safe publication.

## Objective and ownership

Keep serving and indexing recoverable while source data and authored files
change. Expose requested/effective backend, source coverage, publisher state
and activation identity. Permit legacy fallback and generation rollback without
altering canonical data. Keep valuable runtime state outside both checkouts.

Proposed modules: index/freshness.py, index/worker.py, index/runtime.py;
extend B's publisher, manifest, client, registry and consumers/cli/index.py.
Integrate with [status CLI](../../src/muninn/consumers/cli/status.py),
[configuration](../../src/muninn/config.py) and existing deploy conventions.

B owns the additive migration and initial epoch/registry schema. E extends that
same ownership rather than maintaining another pointer, dirty queue or daemon
with independent activation authority.

## Freshness model

Captured mutations increment B's source epoch in the same SQLite transaction.
Name the exact tables/fields covered; all data affecting projection, visibility
and relations must participate. Older writers use the same triggers. Retain
privacy propagation for derived outputs and hidden child links.

Authored files are external to SQLite. Use a bounded, digest-bound inventory of
permitted namespaces; stable reads hash the bytes actually read and detect file
replacement/edit during reading. Record failed scans and coverage. File
timestamps are discovery hints, not source revision identities. Renames and
removals become new inventories; a missing vault or failed scan is not an empty
corpus and cannot cause silent mass deletion.

Compare the requested scope with the active generation's epoch, inventory and
profiles before selecting it. A stale or incompletely covered scope falls back
through C. Recheck returned source digests/visibility before emitting text.
Authored files do not share SQLite's transaction boundary: retain those
request-time checks and describe freshness as observation-bound, not an atomic
snapshot of every live filesystem path.

## Publisher coordination

Initial operation is explicit index sync. Add an optional supervised worker
after the synchronous publication contract exists. Relevant changes coalesce
into one pending build per corpus; new changes during a build record the newer
epoch instead of spawning overlapping publishers. Use a private OS-backed lock
or equivalent lease with reconcilable ownership; a remembered PID alone does
not authorize stealing a live publisher's work.

The worker uses A's reusable embedding cache and B's staging protocol. Expensive
model work is outside canonical write transactions. Capture, scrape, enrichment,
FTS and Qdrant operations do not wait on Braid rebuilds.

Keep durable states: pending, building, ready, failed, stale and active, with
generation/job identity and safe failure category. Active-generation identity
is a separate reference; a new failed build does not replace it. Restart
reconciles staging state before retry. Derived staging can be abandoned and
rebuilt; do not blindly retry an uncertain canonical activation.

## Process lifecycle

Use one shared client implementation. Pin the absolute binary path, build
identity, protocol version and required capabilities. The runtime keeps a
bounded pool of long-lived stdio sessions per generation; the minimal initial
configuration may use one session. Current Braid processes serve requests
serially, so the client serializes each connection and includes queue waiting
in its wall-clock request deadline.

Negotiate on every new connection, bound request/response sizes, and reserve
stdout for protocol messages. A timed-out or malformed exchange invalidates
that connection; late responses must not be mistaken for another request.
Restart backoff prevents repeated expensive startup during an outage. C owns
legacy fallback while the runtime is unavailable.

Readers pin the active generation at request start. On activation, new requests
use the new generation; old sessions drain under bounded deadlines before
closing. They still apply current source rules. Store content is immutable by
serving policy even if SQLite opens runtime sidecars. No mutation RPCs are
exposed to ordinary search callers.

## Proposed operator interface

| Command/configuration | Meaning |
| --- | --- |
| muninn index sync --backend braid | Build a ready generation; leave serving selection unchanged |
| muninn index status | Report active and pending identities, coverage, freshness and failures |
| muninn index activate GENERATION | Reconcile freshness/profile/scope and commit the active reference |
| muninn index rollback GENERATION | Select a retained compatible generation, subject to current source rules |
| muninn index worker | Optional supervised coalescing publisher |
| MUNINN_SEARCH_BACKEND=fts5-qdrant | Bypass Braid and use existing search behavior |
| MUNINN_SEARCH_BACKEND=braid | Request Braid with C's automatic legacy fallback |

Finalize private storage/binary path configuration in config.py and document
effective values without logging secrets. Configuration intent, ready build,
active generation and backend actually used are different status fields.
These command names are proposed and do not exist merely because documented.

## Activation, rollback and retention

Complete and persist generation files before a short canonical transaction
switches the active reference. Compare the captured epoch in that transaction;
reconcile authored inventory before switching and rely on canonical hydration
for subsequent edits. Journal activation identity so an unknown return can be
resolved by reading the pointer rather than repeating a mutation blindly.

Legacy fallback stays usable throughout: continue existing FTS/Qdrant publishing
and reconciliation. A runtime/config rollback never rewrites sources or drops
new additive tables. Do not promise arbitrary old-code compatibility; document
the specific migration/reader behavior observed during implementation. Braid's
new optional query-vector field remains capability-negotiated.

A retained old generation can be too stale or incompatible to serve. Report
that state and use legacy fallback; rollback is not permission to reveal
removed content. Retain active, previous and leased generations. Cleanup may
remove only unreferenced private derived generations/staging/cache entries,
never canonical captures, authored Markdown or files still used by readers.
Close clients before removing SQLite files and their sidecars.

## Observability and completion artifacts

Report source epoch/inventory, profile, generation, Braid revision/digest,
publisher age/state, counts/omissions and structured fallback reasons. Keep raw
queries, source text, vectors, sensitive locators and prompt bodies out of
ordinary logs. No recurring notification or automation is created by this plan.

Deliver worker/runtime ownership, bounded recovery, operator commands and
deployment instructions, retention rules and source/default decision records.
Update the roadmap with actual implementation/activation receipts rather than
checking off the old quality-comparison task from source changes. Evidence
remains governed by the
[current operator policy](README.md#evidence-and-current-operator-policy).
