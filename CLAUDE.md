# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Where truth lives

- `docs/muninn-roadmap.md`: the **current work queue.** Read "Blocking v1" first; if anything there is unfinished, that's the work.
- `docs/muninn-decisions.md`: the ADR log (ADR-001…015). Where an ADR conflicts with `SPEC.md`, **the ADR wins**. ADR-008 – ADR-015 (2026-10-21) reset scope and remove the Saga coupling; superseded entries keep their rationale.
- `SPEC.md`: detailed pipeline design (schema, synthesis I/O schemas, URL sanitization, scraper strategy). It is still authoritative for pipeline mechanics the ADRs don't touch. Superseded sections carry banners.
- `docs/research-intake/`: the Research Intake workflow (X bookmarks → suitability assessment → evidence packets for Odin), with the RI-01 spike and the RI-02/RI-03 plan. The originating Odin design handoff is kept privately (dotprivate).
- `docs/history/`: retired designs, kept for rationale. They include the Skald Protocol and `/note` skill (retired by ADR-008/011) and the Huginn spec (deferred by ADR-015).
- `docs/history/MUNINN.MD` and `docs/history/rawspec.md`: raw brainstorming and the original spec draft, kept as history only.

## Project status

**The browser-bookmark pipeline is implemented** (commit `ab874cb`). It runs ingest → sanitize → dual-pass scrape → Haiku enrich → consumers (CLI, MCP, vault compiler, timeline, Parquet), plus synthesis scaffolding and a real test suite (438 tests on branch `adr-reset-x-first`, where `selectolax<1` is pinned; 1001 on `x-slice`).

**v1 is now X bookmarks first** (ADR-012). The code for every "Blocking v1" item is on branch `x-slice` (stacked on `adr-reset-x-first`), tested against synthetic fixtures only: the migrator, the X connector and sync writer, the `muninn-research-intake` 0.1.0-draft contract, suitability assessment, packet export, X vault pages and the search interface. What remains is operator-run: live X qualification (RI-02a, $5 cap), then the first real sync, assessment and recall check. The browser-bookmark bulk run is v1.1.

## What Muninn is

A **standalone personal archive and wiki of saved material** (Karpathy llm-wiki pattern, ADR-001/006/010):

- One Obsidian-browsable vault, one search index and one MCP endpoint.
- Saved material from any source compiles to `wiki/bookmarks/`, with the source recorded in frontmatter. v1 covers X bookmarks with linked articles and repos; browser bookmarks are v1.1; YouTube, Reddit, GitHub and others come later.
- `concepts/`, `infra/` and `people/` hold curated notes.
- `projects/` holds operator-written **reference** notes only. Odin owns canonical project state (ADR-011).
- `sessions/` is retired. Session transcripts are Skald's; generating session pages from Skald's archive feed is future work.
- **Research Intake** (ADR-009) assesses selected items for suitability and exports versioned, content-addressed **evidence packets** that Odin consumes.

## Architecture — two lanes (ADR-008)

- **Captured lane:** SQLite is canonical.
  - Each source adapter (`ingest/`) writes `bookmarks` rows. The table is source-agnostic, with `source` set to e.g. `bookmarks_html` or `x`.
  - Scrapes go to `scrape_results` (live + at-capture Wayback passes).
  - Enrichment goes to `enriched`: Haiku, idempotent via `enrichment_model` + `enrichment_prompt_version` + `content_hash`.
  - Research Intake (migration 002): `item_links` (post → article/repo/quoted post), append-only `scrape_history` (one row per distinct content revision) and `assessments` (suitability, keyed by revision + profile + policy + model + prompt), plus `interest_profiles`, `evidence_packets`, `sync_checkpoints` and `usage_records`.
  - Output is compiled to the vault's `wiki/bookmarks/{slug}.md`, to FTS5 and Qdrant, and to packet exports. The vault compiler is the *only* writer of `wiki/bookmarks/`.
- **Authored lane:** vault-first, and git is canonical. These are pages the operator or an agent writes by hand (`concepts/`, `infra/`, `people/`, reference `projects/`, `log.md`). They are never mirrored into SQLite, and there are no machine-managed regions.
- **Search indexes are derived data in both lanes.** Qdrant (and Braid, if adopted per ADR-014) can always be rebuilt and is never a source of truth.

Per-bookmark `ADD_DATE` unix timestamps are the authoritative dates for browser bookmarks. Top-level folder names are **user-assigned era labels**, not dates; bracket eras by the min/max `add_date` of their contents. For X and other sources, keep the publication time, the retrieval time and the bookmark observation time separate. "Recently bookmarked" never implies "newly published".

## Code map

`src/muninn/`:

