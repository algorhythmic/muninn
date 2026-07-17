# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Where truth lives

- `docs/muninn-roadmap.md` — **current work queue.** Read "Blocking v1" first; if anything there is unfinished, that's the work.
- `docs/muninn-decisions.md` — ADR log (ADR-001…008). Where an ADR conflicts with `SPEC.md`, **the ADR wins** — the ADRs postdate the spec and record the unified-wiki pivot.
- `SPEC.md` — detailed pipeline design (schema, synthesis I/O contracts, URL sanitization, scraper strategy). Still authoritative for pipeline mechanics not touched by the ADRs.
- `docs/skald-protocol.md` — **canonical contract for session capture and project-state pages.** Overrides older roadmap Phase 3 wording; the `/note` skill (`docs/note-SKILL.md`) is its first emitter and defers to it.
- `docs/huginn-spec.md` — sibling triage service; defines what Muninn must eventually expose (Phase 5).
- `MUNINN.MD` — raw brainstorming history only.

## Project status

**v1 pipeline implemented** (commit `ab874cb`): ingest → sanitize → dual-pass scrape → Haiku enrich → consumers (CLI, MCP, vault compiler, timeline, Parquet), plus synthesis scaffolding and a real test suite. The code was written against `SPEC.md` and predates the unified-wiki ADRs by four days — reconciling it with the vault-first architecture is part of "Blocking v1" (see Known gaps).

## What Muninn is

A **unified personal wiki** (Karpathy llm-wiki pattern, ADR-001/006): one Obsidian-browsable vault with namespaces — `wiki/bookmarks/`, `projects/`, `sessions/`, `infra/`, `concepts/`, `people/` — one Qdrant index (homelab LXC `.19`), one MCP endpoint. v1 populates only `wiki/bookmarks/` from a decade of browser bookmarks (ADR-005 scope discipline); `projects/` is seeded manually; `sessions/` fills via the Skald Protocol. Memex is retired as a name; its scope is `wiki/infra/`.

## Architecture — two lanes, two canonical stores (ADR-008)

- **Bookmark lane:** SQLite is canonical. `raw/bookmarks/bookmarks.html` → `bookmarks` table (sanitized URLs) → `scrape_results` child table (live + at-capture Wayback passes) → `enriched` (Haiku 4.5, idempotent via `enrichment_model` + `enrichment_prompt_version` + `content_hash`) → compiled out to the vault's `wiki/bookmarks/{slug}.md`, FTS5, and Qdrant. The vault compiler is the *only* writer of `wiki/bookmarks/`.
- **Skald lane:** vault-first. Emitters (`/note` now, Bragi in Phase 3) write `wiki/sessions/`, managed regions in `wiki/projects/`, and `wiki/log.md` directly, one git commit per capture. Never mirrored into SQLite. Validator: `scripts/skald_lint.py`.
- **Qdrant is derived data in both lanes** — rebuildable, never a source of truth.

Per-bookmark `ADD_DATE` unix timestamps are authoritative dates; top-level folder names are **user-assigned era labels**, not dates — bracket eras by min/max `add_date` of contents.

## Code map

`src/muninn/` — `ingest/` (Netscape HTML parser + upsert), `sanitize/` (token-shape regex, param denylist, scheme rules), `scrape/` (live, at_capture ±365d, recent_archive; IA CDX cache; rate limiter; auth-wall heuristic), `enrich/` (Haiku via Anthropic API, prompt-cached, idempotency triple), `vector/` (pluggable embeddings — sentence-transformers/EmbeddingGemma default, `hash` placeholder for tests/offline via `MUNINN_EMBEDDING_BACKEND`; Qdrant helpers), `synthesis/` (Opus container orchestrator + JSON Schema validation), `consumers/` (Click CLI `muninn …`, FastMCP stdio server, vault compiler + Jinja template, timeline, Parquet).

CLI: `muninn ingest|scrape|enrich|synthesize|triage|status|deep-pass|export|timeline|vault|mcp`. DB init: `scripts/init-db.py`; schema in `schema.sql` (= `schemas/sql/001_initial.sql`).

