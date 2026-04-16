# Muninn — Specification

Working spec doc. Decisions are pinned here as we settle them; open questions stay in `MUNINN.MD` until resolved.

---

## Vision (long-term)

Muninn is a **digital-footprint aggregator**: a clean, normalized, queryable record of the services and content that have shaped the user's digital life over time. Bookmarks are the v1 ingest because they're the largest and oldest corpus, but the long-term scope explicitly includes:

- Notes systems: Evernote, Apple Notes, Obsidian
- Media: YouTube (history, likes, subscriptions), Spotify, SoundCloud
- Anything else with an export that captures digital activity over time

This vision shapes the v1 schema even though v1 only ingests one source: the schema must be **source-agnostic from day one** so future adapters slot in without a migration.

---

## Constraints (binding on all v1 work)

These are the assumptions every dvergr should treat as fixed. Decisions that contradict them require explicit spec amendment, not silent override.

### Scope and scale
- **Single user, single machine** at v1. No multi-tenant concerns, no shared state, no concurrent-user contention.
- **Personal corpus, never published.** Privacy is binding throughout — see "Sensitive data" constraints below.
- **v1 corpus size assumption:** ~10k bookmarks. Architecture choices (single SQLite file, async worker pool for scraping, single Opus instance for synthesis) are calibrated for this scale. v2 multi-source ingest may push to 100k+ which triggers parallel-dvergr re-evaluation for synthesis specifically.
- **v1 ingests bookmarks only.** Other sources (Evernote, Apple Notes, Obsidian, YouTube, Spotify, SoundCloud) are v2+ adapters into the same source-agnostic schema. Don't build them in v1.

### LLM and authentication
- **Anthropic API key required** for bulk Haiku enrichment (`ANTHROPIC_API_KEY` env var).
- **Anthropic Max subscription required** for synthesis tier (Opus 4.6 + 1M context). Subscription is consumed via the existing `saga-claude-credentials` Docker named volume — Muninn does not manage the subscription auth.
- **Saga Phase 1+2 infrastructure must be present on the host machine at runtime** — credentials volume, keepalive cron, Trail of Bits devcontainer base image. These are **not** Muninn's responsibility to provision; if absent, Muninn fails loudly at synthesis-stage startup.
- **Saga Phase 3 (Odin spec decomposition + multi-dvergr launch) is NOT required at runtime.** Phase 3 may be used at *implementation* time to decompose this spec into dvergar streams (this is the first such use), but Muninn's runtime architecture does not depend on Phase 3 features.
- **`claude -p` and `CLAUDE_CODE_OAUTH_TOKEN` are off-limits for the synthesis path** — both silently force API billing per SAGA_ARCHITECTURE.MD:141. Subscription-mode synthesis requires the mounted-credentials-volume + interactive-mode pattern.

### Networking and scraping
- **HTTP-only scraping in v1.** No Playwright, no headless browser, no JavaScript execution, no screenshots. Deferred to v1.5/v2.
- **Per-domain HTTP politeness:** ≤1 rps to live origins, with a backoff on 429. Honest `User-Agent` identifying Muninn + contact URL. Respect `robots.txt`.
- **Internet Archive politeness (stricter):** ≤0.5 rps global across all IA endpoints (CDX + Wayback). Exponential backoff on 429s. `User-Agent` identifies Muninn + contact, per IA's stated norms. On-disk cache of CDX responses.
- **Internet Archive is the only archive provider in v1.** Other archives (archive.today, etc.) deferred to v2.

### Sensitive data
- **Original (unsanitized) URLs are never stored after ingest.** Only `raw/bookmarks.html` retains the original form; that file is gitignored locally and lives only in the user's private store. The single path from `raw/` → `bookmarks.url` is `sanitize_url()`.
- **Sanitize, don't exclude.** Sensitive domains have `content_visible=0` (no scrape, no enrich, no vault, no MCP) but the **event** (visited domain X on date Y) always survives in `normalized/`. This is binding — don't drop rows.
- **Sanitization rules live in code, not config.** Token-shape regex tuned by users would leak; PR-reviewed code is the right shape. Any change to `src/muninn/sanitize/rules.py` or `tokens.py` must include corresponding test cases in the same PR.
- **No log statement may emit a raw URL.** Logging passes through `redact_url_for_log()` first.

### Storage and compute
- **SQLite is the single canonical store.** Pipeline writes go through SQLite only. DuckDB attaches read-only as the analytics engine.
- **No Alembic-style migration framework in v1.** Recovery from schema change is "drop DB, re-ingest" — `raw/` is the source of truth and the pipeline is idempotent.
- **Qdrant runs on the homelab LXC at `.19`**, no auth in v1 (homelab-internal). Reassessable at first MCP integration.
- **Vector store choice (Qdrant vs. DuckDB-vss) is reassessable at first MCP integration**, not locked.
- **Embedding model is deferred** to first MCP integration; spec doesn't pin one yet.

### Vault topology
- **The compiled vault and the personal vault are never the same vault.** Hard rule. The compiled vault (Muninn output) and the personal vault (future Muninn input via Obsidian adapter) live in separate directories with separate git histories. The future `obsidian` ingest adapter must refuse to ingest from a directory containing a `.muninn-compiled-vault` sentinel file.
- **The compiled vault lives in its own Gitea repo**, not in this repo. Only the vault *generator* lives here.

---

## External services and secrets registry

Exhaustive list of every external dependency, what provisions its secret, and what happens when it's missing. Heimdall's future `secrets:` field (per FEATURE-SECRETS-MANAGEMENT) should provision these; for v1, manual workarounds are acceptable where noted.

| Service | Purpose | Secret kind | Provisioning | Failure mode |
|---|---|---|---|---|
| **Anthropic API** | Bulk Haiku enrichment per bookmark; prompt caching for cost control | `ANTHROPIC_API_KEY` env var | User sets in `.env`; `src/muninn/config.py` reads at startup. **Manual workaround:** populate `.env` from a secrets file the user maintains outside the repo. | `muninn enrich` halts at startup with explicit "ANTHROPIC_API_KEY missing or invalid" message. No partial enrichment. |
| **Anthropic Max (subscription)** | Synthesis container — Opus 4.6 + 1M context for era narratives, deep-passes, ad-hoc analysis | Docker named volume `saga-claude-credentials` containing `/home/vscode/.claude/.credentials.json` | One-time human bootstrap via Saga's `scripts/auth/bootstrap-credentials.sh` (lives in the Saga repo, not this one). Maintained by Saga's keepalive cron (refresh-token rotation every 4h). **Manual workaround:** ensure the volume exists before running any `muninn synthesize` command; if missing, Saga's bootstrap script must be re-run. | `muninn synthesize` exits with "synthesis credentials volume not found — see SAGA_SETUP.MD §5". |
| **Internet Archive (Wayback + CDX)** | At-capture and recent_archive scraper passes | None — public API. Identifying `User-Agent` required: `Muninn/v1 (+<contact-url>)`. Contact URL configurable via `MUNINN_CONTACT_URL` env var. | User sets `MUNINN_CONTACT_URL` in `.env` at first run; UA string is constructed in `src/muninn/scrape/client.py`. | Pass returns `scrape_status=network_error` or `no_archive`; pipeline continues per dead-link policy. Persistent IA outages surface in triage CLI. |
| **Qdrant (homelab LXC `.19`)** | Vector embeddings storage keyed by `bookmark_id` | `QDRANT_URL` env var (e.g., `http://192.168.86.19:6333`); v1 no auth (homelab-internal) | User sets in `.env`; Qdrant service must be running on the LXC. **Manual workaround:** if Qdrant is unavailable at enrich time, SQLite write still happens; `scripts/reconcile-vector-index.py` backfills missing embeddings on next run. | Embedding write fails with logged warning; SQLite enrich row still persists; reconcile script catches up later. |
| **`domain_policy.yml`** | Per-domain `content_visible` policy (sensitive data handling) | Plain YAML config file, committed to repo (with sensitive entries user-edited but un-pushed) | User-edited at repo root | If file missing or malformed: ingest aborts with explicit error. **Never silently default to "scrape everything"** — the safety property requires the file's presence to be acknowledged. |
| **`.env` file** | All env vars above | dotenv format | User copies `.env.example` and fills in values | Pipeline aborts at startup with which-var-missing message. |

### Notes for heimdall provisioning (future)

When FEATURE-SECRETS-MANAGEMENT Tier 1 lands, the heimdall `secrets:` field should be able to provision:
- `ANTHROPIC_API_KEY` from a sops-encrypted secrets file
- `QDRANT_URL` and `MUNINN_CONTACT_URL` from the same source
- The `saga-claude-credentials` volume reference (volume already exists; just needs binding)

Until then, the `.env` + manual volume reference is the workaround.

---

## Work stream decomposition

v1 implementation decomposes into **5 streams**. Each stream owns specific tables and modules; cross-stream contracts are the SQLite schema (Section "Schema") and JSON Schemas (Section "Synthesis container — I/O schemas"). Streams downstream from a dependency cannot complete-promise until the upstream stream's success criteria are met.

### Stream 1 — Ingest + Sanitization

**Owns (writes):** `bookmarks` table.
**Reads:** none — this is the foundation.
**Modules:** `src/muninn/ingest/`, `src/muninn/sanitize/`, `src/muninn/db.py`, `src/muninn/models.py`, `src/muninn/config.py`, `schema.sql`, `schemas/sql/001_initial.sql`, top-level `pyproject.toml`, `.env.example`, `.gitignore`, `domain_policy.yml`.
**Depends on:** nothing.

**Success criteria (completion-promise fires when ALL true):**
1. `scripts/init-db.py` applies `schema.sql` cleanly against a fresh SQLite file; all 7 tables (`bookmarks`, `scrape_results`, `enriched`, `eras`, `analyses`, `cross_references`, `synthesis_runs`) and the `fts_bookmarks` virtual table exist with declared columns and constraints.
2. `sanitize_url()` passes the ≥80 table-driven test cases in `tests/unit/test_sanitize_url.py` and `test_sanitize_tokens.py`.
3. DuckDB JSON round-trip smoke test (`tests/integration/test_duckdb_roundtrip.py`) passes on every JSON column type.
4. `muninn ingest <path/to/bookmarks.html>` reads a Netscape Bookmark HTML fixture and produces idempotent `bookmarks` rows. Re-running yields identical row count and identical `bookmark_id`s (verified by SHA256 of `SELECT * FROM bookmarks ORDER BY bookmark_id`).
5. Every `bookmarks.url` in the output went through `sanitize_url()` (verified by absence of any `DANGEROUS_PARAM_NAMES` substring in any URL).
6. `redacted_param_count`, `redacted_param_names`, `path_redacted` columns populated correctly for the `bookmarks_redaction.html` fixture (which contains tokens, magic links, webhook URLs).
7. `domain_policy.yml` parsed and applied: rows whose domain matches a policy entry have `content_visible=0`.

### Stream 2 — Scrape

**Owns (writes):** `scrape_results` table; updates `bookmarks.enrichment_source`.
**Reads:** `bookmarks`.
**Modules:** `src/muninn/scrape/`, `data/scrape-cache/`, `data/http-cache/`.
**Depends on:** Stream 1.

**Success criteria:**
1. `muninn scrape` runs the dual-pass (live + at_capture) for all `content_visible=1` bookmarks. Both passes complete or record explicit failure for every eligible bookmark.
2. Per-domain politeness verified by test against a fixture HTTP server: ≤1 rps to live origins, ≤0.5 rps to IA endpoints (verified by request timestamps).
3. `at_capture` window respects ±365 days of `bookmarks.captured_at`; rows outside get `scrape_status=no_archive`.
4. `recent_archive` fallback fires automatically and records its own `scrape_results` row when at_capture returns `no_archive`.
5. `bookmarks.enrichment_source` set per priority (`at_capture` → `recent_archive` → `live_fallback` → `none`) for every scraped bookmark.
6. Auth-wall detection: a fixture page containing common login-form markers produces `scrape_status=auth_required`.
7. Re-running scrape is idempotent — existing `scrape_results` rows updated in place per `(bookmark_id, pass)` upsert key; row count constant across re-runs.
8. HTTP cache (`data/http-cache/`) prevents re-fetch on consecutive runs (verified by request-count assertion in test).

