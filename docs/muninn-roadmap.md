# Muninn Roadmap

This is the single view of what's next, what's queued, and what's deferred.
Scan it before each work session to get your bearings.

For detailed design, see [`SPEC.md`](../SPEC.md). The ADRs dated 2026-10-21
(008–015) amend it in places.
For architectural rationale behind key decisions, see [`muninn-decisions.md`](muninn-decisions.md).
For the Research Intake workflow (X bookmarks → evidence packets for Odin), see [`research-intake/`](research-intake/README.md).
For operator guidance to Claude Code sessions working on this repo, see [`CLAUDE.md`](../CLAUDE.md).

---

## Blocking v1 — X bookmarks end to end (ADR-012)

These items must be done to ship v1. v1 is the operator's X bookmarks plus
their linked articles and repositories. Each item goes through capture,
enrichment and suitability assessment, then becomes a vault page that is
searchable through MCP. Each item can also be exported as a Research
Intake evidence packet.

The browser-bookmark pipeline is implemented, but its bulk run is
**v1.1** (below). Work the items in order; independent items may overlap.

- [ ] **P1 dependency fixes.** *In progress 2026-10-21 on branch `adr-reset-x-first`.* These protect every later item, including the v1.1 bulk scrape.
  - Pin `selectolax<1`, and make an extraction-backend import failure fatal. Without the pin, a fresh install silently returns `""` and degrades every scrape.
  - Pin `mcp<2`; `muninn mcp` cannot import on 2.x.
  - Pass `--prompt-version` through to the prompt loader.
  - Add a lockfile.
  - Drop the unused `aiosqlite` and move `pyarrow` to `dev`.
  - Make the `scrape` imports lazy, and add an import-isolation test.

  See [`RI-01-extension-spike.md`](research-intake/RI-01-extension-spike.md) §6.
- [ ] **P2 schema migrator.** Add numbered `schemas/sql/NNN_*.sql` files and `PRAGMA user_version`, with backup-before-migrate. Add an explicit `muninn db migrate [--dry-run]`. Have `conftest` create test databases via migrate. This replaces "drop DB, re-ingest" (ADR-009).
- [ ] **RI-02 X access qualification.**
  - **Live (operator-run):** create the developer app, run the PKCE flow, and fetch a bounded set of pages. Set a **$5 usage-spend cap**. Results go to dotprivate, not this repo.
  - **Dev side:** synthetic fixtures from the documented response shapes, and a connector stub with recorded-response tests.
- [ ] **RI-03 records and producer contract.** Write `muninn-research-intake` 0.1.0-draft: JSON Schema 2020-12 records, valid/invalid fixtures, identity and revision rules, tombstones, and a compatibility policy.
- [ ] **X ingest adapter and linked-content scrape.**
  - `muninn ingest x` (`ingest/x.py`) writes `bookmarks` rows with `source='x'`. Linked URLs become their own rows, joined by `item_links`.
  - Every locator goes through `sanitize_url`.
  - Linked articles and repositories are fetched by the existing `muninn scrape`. The live pass is the current evidence.
  - Add append-only `scrape_history`.
- [ ] **Suitability enrichment and packet export.**
  - `muninn enrich --task suitability` uses the prompt `suitability_v1`. Its idempotency key is the existing triple plus the interest-profile digest. Results go to an append-only `assessments` table.
  - `muninn export packets` writes content-addressed JSON that validates against the RI-03 schemas.
- [ ] **Compile, index and search X pages.** The vault compiler writes X bookmarks to `wiki/bookmarks/`. Index them into Qdrant (EmbeddingGemma, already verified), and verify recall through MCP search.
- [ ] **Retrieval interface (ADR-014).** Put the MCP search tools behind one small search interface, so a later backend swap (Braid) touches one module. Qdrant stays the v1 backend.

## v1.1 — browser bookmarks (formerly v1)

This section is the former v1 scope: the bulk run over the ~10k-bookmark
Netscape HTML export. The pipeline is implemented (commit `ab874cb`) and
tested. The bulk run waits for P1, because of the `selectolax` regression.

- [x] **Stand up the vault skeleton.** *Done 2026-07-16: `~/Documents/Code/muninn-vault`, tagged `v1-skeleton`, pushed to Gitea `david/muninn-vault`.* One git repo with the namespace structure (`raw/`, `wiki/{bookmarks,projects,sessions,infra,concepts,people}/`, `index.md`, `log.md`). *2026-10-21:* `wiki/sessions/` is retired and `wiki/projects/` is reference-only (ADR-011). The vault repo still carries the `skald_lint` script and any `skald:` markers. Removing them is a follow-up in that repo.
- [x] **Update CLAUDE.md for the unified-wiki pattern.** *Done 2026-07-16; revised 2026-10-21 for ADRs 008–015.*
- [ ] **Run the bulk bookmark enrichment.** Use the Python asyncio pool with Haiku via the direct Anthropic API. The input is the `raw/bookmarks/bookmarks.html` export. The output is one `wiki/bookmarks/{slug}.md` per bookmark.
  *Status 2026-07-16: the SQLite pipeline shipped in `ab874cb`. The compiler writes `{vault}/wiki/bookmarks/{slug}.md`, and the privacy/correctness gaps are closed. Remaining: run it on the real export, after P1. Also decide whether to move enrichment from Haiku 4.5 to Haiku 5.5 first (much cheaper, but breaking API changes).*
