# Muninn Decisions

Architecture Decision Records (ADRs) for Muninn. Each entry captures a key choice, why it was made, and what it commits us to.

New entries go at the bottom. Existing entries may be superseded but should not be deleted — crossed-out decisions still carry rationale that future work needs to understand. When superseding, add a new entry and mark the old one's status as "Superseded by ADR-NNN".

Format: title, status, context (what was the situation), decision (what we chose), rationale (why), consequences (what this commits us to).

---

## ADR-001 — Unified wiki over separate Muninn + Memex

**Status:** Accepted (2026-04-19); amended by ADR-011 (2026-10-21)

**Context.** The original framing had Muninn as a bookmark-focused knowledge base and Memex as a separate future project for homelab/infra state. Session journals from Saga's Bragi were intended to land in yet a third place. This implied three would-be-separate systems for persistent memory, each with its own vault, Qdrant index, MCP server, and git repo.

Karpathy's *llm-wiki* pattern (gist, 2026-04) proposes a different architecture: one wiki per operator, with content differentiation via **namespaces and schema rules**, not via separate systems. The pattern's central claim is that cross-namespace linking is where compounding value lives — a bookmark about distributed consensus should link to a project page about Saga (which uses consensus ideas) and to a concept page about consensus, all surfacing together in a single graph.

**Decision.** Unified Muninn. Single Obsidian vault. Namespaces: `bookmarks/`, `projects/`, `sessions/`, `infra/`, `concepts/`, `people/`. Single Qdrant index, single MCP endpoint, single git repo. Memex retires as a separate project name; its intended scope (homelab and infrastructure state) lives as the `wiki/infra/` namespace inside Muninn.

**Rationale.**
- The value of the pattern is *cross-namespace* synthesis. Separate vaults destroy this — nothing crosses the boundary, the LLM doesn't notice connections, retrieval reverts to siloed RAG.
- Obsidian's graph view, backlinks, and Dataview queries are designed for one vault, not federated ones.
- One MCP endpoint is simpler to expose to Claude.ai than three. One set of credentials, one connector, one mental model.
- The "separate project" framing in Muninn's original README was a v1 scope-management statement, not an architectural commitment.

**Consequences.**
- One codebase, one deployment, one MCP endpoint.
- Cross-namespace links work natively.
- Scope creep risk: the unified architecture creates pressure to populate all namespaces in v1. Defense: ADR-005 commits to v1 scope discipline.
- The `wiki/infra/` namespace will eventually need the content that was going to live in Memex. Not blocking v1.

**Amendment (2026-10-21, ADR-011).** The single vault, index and MCP endpoint stand, but they now cover *saved and curated knowledge* only. Saved material lives in `wiki/bookmarks/`, with its source in frontmatter (X first, then browser bookmarks and later sources). Curated notes live in `concepts/`, `people/` and `infra/`. `projects/` holds operator-written reference notes, not canonical state, and `sessions/` is retired. Odin owns project state and Skald owns session transcripts. The "Bragi journals as a third store" framing above is historical.

---

## ADR-002 — Haiku worker pool over dvergr pattern for bulk enrichment

**Status:** Accepted (2026-04, revised from earlier "build via Saga" framing); partially superseded by ADR-013 (2026-10-21) — the bulk Haiku decision stands, the synthesis-execution half is superseded

**Context.** An earlier framing treated Muninn as an exercise of Saga at scale — submit `bookmarks.html` to Saga, Odin decomposes into N parallel scrape-and-summarize workstreams, each dvergr handles a slice. This made Muninn v1 dependent on Saga Phase 3 (spec decomposition + multi-dvergr launch), which has not shipped.

Analysis of the bulk enrichment shape revealed that per-item summarization doesn't need Saga's orchestration infrastructure. The bottleneck is HTTP wait + LLM API latency, which a simple Python asyncio worker pool handles. The dvergr pattern — persistent Claude Code sessions authenticated against a shared credentials volume — is designed for long-running coding tasks that converge on a spec. Per-item stateless summarization is a different shape; forcing it through dvergar is over-engineering.

**Decision.** Bulk bookmark enrichment uses a Python async worker pool calling the Anthropic API directly with Haiku 4.5. No Claude Code instances, no dvergar, no `saga-claude-credentials` volume for this job.

Synthesis jobs (era narratives, deep passes, ad-hoc analyses) *do* run in a Saga-style container with Opus 4.6 and the credentials volume — see SPEC.md §5. This is a sibling of dvergr, not a consumer of Odin/Heimdall.

