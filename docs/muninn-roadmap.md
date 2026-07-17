# Muninn Roadmap

Single view of what's next, what's queued, and what's deferred.
Scan this before each work session to get bearings.

For detailed design, see [`SPEC.md`](../SPEC.md).
For architectural rationale behind key decisions, see [`muninn-decisions.md`](muninn-decisions.md).
For the session-capture contract, see [`skald-protocol.md`](skald-protocol.md).
For operator guidance to Claude Code sessions working on this repo, see [`CLAUDE.md`](../CLAUDE.md).

---

## Blocking v1 — bookmark ingest

Items that must be done to ship v1. Nothing in later sections starts until these are complete.

- [x] **Stand up the vault skeleton.** *Done 2026-07-16: `~/Documents/Code/muninn-vault`, tagged `v1-skeleton`, with `scripts/skald_lint.py` deployed; pushed to Gitea `david/muninn-vault`.* One git repo with the full unified namespace structure:
  ```
  vault/
  ├── raw/
  │   ├── bookmarks/       # bookmarks.html exports and scraped pages
  │   ├── sessions/        # session evidence bundles (skald-protocol.md §4.4)
  │   ├── articles/        # reserved for Obsidian Web Clipper (later)
  │   └── inbox/
  │       └── huginn/      # Huginn promotions land here (Phase 5)
  ├── wiki/
  │   ├── bookmarks/       # v1 populates here
  │   ├── projects/        # empty skeleton + README stub
  │   ├── sessions/        # empty skeleton + README stub
  │   ├── infra/           # empty skeleton + README stub
  │   ├── concepts/        # empty skeleton + README stub
  │   ├── people/          # empty skeleton + README stub
  │   ├── index.md         # auto-maintained catalog
  │   └── log.md           # append-only chronological log
  ├── CLAUDE.md            # schema: per-namespace ingest rules
  ├── SPEC.md              # this repo's spec
  ├── DECISIONS.md         # ADR log
  └── ROADMAP.md           # this file
  ```
  See ADR-001 and ADR-005 in DECISIONS.md for why the unified structure exists at v1 even though only `wiki/bookmarks/` is populated.
- [x] **Update CLAUDE.md for unified-wiki pattern.** *Done 2026-07-16: repo CLAUDE.md rewritten (unified-wiki, two-lane stores, code map, known gaps); vault CLAUDE.md written with per-namespace page formats, ingest rules, and write-ownership table.*
- [ ] **Implement bulk bookmark enrichment worker.** Per SPEC.md §4 — Python asyncio pool, Haiku 4.5 via direct Anthropic API. Input: `raw/bookmarks/bookmarks.html` export. Output: one `wiki/bookmarks/{slug}.md` per bookmark with summary, tags, cross-references, archive fallback URL.
  *Status 2026-07-16: the SQLite pipeline behind this (ingest → dual-pass scrape → Haiku enrich → vault compiler) shipped in commit `ab874cb`; the compiler now writes `{vault}/wiki/bookmarks/{slug}.md` and the privacy/correctness gaps are closed (fail-closed domain policy, MCP visibility, deep-pass drain, .env loading, `--force`/`--prompt-version`). Remaining: real embeddings + Qdrant (next two items), then run the ingest on the real export.*