- [x] **Stand up Qdrant at `.19`.** *Done 2026-07-17: unprivileged LXC 119 (Debian 13), static binary + systemd, data on `/var/lib/qdrant`. The `muninn_bookmarks` collection is live (768-dim, cosine). Remaining nicety: a `/healthz` monitor in Uptime Kuma at `.16`.*
- [ ] **Index the full browser-bookmark corpus into Qdrant.** *Code side done 2026-07-16: pluggable embedding backends, EmbeddingGemma default, embedded query path.* Remaining: run `scripts/reconcile-vector-index.py` after the bulk run.
- [x] **Smoke-test retrieval.** *Done 2026-07-16: the newest 100 bookmarks were scraped, and 58 were enriched and indexed. Recall was verified through MCP search: topical queries scored 0.3–0.5 cosine, against ~0.1 for an off-topic control. **EmbeddingGemma-300m confirmed.***
- [x] **Git-initialize the vault.** *Done 2026-07-16: pushed to Gitea (private), tag `v1-skeleton`.*
- [x] **Write `wiki/index.md` initial version.** *Done 2026-07-16.*
- [ ] **Maintain `wiki/projects/` as reference notes.** *Partial 2026-07-16: `muninn`, `saga`, `huginn` and `homelab` were drafted from repo docs; `nexus` and `esp32-surveillance` are stubs.* Per ADR-011, these are operator-written reference notes with a "not canonical" banner and no machine writes. Odin owns canonical project state.

## Phase 2 — MCP server

Target: make Muninn's saved knowledge available to Claude Code sessions and Claude.ai, and to Odin as a `recall` scope.

- [ ] **Implement the MCP server.** It is bound to the search interface plus the vault filesystem. Entry points:
  - `search(query: str, namespace: str | None, top_k: int = 10)` — semantic + keyword hybrid
  - `get_page(path: str)` — fetch a specific `wiki/*.md` page by path
  - `list_namespace(namespace: str)` — enumerate pages in a namespace with one-line summaries
- [ ] **Expose MCP over Tailscale.** The MCP endpoint is reachable on the tailnet only, with no public internet exposure.
- [ ] **Wire up the Claude.ai remote MCP connector.** Register Muninn as a custom connector, for recall over saved knowledge. Project truth lives in Odin (ADR-011).
- [ ] **Document the MCP contract.** Write `docs/mcp-api.md` for consumers (Odin recall, Claude sessions) and for operator debugging.

## Phase 3 — removed

*Bragi integration was deleted 2026-10-21. Muninn holds no canonical project or session state (ADR-011).*

## Phase 4 — synthesis contract (ADR-013)

This phase comes after the X slice. Synthesis (era narratives, deep passes, ad-hoc analyses) runs through a harness-agnostic task-file → result-file contract:

- Muninn writes a validated task file, using the existing `schemas/json/*` I/O schemas.
- Muninn validates the result file and writes it to the database.
- The default runner is a direct Anthropic API call with an explicit budget. Any operator-chosen harness can run the same task file instead (a local Claude Code session, or an Odin-dispatched session).

- [ ] **Define the contract.** Specify the task and result file layout. Fix `task-input.schema.json`, which rejects the orchestrator's `attempt` field. Specify how a run is triggered: `muninn synthesize … --emit-task` / `--ingest-result`.
- [ ] **Implement the budgeted API runner.** Add per-run and monthly caps, use the batch API where latency allows, and persist usage.
- [ ] **Retire the Saga-style container path.** Remove `containers/synthesis/`, `scripts/launch-synthesis.sh` and the credentials-volume config once the contract lands.
- [ ] **Synthesis use cases.**
  - *Era narratives:* "summarize my reading on [topic] over [period]".
  - *Deep pass:* flagged bookmarks get a longer summary plus key quotes.
  - *Ad-hoc analyses:* an operator slice-spec produces a markdown page filed into `wiki/`.

## Phase 5 — removed

*The Huginn consumer phase was deleted 2026-10-21; Huginn is deferred (ADR-015). Automatic discovery, if ever wanted, would return as another source feeding the same intake, under a new ADR.*

## Phase 6 — Lint and maintenance

This is Karpathy's "Lint" operation: periodic vault hygiene.

- [ ] **Contradiction detection.** A synthesis-tier pass over pages that share tags flags contradictions inline with `<!-- contradiction: {other_page} -->` comments.
- [ ] **Orphan detection.** Pages with zero inbound links get flagged for review or concept extraction.
- [ ] **Stale claim supersession.** When a new ingest contradicts an existing page, the older claim gets a `superseded_at:` frontmatter field. Both versions remain, and git history preserves the original.
- [ ] **Missing cross-reference suggestion.** When a page mentions an entity that exists elsewhere in the vault but isn't linked, the lint pass suggests the link.
- [ ] **Cadence.** Weekly, or on demand from the CLI.