**Rationale.**
- Different workloads want different tools. Stateless API calls for bulk; persistent sessions for sustained coding work.
- Direct API calls are cheaper for this job (Haiku is cheap per token; no container overhead).
- Muninn v1 is not blocked on Saga Phase 3 — it runs on Saga's Phase 1+2 infrastructure (credentials volume pattern, devcontainer base image) but does not require Odin's decomposition.
- The dvergr pattern stays reserved for its native use case: finite-PRD coding tasks with a convergence criterion.

**Consequences.**
- Muninn v1 shippable before Saga Phase 3.
- Two models in play (Haiku for bulk, Opus for synthesis). Two different auth mechanisms (direct API for bulk, credentials volume for synthesis container).
- Clear separation of concerns carries forward to other projects: Huginn's enrichment (also stateless per-item) uses the Haiku pattern, not dvergr. See Huginn spec §8.

---

## ADR-003 — Huginn as separate service, not merged into Muninn

**Status:** Superseded by ADR-015 (2026-10-21)

**Context.** Huginn depends on Muninn at the data layer: it queries Muninn's MCP for scoring signals (contributor overlap, concept overlap, already-bookmarked check) and writes promoted items into Muninn's vault via `raw/inbox/huginn/`. The dependency is real and structural. The question arose: should these two systems be merged into one service?

**Decision.** Huginn and Muninn stay as separate services with a clean interface between them (MCP reads + vault writes).

**Rationale.**

*The dependency is data-shaped, not code-shaped.* Huginn calls Muninn's MCP endpoint the same way it calls GitHub's API — over a well-defined network boundary with a contract. The MCP interface decouples them at the code layer even while coupling them at the data layer. You wouldn't merge Huginn with GitHub because of the GitHub dependency; same principle applies here.

*Muninn has other consumers besides Huginn.* Claude.ai sessions query Muninn's MCP directly during context bootstrap. Obsidian browses the vault without Huginn in the loop. Saga's Bragi writes to the vault (Phase 3). Muninn is the substrate for multiple things. Merging Huginn into Muninn would pull discovery-specific code (source adapters, schedulers, dashboard UI) into the codebase all those other consumers load — wrong shape.

*Operational cadences differ sharply.* Muninn is low-volume, careful, git-versioned, stable. Huginn is high-volume, disposable, polling constantly, with 99% of its work discarded. Coupling their lifecycles means Reddit adapter failures affect knowledge-base availability; Qdrant reindexes slow Huginn's fetch loop; bad Huginn deploys knock MCP offline mid-query for Claude sessions. These aren't theoretical risks — they're the normal shape of coupling two things with different cadences into one process.

*Neither hard-depends on the other.* Muninn is useful without Huginn. Huginn degrades to rule-based scoring without Muninn (graceful, not hard failure). Two services where either can fail without taking the other down is the opposite of the architecture that argues for merging.

**Consequences.**
- Two codebases, two deploy cycles, one MCP + vault interface between them.
- Promotion action is the one explicit coupling point: Huginn writes `raw/inbox/huginn/*.md`, Muninn's normal ingest pipeline picks it up.
- Physical colocation (same Proxmox VM, separate containers) is compatible with logical separation if deployment overhead is a concern. Default: separate LXCs.

---

## ADR-004 — Vault as source of truth for project state; Bragi as its author

**Status:** Superseded by ADR-011 (2026-10-21)

**Context.** The broader problem this whole ecosystem is trying to solve: Claude session memory drifts silently. The operator manually maintains context files ("Updated_Progress") to keep sessions informed. Handoff documents go stale. Memory edits pushed from conversations get the high-signal facts but lose rationale. userMemories accumulates outdated information over time.

This is structural, not a bug. LLM state lags reality at the interface layer — surface-level fixes treat the symptom rather than the disease.

The Karpathy llm-wiki pattern identifies a durable solution: a markdown corpus maintained by the LLM that any future session can query at will. Saga's Bragi already writes session journals to `/opt/saga/vault`; Odin/Mimir/dvergar have a file-based communication protocol. The substrate for this already exists inside Saga; it just needs to connect to Muninn's vault and expose through MCP.

**Decision.** Muninn's `wiki/sessions/` holds Bragi-authored session journals, one per Saga run. Muninn's `wiki/projects/{project}.md` holds the canonical current state for each active project, upserted by Bragi when a session's work materially affects that project (phase completions, scope shifts, renames, decisions ratified). Muninn's MCP is exposed as a Claude.ai remote MCP connector; future Claude sessions query current state directly during context bootstrap.