- [x] **Stand up Qdrant at `.19`.** *Done 2026-07-17: unprivileged LXC 119 (Debian 13), static binary + systemd (`Restart=always`, `onboot=1`), data on dedicated mount `/var/lib/qdrant`; `muninn_bookmarks` collection live (768-dim, cosine); reconcile round-trip verified from the Mac. Remaining nicety: add the `/healthz` monitor in Uptime Kuma at `.16`.* LXC on Proxmox, Debian 13. Persistent volume for the vector index. Restart policy: always. Health check endpoint monitored by Uptime Kuma at `.16`.
- [ ] **Index `wiki/bookmarks/` into Qdrant.** Full-text + summary embeddings. Embedding model choice is a sub-decision (to resolve): EmbeddingGemma-300M via local inference, `text-embedding-3-small` via OpenAI, or voyage-3 via Anthropic-adjacent service. Default: local EmbeddingGemma unless cost/latency data argues otherwise.
  *Status 2026-07-16: code side done — pluggable backends in `vector/embed.py` (default `google/embeddinggemma-300m`, 768-dim, asymmetric query/document prompts, `hash` placeholder kept for tests), MCP query path now embeds queries instead of sending raw text, collection dim follows `MUNINN_EMBEDDING_DIM`. Embeddings verified locally (HF access + `[embeddings]` extra installed, EmbeddingGemma cached, semantic ranking sanity-checked). Remaining: stand up Qdrant at `.19`, run `scripts/reconcile-vector-index.py`, then the recall smoke test settles the model sub-decision.*

## Parallel with v1 (do while ingest is running)