### Stream 3 — Enrich

**Owns (writes):** `enriched` table; Qdrant collection.
**Reads:** `bookmarks`, `scrape_results`.
**Modules:** `src/muninn/enrich/`, `src/muninn/vector/`, `src/muninn/enrich/prompts/per_bookmark_v1.md`.
**Depends on:** Stream 2.

**Success criteria:**
1. `muninn enrich` calls Anthropic Haiku API with prompt caching enabled. Cache hit rate ≥80% on bulk pass after the first 100 calls (verified by API response cache-hit headers).
2. `enriched` rows produced for every bookmark where `content_visible=1` AND `enrichment_source != 'none'`.
3. Idempotency: re-running with the same `(enrichment_model, enrichment_prompt_version, content_hash)` triple performs zero API calls (verified by mock or recorded cassette).
4. `enrichment_model`, `enrichment_prompt_version`, `content_hash` populated on every row (NOT NULL constraints enforce this at the DB level).
5. Qdrant collection populated with embeddings keyed by `bookmark_id`; `count(qdrant_points) == count(enriched_rows)` after a full enrich pass (modulo eventual-consistency lag).
6. `scripts/reconcile-vector-index.py` correctly identifies and backfills any `enriched` row missing from Qdrant.

### Stream 4 — Synthesis

**Owns (writes):** `eras`, `analyses`, `cross_references`, `synthesis_runs`; updates `enriched` (deep-pass overwrites + `key_quotes` column).
**Reads:** `bookmarks`, `enriched`, `scrape_results`.
**Modules:** `src/muninn/synthesis/`, `containers/synthesis/`, `scripts/launch-synthesis.sh`, `schemas/json/`.
**Depends on:** Stream 3, plus the **Saga Phase 1+2 infrastructure** (credentials volume, keepalive cron) being present on the host.

**Success criteria:**
1. `containers/synthesis/Dockerfile` builds successfully; resulting image extends Trail of Bits devcontainer with the schemas and persona baked in.
2. Container starts with `saga-claude-credentials` volume mounted read-only and `/status` (run inside the container) reports `Login method: Claude Max Account` (subscription mode confirmed, NOT API mode).
3. `muninn synthesize era <era_label>` produces a JSON output that validates against `schemas/json/era-narrative.schema.json`. The `eras` row gets written with all required fields populated; `enrichment_model` records the resolved Opus version.
4. `muninn synthesize deep-pass <bookmark_id>` produces a JSON output validating against `schemas/json/deep-pass.schema.json`. Verbatim quote substring check: every entry in `key_quotes` is a verbatim substring of the canonical `content_text` (a quote that fails this check triggers single-retry self-correction).
5. `muninn synthesize analyze "<prompt>"` produces a JSON output validating against `schemas/json/ad-hoc-analysis.schema.json`; row inserted into `analyses` with `filter_query` preserved verbatim.
6. Single-retry self-correction loop: a deliberately-broken first output triggers a re-launch with `correction_instructions` populated; if the second attempt also invalid, status is `validation_failed` in `synthesis_runs` and no DB write happens.
7. `synthesis_runs` audit log populated for every container launch (one row per attempt). `status='cap_hit'` is detectable from a synthetic rate-limit-exhausted scenario.
8. `cross_references` rows from a deep-pass output insert with `created_by='deep_pass'`; `target_bookmark_id` validated against input materials before insert (an FK-violating output triggers correction).

### Stream 5 — Consumers

**Owns (writes):** none — read-only consumers (except the vault generator writes to its external Gitea repo, which is not a SQLite table).
**Reads:** all tables.
**Modules:** `src/muninn/consumers/`, `consumers/vault/templates/`.
**Depends on:** Streams 3 and 4 for the full feature surface. Some consumers (CLI status, triage) work after just Streams 1+2.

**Success criteria:**
1. `muninn` CLI registers as `[project.scripts]` entry in `pyproject.toml`; `muninn --help` works after `pip install -e .`.
2. `muninn triage` lists rows with `scrape_status` failures or `enrichment_source='none'`; output is human-readable and machine-parseable (JSON with `--json` flag).
3. `muninn deep-pass <id>` sets `deep_pass_requested=1` on the `enriched` row; the row is then picked up by `muninn synthesize deep-pass --pending`.
4. `muninn status` shows pipeline state: bookmark count, per-pass scrape counts, enriched count, recent `synthesis_runs` outcomes.
5. Vault generator produces one markdown page per `content_visible=1` bookmark in the configured `MUNINN_VAULT_DIR`; cross-references render as Obsidian-compatible `[[bookmark-slug]]` links bidirectionally (incoming references queried from `cross_references`).
6. MCP server exposes (at minimum) `semantic_search`, `fts_search`, `get_bookmark`, `get_era`, `list_eras` tools; queries against the live store return correct results matching SQLite/Qdrant ground truth.
7. Timeline builder emits JSON via DuckDB-attached-to-SQLite; per-era aggregations match `SELECT era_label, count(*) FROM bookmarks GROUP BY era_label` ground truth.
8. Parquet export (`muninn export parquet --out path.parquet`) produces a file readable by both DuckDB and pandas; JSON columns round-trip cleanly.

### Cross-stream contracts (the things that MUST stay stable)

- The schema in Section "Schema" — column names, types, constraints. Schema changes require spec amendment.
- The JSON Schemas in `schemas/json/` — synthesis container's I/O contract.
- `sanitize_url()` public signature and `SanitizationResult` dataclass shape.
- `domain_policy.yml` schema (single `content_visible: false` toggle per entry; glob patterns).
- `enrichment_prompt_version` naming convention (`<task>_v<N>` matching the file name in `prompts/`).

### Decomposition note for Odin