**Rationale.**
- The userMemories system is fundamentally reactive — it records what was said, not what is currently true. The vault is proactive: Bragi writes *the truth as of this session* and subsequent sessions read it.
- Bragi already does this for Saga's internal vault. The only missing piece is pointing at Muninn's vault as a second output target.
- MCP over Tailscale is simple operationally. No credentials to share; Tailscale's authentication is the trust boundary.
- Version control (git) gives a full audit trail of how project state evolved.

**Consequences.**
- userMemories becomes fallback protection, not canonical state. Memory edits should still be pushed for high-signal corrections between Bragi updates, but they're not the source of truth.
- Requires: Bragi extension (Phase 3 in the roadmap), MCP write endpoint (Phase 2), Claude.ai connector wiring (also Phase 2).
- Every Saga run leaves a trail automatically. The operator does not have to remember to update context files.
- This ADR is the motivating architecture for the entire project ecosystem. If ADR-004 isn't eventually shipped, the rest of the infrastructure is less valuable than it appears.

---

## ADR-005 — Scope discipline: v1 ingests bookmarks only

**Status:** Superseded by ADR-012 (2026-10-21)

**Context.** The unified-wiki architectural decision (ADR-001) creates a scope-creep pressure. If Muninn's architecture is unified, doesn't it follow that v1 should populate all namespaces? The fast-path answer is "yes, do everything at once" — and that reliably leads to nothing shipping.

**Decision.** v1 scope is strictly bookmark ingestion through the Haiku worker pool. All other namespaces (`wiki/projects/`, `wiki/sessions/`, `wiki/infra/`, `wiki/concepts/`, `wiki/people/`) exist as empty directories with brief README stubs describing their purpose. No ingest pipeline populates them at v1.

Population follows the roadmap phase order:
- `projects/` — manually populated by the operator in an hour of writing; enables MCP usefulness immediately.
- `sessions/` — populated by Bragi extension in Phase 3.
- `infra/` — manual drops as operator documents homelab state; possibly Bragi-assisted later.
- `concepts/` and `people/` — emergent, populated during lint passes or when cross-namespace links accumulate enough to justify extraction.

**Rationale.**
- v1 ships. Everything in the architecture is already laid out for expansion; the structure is in place; only the content varies across phases.
- Each later phase is a well-scoped chunk of work with clear acceptance criteria.
- Doing everything at once means nothing is done.
- Manual population of `projects/` (not automated by Bragi yet) is a deliberate choice: the operator spending an hour writing the current state of each project is *also* a forcing function to resolve vague project mental models.

**Consequences.**
- v1 is modestly scoped and achievable.
- Architecture laid out for expansion without refactor.
- Empty namespace directories at v1 look slightly odd to a fresh viewer; the README stubs explain this is intentional.
- Each later phase in the roadmap earns its own explicit scope boundary.

---

## ADR-006 — Karpathy llm-wiki pattern over raw RAG

**Status:** Accepted (2026-04-19); amended by ADR-014 (2026-10-21)

**Context.** Two patterns for making a bookmark corpus queryable by LLMs:

1. **Raw RAG.** Index raw bookmarks (or their scraped content) directly in a vector database. Retrieve chunks at query time. The LLM synthesizes a fresh answer on every query.
2. **LLM-maintained wiki.** An LLM reads each source once, writes a markdown page summarizing and cross-linking it. Index the markdown pages (not the raw content). The wiki itself is a persistent, compounding artifact that stays current as new sources arrive.

Raw RAG is simpler to implement and well-understood. The wiki pattern has more moving parts and real maintenance concerns (hallucination pollution, scalability of the index beyond 100s of pages, stale pages, markdown-graveyard risk if maintenance lapses). The Karpathy gist's own comments surface valid critiques: "RAG with extra steps," "doesn't scale without qmd anyway," "hallucinations pollute the wiki."

**Decision.** Wiki pattern.

**Rationale.**
- **Persistent artifacts compound.** Cross-references get established once, not re-derived per query. A year of reading builds up a knowledge graph the raw-RAG approach never constructs.
- **Human-browsable.** Obsidian works with it natively. The wiki is as useful to the operator as it is to the LLM — that dual-use is the Karpathy pattern's real insight, and it's genuinely different from headless RAG.
- **Source separation.** `raw/` holds immutable originals; `wiki/` holds LLM-generated synthesis. Either can be audited against the other. Contradictions can be flagged on-page.
- **Synthesis over retrieval.** The question "what have I learned about X?" is fundamentally different from "find me chunks about X." The wiki answers the former; raw RAG only answers the latter.