- [x] **Smoke-test retrieval.** *Done 2026-07-16: newest 100 bookmarks scraped (`--limit`), 58 enriched + indexed (42 were auth-walled dashboards or partial extractions), recall verified through the MCP search path — topical queries hit at 0.3–0.5 cosine, off-topic control at ~0.1. **Embedding sub-decision resolved: EmbeddingGemma-300m confirmed.*** Seed ~100 bookmarks. Query Qdrant directly with representative questions. Tune embedding choice and chunk size if recall is poor.
- [ ] **Populate `wiki/projects/` manually.** *Partial 2026-07-16: `muninn`, `saga`, `huginn`, `homelab` drafted from repo docs (verify `saga`'s state — sourced from April-vendored docs); `nexus` and `esp32-surveillance` are stubs awaiting operator content.* One markdown file per active project: `saga.md`, `muninn.md`, `nexus.md`, `huginn.md`, `homelab.md`, `esp32-surveillance.md`. Each ~200-400 words: current status, architecture summary, phase, open questions, links to the source repo or design docs. This is an hour or two of writing; it makes the MCP immediately useful for Claude sessions even before Bragi automates updates.
- [x] **Git-initialize the vault.** *Done 2026-07-16: pushed to Gitea `david/muninn-vault` (private) with tag `v1-skeleton`.* Commit the full skeleton + seeded projects. Push to Gitea at `.18`. Tag `v1-skeleton`.
- [x] **Write `wiki/index.md` initial version.** *Done 2026-07-16.* Manually, for v1. LLM auto-maintenance comes in Phase 2.

## Phase 2 — MCP server

Target: unlock Muninn for Claude Code sessions, and eventually for Claude.ai and Huginn.

- [ ] **Implement MCP server.** Container on Proxmox, bound to Qdrant + vault filesystem. Entry points:
  - `search(query: str, namespace: str | None, top_k: int = 10)` — semantic + keyword hybrid
  - `get_page(path: str)` — fetch a specific `wiki/*.md` page by path
  - `list_namespace(namespace: str)` — enumerate pages in a namespace with one-line summaries
  - `ingest_raw(path: str, content: str)` — write a file into `raw/` and queue for enrichment (called by Huginn's promote action in Phase 5)
- [ ] **Expose MCP over Tailscale.** MCP endpoint reachable on the tailnet. No public internet exposure.
- [ ] **Wire up Claude.ai remote MCP connector.** Register Muninn as a custom connector in Claude.ai. Verify future sessions can query vault content during context bootstrap. This is the structural fix for the memory-drift problem that has motivated this whole direction — see ADR-004.
- [ ] **Document the MCP contract.** `docs/mcp-api.md`. Essential for Huginn's scorer (Phase 5) and for operator debugging.

## Phase 3 — Bragi integration

Saga's Bragi already writes to `/opt/saga/vault`. Extending its write target to Muninn's vault closes the loop on session state capture.

**Canonical contract: [`skald-protocol.md`](skald-protocol.md)** (drafted 2026-07-16; pulls the session-page template forward so the `/note` skill ships before Bragi). Where items below disagree with the protocol, the protocol wins.

- [ ] **Coordinate with Saga.** Bragi spec needs a second output target. Add a section to Saga's `saga-spec.md` describing the Muninn integration. Bragi reads from `/opt/saga/odin-ws` (RO, unchanged) and writes to both `/opt/saga/vault/` (RW, existing) and the Muninn vault (RW, new): finished session pages to `wiki/sessions/`, optional evidence bundles to `raw/sessions/` (skald-protocol.md §4.4).
- [x] **Session page template.** Done — pulled forward into [`skald-protocol.md`](skald-protocol.md) §4: `wiki/sessions/YYYY-MM-DD-{primary-project}-{slug}.md`, frontmatter schema v1 (`projects: [primary, ...]`, `emitter`, `outcome`, optional timing/decisions/sources); body is the compiled narrative.
- [ ] **Project state upsert.** When a Saga session's work materially changes a project (phase completion, scope shift, rename, decision ratified), Bragi upserts the relevant `wiki/projects/{project}.md`. This is the single most important piece for killing context drift — every Saga run leaves an accurate trail without operator action.
- [ ] **Verify credentials volume compatibility.** Bragi currently uses `saga-claude-credentials` for Max-subscription auth; the vault-write operation is pure filesystem (no auth needed), so this should be a clean extension. Sanity-check during implementation.
- [ ] **Forward-compatibility with generalization Move 1.** Session pages are YAML frontmatter + markdown body. Frontmatter is the primary carrier of structured data (session_id, projects, duration, outcome, agent roster, model versions) and must be additive — new typed fields can be added over time without breaking the existing corpus. When Saga's generalization Move 1 (typed journals) eventually lands, it extends frontmatter; it does not migrate the vault. See Saga's `GENERALIZATION.md` §3 Move 1.

## Phase 4 — Opus 4.6 synthesis container

Per SPEC.md §5. Runs alongside the Haiku bulk worker, not replacing it — different job, different model.

- [ ] **Build synthesis container.** Saga-style: mounts `saga-claude-credentials` volume, runs interactive Claude Code session (Opus 4.6), 1M context window. Filesystem layout mirrors dvergr pattern.
- [ ] **Define synthesis triggers.** Three initial use cases:
  - *Era narratives* — "summarize my reading on [topic] over the past year"
  - *Deep-pass* — flagged bookmarks get a longer, Opus-quality summary beyond the Haiku bulk enrichment
  - *Ad-hoc operator queries* — operator submits a slice-specification via Telegram, synthesis container produces a markdown page filed into `wiki/`
- [ ] **Operator interface.** Telegram command: `/synthesize {slice-spec}`. Result posted back to Telegram with a link to the wiki page. Uses the same Telegram bridge Saga already has.

## Phase 5 — Huginn as consumer

Muninn's readiness for external consumers is a gating decision for Huginn v0.2.

- [ ] **MCP stability SLO.** MCP endpoint must handle Huginn's query volume (maybe 50-200 queries per fetch cycle) without degrading. Benchmark and tune.
- [ ] **Vault write path SLO.** Promotion from Huginn writes `raw/inbox/huginn/{item_id}.md`. Latency must be <1s p99. Atomicity: the write is a single file drop, so fsync-after-write is sufficient.
- [ ] **Document the promotion protocol.** `docs/huginn-integration.md` specifies frontmatter schema, directory layout, and the ingest handoff (Muninn's bookmark enricher picks up new files from `raw/inbox/huginn/` and processes them through normal ingest flow, with the Huginn frontmatter preserved as provenance).
- [ ] **Contributor graph query endpoint.** `muninn.search_contributors()` specifically. Important enough to Huginn's scoring that it gets first-class MCP endpoint treatment rather than emerging from generic search.

## Phase 6 — Lint and maintenance

Karpathy's "Lint" operation. Periodic vault hygiene.

- [ ] **Contradiction detection.** Opus 4.6 pass over pages sharing tags; flags contradictions inline with `<!-- contradiction: {other_page} -->` comments.
- [ ] **Orphan detection.** Pages with zero inbound links get flagged for review or concept extraction.
- [ ] **Stale claim supersession.** When a new ingest contradicts an existing page, the older claim gets a `superseded_at:` frontmatter field; both versions remain (git history preserves the original).
- [ ] **Missing cross-reference suggestion.** When a page mentions an entity that exists elsewhere in the vault but isn't linked, the lint pass suggests the link.
- [ ] **Cadence.** Weekly or on-demand via Telegram trigger.

## Feature backlog — design complete, not yet scheduled

| Feature | Notes |
|---|---|
| Public portfolio subset | A curated subset of `wiki/bookmarks/` published as a static site ("things worth remembering"). Long-shot; requires deciding what's publishable vs. private. |
| Multi-source bookmark ingest | Pocket, Pinboard, and other bookmark export formats in addition to `bookmarks.html`. Adapter pattern; relatively simple once v1 is solid. |
| Automatic concept extraction | Lint pass that identifies recurring themes and creates `wiki/concepts/` pages for them. Currently concepts are created manually or ad-hoc during synthesis. |
| Obsidian Web Clipper integration | Operator drops articles into `raw/articles/` via the browser extension; Muninn enriches into `wiki/bookmarks/` (or its own namespace). |
| Dataview views | Obsidian Dataview queries for common operator workflows — "bookmarks from last month by tag," "projects with recent activity," etc. |

## Deferred

| ID | Summary | Blocker |
|---|---|---|
| MULTI-INSTANCE-SYNTHESIS | Saga dvergar parallelizing synthesis over huge corpus | Corpus growth making single-instance slow — not a current constraint |
| SEMANTIC-DEDUPE | Detect when two bookmarks point at essentially the same content | Requires scale to matter; hand-curate for v1 |
| NON-BOOKMARK-SOURCES | Support YouTube transcripts, PDFs, podcasts as sources | v2; changes ingest shape significantly |
| MULTI-USER | Share subsets of vault with other people | May never happen; personal tool by design |

## Completed

| Date | Summary |
|---|---|
| 2026-04 | Project conceived as personal bookmark knowledge base |
| 2026-04 | SPEC.md drafted — six core design decisions resolved |
| 2026-04 | Initial CLAUDE.md |
| 2026-04-19 | **Unified-wiki architectural decision** — scope expanded from bookmarks-only to unified vault with namespaces; Memex retired as separate project name. See ADR-001 and ADR-005. |
| 2026-04-19 | **Ingest pattern confirmed** — bulk Haiku worker pool for bookmarks; Opus 4.6 synthesis container for deep passes. Saga Phase 3 not required for v1. See ADR-002. |
| 2026-04-19 | **Huginn integration shape defined** — consumer via MCP reads and vault writes, not merged into Muninn. See ADR-003. |
| 2026-04-19 | ROADMAP.md and DECISIONS.md created from conversation synthesis |
| 2026-04-15 | **v1 pipeline implemented** (commit `ab874cb`) — ingest, sanitize, dual-pass scrape, Haiku enrich, synthesis scaffolding, consumers (CLI/MCP/vault/timeline/Parquet), tests. Predates the unified-wiki ADRs; reconciliation tracked in "Blocking v1." |
| 2026-07-16 | **Skald Protocol drafted** (`skald-protocol.md`) — session-capture and project-state contract; Phase 3's session template pulled forward. `/note` skill authored as first emitter (`note-SKILL.md`). |

---

## How to use this document

When starting a work session on Muninn:

1. Read the "Blocking v1" section. If anything there is unfinished, that's your work.
2. If "Blocking next" is clear, check the next phase's items. The phases are sequenced; don't jump ahead unless a specific phase item is independent (rare).
3. Before making architectural choices not already covered, read DECISIONS.md for context. New architectural calls get new ADR entries.
4. Update this file at the end of each session: check off completed items, add notes to "Parallel" sections if mid-flight, move finished work to "Completed."

This roadmap is the wiki equivalent of the operational source-of-truth. It lives in git; its history is the project's history.
