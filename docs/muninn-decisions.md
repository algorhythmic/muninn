# Muninn Decisions

Architecture Decision Records (ADRs) for Muninn. Each entry captures a key choice, why it was made, and what it commits us to.

New entries go at the bottom. Existing entries may be superseded but should not be deleted — crossed-out decisions still carry rationale that future work needs to understand. When superseding, add a new entry and mark the old one's status as "Superseded by ADR-NNN".

Format: title, status, context (what was the situation), decision (what we chose), rationale (why), consequences (what this commits us to).

---

## ADR-001 — Unified wiki over separate Muninn + Memex

**Status:** Accepted (2026-04-19)

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

---

## ADR-002 — Haiku worker pool over dvergr pattern for bulk enrichment

**Status:** Accepted (2026-04, revised from earlier "build via Saga" framing)

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

**Status:** Accepted (2026-04-19)

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

**Status:** Accepted (2026-04-19)

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

**Status:** Accepted (2026-04-19)

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

**Status:** Accepted (2026-04-19)

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

**Status.** Accepted 2026-04-19.

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

## Superseded

*Entries here when earlier decisions are explicitly replaced by later ones. None yet.*