**Mitigations for known risks.**
- Every `wiki/*.md` page includes source citations in frontmatter (`sources:` field linking back to `raw/`). Hallucinations are detectable and attributable.
- Periodic lint passes (Roadmap Phase 6) check for contradictions, stale claims, orphan pages, missing cross-references.
- Git version history allows revert on bad ingest — the vault is a first-class code artifact, not an opaque database.
- Scalability concern addressed by Qdrant + embeddings for retrieval, not by relying on `index.md` alone at scale. The `index.md` stays as a human-readable catalog, not the LLM's primary retrieval path.

**Consequences.**
- More upfront implementation work than raw RAG.
- Requires ongoing maintenance discipline (lint cadence, periodic synthesis passes).
- Payoff compounds with corpus size — the inverse of raw RAG's scaling curve where relevance degrades as the haystack grows.
- The `DECISIONS.md` file itself is an expression of the pattern: not just "what we decided" but "why we decided it, in enough detail that future work doesn't re-litigate settled questions."

---

## ADR-007 — Muninn and Huginn are Saga's workloads, not its generalization test cases

**Status:** Superseded by ADR-010 (2026-10-21)

**Context.** Saga has a generalization roadmap (`saga/docs/GENERALIZATION.md`) that describes converting it from a Norse-themed Claude-Code-specific implementation into a reusable cooperative-agent framework. The generalization is gated on trigger conditions — most notably "a concrete second use case has shown up" (an operator running Saga-shaped on a non-Saga domain). Muninn and Huginn are both built by Saga, and Huginn's profile is uncannily close to §4's "Continuous research / market intelligence" architectural fit. This prompted the question of whether either project counts toward the generalization triggers.

**Decision.** Muninn and Huginn are Saga's **workloads**, not alternate deployments of Saga's pattern. Neither satisfies a generalization trigger on its own. Muninn additionally serves as **substrate** for Saga's generalization Move 1 (typed journals) via its `sessions/` namespace, but this is a data relationship, not an architectural one.

**Rationale.**

- Saga's generalization doc defines Trigger #1 precisely: "an operator wants to run Saga-shaped on a non-Saga domain, or a collaborator asks 'can I fork this for X?'" Projects Saga *builds* do not meet this bar — they do not deploy Saga's planner/auditor/worker pattern at their own runtime. Muninn runs a Python worker pool + Qdrant + MCP server; Huginn runs FastAPI + DuckDB + Haiku enrichment. Neither has Odin, Mimir, or dvergar at execution time.
- Huginn's adjacency to §4's "Continuous research" profile is real but not current. The §4 fit applies to Saga's *pattern*, which requires Moves 1–5 before a continuous-research deployment becomes feasible as a long-running Saga-shaped service. Huginn v0.1 is scoped as a single-process service precisely because that's cheaper and sufficient for the immediate need (see ADR-003). A future "Huginn as Saga-shaped deployment" remains a real option if operational complexity justifies it in phases v0.3+, but it is not a current decision.
- Muninn's `sessions/` namespace is where Saga's agent journals land once Phase 3 (Bragi integration) completes. Generalization Move 1 converts free-form journal entries into typed event streams — those typed entries will live in Muninn. The quality of Muninn's vault design shapes how cheap Move 1 is when its trigger fires.

**Consequences.**