Streams 1–3 are fully sequential (Stream N depends on Stream N-1's success criteria). Streams 4 and 5 can begin in parallel once Stream 3 starts but cannot complete until Stream 3 is done. Total sequencing:

```
Stream 1 ──► Stream 2 ──► Stream 3 ──┬──► Stream 4
                                     └──► Stream 5
```

If Phase 3 wants to run more than 3 dvergar concurrently in early streams, the work doesn't naturally split further at v1 scale — the 5-stream decomposition is the natural granularity.

---

## Rejected alternatives

Consolidated record of design decisions taken AND the alternatives explicitly rejected, with reasons. Captures the rationale that would otherwise be lost. Per FEATURE-SPEC-DEVELOPMENT Tier 1, rejected paths matter as much as chosen ones — they constrain future revisits.

### Scope and ingest

- **Sonnet-everywhere for enrichment.** Rejected: ~5–10× Haiku's cost across the bulk pass; Haiku is competent for "extract structured tags + a 2-paragraph summary from cleaned text"; Opus deep-pass exists as the escape hatch for entries where the user wants better.
- **Opus-everywhere for enrichment.** Rejected: far too expensive at corpus scale; Opus reasoning is overkill for tag/summary extraction from clean text.
- **Multi-source ingest in v1** (browser history, Pocket, HN favorites, Arena, Evernote, Apple Notes, etc.). Rejected: too much surface area before the schema is proven. v1 commits to bookmarks-only; v2+ adapters slot into the same source-agnostic schema.

### Scraping

- **Playwright from day 1.** Rejected: 10–50× per-page cost, browser binary infra weight; readability covers article-shaped content (the bulk of a 10-year corpus); JS-heavy long tail (Twitter, modern SPAs) gets `scrape_status=js_required` for a v1.5 Playwright pass to target.
- **Live-only scrape pass.** Rejected: loses the historical content the user actually saw; Muninn is a memory system, not a current-state mirror.
- **Archive-only scrape pass.** Rejected: loses the "is this still live / has it changed" diff signal; live pass is also faster on fresh URLs IA may not yet have crawled.
- **Screenshot capture in v1.** Rejected: requires a browser, which is the Playwright tax we're avoiding.

### Sensitive data

- **Opt-in skiplist (allowlist) for sensitive domains.** Rejected: tedious for a 10k+ corpus where the user doesn't remember half the domains; opt-out is simpler; universal sanitization + auth-wall detection are the safety net.
- **Skip sensitive domains entirely from `normalized/`.** Rejected: loses event-level signal which is the entire product. The user's explicit reframing was sanitize-don't-exclude — visiting a banking site on date X is signal even if no content is stored.
- **Per-domain policy with 4 toggles** (`scrape`, `enrich`, `vault`, `mcp` independently). Rejected: unnecessarily granular. Single `content_visible` toggle gates all four together; nobody has a real use case for "scrape but don't enrich" or "enrich but hide from MCP."
- **Sanitization rules in YAML config.** Rejected: token-shape regex tuning by users would leak. Code-with-PR-review is the right shape for a leakage-critical surface.
- **Generic high-entropy token detection.** Rejected at v1: too many false positives (YouTube `v=`, Spotify track IDs, GitHub gist IDs). JWT-specific regex + named denylist covers realistic cases. Defer to v2 with allowlist built from observed real-corpus data.

### Storage and store choices

- **Multiple separate repos, one per component.** Rejected at v1: schema churn during early development requires single-commit changes, not cross-repo coordination dance with version pins. Components graduate to their own repos only after schema stabilizes.
- **Vault inside this repo.** Rejected: vault is data with its own git lifecycle; mixing it with the build pipeline conflates tool with data. Compiled vault lives in its own Gitea repo.
- **Compiled vault and personal vault as the same vault.** Rejected (hard rule): ingest loops, authorship ambiguity. Two vaults, sentinel file enforces.
- **DuckDB-vss replacing Qdrant for v1 vector store.** Rejected at v1 (revisable): Qdrant is more mature, slot already allocated on `.19`, MCP wants production-shaped backend. Reassessable at first MCP integration.
- **Append-mode `scrape_results` history.** Rejected at v1: adds complexity. `scrape_results` is upsert-by-`(bookmark_id, pass)`; if attempt history matters later, a separate `scrape_attempts` log table can be added non-breakingly.
- **Tombstone-on-removal for un-bookmark events.** Rejected at v1: hard delete is simpler. Tombstones become interesting at v2 when the digital-footprint vision (where event removal carries meaning) actually matters.
- **Alembic-style migration framework at v1.** Rejected: overkill when re-ingest is the recovery path and `raw/` is the source of truth.
- **SQL-trigger FTS5 sync.** Rejected: multi-table denormalization triggers are notoriously fiddly; application-layer sync in the same transaction is cleaner.

### LLM execution

- **`claude -p` for synthesis.** Rejected: silently forces API billing per SAGA_ARCHITECTURE.MD:141, defeating the whole point of subscription-mode synthesis. Only the mounted-credentials-volume + interactive-mode pattern preserves subscription billing.
- **`CLAUDE_CODE_OAUTH_TOKEN` for synthesis.** Rejected for the same reason — silently forces API mode.
- **Multi-instance synthesis (parallel dvergar) at v1.** Rejected: total synthesis volume is small (dozens of era narratives, sparse deep-passes). Single Opus-1M instance fits within Max-plan rate windows. Saga's parallel-dvergr pattern earns its keep at v2 scale.
- **Three-tier model split** (Haiku → Sonnet → Opus). Rejected in favor of two-tier (Haiku → Opus-1M) once 1M context made it unnecessary to split synthesis between Sonnet (eras) and Opus (deep-passes). Fewer model-version-drift surfaces; simpler tier boundary.
- **Silent fallback to API Opus on subscription cap-hit.** Rejected: surprise charges are worse than an explicit "resume after cooldown" message. Fail loudly; manual `--llm-mode=api-only` override exists for emergencies.

---

## Decision 1 — Scope of v1: **bookmarks-only**

**Resolved.** v1 ingests Netscape Bookmark HTML exports only. Ship the dataset against one well-understood input, nail the schema end-to-end, then add other sources as v2+ ingest adapters.

### Schema implications (binding on the v1 schema)

The `normalized/` table must support future ingest sources without migration. Per-row fields:

- `source` — enum/string identifying the ingest source (`bookmarks` for v1; `evernote`, `apple_notes`, `obsidian`, `youtube`, `spotify`, `soundcloud`, … later)
- `source_id` — stable identifier within the source (URL for bookmarks; note GUID for Evernote; track URI for Spotify; etc.)
- `captured_at` — when the user added/saved/interacted (bookmarks: `ADD_DATE`; notes: created_at; media: played_at/liked_at)
- `title` — display title
- `url` — optional; not all sources have one (notes don't)
- `folder_path[]` — optional; bookmarks have nested folders, notes have notebooks/folders, media often doesn't
- `era_label` — optional; v1 derives from bookmark folder names ("Jan 1", "Feb 8"); other sources may leave null or supply their own
- `domain` — optional; meaningful for URL-bearing sources
- `source_metadata` — JSON blob for source-specific fields that don't generalize (e.g., bookmark `ICON_URI`, Spotify track duration, YouTube channel ID)

The `enriched/` columns (`summary`, `tags`, `entities`, `content_type`, `language`, `word_count`, …) apply across sources but some will be null per-source (no `domain` for an Apple Note; no scrape for a Spotify track).

**Scrape results live in a child table, not on the bookmark row** — see Decision 3. A bookmark row has 0..N `scrape_results` rows, one per pass (live, at-capture-archive, future Playwright pass, etc.).

### Out of scope for v1

- Any non-bookmark ingest adapter (deferred to v2)
- Browser history (separate adapter, even though it's URL-shaped — different export format, different semantics)
- Cross-source deduplication (a YouTube URL bookmarked AND in YouTube history is fine to have twice in v1)

---

## Decision 2 — Delivery shape: **multi-language monorepo, vault stays out**

**Resolved.** Muninn is a single repo containing all components (parser, scraper, enricher, indexes, MCP server, CLI, compiled-vault generator). Multi-language is acceptable. Components graduate to their own repos only after the schema stabilizes and a component proves it has an independent lifecycle.

**Why monorepo at v1:** the SQLite schema will churn through early ingest. In-repo schema changes are one commit; cross-repo changes are a coordination dance with version pins. Not worth the overhead before the schema has been pressure-tested by a real run.

### Language choices

Defaults, revisable per-component if a concrete need surfaces:

- **Ingest, normalization, enrichment, vault generator, CLI:** **Python**. Bottleneck is HTTP wait + LLM API latency, not CPU. Python wins on rich HTML/readability parsing, mature LLM SDKs, stdlib `sqlite3`, fast iteration during schema churn. Use `uv` for dependency/lockfile management.
- **MCP server:** decide at MCP-build time. Python is the path of least resistance (shares schema layer with the rest); TypeScript is plausible if MCP tooling matures faster there.
- **Any future timeline view / UI:** TypeScript when the time comes.

### Vault topology — **two vaults, never one**

The "vault" has two roles that must be separate vaults to avoid loops and authorship confusion:

- **Compiled vault (Muninn output, Layer 1 of the README):** Bragi-style, one markdown page per `enriched/` row. Machine-generated and regeneratable. Lives in its own Gitea repo, separate from this repo. User browses it in Obsidian but treats it as derived data — hand-edits are a v2 question (round-tripping user-added tags/notes back into `enriched/` is non-trivial).
- **Personal vault (future Muninn input):** the user's own hand-authored Obsidian notes. Read by the `obsidian` ingest adapter when that adapter ships. Lives wherever the user keeps their personal vault — Muninn does not manage it.

**Hard constraint on the future `obsidian` ingest adapter:** it must refuse to ingest from the compiled vault. Enforce by sentinel file (e.g., `.muninn-compiled-vault` at the vault root) that the adapter checks for and aborts on.

This repo contains neither vault. The compiled-vault generator (code) lives here; the compiled vault (data) lives in its own Gitea repo. The personal vault lives wherever the user keeps it.

---

## Decision 3 — Scraper strategy: **HTTP-only v1, dual concurrent pass (live + at-capture archive)**

**Resolved.** v1 ships with HTTP-only scraping — no Playwright, no headless browser, no screenshots. Each bookmark is fetched in two concurrent passes:

- **Live pass:** GET the URL as it exists today.
- **At-capture pass:** ask Internet Archive's Wayback Machine (CDX API) for the snapshot nearest the bookmark's `add_date`, then fetch that snapshot.

Why dual-pass: Muninn is a memory system. The at-capture snapshot is "what the user actually saw when they bookmarked it" — semantically more faithful than the (possibly mutated, paywalled, dead, or domain-squatted) live version. The live version is still useful as a "has this changed / is it still here" signal.

Failures and partial extractions are first-class: every pass records its outcome, so a v1.5/v2 fallback pass (Playwright, screenshots, alternate archive snapshots, internet archive, etc.) can target the failed/partial subset cleanly without re-scraping the whole corpus.

### Library and extraction

- **HTTP client:** `httpx` (async, supports HTTP/2, on-disk cache via `hishel` or similar).
- **Extraction:** `readability-lxml` (Python port of arc90's algorithm) — battle-tested, no JS runtime needed. Produces clean main-content text + HTML.
- **Storage:** both raw response HTML *and* extracted readable text/HTML, so re-extraction with a different algorithm later doesn't require re-fetch.

### Politeness

- **Live origins:** per-domain ≤1 rps, parallel across domains. Honest `User-Agent` identifying Muninn + contact URL. Respect `robots.txt`. On-disk HTTP cache keyed by URL+date so re-runs don't re-fetch.
- **Internet Archive endpoints (CDX + Wayback):** stricter — global ≤0.5 rps across all IA traffic (not per-domain; it's all one shop). Exponential backoff on 429s. On-disk cache of CDX responses so we never re-query the same URL's snapshot list. `User-Agent` identifies Muninn + contact, per IA's stated norms.

### `scrape_results` child table (replaces per-row scrape columns)

| column | type | notes |
|---|---|---|
| `bookmark_id` | FK → bookmark row | |
| `pass` | enum | `live`, `at_capture`, future: `playwright`, `recent_archive`, … |
| `fetched_at` | timestamp | when Muninn ran this pass |
| `target_timestamp` | timestamp / null | for archive passes: the date we asked IA to find a snapshot closest to (= bookmark `add_date`). null for `live`. |
| `actual_snapshot_at` | timestamp / null | for archive passes: what IA actually returned (may be days/weeks off `target_timestamp`) |
| `archive_url` | string / null | IA URL of the snapshot used, when applicable |
| `final_url` | string | post-redirect URL actually fetched |
| `http_status` | int / null | |
| `scrape_status` | enum | `ok`, `partial`, `failed`, `js_required`, `paywall`, `robots_disallowed`, `no_archive`, `network_error`, `timeout` |
| `extraction_quality` | enum | `ok`, `partial`, `failed` — readability's confidence / our judgement |
| `content_text` | text / null | extracted main-content text |
| `content_html` | text / null | extracted main-content HTML |
| `raw_html_path` | string / null | path to raw response on disk (gzipped); kept so re-extraction doesn't require re-fetch |
| `error_detail` | text / null | exception class + message for failures |

The pair `(scrape_status, extraction_quality)` distinguishes "fetched but garbage" (`ok`/`failed`) from "couldn't fetch" (`failed`/null) from "got it, parsed cleanly" (`ok`/`ok`). The user's three-way bucket — clean / partial / failed — falls out of these.

### Which pass is canonical for downstream enrichment?

**At-capture content is canonical** for summary, embedding, tags, and entities — it's what the user actually saw. Live content is retained as a diff signal: a future "has this changed materially since you bookmarked it" view, and as a fallback when the at-capture pass returns `no_archive`. If both passes fail, the bookmark stays in `normalized/` but has no `enriched/` content; the LLM enrichment step skips it.

### Out of scope for v1 (deferred to v1.5/v2)

- Playwright / headless browser fallback for `js_required` rows
- Screenshot capture
- Other archives beyond Internet Archive (e.g., archive.today) when IA fails
- Re-scraping on a schedule (link-rot watch)
- YouTube transcript fetch, paper PDF extraction, other content-type-specific extractors

---

## Decision 4 — Dead-link / archive-fallback policy

**Resolved.** The dual-pass model from Decision 3 already routes every bookmark through Wayback by default, so this decision narrows to "what to do when the at-capture pass itself fails."

### Policy

1. **At-capture tolerance window:** ±365 days from `add_date`. If CDX returns no snapshot within that window, the at-capture pass records `scrape_status = no_archive`.
2. **`recent_archive` fallback (on by default):** when at-capture is `no_archive`, automatically attempt a third pass that asks IA for the most recent snapshot of the URL regardless of date. Recorded as a separate row in `scrape_results` with `pass = recent_archive` and `target_timestamp = null`. Same politeness rules as at-capture.
3. **`enrichment_source` selection on the bookmark row:** chosen by this priority order, recorded as a column on the bookmark row:
   1. `at_capture` — at-capture pass succeeded
   2. `recent_archive` — at-capture failed, recent_archive pass succeeded
   3. `live_fallback` — both archive passes failed, live pass succeeded; live content is used as canonical for summary/embedding/tags, but the flag makes the divergence-from-historical-content explicit to all downstream consumers
   4. `none` — all passes failed; bookmark stays in `normalized/` but enrichment is skipped and the row is surfaced in the "needs triage" CLI report
4. **No further auto-retries.** Bookmarks with `enrichment_source = none` are not auto-retried on subsequent runs (would burn IA quota for low yield); the triage CLI lets the user mark a row for manual content paste or for re-attempt with v1.5 tools (Playwright, alternate archives) when those land.

### Downstream consequences

- Timeline view, MCP, and vault generator all read `enrichment_source` and can surface the provenance — e.g., the compiled vault page for a `live_fallback` bookmark notes "content reflects live page, not the version captured on `add_date`."
- The `actual_snapshot_at` from the chosen `scrape_results` row is exposed too — when a `recent_archive` snapshot is years off `add_date`, that's user-visible.

---

## Decision 5 — Sensitive data: sanitize, don't exclude

**Resolved.** Muninn is a digital-footprint dataset; event-level correlation is the product. Excluding bookmarks loses the signal we want. Almost no domain warrants full exclusion. The concern is (a) not storing secrets and (b) not wasting effort scraping content from auth-walled sites — but the *event* (visited domain X on date Y) always survives in `normalized/`.

Three orthogonal mechanisms, no overlap:

### 1. Universal URL sanitization (applies to every row at ingest)

Implemented in code (Python module), not config. User-tunable regexes for token shapes get this wrong and leak secrets; a code module with each rule documented and rationale-commented is the right shape.

Strips at ingest:

- **Known dangerous query params** (denylist): `access_token`, `api_key`, `apikey`, `session`, `sessionid`, `code`, `state`, `pwd`, `password`, `signature`, `X-Amz-Signature`, `X-Amz-Credential`, `X-Amz-Security-Token`, `X-Goog-Signature`, `auth`, `bearer`, `jwt`, `token`, `secret`, `client_secret`, plus JWT-shape detection (`eyJ…\.eyJ…\..+`) on any param value.
- **Path-as-credential domains** (per-domain pattern recognition): magic login links and webhook URLs are URLs *whose path is the secret*. For known patterns (Slack incoming webhooks: `hooks.slack.com/services/T…/B…/…`; Discord webhooks: `discord.com/api/webhooks/…/…`; common magic-link patterns) reduce the path to a sentinel like `/[redacted-token]`.
- **Tracking params** (cosmetic, not security — but might as well): `utm_*`, `fbclid`, `gclid`, `mc_eid`, etc. Removed silently.

Per-row metadata recorded:

- `redacted_param_count` — how many params were stripped
- `redacted_param_names` — array of names stripped (the names themselves are not sensitive, only the values were)
- `path_redacted` — boolean

**Original URL is never stored anywhere on disk after ingest.** Only the sanitized form lands in `normalized/`. The single place the unsanitized URL exists is `raw/bookmarks.html`, which is gitignored locally and lives only in the user's private store — never in any export, vault, or MCP response.

### 2. Per-domain scrape policy (`domain_policy.yml`)

```yaml
# domain_policy.yml — committed to the repo, populated by the user
# Defaults to scrape-everything. Entries here opt domains OUT of content fetch
# while preserving the event in normalized/.
content_visible: false   # global default override; rarely set
domains:
  - chase.com
  - "*.kaiserpermanente.org"
  - acme-corp.atlassian.net
paths:
  - "github.com/acme-corp/*"
```

A single toggle per entry: `content_visible: false` (implicit when domain is listed). When false, Muninn:

- Skips both scrape passes (live + at-capture) — saves IA quota and live-fetch time
- Skips enrichment (no summary, no embedding)
- Skips vault page generation
- Hides the row from MCP responses

The bookmark stays in `normalized/` with sanitized URL, title, folder, era, `add_date`, and domain — all the event-level signal. Glob patterns (not regex) for `domains` and `paths`.

Ships empty in v1, with comprehensive commented-out examples (common banking, medical, work SaaS, intranet patterns) so users see what kinds of entries are typical without anything being assumed about them.

### 3. Auth-wall detection (automatic, no config)

The scraper recognizes login walls and authentication-required responses heuristically (HTTP status patterns, presence of common login form markers, content-length thresholds, keyword detection in title/body). Records `scrape_status = auth_required` and skips enrichment for that pass. Catches sensitive-content sites the user forgot to add to `domain_policy.yml` — a bookmark to a Notion private page or a personal Google Doc returns a sign-in wall, gets flagged automatically, and never lands in the vault or vector index as "content."

---

## Decision 6 — LLM tiering: Haiku (API) for bulk, Opus 4.6 + 1M (Saga-style subscription container) for synthesis

**Resolved.** Two-tier model, two execution paths, sharing nothing operationally.

### Tier 1 — Bulk per-bookmark enrichment: Haiku 4.5 via Anthropic API

Per-bookmark work is "extract structured metadata + summary from cleaned readable text" — high volume, low judgement, perfect for Haiku.

- Direct Anthropic API calls (`ANTHROPIC_API_KEY`), Python async worker pool.
- **Prompt caching enabled** — system prompt + extraction schema is identical across thousands of calls, classic cache-hit shape.
- **Idempotency via `enrichment_prompt_version` + `content_hash`** on every enrichment row. Re-runs only re-process rows where either changed; a no-change run is a no-op. This is the single most important cost-control mechanism — it makes prompt iteration cheap.
- **`enrichment_model` column** records the resolved model ID (`claude-haiku-4-5-20251001`) for reproducibility and version-drift visibility.

Outputs per bookmark: `summary`, `tags[]`, `entities[]`, `content_type`, `language`, `word_count`. Written into `enriched/`.

### Tier 2 — Synthesis: Opus 4.6 + 1M context via subscription-mode container

Per-era narratives, flagged deep-passes, and ad-hoc cross-corpus analysis. Lower volume, much higher synthesis demand, and benefits from very large context (an era + its neighbors as context, or a topic-cluster spanning years).

#### Auth and execution mechanics (grounded in Saga's pattern)

- **Subscription billing requires interactive mode, not `claude -p`.** Per SAGA_ARCHITECTURE.MD:141, `CLAUDE_CODE_OAUTH_TOKEN` silently forces API billing — so `-p` mode and any env-var-based auth are off the table for subscription-billed work. The only subscription-mode path is Saga's: a container with the `saga-claude-credentials` Docker named volume mounted at `/home/vscode/.claude`, running `claude --dangerously-skip-permissions` in a tmux session.
- **Muninn's synthesis container is a sibling of dvergr**, not a consumer of Odin/heimdall:
  - Base image: Trail of Bits devcontainer (same as dvergr), with iptables extended to whitelist `api.anthropic.com` (already in base) and `web.archive.org` / `archive.org` (Muninn-specific, only needed if synthesis pass re-fetches archived content).
  - Mount: `saga-claude-credentials:/home/vscode/.claude:ro` (read-only — synthesis containers don't need to mutate auth state).
  - Workspace: `/opt/muninn/synthesis-ws/<task-id>/` containing input data (era summaries, neighboring-era context, instructions) and an output directory the agent writes JSON results into.
  - Persona: `CLAUDE.md` baked into the image defines the synthesis behavior (output schema, "write JSON to `/workspace/output/<task-id>.json` then signal completion," etc.).
  - Lifecycle: launched on demand by Muninn's Python orchestrator; runs to completion via ralph-loop completion-promise; container exits with `--rm`.
- **Keepalive piggybacks on Saga's existing 4-hour keepalive cron.** No Muninn-specific keepalive needed — same credentials volume, same refresh-token surface, same fragility, same fix.
- **Single instance at v1 scale.** 20–50 eras × ~2 minutes per Opus invocation = ~1.7 hours sequentially per full era-narrative regeneration. Comfortably fits within Max-plan rate windows. Multi-instance synthesis batching becomes a v2 concern when corpus size or simultaneous task volume actually requires it.

#### Synthesis tasks at v1

- **Era narrative:** input is this era's Haiku summaries + immediately neighboring eras' summaries + folder-structure context (~80–200k tokens, well under 1M). Output: one paragraph narrative + extracted dominant topics, dominant domains, inferred year range. Written to the `eras/` table.
- **Flagged deep-pass:** user marks a bookmark via CLI; orchestrator launches a synthesis container with that bookmark's full scraped content + same-era cluster as context. Output: an Opus-quality summary that overwrites the Haiku one and sets `enrichment_model = claude-opus-4-6` on that row. Often interactive — user can attach to the tmux session and iterate on the narrative.
- **Ad-hoc analysis:** user-initiated CLI command ("how did my Rust interest evolve"). Runs against a topic-filtered subset of summaries. Output: ad-hoc narrative written to a queryable analysis-results table.

#### Reliability and failure modes

- **Subscription cap hit mid-batch:** orchestrator detects the cap-exhausted message in the container's tmux output, logs which task was pending, exits the container cleanly, and surfaces "rate limit hit; resume with `muninn synthesis resume` after cooldown." Failing loudly is correct here — silent fallback to API Opus would land surprise charges on the user.
- **Subscription unavailable** (no credentials volume, container fails to start, network down): orchestrator fails loudly with a clear message. Override path is `--llm-mode=api-only` which bills API for the synthesis pass too. Never silent fallback.
- **Model version drift on subscription side:** `enrichment_model` column records the resolved Opus version (e.g., `claude-opus-4-6`). When subscription's Opus rev's, version-drift is visible per-row. Era narratives can be flagged for regeneration when their `enrichment_model` is older than the current subscription version.

### What this means for the Saga dependency

Muninn depends on Saga **infrastructure (Phase 1+2, already done as of 2026-04-13)** — credentials volume, keepalive cron, Trail of Bits base image, the dvergr container-launch pattern. Muninn does **not** depend on Saga **orchestration (Phase 3, not yet shipped)** — Odin's spec-decomposition and multi-dvergr launch machinery aren't needed at v1's single-instance synthesis volume.

This is a meaningful unblock from the original `README.md` framing: v1 Muninn can be built immediately, on top of the Saga infrastructure that already exists, without waiting for Phase 3.

---

## Schema — concrete `CREATE TABLE` statements

Translates the prose schema in earlier sections into executable SQL. Lives in `schema.sql` at the repo root and is applied at DB init.

### Conventions

- **Primary keys** are synthetic `INTEGER PRIMARY KEY AUTOINCREMENT` where natural keys are composite or unstable. Composite natural keys are enforced via `UNIQUE` constraints. This keeps FK relationships simple (referencing tables hold a single integer).
- **Timestamps** are `INTEGER` Unix epoch seconds. SQLite has no native datetime; epoch integers are sortable, JOIN-able, and timezone-safe.
- **Arrays** (folder paths, tags, entities, dominant topics) are stored as JSON arrays in `TEXT` columns with `CHECK (column IS NULL OR json_valid(column))`. SQLite's `json_each()` table-valued function handles unnest queries.
- **Booleans** are `INTEGER` 0/1 with `CHECK (col IN (0, 1))` — SQLite doesn't have a BOOL type.
- **Enums** are `TEXT` with `CHECK (col IN (...))`. Adding a value later is an `ALTER TABLE ... DROP CHECK` + re-add — annoying but rare.

### `bookmarks` — the source-agnostic normalized row

```sql
CREATE TABLE bookmarks (
    bookmark_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT    NOT NULL,
    source_id               TEXT    NOT NULL,
    captured_at             INTEGER NOT NULL,
    title                   TEXT,
    url                     TEXT,                                       -- sanitized form only; raw URL never stored
    folder_path             TEXT    CHECK (folder_path IS NULL OR json_valid(folder_path)),
    era_label               TEXT,
    domain                  TEXT,
    source_metadata         TEXT    CHECK (source_metadata IS NULL OR json_valid(source_metadata)),
    redacted_param_count    INTEGER NOT NULL DEFAULT 0,
    redacted_param_names    TEXT    CHECK (redacted_param_names IS NULL OR json_valid(redacted_param_names)),
    path_redacted           INTEGER NOT NULL DEFAULT 0 CHECK (path_redacted IN (0, 1)),
    content_visible         INTEGER NOT NULL DEFAULT 1 CHECK (content_visible IN (0, 1)),
    enrichment_source       TEXT,
    ingested_at             INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (source, source_id),
    CHECK (enrichment_source IS NULL OR enrichment_source IN
           ('at_capture', 'recent_archive', 'live_fallback', 'none'))
);

CREATE INDEX idx_bookmarks_captured_at ON bookmarks (captured_at);
CREATE INDEX idx_bookmarks_domain      ON bookmarks (domain);
CREATE INDEX idx_bookmarks_era_label   ON bookmarks (era_label);
CREATE INDEX idx_bookmarks_source      ON bookmarks (source);
```

Notes:
- `(source, source_id)` is the upsert key. Re-running ingest is idempotent — same `bookmark_id` survives.
- `url`, `folder_path`, `era_label`, `domain` are nullable because future sources (Apple Notes, Spotify) won't all have them.
- `enrichment_source` is set by the scraper's outcome-routing logic (Decision 4); null means enrichment hasn't run yet.
- `content_visible = 0` means `domain_policy.yml` opted this row out of scrape/enrich/vault/MCP. Event survives in this table; nothing downstream sees it.

### `scrape_results` — child of bookmarks, one row per pass

```sql
CREATE TABLE scrape_results (
    scrape_result_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bookmark_id         INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    pass                TEXT    NOT NULL,
    fetched_at          INTEGER NOT NULL,
    target_timestamp    INTEGER,                                        -- for archive passes: date asked of IA
    actual_snapshot_at  INTEGER,                                        -- for archive passes: date IA returned
    archive_url         TEXT,
    final_url           TEXT,                                           -- post-redirect URL actually fetched
    http_status         INTEGER,
    scrape_status       TEXT    NOT NULL,
    extraction_quality  TEXT,
    content_text        TEXT,
    content_html        TEXT,
    raw_html_path       TEXT,                                           -- gzipped raw response on disk
    error_detail        TEXT,
    UNIQUE (bookmark_id, pass),
    CHECK (pass IN ('live', 'at_capture', 'recent_archive', 'playwright', 'manual')),
    CHECK (scrape_status IN
           ('ok', 'partial', 'failed', 'js_required', 'paywall',
            'robots_disallowed', 'no_archive', 'network_error',
            'timeout', 'auth_required')),
    CHECK (extraction_quality IS NULL OR extraction_quality IN ('ok', 'partial', 'failed'))
);

CREATE INDEX idx_scrape_results_bookmark ON scrape_results (bookmark_id);
CREATE INDEX idx_scrape_results_status   ON scrape_results (scrape_status);
```

Notes:
- **Upsert by `(bookmark_id, pass)`** — one row per pass per bookmark. Re-running a pass mutates the existing row in place. If we later want full attempt history, that becomes a separate `scrape_attempts` log table — non-breaking addition.
- `pass = 'manual'` reserved for content the user pastes in via the triage CLI when all auto passes fail.
- `pass = 'playwright'` reserved for v1.5 — column shape already supports it, no migration needed when that pass ships.
- `ON DELETE CASCADE` on the FK so a removed bookmark cleans up its scrape rows.

### `enriched` — LLM-generated metadata, 1:1 with bookmarks

```sql
CREATE TABLE enriched (
    bookmark_id                 INTEGER PRIMARY KEY REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    summary                     TEXT,
    tags                        TEXT    CHECK (tags IS NULL OR json_valid(tags)),
    entities                    TEXT    CHECK (entities IS NULL OR json_valid(entities)),
    content_type                TEXT,
    language                    TEXT,
    word_count                  INTEGER,
    enrichment_model            TEXT    NOT NULL,                       -- e.g., claude-haiku-4-5-20251001
    enrichment_prompt_version   TEXT    NOT NULL,                       -- e.g., v3 or a git short-sha
    content_hash                TEXT    NOT NULL,                       -- sha256 of the canonical content_text used
    enriched_at                 INTEGER NOT NULL,
    deep_pass_requested         INTEGER NOT NULL DEFAULT 0 CHECK (deep_pass_requested IN (0, 1))
);

CREATE INDEX idx_enriched_content_type ON enriched (content_type);
CREATE INDEX idx_enriched_deep_pass    ON enriched (deep_pass_requested) WHERE deep_pass_requested = 1;
```

Notes:
- `(enrichment_model, enrichment_prompt_version, content_hash)` is the idempotency triple. The enricher computes the would-be triple before calling the LLM; if it matches the existing row, skip. Drop-prompt-version-or-rescrape triggers re-enrichment naturally.
- Partial index on `deep_pass_requested = 1` makes "find rows needing Opus re-pass" trivially fast — typical query is `SELECT bookmark_id FROM enriched WHERE deep_pass_requested = 1`.
- No `content_text` here — JOIN to `scrape_results` on the canonical pass (selected by `bookmarks.enrichment_source`) when needed. Avoids denormalization.

### `eras` — derived per-era synthesis

```sql
CREATE TABLE eras (
    era_label                   TEXT    PRIMARY KEY,
    inferred_year               INTEGER,
    start_date                  INTEGER,
    end_date                    INTEGER,
    bookmark_count              INTEGER NOT NULL,
    dominant_topics             TEXT    CHECK (dominant_topics IS NULL OR json_valid(dominant_topics)),
    dominant_domains            TEXT    CHECK (dominant_domains IS NULL OR json_valid(dominant_domains)),
    narrative                   TEXT,
    enrichment_model            TEXT,                                   -- e.g., claude-opus-4-6
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER
);
```

Notes:
- No FK to `bookmarks` — `era_label` is the join key, and a bookmark's `era_label` can change if the user re-classifies a folder. Don't bind a derived table to a mutable label.
- `start_date` / `end_date` derived from `MIN(captured_at)` / `MAX(captured_at)` over the era's bookmarks at synthesis time.
- `narrative`, `dominant_topics`, `dominant_domains` come from the Opus synthesis container output.

### `cross_references` — bookmark-to-bookmark relationships

```sql
CREATE TABLE cross_references (
    cross_reference_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    target_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    relationship        TEXT,                                           -- e.g., "expands on", "contradicts", "same author"
    rationale           TEXT,                                           -- short justification from the model that produced it
    created_by          TEXT    NOT NULL,                               -- 'deep_pass' | 'manual' | future: 'auto_clustering'
    created_at          INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (source_bookmark_id, target_bookmark_id, created_by)
);

CREATE INDEX idx_cross_references_source ON cross_references (source_bookmark_id);
CREATE INDEX idx_cross_references_target ON cross_references (target_bookmark_id);
```

Notes:
- Populated by the deep-pass synthesis output (the `cross_references` field — see Synthesis I/O Schemas section). Manual entry via the CLI is supported. Future: an auto-clustering pass could populate `created_by = 'auto_clustering'` rows in batch.
- The `## Cross-references` section in the compiled vault page template (per `README.md`) is rendered from this table, joining both directions (`source = X` and `target = X`).
- Asymmetric by design — "X expands on Y" doesn't imply "Y is expanded by X" as a stored row; consumers query both directions when displaying.

### `analyses` — ad-hoc cross-corpus synthesis results

```sql
CREATE TABLE analyses (
    analysis_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title                       TEXT    NOT NULL,                       -- user-supplied prompt label
    prompt                      TEXT    NOT NULL,                       -- the actual user prompt
    filter_query                TEXT,                                   -- JSON spec of which bookmarks were in scope
    narrative                   TEXT,
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER NOT NULL DEFAULT (unixepoch())
);
```

Notes:
- Holds the output of "how did my Rust interest evolve"-style queries. Append-only; each query produces a new row.
- `filter_query` is a JSON spec the orchestrator built (e.g., `{"tag": "rust", "captured_at_range": [...]}`); kept so the user can re-run the same analysis later against fresh data.

### `fts_bookmarks` — full-text search index

```sql
CREATE VIRTUAL TABLE fts_bookmarks USING fts5 (
    title,
    summary,
    content_text,
    tags,
    content=''                                                          -- contentless: don't duplicate text
);
```

Notes:
- Contentless FTS5 table — stores tokens only, not the original text. Saves storage; queries return `bookmark_id` (the rowid), then JOIN back for display.
- **Sync at the application layer**, not via SQL triggers. Python's enricher and scraper write to `enriched` / `scrape_results` AND `fts_bookmarks` in the same transaction. Triggers for FTS5 sync are notoriously fiddly across multi-table denormalization (the FTS row needs a JOIN of three tables) and don't earn their keep.

### Vector index — out of SQLite

The Qdrant collection lives on the homelab LXC at `.19`. SQLite holds the canonical text + metadata; Qdrant holds embeddings keyed by `bookmark_id`. The enricher writes to both: enriched row to SQLite, embedding to Qdrant, in that order. If Qdrant is unavailable, SQLite write still happens; a re-run reconciles missing embeddings (`SELECT bookmark_id FROM enriched WHERE bookmark_id NOT IN (qdrant.point_ids)`).

**Vector store choice is reassessable, not locked.** Qdrant is the v1 default given (a) the homelab `.19` slot is already allocated, (b) it has a production-shaped track record, and (c) MCP queries want a stable backend. DuckDB's `vss` extension (HNSW, in-process, no separate service) is a viable simpler alternative if "fewer moving parts" outweighs maturity at re-evaluation time. Decision deferred to first real MCP integration.

---

## Compute layer — SQLite (canonical) + DuckDB (analytics)

Single SQLite file is the source of truth. DuckDB attaches to it read-only as a smarter query engine for analytical workloads. No data duplication, no ETL, no sync problem.

```sql
-- DuckDB session for analytics
ATTACH 'muninn.db' (TYPE sqlite);
SELECT era_label,
       count(*)                                  AS bookmarks,
       array_agg(DISTINCT domain ORDER BY domain) AS domains
FROM   sqlite_db.bookmarks
GROUP  BY era_label;
```

### Role split

| Component | Engine | Why |
|---|---|---|
| Ingest, scrape_results writes, enricher writes | **SQLite** (write) | Transactional, single-writer, simple |
| MCP server point lookups (semantic + FTS hits → row hydration) | **SQLite** (read) | Low-latency, indexed, well-shaped for `WHERE bookmark_id IN (...)` |
| Vault generator reads | **SQLite** (read) | Per-bookmark page rendering — single-row reads |
| CLI ad-hoc queries (simple) | **SQLite** (read) | Defaults; promote to DuckDB only when query shape needs it |
| Timeline view | **DuckDB** (read over attached SQLite) | Time-bucketed aggregations, window functions over `captured_at`, era boundaries |
| Cross-era analysis populating `analyses` | **DuckDB** (read over attached SQLite) | Group-by + array aggregation across era × tag × domain matrices |
| Parquet export consumer | **DuckDB** | `COPY (SELECT ... FROM sqlite_db.bookmarks) TO 'export.parquet' (FORMAT PARQUET)` is one line; native Parquet support is the whole reason DuckDB is in the stack |

### Failure containment

- DuckDB layer breaks → canonical store unaffected; only analytical consumers degrade.
- SQLite breaks → bigger problems; pipeline halts. WAL mode + the standard backup story (the SQLite file in a backed-up directory) is the recovery path.

### v1 implementation gate — JSON round-trip smoke test

Our `TEXT CHECK json_valid(...)` columns (`folder_path`, `tags`, `entities`, `dominant_topics`, `dominant_domains`, `source_metadata`, `redacted_param_names`, `filter_query`) need to read cleanly through DuckDB's SQLite extension. They should — DuckDB's `json_extract` and friends operate on TEXT-stored JSON — but this is the kind of thing that breaks on a specific version mismatch and gets discovered late. Make a test fixture with non-trivial JSON in each column type, write it through SQLite, read it back through DuckDB, assert structure equality. Block v1 on this passing.

---

## Synthesis container — I/O schemas

The Opus synthesis container (Decision 6, Tier 2) reads task specs from a workspace input directory, writes results to an output directory, and exits. The orchestrator validates outputs against committed JSON Schema files before writing to the DB. Schema files are the contract; the container's CLAUDE.md persona references them by path.

### Workspace layout

```
/workspace/
├── CLAUDE.md                                   # baked into image; defines persona, references schemas
├── input/
│   └── <task-id>.json                          # task type, params, materials — written by orchestrator
├── output/
│   └── <task-id>.json                          # synthesis result — written by container
├── status/
│   └── <task-id>.status.json                   # optional progress signals for long-running observability
└── schemas/                                    # JSON Schema files, baked into image
    ├── task-input.schema.json
    ├── era-narrative.schema.json
    ├── deep-pass.schema.json
    └── ad-hoc-analysis.schema.json
```

JSON Schemas live in the repo at `synthesis/schemas/` and are baked into the synthesis container image at build time. The orchestrator uses the same files to validate outputs before DB writes — single source of truth.

### Task input — common envelope

All task types share an envelope; `task_type` discriminates and `parameters` + `materials` are task-specific.

```json
{
  "task_id": "era-narrative-2026-04-15-jan-1",
  "task_type": "era_narrative",
  "prompt_version": "era-narrative-v1",
  "output_path": "/workspace/output/era-narrative-2026-04-15-jan-1.json",
  "schema_path": "/workspace/schemas/era-narrative.schema.json",
  "correction_instructions": null,
  "parameters": { /* task-specific */ },
  "materials":  { /* task-specific */ }
}
```

`correction_instructions` is null on the first attempt. On a retry after schema-validation failure, the orchestrator populates it with the validation errors so the model can correct itself.

### Task type 1 — `era_narrative`

**Input parameters + materials:**
```json
{
  "parameters": {
    "era_label": "Jan 1",
    "neighboring_eras": ["Dec 4", "Feb 8"]
  },
  "materials": {
    "era_bookmarks": [
      {
        "bookmark_id": 4421,
        "title": "Raft Refloated",
        "summary": "...",
        "tags": ["distributed-systems", "consensus"],
        "captured_at": 1514764800,
        "domain": "github.com"
      }
    ],
    "neighboring_era_summaries": {
      "Dec 4": [{"bookmark_id": 4319, "title": "...", "summary": "..."}],
      "Feb 8": [{"bookmark_id": 4534, "title": "...", "summary": "..."}]
    },
    "folder_structure": "Jan 1/\n  ├─ rust/\n  └─ papers/\n     ├─ raft/\n     └─ paxos/"
  }
}
```

**Required output:**
```json
{
  "narrative": "Through January, the focus shifted from broad-strokes Rust exploration into a deep dive on consensus algorithms — Raft especially. The neighboring 'Dec 4' era shows the lead-in (general systems reading); 'Feb 8' shows the follow-through into formal verification work.",
  "inferred_year": 2018,
  "dominant_topics": ["distributed-systems", "rust", "consensus", "raft"],
  "dominant_domains": ["github.com", "lobste.rs", "morningpaper.com"],
  "synthesis_metadata": {
    "model": "claude-opus-4-6",
    "prompt_version": "era-narrative-v1",
    "input_token_count": 84231,
    "output_token_count": 1047,
    "neighboring_eras_used": ["Dec 4", "Feb 8"]
  }
}
```

**Orchestrator stamps these deterministically** (the model doesn't compute them): `start_date`, `end_date`, `bookmark_count`. They're known from the materials the orchestrator built.

**Schema constraints worth noting:**
- `narrative`: minLength 50, maxLength 4000 chars (one-paragraph-ish).
- `dominant_topics`, `dominant_domains`: arrays, minItems 1, maxItems 10. The model picks the actually-dominant ones; the orchestrator doesn't post-rank.
- `inferred_year`: integer in [1990, current_year+1]. Sanity bound.

### Task type 2 — `deep_pass`

User flagged a bookmark via CLI (`muninn deep-pass <bookmark_id>`); orchestrator launches a synthesis container with that bookmark's full content + same-era cluster as context. Output overwrites the existing `enriched` row and adds rows to `cross_references`.

**Input parameters + materials:**
```json
{
  "parameters": {
    "bookmark_id": 4421
  },
  "materials": {
    "bookmark": {
      "bookmark_id": 4421,
      "title": "...",
      "url": "...",
      "captured_at": 1514764800,
      "era_label": "Jan 1",
      "content_text": "<full canonical scraped content>",
      "current_haiku_summary": "<the existing summary, for the model to improve on>",
      "current_haiku_tags": ["..."]
    },
    "same_era_cluster": [
      {"bookmark_id": 4319, "title": "...", "summary": "...", "tags": ["..."]}
    ]
  }
}
```

**Required output:**
```json
{
  "summary": "<deeper, possibly-longer Opus summary>",
  "tags": ["tag1", "tag2"],
  "entities": [
    {"name": "Diego Ongaro", "type": "person"},
    {"name": "Stanford", "type": "organization"},
    {"name": "Raft", "type": "concept"}
  ],
  "content_type": "paper",
  "language": "en",
  "word_count": 12431,
  "key_quotes": [
    "<verbatim quote 1>",
    "<verbatim quote 2>"
  ],
  "cross_references": [
    {
      "target_bookmark_id": 4319,
      "relationship": "expands on",
      "rationale": "covers the same Paxos→Raft trajectory but from the implementer's perspective"
    }
  ],
  "synthesis_metadata": {
    "model": "claude-opus-4-6",
    "prompt_version": "deep-pass-v1",
    "input_token_count": 12431,
    "output_token_count": 612
  }
}
```

**Schema constraints worth noting:**
- `summary`: minLength 100, no upper bound (Opus deep-passes can be substantially longer than Haiku's).
- `entities[].type`: enum (`person`, `organization`, `place`, `concept`, `tool`, `paper`, `event`).
- `content_type`: enum (`article`, `paper`, `video`, `tool`, `doc`, `talk`, `thread`, `other`).
- `key_quotes`: array, maxItems 5, each item maxLength 500 chars. Verbatim from `content_text` — orchestrator does a substring check post-validation; quotes that don't appear verbatim trigger correction.
- `cross_references[].target_bookmark_id`: must exist in `same_era_cluster`. Orchestrator validates the FK before insert.

**Orchestrator side effects:**
- Overwrites the `enriched` row for this `bookmark_id`. Updates `enrichment_model = claude-opus-4-6`, `enrichment_prompt_version = deep-pass-v1`, sets `deep_pass_requested = 0` (clearing the flag).
- Inserts `cross_references` rows with `created_by = 'deep_pass'`. UNIQUE constraint on `(source, target, created_by)` makes re-runs idempotent.
- `key_quotes` written to a new `key_quotes` TEXT JSON column on `enriched` — null for Haiku-produced rows, populated for deep-pass rows. **Schema addition** noted below.

### Task type 3 — `ad_hoc_analysis`

User-initiated CLI command (`muninn analyze "how did my Rust interest evolve"`); orchestrator filters bookmarks by topic, gathers their summaries, hands them to a synthesis container.

**Input parameters + materials:**
```json
{
  "parameters": {
    "title": "Rust interest evolution",
    "user_prompt": "Looking across my bookmarks, how has my interest in Rust evolved over time? What were the entry points, the deep-dive periods, the plateaus?",
    "filter_query": {
      "tags_any": ["rust"],
      "captured_at_range": [1514764800, 1735689600]
    }
  },
  "materials": {
    "bookmarks": [
      {"bookmark_id": 4421, "captured_at": 1514764800, "title": "...", "summary": "...", "tags": ["..."], "era_label": "Jan 1"}
    ]
  }
}
```

**Required output:**
```json
{
  "narrative": "<extended analysis, possibly multiple paragraphs>",
  "key_findings": [
    "Entry point was systems-programming curiosity in 2018 (era 'Jan 1'), driven by reading about consensus protocols.",
    "Deep-dive period in 2020-2021 — concentrated in the 'May 12' and 'Aug 3' eras."
  ],
  "referenced_bookmarks": [4421, 4598, 4612],
  "synthesis_metadata": {
    "model": "claude-opus-4-6",
    "prompt_version": "ad-hoc-analysis-v1",
    "input_token_count": 84231,
    "output_token_count": 2347
  }
}
```

**Schema constraints worth noting:**
- `narrative`: minLength 200, no upper bound.
- `key_findings`: array, minItems 1, maxItems 20.
- `referenced_bookmarks`: array of `bookmark_id` integers; each must appear in the input `materials.bookmarks`. Orchestrator validates.

**Orchestrator side effects:**
- Inserts a row in `analyses` with the synthesis output. Append-only — re-running the same prompt produces a new row, not an overwrite.
- The `filter_query` from the input is preserved verbatim in `analyses.filter_query`, so the user can re-run the same analysis later against fresh data.

### Validation and self-correction loop

1. Container exits, orchestrator reads `/workspace/output/<task-id>.json`.
2. Validate against the task's schema file. If valid → write to DB, done.
3. If invalid:
   - Log validation errors
   - Re-launch the same task container with `correction_instructions` populated with the validation errors
   - Container's CLAUDE.md persona has explicit instructions for handling `correction_instructions != null` — re-do the synthesis with the corrections applied
4. If second attempt also invalid → mark task as `synthesis_failed` (in a `synthesis_runs` log table — see below), surface in the triage CLI for human review. No further auto-retries.

Single self-correction is the right cap: if the model can't produce schema-valid output after one corrected attempt, the issue is the prompt or the schema, not the model.

### `synthesis_runs` — log of every container launch

```sql
CREATE TABLE synthesis_runs (
    synthesis_run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 TEXT    NOT NULL,
    task_type               TEXT    NOT NULL,
    attempt                 INTEGER NOT NULL,                           -- 1 = first try, 2 = correction
    started_at              INTEGER NOT NULL,
    completed_at            INTEGER,
    status                  TEXT    NOT NULL,                           -- 'running' | 'completed' | 'validation_failed' | 'container_failed' | 'cap_hit'
    enrichment_model        TEXT,
    enrichment_prompt_version TEXT,
    input_token_count       INTEGER,
    output_token_count      INTEGER,
    validation_errors       TEXT    CHECK (validation_errors IS NULL OR json_valid(validation_errors)),
    container_id            TEXT,
    UNIQUE (task_id, attempt),
    CHECK (task_type IN ('era_narrative', 'deep_pass', 'ad_hoc_analysis')),
    CHECK (status IN ('running', 'completed', 'validation_failed', 'container_failed', 'cap_hit'))
);

CREATE INDEX idx_synthesis_runs_status ON synthesis_runs (status);
```

Notes:
- Append-only; one row per launch attempt. Lets us audit "this era was synthesized 3 times — twice with v1 prompt, once after we bumped to v2."
- `status = 'cap_hit'` is the subscription-rate-limit-exhausted case from Decision 6's reliability section — orchestrator detects, exits cleanly, surfaces "resume after cooldown."
- `validation_errors` populated on `validation_failed` so the triage CLI can show "the model produced X but the schema required Y" without re-running the container.

### Schema addition triggered by deep-pass

```sql
ALTER TABLE enriched ADD COLUMN key_quotes TEXT
    CHECK (key_quotes IS NULL OR json_valid(key_quotes));
```

(Folded into `schema.sql` directly — no actual ALTER needed pre-v1 since the DB hasn't shipped.)

---

## Monorepo directory layout

Modern `src/`-layout Python package for code, with non-Python artifacts (Docker, JSON Schemas, SQL DDL, scripts) at the repo root in conventional locations. Mirrors Saga's `containers/` and `scripts/` conventions where they apply.

```
muninn/
├── README.md                       # public framing
├── SPEC.md                         # this document — authoritative spec
├── MUNINN.MD                       # original brainstorming history (kept for context)
├── CLAUDE.md                       # guidance for Claude Code sessions developing this repo
├── pyproject.toml                  # uv-managed; Python ≥3.12
├── uv.lock
├── .env.example                    # MUNINN_DATA_DIR, MUNINN_RAW_DIR, ANTHROPIC_API_KEY, QDRANT_URL, …
├── .gitignore                      # raw/, data/, .venv/, .env
├── domain_policy.yml               # user-edited; ships with commented examples
├── schema.sql                      # the DDL — applied at DB init
│
├── src/
│   └── muninn/
│       ├── __init__.py
│       ├── config.py               # env loading, paths, model IDs, prompt-version constants
│       ├── db.py                   # SQLite (write) + DuckDB (read) connection management; PRAGMA setup
│       ├── models.py               # Pydantic models matching schema rows
│       │
│       ├── ingest/
│       │   ├── __init__.py
│       │   ├── bookmarks_html.py   # Netscape Bookmark HTML parser (handles ADD_DATE, nested DL/DT)
│       │   └── pipeline.py         # raw/ → bookmarks table; era_label derivation from folder names
│       │
│       ├── sanitize/               # CRITICAL — secret-leakage prevention; see Decision 5
│       │   ├── __init__.py
│       │   ├── url.py              # the public sanitize_url(raw) function called by ingest
│       │   ├── rules.py            # dangerous-param denylist + path-as-credential domain patterns
│       │   └── tokens.py           # JWT/token-shape regex
│       │
│       ├── scrape/
│       │   ├── __init__.py
│       │   ├── client.py           # httpx wrapper with politeness (per-domain rate limit, on-disk cache)
│       │   ├── extract.py          # readability-lxml wrapper + extraction_quality classifier
│       │   ├── live.py             # live pass
│       │   ├── at_capture.py       # Wayback CDX lookup + at-capture fetch (±365d window)
│       │   ├── recent_archive.py   # recent_archive fallback pass
│       │   ├── auth_wall.py        # auth-wall detection heuristics
│       │   ├── domain_policy.py    # loads + applies domain_policy.yml; sets content_visible
│       │   └── pipeline.py         # dual-pass orchestration; enrichment_source routing
│       │
│       ├── enrich/
│       │   ├── __init__.py
│       │   ├── haiku.py            # Anthropic API client; bulk per-bookmark enrichment with prompt caching
│       │   ├── idempotency.py      # (enrichment_model, enrichment_prompt_version, content_hash) gate
│       │   ├── pipeline.py         # async worker pool driving haiku.py
│       │   └── prompts/
│       │       ├── README.md       # how to add/version prompts; prompt_version naming convention
│       │       └── per_bookmark_v1.md
│       │
│       ├── synthesis/
│       │   ├── __init__.py
│       │   ├── orchestrator.py     # public entry — launch container, validate, write DB
│       │   ├── container.py        # docker run invocation; saga-claude-credentials volume mount; tmux output capture
│       │   ├── validation.py       # JSON Schema validation + verbatim-quote substring check
│       │   ├── correction.py       # single-retry self-correction loop
│       │   └── tasks/
│       │       ├── era_narrative.py
│       │       ├── deep_pass.py
│       │       └── ad_hoc_analysis.py
│       │
│       ├── vector/
│       │   ├── __init__.py
│       │   ├── qdrant.py           # client wrapper; collection management
│       │   └── embed.py            # embedding generation (model TBD at first MCP integration)
│       │
│       └── consumers/
│           ├── __init__.py
│           ├── mcp/
│           │   ├── __init__.py
│           │   ├── server.py
│           │   └── tools.py        # MCP tool defs: semantic_search, fts_search, get_bookmark, get_era, …
│           ├── vault/
│           │   ├── __init__.py
│           │   ├── compiler.py     # one-page-per-bookmark generator → external Gitea repo
│           │   └── templates/
│           │       └── bookmark_page.md.j2
│           ├── cli/
│           │   ├── __init__.py
│           │   ├── main.py         # `muninn` entry point (registered in pyproject.toml [project.scripts])
│           │   ├── ingest.py       # `muninn ingest <path/to/bookmarks.html>`
│           │   ├── scrape.py       # `muninn scrape [--pass live|at_capture|recent_archive|all]`
│           │   ├── enrich.py       # `muninn enrich [--force] [--prompt-version vN]`
│           │   ├── synthesize.py   # `muninn synthesize era|deep-pass|analyze ...`
│           │   ├── triage.py       # `muninn triage` — list scrape_status failures, manual content paste
│           │   └── status.py       # pipeline state, counts, recent synthesis_runs
│           ├── timeline/
│           │   ├── __init__.py
│           │   └── builder.py      # DuckDB-driven time-bucketed aggregations; emits JSON for any UI
│           └── parquet/
│               ├── __init__.py
│               └── export.py       # DuckDB COPY ... TO 'export.parquet'
│
├── schemas/
│   ├── json/                       # JSON Schemas — single source of truth for synthesis I/O
│   │   ├── task-input.schema.json
│   │   ├── era-narrative.schema.json
│   │   ├── deep-pass.schema.json
│   │   └── ad-hoc-analysis.schema.json
│   └── sql/
│       └── 001_initial.sql         # symlink or duplicate of schema.sql; future migrations land here
│
├── containers/
│   └── synthesis/
│       ├── Dockerfile              # FROM trail-of-bits-devcontainer; bakes schemas/json/, CLAUDE.md, init script
│       ├── CLAUDE.md               # synthesis container persona; references /workspace/schemas/*.json
│       └── workspace-init.sh       # sets up /workspace/{input,output,status} at container start
│
├── scripts/
│   ├── launch-synthesis.sh         # docker run wrapper — mounts saga-claude-credentials:ro + workspace
│   ├── init-db.py                  # applies schema.sql; idempotent
│   └── reconcile-vector-index.py   # finds enriched rows missing from Qdrant; backfills
│
├── tests/
│   ├── unit/
│   │   ├── test_sanitize_url.py    # CRITICAL — leakage prevention; high-coverage table-driven tests
│   │   ├── test_sanitize_tokens.py
│   │   ├── test_ingest_html.py
│   │   ├── test_idempotency.py
│   │   └── …
│   ├── integration/
│   │   ├── test_dual_pass_scrape.py    # against fixture HTTP server + a Wayback mock
│   │   ├── test_pipeline_idempotent.py # full re-run = no-op; the cost-control gate
│   │   ├── test_duckdb_roundtrip.py    # the v1 implementation gate; JSON columns through DuckDB
│   │   └── test_synthesis_validation.py # JSON Schema + verbatim quote check; correction loop
│   └── fixtures/
│       ├── bookmarks_sample.html       # small synthetic export
│       ├── bookmarks_redaction.html    # exports containing tokens, magic links, webhooks for sanitize tests
│       └── readability_pages/          # captured HTML for extraction tests
│
├── docs/                           # already exists
│   ├── SAGA_ARCHITECTURE.MD
│   ├── SAGA_INSIGHTS_1.MD
│   ├── SAGA_INSIGHTS_2.MD
│   └── SAGA_SETUP.MD
│
├── raw/                            # GITIGNORED — user's bookmark HTML. Override with MUNINN_RAW_DIR.
│   └── (bookmarks.html, dated re-exports)
│
└── data/                           # GITIGNORED — derived artifacts. Override with MUNINN_DATA_DIR.
    ├── muninn.db                   # SQLite — the canonical store
    ├── scrape-cache/               # gzipped raw HTML, referenced by scrape_results.raw_html_path
    └── http-cache/                 # httpx on-disk cache
```

### Choices worth flagging

- **`src/`-layout Python package.** Modern convention; prevents accidental local-import pollution during testing, makes packaging cleaner. Worth the small extra import depth.
- **Prompts are versioned files in `enrich/prompts/`.** Each prompt version is a separate `.md` file (`per_bookmark_v1.md`, `per_bookmark_v2.md`, …). The `enrichment_prompt_version` column references the filename. Keeps prompt versioning explicit, reviewable in PRs, and reproducible — re-running a re-pass with `v1` against current data uses the file as it existed.
- **`schema.sql` at root + `schemas/sql/001_initial.sql`.** Top-level `schema.sql` is what `init-db.py` applies; `schemas/sql/` is the future migration directory's seed. For v1 they hold the same content (a symlink works); when v2 introduces non-reproducible state requiring real migrations, `schemas/sql/002_*.sql` etc. start landing there. The cost is one symlink today; the benefit is a migration story already in the right place when needed.
- **`containers/synthesis/`** mirrors Saga's `containers/dvergr/`, `containers/odin/` pattern. Each container gets its own subdirectory with Dockerfile + CLAUDE.md + init script. Single container in v1; structure leaves room for a dedicated scrape container or vault-compiler container if rate-limit isolation or scheduling complexity ever justifies one.
- **`raw/` and `data/` are top-level gitignored directories**, with `MUNINN_RAW_DIR` and `MUNINN_DATA_DIR` env-var overrides. Saga's pattern is `/opt/saga/...` outside the repo. Top-level here is simpler for v1 (one dev machine, one user); env-var override means deploying to a homelab path is a config change, not a code change.
- **No vault directory.** The compiled vault lives in its own Gitea repo per Decision 2; only the vault *generator* lives here under `consumers/vault/`. The generator's output target path is configured via env (`MUNINN_VAULT_DIR`).
- **No frontend.** Timeline view is data-layer in v1 (`consumers/timeline/builder.py` emits JSON); any UI is a future consumer or a separate repo.
- **`scripts/`** holds entry-point scripts that aren't part of the importable package — the container launch wrapper, DB init, vector index reconciliation. The user-facing CLI is `muninn` registered as a `[project.scripts]` entry in `pyproject.toml`, not a script here.
- **Tests organized as `unit/` + `integration/` + `fixtures/`.** Unit mirrors the source tree at module granularity; integration is by feature (full pipeline runs). Sanitization tests are explicitly highlighted because that's the highest-leakage-risk surface — they earn their own emphasis in the test directory.

---

## URL sanitization rules

The highest-leakage-risk surface in the system. These rules live in `src/muninn/sanitize/`, applied at ingest time before any URL is written to `bookmarks.url`. The sanitizer's contract is total: every URL goes through it; the only path to `bookmarks.url` is via `sanitize_url()`.

### Philosophy

1. **Err on the side of stripping.** False positives (over-stripping a legitimate param) lose information; false negatives (under-stripping a token) leak credentials. Asymmetric cost — stripping wins ties.
2. **Names are not sensitive; values are.** When stripping a param, record its name (so we can audit what was redacted) but never log, store, or transmit the value.
3. **Code, not config.** Per Decision 5(c), the rule list lives in a Python module with each rule rationale-commented. User-tunable regex rules get wrong → leak. The denylist + path-pattern set is updated via PR review, not YAML editing.
4. **Allowlist for legitimate long-ID params is v2.** v1 strips aggressively; the rare false positive (a long ID in a denylist-named param) is acceptable.

### Public contract — `sanitize_url()`

```python
# src/muninn/sanitize/url.py
from dataclasses import dataclass, field

@dataclass
class SanitizationResult:
    sanitized_url: str | None              # None when URL is unparseable / unsupported scheme
    redacted_param_names: list[str] = field(default_factory=list)
    path_redacted: bool = False
    userinfo_redacted: bool = False
    parse_error: str | None = None         # set when sanitized_url is None

    @property
    def redacted_param_count(self) -> int:
        return len(self.redacted_param_names)

def sanitize_url(raw_url: str) -> SanitizationResult:
    """
    Total function: never raises. Returns SanitizationResult with
    sanitized_url=None and parse_error set if the URL cannot be sanitized
    safely (unsupported scheme, malformed beyond parsing).
    """
```

The ingest pipeline calls `sanitize_url()` on every URL from raw HTML. If `sanitized_url is None`, the bookmark row is still inserted with `url = NULL` and `source_metadata.parse_error` populated — the *event* survives even when the URL doesn't.

### Rule 1 — Dangerous query parameter names (case-insensitive denylist)

`src/muninn/sanitize/rules.py`:

```python
# Each rule below documents its rationale. Add new entries via PR;
# update tests in tests/unit/test_sanitize_url.py simultaneously.

DANGEROUS_PARAM_NAMES: frozenset[str] = frozenset(
    name.lower() for name in [
        # ── OAuth / session tokens ──────────────────────────────────────
        "access_token",     # OAuth bearer access token
        "refresh_token",    # OAuth refresh token
        "id_token",         # OIDC ID token (JWT)
        "token",            # generic — used by many magic-link flows
        "auth_token",
        "authorization",    # rare in URLs but happens
        "bearer",
        "jwt",

        # ── OAuth flow artifacts ───────────────────────────────────────
        "code",             # OAuth authorization code (single-use, but stripping is safer)
        "state",            # OAuth state — usually CSRF token; can encode session
        "nonce",            # OIDC nonce

        # ── Session identifiers ────────────────────────────────────────
        "session", "sessionid", "session_id", "sid",
        "phpsessid", "jsessionid", "aspsessionid",

        # ── Credentials ────────────────────────────────────────────────
        "password", "passwd", "pwd", "pass",
        "secret", "client_secret",
        "apikey", "api_key", "app_key", "app_secret",
        "private_key",

        # ── Magic links / single-use credentials ───────────────────────
        "reset_token", "reset_password_token",
        "confirmation_token", "verification_token", "verify_token",
        "magic_link_token", "login_token", "auth_code",

        # ── AWS pre-signed URLs ────────────────────────────────────────
        "x-amz-signature", "x-amz-credential", "x-amz-security-token",
        "x-amz-algorithm", "x-amz-date", "x-amz-expires",
        "x-amz-signedheaders",
        "awsaccesskeyid",   # S3 v2 signing
        "signature",        # S3 v2 — generic name; might over-strip but safer
        "expires",          # S3 v2 — over-strip risk acceptable

        # ── GCS pre-signed URLs ────────────────────────────────────────
        "x-goog-signature", "x-goog-credential", "x-goog-algorithm",
        "x-goog-date", "x-goog-expires", "x-goog-signedheaders",

        # ── Azure SAS tokens ───────────────────────────────────────────
        "sig", "sv", "se", "sp", "sr", "st", "spr", "ss", "srt",
        # SAS params are short-named; over-strip risk on params like
        # "sr" (search results) is acceptable for the leakage protection.

        # ── Zoom / meeting passwords ───────────────────────────────────
        "tk",               # Zoom join token
        # "pwd" already covered above
    ]
)

# Tracking params — cosmetic, stripped silently, not recorded in
# redacted_param_names (zero-leakage value, no audit value either).
TRACKING_PARAM_NAMES: frozenset[str] = frozenset(
    name.lower() for name in [
        "utm_source", "utm_medium", "utm_campaign", "utm_term",
        "utm_content", "utm_id", "utm_name", "utm_term",
        "fbclid",                       # Facebook click ID
        "gclid", "gbraid", "wbraid",    # Google ad click IDs
        "mc_eid", "mc_cid",             # Mailchimp tracking
        "_ga", "_gl",                   # Google Analytics linker
        "yclid",                        # Yandex
        "msclkid",                      # Microsoft ads
        "twclid",                       # Twitter ads
        "li_fat_id",                    # LinkedIn
    ]
)
```

Stripping behavior:
- For a param name in `DANGEROUS_PARAM_NAMES` → drop the param, append the (lowercased) name to `redacted_param_names`.
- For a param name in `TRACKING_PARAM_NAMES` → drop silently, do NOT record. (Tracking params have no leakage value worth auditing.)
- Match is case-insensitive on the param name (URL spec is case-sensitive but real-world handling is mixed; safer to be insensitive).
- Wildcard `utm_*` — covered by enumerating common ones; the long tail is rare. If `utm_*` becomes a real source of pollution, switch to a regex prefix match.

### Rule 2 — JWT-shape detection in any param value

`src/muninn/sanitize/tokens.py`:

```python
import re

# JWT structure: <header>.<payload>.<signature> where header and payload
# are base64url-encoded JSON objects, both starting with "{" → "eyJ" base64.
# Match three base64url-charset segments separated by dots, with the first
# two prefixed "eyJ".
JWT_PATTERN = re.compile(
    r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"
)

def looks_like_jwt(value: str) -> bool:
    """True if the value contains anything matching JWT structure."""
    return bool(JWT_PATTERN.search(value))
```

Applied to every param value regardless of name. If a value looks JWT-shaped, drop the param (record name in `redacted_param_names`). Catches `?foo=eyJhbGciOiJIUzI1NiJ9.eyJ...` even when `foo` is an innocuous-looking name.

**Out of scope for v1: generic high-entropy token detection.** Heuristics like "param value is `[A-Za-z0-9_-]{32,}` and not in known-safe-id list" produce too many false positives (YouTube `v=`, Spotify track IDs, GitHub gist IDs, etc.). Defer to v2 once we can build the allowlist from observed real-corpus data.

### Rule 3 — Path-as-credential domain patterns

Some URLs are credentials *as a whole* — the path itself is the secret. These need path-level redaction, not just query-param stripping. Detected by domain + path-shape patterns.

```python
# src/muninn/sanitize/rules.py (continued)

# Each entry: (domain regex, path regex, redacted_path replacement).
# Match on domain first (cheap), then path. Order matters — first match wins.
PATH_CREDENTIAL_PATTERNS: list[tuple[re.Pattern, re.Pattern, str]] = [
    # Slack incoming webhooks: hooks.slack.com/services/T<id>/B<id>/<token>
    # The token segment IS the credential; without it the URL is meaningless.
    (re.compile(r"^hooks\.slack\.com$"),
     re.compile(r"^/services/.+"),
     "/services/[redacted]"),

    # Discord webhooks: discord.com/api/webhooks/<id>/<token>
    # Same shape — token is the credential.
    (re.compile(r"^discord(app)?\.com$"),
     re.compile(r"^/api/webhooks/.+"),
     "/api/webhooks/[redacted]"),

    # Telegram bot API: api.telegram.org/bot<id>:<token>/<method>
    # The bot<id>:<token> segment is the credential.
    (re.compile(r"^api\.telegram\.org$"),
     re.compile(r"^/bot[^/]+/.*"),
     "/[redacted]"),

    # Generic magic-link patterns — common path shapes across many services.
    # Match domain-agnostic; only when the path component name suggests
    # credential semantics AND the following segment is "long enough" to
    # plausibly be a token (≥16 chars).
    (re.compile(r".*"),  # any domain
     re.compile(r"^/(magic-link|passwordless|verify-email|reset-password|confirm-email|email-confirmation)/[A-Za-z0-9_-]{16,}.*"),
     None),  # special: replace just the token segment, keep the prefix
]
```

For the magic-link pattern (`None` replacement), the sanitizer rewrites `/reset-password/abc123def456...` → `/reset-password/[redacted]`. Other patterns replace the entire path.

`path_redacted = True` is set on any of these matches. The redaction is destructive — there's no way to recover the original from the sanitized form, by design.

### Rule 4 — Scheme handling

```python
SUPPORTED_SCHEMES: frozenset[str] = frozenset(["http", "https"])

# Schemes that get rejected entirely — the URL doesn't survive ingest.
# parse_error is set; bookmark row inserted with url=NULL.
REJECTED_SCHEMES: frozenset[str] = frozenset([
    "javascript",   # XSS payloads bookmarked via "javascript:" — never
    "data",         # data URLs can be huge and contain anything
    "vbscript",
    "file",         # local file paths leak filesystem info
])

# Schemes that pass through unsanitized but are stored as-is. The body of
# a "mailto:" or "tel:" URL is the value being preserved.
PASSTHROUGH_SCHEMES: frozenset[str] = frozenset(["mailto", "tel", "sms"])
```

For `mailto:`, the email address is the "URL" — recorded as-is. The user bookmarked a `mailto:` for a reason; preserve it. Per Decision 5, sanitization is about secrets, not about excluding events.

### Rule 5 — Userinfo

URLs of the form `https://user:password@host/path` — strip the entire userinfo segment. Set `userinfo_redacted = True`. The credential was in the URL itself.

### Rule 6 — Fragment sanitization

Fragment identifiers (`#section-2`) are typically client-side anchors. BUT — some single-page apps encode auth tokens as fragments (`#access_token=...&token_type=Bearer`). Apply the same `DANGEROUS_PARAM_NAMES` denylist + JWT detection to fragment-encoded params.

### Rule 7 — Normalization

Applied to every URL after sanitization, before storage:

- Lowercase scheme and host (case-insensitive per spec)
- Strip default ports (`:80` for http, `:443` for https)
- Convert IDN to punycode (`xn--…`) for the storage form
- Preserve path case (case-sensitive per spec)
- Preserve trailing slash (some sites differentiate `/foo` vs. `/foo/`)
- Sort query params alphabetically (canonicalization for dedup; not strictly necessary but cheap and useful)

### Edge cases documented (explicit)

| Case | Behavior |
|---|---|
| Unparseable URL (malformed beyond `urllib.parse`) | `sanitized_url=None`, `parse_error="unparseable"` |
| Rejected scheme (`javascript:`, `data:`, `file:`) | `sanitized_url=None`, `parse_error="unsupported_scheme:<scheme>"` |
| Empty URL or whitespace-only | `sanitized_url=None`, `parse_error="empty"` |
| URL with userinfo | Strip userinfo; `userinfo_redacted=True` |
| URL with no path | Path = `/` after normalization |
| URL with fragment containing tokens | Fragment params filtered by same denylist |
| `mailto:`, `tel:`, `sms:` | Pass through unchanged; no redaction |

### Test coverage requirements

Sanitization is the highest-leakage-risk component. `tests/unit/test_sanitize_url.py` and `tests/unit/test_sanitize_tokens.py` are table-driven, with at least the following coverage:

- **Per `DANGEROUS_PARAM_NAMES` family** — at least one test per category (OAuth, session, credentials, magic links, AWS-signed, GCS-signed, Azure SAS, Zoom). Total: ≥30 cases.
- **Per `PATH_CREDENTIAL_PATTERNS`** — positive case (matches and gets redacted) + negative case (similar URL on the same domain that should NOT be redacted, e.g., `discord.com/api/users/123` ≠ a webhook). Total: ≥10 cases.
- **JWT detection** — JWT in various param positions, JWT-shaped string that's actually not a JWT (rare, document the false positive risk), JWT in fragment. Total: ≥8 cases.
- **Tracking param stripping** — each `TRACKING_PARAM_NAMES` family. Total: ≥8 cases.
- **Edge cases** — every row in the edge cases table above. Total: ≥7 cases.
- **Negative cases (must pass through unchanged)** — Wikipedia article URLs, GitHub repo URLs, HN comment URLs, YouTube watch URLs, Spotify track URLs, Google Doc share URLs (these contain long IDs that are NOT credentials per se), arXiv paper URLs. Total: ≥15 cases. **Critical for catching over-stripping regressions.**

Total target: **≥80 explicit test cases** at v1, growing with the rule list.

### CI gate

Sanitization tests run on every PR. Any change to `rules.py` or `tokens.py` requires:
1. The corresponding test case(s) added/updated in the same PR
2. A reviewer's explicit ack of the rationale-comment update
3. A diff comment explaining what category (over-strip risk vs. leakage gap) the change addresses

This is the single component where "ship it and iterate" is wrong — leaked secrets are committed to the DB and propagated to the vector index and the vault. Iteration after a leak doesn't un-leak.

### Defense-in-depth — `raw/` is the only place originals live

Per Decision 5, the original (unsanitized) URL is **never** stored after ingest. The defense:

1. `raw/bookmarks.html` is gitignored locally, excluded from any export, lives only in the user's private store.
2. `sanitize_url()` is the only path from `raw/` to `bookmarks.url`. The pipeline structure makes it impossible to insert a `bookmarks.url` row that didn't go through it (enforced at the application layer; if paranoid, additionally enforced via a CHECK constraint that rejects URLs containing known-dangerous param names — defense in depth).
3. No log statement anywhere in the codebase emits the raw URL. Logging passes through a `redact_url_for_log()` helper that calls `sanitize_url()` first.

### SQLite operational settings (set at every connection)

```sql
PRAGMA journal_mode = WAL;          -- concurrent reader/writer (MCP queries while bulk enricher writes)
PRAGMA synchronous = NORMAL;        -- WAL-safe, faster than FULL
PRAGMA busy_timeout = 5000;         -- 5s lock wait before failing
PRAGMA foreign_keys = ON;           -- off by default in SQLite, on per-connection
PRAGMA temp_store = MEMORY;
```

### Migration story

**No migration framework in v1.** The DB is a derived artifact — `raw/bookmarks.html` is the source of truth, the pipeline is idempotent, and recovery from any schema change is "drop the DB, re-ingest." Alembic-style migrations are overkill at this stage. When v2 multi-source ingest lands, schema changes that genuinely need preservation (e.g., user-added tags or `deep_pass_requested` flags that aren't reproducible from raw/) will force the migration question — handle it then.

### Bookmark removal on re-ingest

If a bookmark exists in the DB but is absent from a re-imported `raw/bookmarks.html`, **delete it** (cascading scrape_results, enriched). The user's act of un-bookmarking is treated as canonical. Future enhancement: tombstone rows with `removed_at` to preserve un-bookmark events as a signal — deferred to v2 when the digital-footprint vision (where event removal carries meaning) actually matters.
