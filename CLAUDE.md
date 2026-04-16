# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project status

**Spec drafted, implementation pending.** The repo contains:

- `README.md` — public-facing framing
- `MUNINN.MD` — original brainstorming transcript with the user
- `SPEC.md` — **authoritative** working specification with all six core design decisions resolved + Constraints, External services registry, Work stream decomposition (5 streams with success criteria), Rejected alternatives. Schema, synthesis I/O contracts, monorepo layout, and URL sanitization rules all pinned.
- `rawspec.md` — entity-page-friendly restructuring of `SPEC.md`, prepared for handoff to Saga Phase 3 (Odin spec ingestion). Inferred from Saga's `raw/` + `wiki/` schema since the canonical `odin/specs/templates/project-template/CLAUDE.md` wasn't available at authoring time. Drops into Saga's `raw/spec.md` slot. **`SPEC.md` is authoritative if the two ever diverge.**
- `docs/SAGA_*.MD` — Saga architecture/setup/insights docs that ground the synthesis-container design

No code, no build, no tests, no commits yet. Most current tasks are continued spec work, design document edits, or small manual prototypes — not full implementation. When in doubt about a design detail, `SPEC.md` is authoritative; `MUNINN.MD` is raw brainstorming history.

### Saga dependency (revised)

Originally framed as "blocked on Saga Phase 3." This is **no longer the case** — the spec decoupled Muninn from Phase 3:

- **Depends on Saga Phase 1+2 infrastructure (already done):** the `saga-claude-credentials` Docker named volume, the keepalive cron that exercises refresh-token rotation, the Trail of Bits devcontainer base image, the dvergr container-launch pattern.
- **Does NOT depend on Saga Phase 3 orchestration (not yet shipped):** Odin's spec decomposition and multi-dvergr launch machinery aren't needed at v1's single-instance synthesis volume.

v1 implementation can begin immediately on top of existing Saga infrastructure.

## What Muninn is

A personal knowledge base built from a decade of accumulated bookmarks, exposed through two interfaces:

- **Layer 1 — Obsidian-browsable vault** (Bragi-style: one markdown page per bookmark, git-versioned).
- **Layer 2 — semantic index** (Qdrant on the homelab LXC at `.19`) exposed via an MCP server for LLM brainstorm queries.

Crucially, the design has shifted (per `MUNINN.MD`) to be **data-pipeline-first, consumers-second**. The core product is a clean, normalized SQLite dataset; vault, MCP, CLI, timeline view, and Parquet exports are all downstream consumers of the same store. Don't over-couple new design work to the Saga/MCP path — other consumers are expected.

## Architecture (planned data flow)

```
raw/         bookmarks.html committed untouched
  ↓
normalized/  SQLite — url, title, folder_path[], era_label, add_date, domain
  ↓
enriched/    same SQLite + scrape/summary/tags/entities/content_type columns
  ↓          (each enrichment stage idempotent + resumable)
  ├─► SQLite FTS5 index
  ├─► Qdrant vector index (.19)
  └─► eras/ derived table — per "era_label" narrative
consumers/   MCP server, Obsidian vault, CLI, timeline view, Parquet export
```

Key insight that should anchor schema work: Netscape bookmark HTML carries `ADD_DATE="<unix-ts>"` per entry, so per-bookmark dates are authoritative. The user's nested folder names ("Jan 1", "Feb 8", "Mar 4") are **user-assigned era labels**, not dates — bracket each era's real time range by the min/max `add_date` of its contents.

SQLite is the chosen store (portable single file, FTS5 built in, readable from anything). Schema stability matters: re-running cheap stages is fine, but re-running 10k LLM summarizations is expensive — pin the stage-2 schema before any enrichment runs.

## Resolved design decisions (see `SPEC.md` for details)

All six core decisions are settled:

1. **Scope of v1:** bookmarks-only; schema is source-agnostic for future Evernote/Apple Notes/Obsidian/YouTube/Spotify/SoundCloud adapters
2. **Delivery shape:** multi-language monorepo (Python primary), Obsidian vault is OUT of this repo (separate Gitea repo); two distinct vaults — compiled (Muninn output) and personal (future Muninn input) — must never be the same vault
3. **Scraper strategy:** HTTP-only v1, dual concurrent pass per bookmark (live + at-capture-from-Wayback). Playwright/screenshots deferred to v1.5/v2. Scrape results live in a `scrape_results` child table, not on the bookmark row, so future passes (Playwright, recent_archive, etc.) slot in without migration
4. **Dead-link policy:** at-capture window ±365 days; recent_archive fallback on by default; live_fallback as canonical when both archive passes fail (with `enrichment_source` flag making provenance explicit downstream)
5. **Sensitive data:** sanitize, don't exclude. Universal URL sanitization in code (token-shape regex, dangerous-param denylist, path-as-credential domain handling); per-domain `domain_policy.yml` with single `content_visible: false` toggle that gates scrape+enrich+vault+mcp together but never excludes from `normalized/`; auth-wall detection automatic
6. **LLM tiering:** Haiku 4.5 via Anthropic API for bulk per-bookmark enrichment; Opus 4.6 with 1M context via subscription-mode container (sibling of dvergr) for synthesis. `claude -p` is **not usable** for subscription billing — it silently forces API mode (per SAGA_ARCHITECTURE.MD:141). Synthesis uses Saga's interactive-mode pattern with the credentials volume mounted

When extending the spec, preserve the binding constraints: source-agnostic schema, never-the-same-vault rule for input/output Obsidian vaults, original-URL-never-stored after ingest, idempotency via `enrichment_prompt_version` + `content_hash` + `enrichment_model` columns.

## Naming convention

Muninn started as a Norse name to match Saga's ecosystem, but per the user's correction in `MUNINN.MD`: **Norse naming is not required going forward.** Muninn has uses well beyond Saga; don't assume sibling components must inherit Norse names. Don't frame Muninn primarily as "the thing Saga builds" — it's a standalone dataset that Saga happens to be one builder of.

## Privacy

The bookmark corpus is personal and quasi-identifying (reveals employment, projects, debugging, interests). Only `README.md` and project-level design docs are public. Anything containing real bookmark data, scraped content, or user-specific patterns stays out of public-facing files.

## Related projects in the user's ecosystem

- **Saga** — provides the dvergar fleet that does parallel scrape+summarize; Bragi compiles the vault. Phase 3 is the prerequisite.
- **Homelab** — hosts Qdrant (`.19`) and the vault's Gitea repo.
- **Memex** *(future)* — sibling using the same architecture pattern for homelab state.