- Muninn's `sessions/` page format must be forward-compatible with Move 1. The decision: YAML frontmatter + markdown body, with frontmatter carrying all structured data additively. Move 1 extends frontmatter; it does not migrate the vault. This is captured as a Phase 3 roadmap item in `ROADMAP.md`.
- Huginn's spec adopts Saga's §6 shape-preserving habits as **build-time requirements**, not aspirations: topic-builder helpers instead of scattered MQTT strings, typed event payloads with `schema_version`, `run_id` threading through logical operations, surface-agnostic `notify_operator` abstraction, Norse names as names rather than types. See `huginn-spec.md` §15.
- Neither project is tracked as progress toward generalization triggers. The second-use-case trigger requires a genuinely external adopter. Discipline: do not conflate "Saga built this" with "Saga generalizes to this."
- If a Saga-internal abstraction actively fights Muninn or Huginn development twice (e.g. Bragi's output shape is wrong for Muninn's vault; dvergr prompts are too coding-specific to handle Muninn's Python-only enrichment tasks), those incidents count toward generalization Trigger #3 ("a Saga-internal abstraction has fought a real request. Twice."). Worth logging as they occur rather than reconstructing later.

---

## ADR-008 — Two lanes, two canonical stores: a captured lane in SQLite, an authored lane vault-first

**Status:** Accepted (2026-10-21). Drafted 2026-07-16 as "bookmark lane / skald lane", ratified with the scope and names below.

**Context.**
- SPEC.md's delivery-shape decision makes SQLite the single canonical store, with the vault compiled from it. Commit `ab874cb` implemented exactly that.
- ADR-001 and ADR-006 then made the vault the product surface: `raw/` holds originals and `wiki/` holds synthesis.
- The July draft of this ADR added a second, vault-first lane for "skald emitter" pages: sessions, managed project regions and `log.md`.
- ADR-011 now retires session pages and machine-written project regions.
- The name "skald" collides with the standalone Skald transcript archiver (github.com/algorhythmic/skald), which is an unrelated tool.

**Decision.** Muninn has two content lanes with different canonical stores:

- **Captured lane:** X bookmarks, browser bookmarks, their linked articles and repositories, and any future captured source.
  - SQLite is canonical.
  - `wiki/bookmarks/` is compiled output; the vault compiler is its only writer, and hand edits are overwritten on recompile.
  - Evidence packets (ADR-009) are derived exports of this lane.
- **Authored lane:** operator-written notes in `wiki/concepts/`, `wiki/infra/`, `wiki/people/`, `wiki/projects/` (reference notes only, per ADR-011) and `wiki/log.md`.
  - The markdown in git *is* the canonical record and is never mirrored into SQLite.
  - No machine writes, unless a later ADR authorizes a specific writer.
- **Derived indexes:** Qdrant, FTS and any retrieval engine adopted under ADR-014 are rebuildable from either lane.

**The name "skald" is retired inside muninn.** The July Skald Protocol and its "skald lane", `skald:` markers, `skald(...)` commit prefixes and `~/.skald/` paths are historical. They don't describe the Skald tool and must not be read as an integration with it.

**Rationale.**
- SQLite gives the captured lane idempotent, resumable, auditable processing: re-running expensive stages safely is the point.
- Authored notes have none of that shape. They are low-volume and human-readable, and git already provides their atomicity and audit trail.
- Routing them through SQLite adds a store without adding a guarantee.

**Consequences.**
- SPEC.md's "single canonical store" wording is scoped to the captured lane.
- Per-namespace write ownership is documented in the vault's own CLAUDE.md. Compiled namespaces are machine-owned; authored namespaces are operator-owned.
- Indexers read `wiki/` uniformly but never "repair" authored pages from SQLite.
- The vault repository needs a matching change: retire its deployed `skald_lint.py` and any `skald:` markers.

---

## ADR-009 — Research Intake as a stage-native workflow; revisioned records and explicit migrations

**Status:** Accepted (2026-10-21). It replaces the same-day RI-01 draft, which had proposed separate `archive`/`sources`/`intake` packages.

**Context.**
- The operator wants selected external material turned into evidence-backed proposals. It starts with X bookmarks and their linked articles and repositories.
- Muninn captures and assesses that material. Odin (github.com/algorhythmic/odin) reviews applicability, drafts proposals and records the operator's decisions.
- See [`research-intake/`](research-intake/README.md).
- Muninn is already organized as pipeline stages (`ingest → scrape → enrich → consumers`) over one source-agnostic `bookmarks` table. "Source-agnostic schema" is a binding SPEC constraint.
- A separate package hierarchy would duplicate that structure.
- Paid enrichments, archive captures and decision evidence cannot be rebuilt from `raw/`. SPEC's "drop DB, re-ingest" recovery rule no longer holds; SPEC itself deferred that question to multi-source ingest.

**Decision.**

1. **"Research Intake" is a workflow name, not a module.** It is implemented as additions to existing stages:
   - **Capture.** A new `ingest/x.py` adapter writes `bookmarks` rows with `source='x'` and `source_id=<post id>`.
     - Author, publication time and post text live in `source_metadata`, or in additive columns when queries need them.
     - Each linked URL becomes its own row (`source='x_link'`), related to its post through a small `item_links` table.
     - Every locator passes through `sanitize_url`.
   - **Linked material.** The existing `scrape` stage fetches it. The live pass is current evidence; archive passes stay labelled historical.
   - **Revisions.**
     - Append-only `scrape_history` and `assessments` tables.
     - Current-state tables keep upserting, so browser-bookmark semantics are unchanged.
     - New content creates a new history row; reassessment appends.
   - **Suitability.** A second enrichment task (`muninn enrich --task suitability`).
     - Its idempotency key is the existing model + prompt version + content hash, plus the digest of an interest-profile YAML.
     - Disposition is one of `candidate`, `reference_only`, `needs_evidence`, `deferred` or `rejected`. Retrieval failures are operational states, not dispositions.
   - **Evidence packets.**
     - A `consumers/packets/` export (`muninn export packets`), beside Parquet and timeline.
     - Packets are JSON, schema-validated and content-addressed (sha256).
     - They carry separate publication, retrieval and observation times.
     - Deleting source material leaves a resolvable tombstone.
2. **Explicit migrations:**
   - numbered `schemas/sql/NNN_*.sql` files
   - `PRAGMA user_version` as the applied version
   - backup before migrating
   - an explicit `muninn db migrate`, never run implicitly
   - additive-only changes to existing tables, except where a CHECK enum forces a table rebuild, which is done inside a migration
3. **The Odin interface** is the published, versioned producer contract `muninn-research-intake` plus packet files. Neither side reads the other's database. The workflow runs without Odin deployed.

**Rationale.**
- One capture path avoids duplicating connectors, sanitization, deduplication, storage and retries.
- X bookmarks get vault pages and search for free, through the existing compiler and index.
- Capture and evaluation are already isolated: stages are separate commands, each with its own `--limit` and budget, so a model outage in `enrich` cannot stop `ingest` or `scrape`. This carries forward ADR-003's failure-isolation argument (see ADR-015) without a service or package boundary.

**Consequences.**
- No `muninn.archive`, `muninn.sources` or `muninn.intake` packages, and no `intake` CLI group.
- The `bookmarks` table now also holds X posts and linked items.
- Amends SPEC.md's storage constraints (migrations) and its rejection of append-mode scrape history, for the new history tables only.
- The export path must not require Qdrant, embeddings, DuckDB, Parquet, MCP or vault compilation.
- The X connector carries the maintenance-burden risk that made the Huginn spec exclude X. It is bounded by the RI-02 stop conditions and RI-08's maintenance-time measurement.

---

## ADR-010 — Muninn is a standalone tool; Saga lineage recorded

**Status:** Accepted (2026-10-21). Supersedes ADR-007.

**Context.**
- Muninn's April–July design treated it as a workload Saga builds. It depended on Saga infrastructure for synthesis and expected Saga's journaling agent to write its session and project pages.
- Since then, Saga's scope has changed and its planner has been extracted as Odin (see Odin's public README, github.com/algorhythmic/odin). Muninn no longer depends on Saga's internals.
- Several names muninn's docs used now mean something else, or never meant what muninn assumed.

