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

- [x] **P1 dependency fixes.** *Done 2026-10-21 (PR #1, branch `adr-reset-x-first`).* Pins for `selectolax<1`, `mcp<2` and `anthropic` 2.x, a fatal extraction-backend import, `--prompt-version` passthrough, `uv.lock`, `aiosqlite` dropped and `pyarrow` moved to `dev`, lazy `scrape` imports with an import-isolation test. See [`RI-01-extension-spike.md`](research-intake/RI-01-extension-spike.md) §6.
- [x] **P2 schema migrator.** *Done 2026-10-21 on `x-slice`.* Numbered `schemas/sql/NNN_*.sql`, `PRAGMA user_version`, backup-before-migrate, `muninn db migrate [--dry-run]` and `muninn db status`; `conftest` builds test databases through the migrator. Replaces "drop DB, re-ingest" (ADR-009, RI-01 §4).
- [ ] **RI-02 X access qualification.**
  - [ ] **Live (operator-run):** create the developer app, run `muninn x auth`, and fetch a bounded set of pages (`muninn ingest x --max-pages 2`). Set a **$5 usage-spend cap**. Results go to dotprivate, not this repo. Settles the open questions in the contract (Article body, folder semantics, history depth, how deleted/protected posts surface, expansion billing).
  - [x] **Dev side.** *Done 2026-10-21 on `x-slice`.* Synthetic fixtures (`tests/fixtures/x/`) from the documented response shapes, and the connector `ingest/x.py` with recorded-response tests.
- [x] **RI-03 records and producer contract.** *0.1.0-draft, validated, 2026-10-21 on `x-slice`.* `docs/contracts/MUNINN-RESEARCH-INTAKE.md`: JSON Schema 2020-12 records, valid/invalid fixtures, identity and revision rules, tombstones, compatibility policy and a release manifest. Needs operator and Odin review before 0.1.0.
- [x] **X ingest adapter and linked-content scrape.** *Code done 2026-10-21 on `x-slice`; no live run yet.*
  - `muninn ingest x` (`ingest/x_store.py`) writes posts as `source='x'`, articles as `x_article` and linked URLs as `x_link` rows joined by `item_links`; incremental by default, resumable from `sync_checkpoints`, with per-page `usage_records`.
  - Every locator goes through `sanitize_url`; links hidden by domain policy become `[hidden link]` in stored text.
  - `muninn scrape` skips X rows, takes `--source` and `--refresh`, writes append-only `scrape_history`, and expires live cache entries (D9).
- [x] **Suitability enrichment and packet export.** *Code done 2026-10-21 on `x-slice`; no live model run yet.*
  - `muninn enrich --task suitability` (prompt `suitability_v1`) appends to `assessments`, keyed by content revision + profile digest + policy version + model + prompt version, with `--limit` and `--max-cost-usd`.
  - `muninn export packets` writes content-addressed JSON validated against the RI-03 schemas; `--withdraw` leaves a tombstone.
- [x] **Compile, index and search X pages.** *Code done 2026-10-21 on `x-slice`, verified end to end on synthetic data.* X posts, articles and linked items compile to `wiki/bookmarks/` with bidirectional links; they reach FTS5 and Qdrant through the normal enrichment path; recall verified through the MCP tools. Remaining: the same check on the first real sync.
- [x] **Retrieval interface (ADR-014).** *Done 2026-10-21 on `x-slice`.* `muninn.search`, selected by `MUNINN_SEARCH_BACKEND`; Qdrant + FTS5 stay the v1 backend.

**First real run (operator):** after RI-02a, run `muninn db migrate` on the homelab database, then `muninn ingest x`, `muninn scrape --source x_link`, `muninn enrich`, `muninn enrich --task suitability --limit 20`, `muninn export packets`, `muninn vault compile` and `scripts/reconcile-vector-index.py`, and spot-check recall. Before any bulk model run, make one small real `muninn enrich` call to confirm the Anthropic SDK 2.x path, which no test exercises.

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

- [x] **Implement the MCP server.** *Done 2026-10-21 on `x-slice`* (contract in [`mcp-api.md`](mcp-api.md)). It is bound to the search interface plus the vault filesystem. Entry points:
  - `search(query: str, namespace: str | None, top_k: int = 10)` — semantic + keyword hybrid
  - `get_page(path: str)` — fetch a specific `wiki/*.md` page by path
  - `list_namespace(namespace: str)` — enumerate pages in a namespace with one-line summaries
- [ ] **Expose MCP over Tailscale.** The MCP endpoint is reachable on the tailnet only, with no public internet exposure. *Code side done 2026-10-21:* `muninn mcp --transport http [--host H] [--port P] [--allowed-host NAME]` (streamable HTTP, loopback by default, no auth layer). Remaining (operator): `tailscale serve` / ACLs on the homelab host.
- [ ] **Wire up the Claude.ai remote MCP connector.** Register Muninn as a custom connector, for recall over saved knowledge. Project truth lives in Odin (ADR-011).
- [x] **Document the MCP contract.** *Done 2026-10-21:* [`mcp-api.md`](mcp-api.md). Write `docs/mcp-api.md` for consumers (Odin recall, Claude sessions) and for operator debugging.

## Phase 3 — removed

*Bragi integration was deleted 2026-10-21. Muninn holds no canonical project or session state (ADR-011).*

## Phase 4 — synthesis contract (ADR-013)

This phase comes after the X slice. Synthesis (era narratives, deep passes, ad-hoc analyses) runs through a harness-agnostic task-file → result-file contract:

- Muninn writes a validated task file, using the existing `schemas/json/*` I/O schemas.
- Muninn validates the result file and writes it to the database.
- The default runner is a direct Anthropic API call with an explicit budget. Any operator-chosen harness can run the same task file instead (a local Claude Code session, or an Odin-dispatched session).

- [x] **Define the contract.** *Done 2026-10-21 on `x-slice`:* `muninn-synthesis/1` in [`synthesis-contract.md`](synthesis-contract.md); `muninn synthesize … --emit-task DIR` and `muninn synthesize ingest-result FILE`; `task-input.schema.json` accepts `attempt`.
- [x] **Implement the budgeted API runner.** *Done 2026-10-21 on `x-slice`:* per-run `--max-cost-usd` (default $3) and a required monthly cap, pre-call worst-case check, measured usage in `usage_records`. The Batch API and structured outputs remain future work.
- [x] **Retire the Saga-style container path.** *Done 2026-10-21:* `containers/synthesis/`, `scripts/launch-synthesis.sh`, `synthesis/container.py` and the credentials-volume config are removed.
- [ ] **Synthesis use cases.**
  - *Era narratives:* "summarize my reading on [topic] over [period]".
  - *Deep pass:* flagged bookmarks get a longer summary plus key quotes.
  - *Ad-hoc analyses:* an operator slice-spec produces a markdown page filed into `wiki/`.

## Phase 5 — removed

*The Huginn consumer phase was deleted 2026-10-21; Huginn is deferred (ADR-015). Automatic discovery, if ever wanted, would return as another source feeding the same intake, under a new ADR.*

## Phase 6 — Lint and maintenance

This is Karpathy's "Lint" operation: periodic vault hygiene.

- [x] **Contradiction detection.** A synthesis-tier pass over pages that share tags flags contradictions inline with `<!-- contradiction: {other_page} -->` comments. *Done 2026-10-23 on `x-slice`:* `muninn synthesize lint --tag TAG` (a `muninn-synthesis/1` task, budgeted like the others); findings live in SQLite (migration 003) and the compiler renders them only into compiled `wiki/bookmarks/` pages. See [`vault-lint.md`](vault-lint.md).
- [x] **Orphan detection.** Pages with zero inbound links get flagged for review or concept extraction. *Done 2026-10-23:* `muninn lint` (deterministic, read-only), which also reports unresolved links.
- [x] **Stale claim supersession.** When a new ingest contradicts an existing page, the older claim gets a `superseded_at:` frontmatter field. Both versions remain, and git history preserves the original. *Done 2026-10-23:* the same lint task's `supersedes` relation; the compiler adds `superseded_at:` and `superseded_by:` to the older compiled page. Authored pages are never edited.
- [x] **Missing cross-reference suggestion.** When a page mentions an entity that exists elsewhere in the vault but isn't linked, the lint pass suggests the link. *Done 2026-10-23:* `muninn lint` suggests links to concepts/infra/people/projects pages (report-only).
- [x] **Cadence.** Weekly, or on demand from the CLI. *Done 2026-10-23:* on demand via the CLI; an example weekly systemd user timer for the deterministic lint is in `deploy/systemd/` (not installed; the model tier stays manual because it spends money). Remaining (operator): install the timer on the homelab host.

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
| Session pages from Skald's archive feed | A batch summarizer turns Skald's verbatim transcript archive into curated `wiki/sessions/` pages, as a captured-lane source (ADR-011). It replaces the retired `/note` skill. **Depends on:** Skald publishing its archive-feed contract (`ARCHIVE-FEED-V1`, a candidate as of 2026-10-21) so muninn reads pinned feed files rather than Skald's socket; and Skald enrolling every login whose sessions matter (each Claude Code login's `projects/` root is a separate `skald connect --root`). Live progress of a running session is not this item's job; agents read the source transcript directly for that |
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