- `ingest/`: Netscape HTML parser and upsert; `x.py` is the X API v2 client (OAuth 2.0 PKCE, pagination, checkpoints, error taxonomy) and `x_store.py` the sync writer (posts `source='x'`, articles `x_article`, linked URLs `x_link`).
- `sanitize/`: token-shape regex, param denylist, scheme rules.
- `scrape/`: live, at_capture ±365d and recent_archive passes; IA CDX cache; rate limiter; auth-wall heuristic.
- `enrich/`: Haiku via the Anthropic API, prompt-cached, with the idempotency triple. `suitability.py` is the `--task suitability` assessment (interest profile in `interest_profile.py`, price table and budget in `pricing.py`).
- `vector/`: pluggable embeddings. sentence-transformers/EmbeddingGemma is the default; `hash` is a placeholder for tests/offline, selected via `MUNINN_EMBEDDING_BACKEND`. Also the Qdrant helpers and `reconcile.py` (backfills missing points, refreshes stale ones, drops points for hidden rows; `scripts/reconcile-vector-index.py` wraps it).
- `search/`: the retrieval interface (ADR-014). MCP search goes through `get_search_backend()` (`MUNINN_SEARCH_BACKEND`, default `fts5-qdrant`); `hybrid.py` fuses keyword and semantic rankings (RRF). Backends return bookmark ids only; visibility is enforced when hits are hydrated from SQLite. `consumers/mcp/vault_fs.py` serves vault pages (`get_page`, `list_namespace`, authored-lane scan) with strict path and visibility checks.
- `intake_records.py`, `revisions.py`: row → contract-record mapping and the shared `scrape_history` writer (stdlib-only).
- `synthesis/`: orchestrator, JSON Schema validation, the task/result file contract (`muninn-synthesis/1`, `docs/synthesis-contract.md`) and the budgeted direct API runner (default model `claude-opus-5-5`; per-run `--max-cost-usd`, and a required monthly cap `MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD`). The Saga-style container path is removed (ADR-013).
- `consumers/`: Click CLI `muninn …`, FastMCP stdio server, vault compiler with Jinja templates (X items in `vault/x_pages.py`), timeline, Parquet, and `packets/` (evidence-packet export, canonical JSON, tombstones).

CLI: `muninn ingest [html] PATH|ingest x|x auth|scrape|enrich [--task suitability]|synthesize {era|deep-pass|analyze} [--emit-task DIR]|synthesize ingest-result FILE|triage|status|deep-pass|export parquet|export packets|timeline|vault|mcp [--transport stdio|http]|db migrate|db status`. MCP tools and their privacy rules are specified in `docs/mcp-api.md`.

DB: numbered migrations in `schemas/sql/NNN_*.sql`, applied only by `muninn db migrate [--dry-run]` (backup first) or by `init_db()` / `scripts/init-db.py` for a fresh file. `PRAGMA user_version` is the applied version; `connect()` refuses a missing, empty or behind database and warns on an ahead one. `schema.sql` is a test-guarded byte mirror of `001_initial.sql` only, not the full schema.

The Research Intake producer contract is `docs/contracts/MUNINN-RESEARCH-INTAKE.md` with schemas in `schemas/json/intake/` and a release manifest checked by `scripts/intake-release-manifest.py --check`. Odin copies the contract byte-for-byte, so it has no relative links.

## Known gaps (audited 2026-07-16 and 2026-10-21; verify before trusting, fix opportunistically)