**Decision.** Muninn has **no Saga dependency** at runtime, build time or test time. Any harness may *develop* it: Odin-supervised, a local Claude Code session or a Saga run. That is a development choice, not an architectural relationship. Muninn's relationships to sibling tools are explicit contracts (ADR-009, ADR-011, ADR-013, ADR-014).

Where the names in muninn's docs stand (as of 2026-10-21):

| Name in muninn's docs | Status now | Muninn consequence |
|---|---|---|
| Odin (Saga's planner) | **Extracted** into its own repo (github.com/algorhythmic/odin; design docs, no code yet). It owns commitments, project memory, proposals and decisions | Muninn holds no canonical project state (ADR-011). Odin consumes evidence packets and recall |
| Bragi (Saga's journaling compiler) | **Remains in Saga.** It compiles Saga's own vault, has no muninn output target, and **did not become Skald** | Removed as a muninn author or emitter |
| Skald | An **independent** tool, never part of Saga (github.com/algorhythmic/skald). A model-free archive of Claude Code, Codex and Devin transcripts | Shares only a name with muninn's retired protocol (ADR-008). A possible future captured-lane source (ADR-011) |
| "heimdall provisioning" (SPEC) | Was the legacy name of **Saga's launcher**. Standalone Heimdall (github.com/algorhythmic/heimdall) is a task-continuity tool and does no secrets provisioning | Muninn manages its own credentials (`.env`) |
| Saga credentials volume, keepalive cron, devcontainer base, Telegram bridge | Saga's own mechanisms; their future is Saga's business | Not used by muninn (ADR-013); no Telegram interface |
| "`claude -p` silently forces API billing" | An April 2026 observation, not re-verified; Anthropic's current guidance says `-p` and the Agent SDK draw on subscription limits | Not encoded in muninn (ADR-013) |
| Generalization "Move 1" typed journals | Specified by Odin's chat log, not muninn | ADR-007's substrate claim withdrawn |
| Braid | An independent retrieval engine (github.com/algorhythmic/braid), never part of Saga | Candidate retrieval layer (ADR-014) |
| Huginn | Spec only, never implemented | Deferred (ADR-015) |

**Rationale.**
- The only code-level coupling was the synthesis path, and it never worked.
- The Saga mechanisms it relied on are outside muninn's control and not guaranteed to it.
- Coupling a personal archive to an execution service's internals buys nothing that a file contract doesn't.

**Consequences.**
- SPEC.md loses the following as binding constraints: Saga Phase 1+2 as a runtime requirement, the "heimdall provisioning" notes, the "decomposition note for Odin", and the `claude -p` rule.
- README, CLAUDE.md and the roadmap drop the "Saga builds muninn" framing.
- Vendored Saga documents are private material. They move out of this public repo into the operator's private store.

---

## ADR-011 — Muninn holds no canonical project or session state

**Status:** Accepted (2026-10-21). Supersedes ADR-004 and amends ADR-001.

**Context.**
- ADR-004 made `wiki/projects/` the canonical current state of each project, upserted by Saga's journaling agent, and made `wiki/sessions/` its session journals.
- Neither writer exists for muninn.
- Odin now owns commitments and project memory: its commitment record is the only editable record of task state.
- Skald archives session transcripts verbatim.
- The `/note` skill (muninn's only session-page emitter) is a manual step that duplicates both.

**Decision.**
- **`wiki/projects/`** holds operator-written **reference notes**. Each carries a banner: "Reference, not canonical — Odin owns commitments and decisions."
  - No machine writes and no managed regions.
  - It may later become a read-only view derived from an Odin export, if Odin publishes one.
- **Session pages and the `/note` skill are retired.** `wiki/sessions/` stays empty.
  - `raw/sessions/` is reserved.
  - Future option, which needs its own ADR: generate session pages from Skald's archive feed as a captured-lane source, summarized the way bookmarks are enriched.
- The **MCP and Claude.ai connector goal remains**, for recall over saved and curated knowledge, not for project truth.

**Rationale.**
- One canonical owner per kind of state.
- Reference notes keep their real value now: the only project summaries a Claude session can fetch until Odin exists, cross-links from bookmarks, and seed material for the Research Intake interest profile.
- Retiring session pages removes a manual step and a protocol while Skald already captures everything.

**Consequences.**
- The roadmap's Bragi-integration phase is deleted.
- `docs/skald-protocol.md`, `docs/note-SKILL.md` and `scripts/skald_lint.py` are retired to history. The vault repo needs a matching cleanup.
- Project-page drift is the operator's to manage; the banner makes the status explicit.

---

## ADR-012 — v1 is X bookmarks end to end

**Status:** Accepted (2026-10-21). Supersedes ADR-005.

**Context.**
- ADR-005 scoped v1 to browser bookmarks: a ~10k-item HTML export, with no bulk run yet.
- The operator wants X bookmarks first, because the corpus is smaller and easier to test.
- The browser-bookmark pipeline already exists and passes its tests.

**Decision.** v1 is the **X bookmark slice end to end**:
- API sync
- linked articles and repositories
- sanitize
- enrich
- compile into `wiki/bookmarks/`
- index
- MCP search
- Research Intake suitability assessment and evidence-packet export (ADR-009)

Scope rules:
- The browser-bookmark pipeline and its tests are kept; its bulk run moves to **v1.1**.
- Scope discipline (one source at a time) is retained.
- The other namespaces stay as stubs.

**Rationale.**
- A small, real corpus exercises every stage, including the new ones.
- It does so cheaply and with fast feedback, before the larger bulk run.

**Consequences.**
- The roadmap's "Blocking v1" section is rewritten around the X slice.
- **The X API's retrievable depth is unverified.** Historically it was the 800 most recent bookmarks; RI-02 measures it. v1 does not promise complete X history.
- X access is pay-per-use with operator-approved spending limits (RI-02).

---

## ADR-013 — Harness-agnostic synthesis contract

**Status:** Accepted (2026-10-21). Partially supersedes ADR-002, for synthesis execution only. *Implemented 2026-10-21 on `x-slice`:* the contract is specified in `docs/synthesis-contract.md`, and the container path is removed.

**Context.**
- Synthesis covers three jobs: era narratives, deep passes and ad-hoc analyses.
- It was designed to run in a container that mounted Saga's subscription credentials, to avoid API billing.
- The container never received its task, its base image cannot be built, it depends on Saga-owned credentials mechanisms, and the billing premise is unverified (ADR-010).
- None of it has ever run.
- The orchestrator, JSON schemas and output validation do work.

**Decision.** Synthesis is a **file contract**:
- Muninn emits a validated task file and accepts a validated result file, using the existing `schemas/json/*` schemas.
- **The default runner is a direct API call** with an explicit per-run budget, batchable where latency doesn't matter.
- Other runners are optional and use the same files: a local Claude Code session, an Odin-dispatched session or a Saga run.
- `containers/synthesis/` and `scripts/launch-synthesis.sh` are frozen and removed when the contract lands.
- No credentials volume, no Trail of Bits base image, no `claude -p` rule.
- Synthesis is not on the v1 critical path; it is implemented after the X slice.

**Rationale.**
- The validated-file boundary is the part of the design that works.
- It makes the runner a deployment choice.
- Direct API is the simplest unattended path.

The volume is small. Rough estimates at current prices (Opus 5.5 at $4 input and $20 output per MTok; batch is 50% off):
- an era narrative: about $0.50
- a deep pass: about $0.10

These are estimates, not measurements.

**Consequences.**
- SPEC Decision 6's Tier 2 mechanics and Stream 4's subscription acceptance test are superseded.
- ADR-002's bulk decision (Haiku through the direct API) stands, minus its dvergr framing.
- API spend for synthesis requires a configured budget.

---

## ADR-014 — Retrieval behind one interface; evaluate Braid after the X slice

**Status:** Accepted (2026-10-21). Amends ADR-006.

**Context.**
- ADR-006's wiki pattern stands. Retrieval today is Qdrant (EmbeddingGemma vectors) plus SQLite FTS5, both derived data (ADR-008).
- Braid (github.com/algorhythmic/braid) is an independent Go retrieval engine. It fuses keyword (BM25), dense, graph (PageRank over typed edges) and recency signals into a token budget, and explains every result.
- It could replace both Qdrant and FTS5 and turn cross-references into graph retrieval.
- It is an early implementation with no published retrieval-quality evidence.
- Gaps against muninn:
  - It is a Go binary with a stdio protocol and no MCP.
  - Its only query-time embedding provider is Ollama, which lacks EmbeddingGemma's asymmetric query/document prompts.
  - It has no markdown awareness.

**Decision.**
- Search goes behind **one retrieval interface** during the X work.
- **Qdrant stays for v1.**
- After the X slice has real pages, run a bounded **Braid evaluation spike**:
  - 20–30 labelled queries
  - Braid's evaluation harness (recall, MRR, nDCG, no-answer and forbidden-hit gates) against the Qdrant path
- Adopt Braid only on measured recall, and only if its operating cost is accepted.

**Rationale.**
- Evaluating retrieval without a real corpus is meaningless.
- X bookmarks produce the link structure Braid exploits: post → article → repository.
- The interface makes the later swap cheap either way.

**Consequences.**
- The roadmap gains a post-v1 Braid spike.
- Adoption would remove the Qdrant host dependency, in exchange for building and supervising a Go binary.

---

## ADR-015 — Huginn deferred

**Status:** Accepted (2026-10-21). Supersedes ADR-003.

**Context.**
- `huginn-spec.md`, now in `docs/history/`, scoped a separate triage service that was never implemented:
  - **automatic discovery:** GitHub trending and search, later Hacker News, Reddit RSS and email newsletters
  - **scoring:** velocity (attention against a 30-day baseline), graph overlap with muninn via MCP, and topic match against an interest profile
  - a one-line Haiku "why this surfaced"
  - an ephemeral DuckDB feed with a 30-day TTL
  - an HTMX skim dashboard with promote/reject
  - promotion into muninn's vault
- It expected muninn MCP methods that don't exist, and excluded X.
- Research Intake (ADR-009) now covers Huginn's durable half: source connectors, normalized items, an interest profile, cheap per-item assessment and human selection. It does so with durable evidence instead of a TTL, and with Odin review instead of wiki promotion.
- What remains unique to Huginn is the automatic, high-volume discovery feed.

**Decision.**
- Huginn is not planned.
- If automatic discovery is wanted later, it returns as **another captured source**, with its own budget and `discovered` provenance, under a new ADR.
- ADR-003's cadence and failure-isolation argument is carried as a design rule: discovery-scale work runs as separate stages with separate budgets and never blocks capture or the knowledge base.

**Rationale.**
- The separation argued for in ADR-003 was for a service that doesn't exist.
- The useful parts already have a home.

**Consequences.**
- Roadmap Phase 5 (Huginn as consumer) is deleted.
- The `search_contributors`, `search_concepts` and `has_bookmark` MCP methods are not commitments.
- `raw/inbox/huginn/` is not reserved.

---

## Superseded

- ADR-002 (synthesis-execution half) → ADR-013
- ADR-003 → ADR-015
- ADR-004 → ADR-011
- ADR-005 → ADR-012
- ADR-007 → ADR-010
- Amended, not superseded: ADR-001 by ADR-011, and ADR-006 by ADR-014.