## Post-X

- [ ] **Braid evaluation spike (ADR-014).**
  1. Export the X-bookmark pages and links to Braid's nodes and edges.
  2. Label 20–30 queries with known answers.
  3. Compare Braid against the Qdrant path with Braid's eval harness (recall, MRR, nDCG).
  4. Record the operating cost: the Go binary, and the asymmetric-embedding gap.

  Adopt Braid only on measured results.
- [ ] **Research Intake RI-04 – RI-08** follow-through per the Odin design handoff (kept privately). Muninn's share of RI-07 is publishing the contract; Odin owns executive review, proposals and decisions.

## Future

| Item | Notes |
|---|---|
| Session pages from Skald's archive feed | A batch summarizer turns Skald's verbatim transcript archive into curated `wiki/sessions/` pages, as a captured-lane source (ADR-011). It replaces the retired `/note` skill |
| Other saved-material sources | YouTube, Reddit, Instagram, GitHub stars, Pocket/Pinboard exports. Each is one adapter into the source-agnostic `bookmarks` table |

## Feature backlog — design complete, not yet scheduled

| Feature | Notes |
|---|---|
| Public portfolio subset | A curated subset of `wiki/bookmarks/` published as a static site ("things worth remembering"). A long shot; it requires deciding what's publishable vs. private. |
| Automatic concept extraction | A lint pass that identifies recurring themes and creates `wiki/concepts/` pages for them. Currently, concepts are created manually or ad hoc during synthesis. |
| Obsidian Web Clipper integration | The operator drops articles into `raw/articles/` via the browser extension, and Muninn enriches them into `wiki/bookmarks/` (or its own namespace). |
| Dataview views | Obsidian Dataview queries for common operator workflows: "bookmarks from last month by tag", "projects with recent activity", and so on. |

## Deferred

| ID | Summary | Blocker |
|---|---|---|
| SEMANTIC-DEDUPE | Detect when two bookmarks point at essentially the same content | Requires scale to matter; hand-curate for v1 |
| NON-TEXT-SOURCES | PDFs, podcasts, video beyond transcripts | Changes the ingest shape significantly |
| MULTI-USER | Share subsets of the vault with other people | May never happen; personal tool by design |

## Completed

| Date | Summary |
|---|---|
| 2026-04 | Project conceived as a personal bookmark knowledge base |
| 2026-04 | SPEC.md drafted, with six core design decisions resolved |
| 2026-04 | Initial CLAUDE.md |
| 2026-04-15 | **v1 pipeline implemented** (commit `ab874cb`): ingest, sanitize, dual-pass scrape, Haiku enrich, synthesis scaffolding, consumers (CLI/MCP/vault/timeline/Parquet), tests |
| 2026-04-19 | **Unified-wiki architectural decision.** Scope expanded to a unified vault with namespaces; Memex retired. See ADR-001 and ADR-005 (005 since superseded by ADR-012). |
| 2026-04-19 | **Ingest pattern confirmed:** bulk Haiku worker pool. See ADR-002 (its synthesis half is superseded by ADR-013). |
| 2026-04-19 | **Huginn integration shape defined.** See ADR-003 (superseded by ADR-015). |
| 2026-04-19 | ROADMAP.md and DECISIONS.md created from conversation synthesis |
| 2026-07-16 | **Skald Protocol drafted**, with the `/note` skill as its first emitter. Retired 2026-10-21 (ADR-008, ADR-011) and moved to `docs/history/`. |
| 2026-10-21 | **RI-01 Research Intake extension spike.** It established the test baseline (446/446 with `selectolax<1`; 438 after P1 retired the 17 `skald_lint` tests and added 9), found the dependency-drift defects, and produced the migration plan. See [`research-intake/`](research-intake/README.md). |
| 2026-10-21 | **ADR reset (ADR-008 – ADR-015).** It covers:<br>• Muninn as a standalone tool, with the Saga lineage recorded<br>• no canonical project or session state<br>• X bookmarks first<br>• a harness-agnostic synthesis contract<br>• pluggable retrieval, with a Braid evaluation after X<br>• Huginn deferred<br>• stage-native Research Intake with explicit migrations |

---

## How to use this document

When starting a work session on Muninn:

1. Read the "Blocking v1" section. If anything there is unfinished, that's your work.
2. If "Blocking v1" is clear, check the next phase's items. The phases are sequenced; don't jump ahead unless a specific phase item is independent (rare).
3. Before making architectural choices not already covered, read `muninn-decisions.md` for context. New architectural calls get new ADR entries.
4. Update this file at the end of each session: check off completed items, add status notes to items that are mid-flight, and move finished work to "Completed".

This roadmap is the wiki equivalent of the operational source of truth. It lives in git, and its history is the project's history.