1. Haiku prompt caching never hits (0% on the 58-call smoke run; the shared prefix is likely below the model's minimum cacheable token count). Investigate before any bulk enrichment run. The cost impact is modest at Haiku prices but nonzero.
2. Vault "Related" links come from era/tag heuristics, not `cross_references` (SPEC wants bidirectional model-produced refs). Revisit once deep passes populate that table.
3. ~~The synthesis container never received its task, and `task-input.schema.json` rejected `attempt`.~~ Closed on `x-slice`: the container path is removed and the task/result contract accepts `attempt` (ADR-013). No synthesis run has happened against a real model yet.
4. No robots.txt respect in the live scraper.
5. Dependency drift and a prompt-version bug, found 2026-10-21 (details in `docs/research-intake/RI-01-extension-spike.md` §6). **Fixed on branch `adr-reset-x-first`:** selectolax/mcp/anthropic pins, `uv.lock`, fatal extraction import, `--prompt-version` passthrough with fail-fast. Live Anthropic SDK 2.x behaviour is untested; every test mocks the client.
6. Re-enrichment (`enrich --force`, a new prompt version, or changed content) still overwrites a deep pass's summary and tags, and there is no column recording a deep pass's own provenance. Whether deep-pass content should survive is undecided. (The original D6, the deep pass clobbering the idempotency triple, is fixed on `x-slice`.)
7. Prompt caching cannot hit on Haiku 4.5 for either enrichment task: its minimum cacheable prefix is 4096 tokens and both shared prefixes are roughly 1k. This probably explains gap 1.

Closed 2026-07-16 (same-day fix pass):

- The compiler now targets `{vault}/wiki/bookmarks/{slug}.md`.
- A missing or malformed `domain_policy.yml` aborts.
- MCP `get_bookmark()` hides `content_visible=0` rows.
- The deep pass clears `deep_pass_requested`.
- `.env` is loaded, and real environment variables win.
- The enrich CLI has `--force` and `--prompt-version`.
- FTS re-enrichment no longer fails. Contentless FTS5 rejected DELETE, so the table is now content-storing.

## Design decisions

Six SPEC decisions stand except where ADRs refine them:

- a source-agnostic schema (the v1 *source* is now X, per ADR-012)
- a multi-language monorepo, with the vault in a **separate** Gitea repo
- an HTTP-only dual-pass scraper (`scrape_results` child table)
- at-capture ±365d with recent_archive fallback and `enrichment_source` provenance
- sanitize-don't-exclude, with `domain_policy.yml` gating visibility
- Haiku via the API for bulk; synthesis per ADR-013

The ADRs:

| ADR | Decision | Status |
|---|---|---|
| 001 | Unified wiki | Amended by 011 |
| 002 | Haiku worker pool for bulk | Synthesis half superseded by 013 |
| 003 | Huginn separate | Superseded by 015 |
| 004 | Vault as project-state truth | Superseded by 011 |
| 005 | v1 = browser bookmarks | Superseded by 012 |
| 006 | Wiki over raw RAG | Amended by 014 |
| 007 | Saga workloads | Superseded by 010 |
| 008 | Captured and authored lanes | Accepted |
| 009 | Stage-native Research Intake with explicit migrations | Accepted |
| 010 | Standalone tool, with the Saga lineage recorded | Accepted |
| 011 | No canonical project or session state | Accepted |
| 012 | v1 = X bookmarks first | Accepted |
| 013 | Harness-agnostic synthesis contract | Accepted |
| 014 | Pluggable retrieval; Braid evaluation after X | Accepted |
| 015 | Huginn deferred | Accepted |

Binding constraints to preserve:

- the schema stays source-agnostic
- the original URL is never stored after ingest; every locator goes through `sanitize_url`
- every enrichment and assessment has idempotency columns
- **the compiled vault and the personal vault must never be the same vault**

## The vault

It is a separate repo: `~/Documents/Code/muninn-vault` locally (on the Mac), pushed to Gitea `.18` (`david/muninn-vault`), and deployed at `/opt/muninn/vault` on the homelab.

- Never edit `wiki/bookmarks/` by hand; it is compiled.
- `wiki/projects/` is operator-owned reference prose, and nothing machine-edits it.
- The vault repo still carries the retired `skald_lint` script and any `skald:` markers. Removing them is a follow-up in that repo.

## Sibling tools

Muninn has **no dependency on Saga** at runtime, build or test time (ADR-010). Any harness may develop it. The other tools relate to it only through contracts:

- **Odin**: consumes Research Intake evidence packets (the `muninn-research-intake` producer contract) and uses muninn as an optional `recall` scope. It owns project state, proposals and decisions. Neither side reads the other's database.
- **Skald**: an independent, model-free transcript archiver. It is a possible future *source* of session pages via its archive feed. It is unrelated to the retired "Skald Protocol", which shared only the name.
- **Braid**: an independent hybrid retrieval engine, and a candidate backend behind the search interface (evaluated after the X slice, per ADR-014).
- **Saga**: a separate execution service. Muninn uses none of its credentials volume, launcher, containers or agents. Bragi is still Saga's and writes only to Saga's vault.
- **Homelab**: Qdrant (`.19`), Gitea (`.18`), Uptime Kuma (`.16`).

## Naming

Norse naming is not required going forward. Muninn is a standalone dataset and tool. Don't frame it as "the thing Saga builds". Don't reuse sibling-tool names (skald, heimdall, braid, odin) for muninn-internal concepts.

## Privacy

The bookmark corpus is personal and quasi-identifying. Only `README.md` and project-level design docs are public.

- Real bookmark data, scraped content, X API responses and user-specific patterns stay out of public-facing files.
- The GitHub remote is public-facing; the Gitea remote is private.
- Private non-secret material goes to **dotprivate**: `.private/muninn/`, a sparse clone of the operator's private notes repo, globally git-ignored. Write it there, then run `dotprivate sync`. This covers design handoffs from private repos, X API qualification results and scrubbed raw responses.
- Real secrets never go there unencrypted.