## Known gaps (audited 2026-07-16 — verify before trusting, fix opportunistically)

1. Semantic search is implemented but unproven: real embeddings (EmbeddingGemma via `muninn[embeddings]` extra) and the query-path fix landed 2026-07-16, but the extra isn't installed in any environment, Qdrant isn't stood up, and recall is untested (roadmap smoke test). Note `google/embeddinggemma-300m` is HF-gated — accept the license + `hf auth login`, or set `MUNINN_EMBEDDING_MODEL` to an ungated 768-dim model.
2. Vault "Related" links come from era/tag heuristics, not `cross_references` (SPEC wants bidirectional model-produced refs) — revisit once deep passes populate that table.
3. Synthesis container launches interactive `claude` but never feeds it the task JSON; `task-input.schema.json` rejects the orchestrator's own `attempt` field. (Phase 4 — don't fix ahead of the roadmap.)
4. No robots.txt respect in the live scraper.

Closed 2026-07-16 (same-day fix pass): compiler now targets `{vault}/wiki/bookmarks/{slug}.md`; missing/malformed `domain_policy.yml` aborts; MCP `get_bookmark()` hides `content_visible=0` rows; deep pass clears `deep_pass_requested`; `.env` is loaded (env wins); enrich CLI has `--force`/`--prompt-version`; FTS re-enrichment no longer fails (contentless FTS5 rejected DELETE — table is now content-storing).

## Design decisions

Six SPEC decisions stand except where ADRs refine them: bookmarks-only v1 with source-agnostic schema; multi-language monorepo with the vault in a **separate** Gitea repo; HTTP-only dual-pass scraper (`scrape_results` child table); at-capture ±365d with recent_archive fallback and `enrichment_source` provenance; sanitize-don't-exclude with `domain_policy.yml` gating visibility; Haiku for bulk / Opus subscription-container for synthesis (`claude -p` silently forces API billing — use Saga's interactive-container pattern).

ADRs: 001 unified wiki; 002 Haiku worker pool, no dvergar for bulk; 003 Huginn stays separate; 004 vault is source of truth for project state; 005 v1 = bookmarks only; 006 wiki over raw RAG; 007 Muninn/Huginn are Saga workloads, not generalization triggers; 008 two-lane canonical-store carve-out.

Binding constraints to preserve: source-agnostic schema; original URL never stored after ingest; idempotency columns on every enrichment; **compiled vault and personal vault must never be the same vault**.

## The vault

Separate repo: `~/Documents/Code/muninn-vault` locally, pushed to Gitea `.18` (`david/muninn-vault`), deployed at `/opt/muninn/vault` on the homelab. Never edit `wiki/bookmarks/` by hand (compiled); never machine-edit outside skald markers in `wiki/projects/` (operator-owned prose).

## Saga dependency

Depends on Saga Phase 1+2 only (credentials volume, keepalive cron, devcontainer base, container-launch pattern) — already done. Does **not** depend on Phase 3 orchestration. v1 work proceeds immediately.

## Naming

Norse naming is not required going forward. Muninn is a standalone dataset that Saga happens to be one builder of — don't frame it as "the thing Saga builds."

## Privacy

The bookmark corpus is personal and quasi-identifying. Only `README.md` and project-level design docs are public. Real bookmark data, scraped content, and user-specific patterns stay out of public-facing files. The GitHub remote is public-facing; the Gitea remote is private.

## Related projects

- **Saga** — dvergar fleet + Bragi (future skald emitter, Phase 3); provides the synthesis container pattern.
- **Huginn** — information-triage dashboard; consumes Muninn MCP + writes `raw/inbox/huginn/` promotions at its v0.2 (Muninn Phase 5).
- **Homelab** — Qdrant (`.19`), Gitea (`.18`), Uptime Kuma (`.16`).

## Skald

- skald.project: muninn
- skald.vault: /Users/daviddunn/Documents/Code/muninn-vault
