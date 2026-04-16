---
project: muninn
version: v1-spec
status: ready-for-decomposition
authoritative_source: SPEC.md
ingest_target: raw/spec.md (in the Muninn project wiki under Odin)
phase: handoff to Saga Phase 3 (first real run)
last_updated: 2026-04-15
---

# Muninn — Structured Specification

This is the **structured/entity-page-friendly** view of `SPEC.md`. Same content, organized for clean compilation into the Odin-managed project wiki. Each entity is a self-contained block with a consistent header so Odin's compile step can extract it into its own wiki page without cross-cutting concerns.

> **Template caveat:** the canonical wiki template lives in the Saga repo at `odin/specs/templates/project-template/CLAUDE.md`, which was not available at authoring time. The structure below is inferred from Saga's documented `raw/` + `wiki/` schema (per `docs/SAGA_INSIGHTS_1.MD` §3.1–3.2). If the actual template differs, this file should be reformatted before ingestion. `SPEC.md` remains authoritative regardless of the structural form.

---

## 0. Project metadata

```yaml
entity_type: project
name: muninn
purpose: Personal digital-footprint aggregator. v1 ingests bookmarks; v2+ adds notes (Evernote/Apple Notes/Obsidian) and media (YouTube/Spotify/SoundCloud).
authoritative_spec: SPEC.md
runtime_dependencies:
  - saga-phase-1
  - saga-phase-2
  - anthropic-api
  - anthropic-max
  - internet-archive
  - qdrant-homelab-19
implementation_decomposition: 5 work streams (see §3)
single_user: true
single_machine_v1: true
```

---

## 1. Constraints (binding on all v1 work)

```yaml
entity_type: constraint_set
applies_to: all-streams
```

**Scope and scale**
- Single user, single machine at v1.
- Personal corpus, never published.
- ~10k bookmarks at v1; v2 may push to 100k+.
- Bookmarks-only ingest at v1; other sources are v2+ adapters.

**LLM and authentication**
- Anthropic API key required for bulk Haiku enrichment.
- Anthropic Max subscription required for synthesis tier (consumed via Saga's existing `saga-claude-credentials` Docker named volume).
- Saga Phase 1+2 infrastructure must be present on the host at runtime.
- Saga Phase 3 NOT required at runtime (used at implementation time only).
- `claude -p` and `CLAUDE_CODE_OAUTH_TOKEN` are off-limits for synthesis (silently force API billing per SAGA_ARCHITECTURE.MD:141).

**Networking**
- HTTP-only scraping; no Playwright in v1.
- Per-domain ≤1 rps to live origins.
- ≤0.5 rps global to Internet Archive; identifying UA required.
- Internet Archive is the only archive provider at v1.

**Sensitive data**
- Original (unsanitized) URLs never stored after ingest.
- Sanitize, don't exclude (events always survive in `normalized/`).
- Sanitization rules in code, not config.
- No log statement may emit a raw URL.

**Storage**
- SQLite is the single canonical store; DuckDB attaches read-only.
- No migration framework at v1; recovery is "drop DB, re-ingest".
- Qdrant on `.19`, no auth at v1; reassessable at first MCP integration.

**Vault topology**
- Compiled vault and personal vault are never the same vault (hard rule).
- Compiled vault lives in its own Gitea repo, not in this repo.

---

## 2. External services and secrets

Each row is one entity for compile-step extraction.

```yaml
entity_type: external_service
name: anthropic-api
purpose: Bulk Haiku enrichment per bookmark; prompt caching for cost control
secret_kind: env-var
secret_name: ANTHROPIC_API_KEY
provisioning: User sets in .env; src/muninn/config.py reads at startup
heimdall_workaround_v1: Manual .env population from user-maintained secrets file
failure_mode: muninn enrich halts at startup with explicit missing-key message
```

```yaml
entity_type: external_service
name: anthropic-max
purpose: Synthesis container — Opus 4.6 + 1M context for era narratives, deep-passes, ad-hoc analysis
secret_kind: docker-named-volume
secret_name: saga-claude-credentials
secret_path_in_container: /home/vscode/.claude/.credentials.json
provisioning: One-time human bootstrap via Saga's scripts/auth/bootstrap-credentials.sh; maintained by Saga's keepalive cron (4-hourly refresh-token rotation)
heimdall_workaround_v1: Volume must exist on host before muninn synthesize is run; if missing, Saga's bootstrap script must be re-run
failure_mode: muninn synthesize exits with "synthesis credentials volume not found"
```

```yaml
entity_type: external_service
name: internet-archive
purpose: At-capture and recent_archive scraper passes (Wayback CDX + content fetch)
secret_kind: none
identifying_user_agent: "Muninn/v1 (+<contact-url>)"
contact_url_secret: MUNINN_CONTACT_URL env var
provisioning: User sets MUNINN_CONTACT_URL in .env; UA constructed in src/muninn/scrape/client.py
politeness: ≤0.5 rps global; exponential backoff on 429s; on-disk cache of CDX responses
failure_mode: Pass returns scrape_status=network_error or no_archive; pipeline continues per dead-link policy
```

```yaml
entity_type: external_service
name: qdrant
purpose: Vector embeddings storage keyed by bookmark_id
secret_kind: env-var
secret_name: QDRANT_URL
example_value: http://192.168.86.19:6333
auth_v1: none (homelab-internal)
provisioning: User sets in .env; Qdrant service must be running on the LXC at .19
failure_mode: Embedding write fails with logged warning; SQLite enrich row still persists; scripts/reconcile-vector-index.py backfills on next run
reassessable: true (Qdrant vs DuckDB-vss at first MCP integration)
```

```yaml
entity_type: external_service
name: domain-policy-yml
purpose: Per-domain content_visible policy (sensitive data handling)
secret_kind: yaml-config-file
file_path: ./domain_policy.yml
provisioning: User-edited at repo root
failure_mode: If file missing or malformed, ingest aborts with explicit error. Never silently default to scrape-everything.
```

---

## 3. Work streams

5 streams. Each stream is a candidate for one dvergr (or one sub-fleet if Phase 3 wants finer-grained decomposition within a stream).

### Stream 1 — Ingest + Sanitization

```yaml
entity_type: work_stream
id: stream-1-ingest-sanitize
owns_writes: [bookmarks]
reads: []
modules:
  - src/muninn/ingest/
  - src/muninn/sanitize/
  - src/muninn/db.py
  - src/muninn/models.py
  - src/muninn/config.py
  - schema.sql
  - schemas/sql/001_initial.sql
  - pyproject.toml
  - .env.example
  - .gitignore
  - domain_policy.yml
depends_on: []
parallelizable_within: false
estimated_dvergar: 1
```

**Success criteria (completion-promise fires when ALL true):**
1. `scripts/init-db.py` applies `schema.sql` cleanly against a fresh SQLite file; all 7 tables (`bookmarks`, `scrape_results`, `enriched`, `eras`, `analyses`, `cross_references`, `synthesis_runs`) plus `fts_bookmarks` virtual table exist with declared columns and constraints.
2. `sanitize_url()` passes ≥80 table-driven test cases in `tests/unit/test_sanitize_url.py` and `test_sanitize_tokens.py`.
3. DuckDB JSON round-trip smoke test (`tests/integration/test_duckdb_roundtrip.py`) passes on every JSON column type.
4. `muninn ingest <path/to/bookmarks.html>` reads a Netscape Bookmark HTML fixture and produces idempotent `bookmarks` rows. Re-running yields identical row count and identical `bookmark_id`s (verified by SHA256 of `SELECT * FROM bookmarks ORDER BY bookmark_id`).
5. Every `bookmarks.url` went through `sanitize_url()` (verified by absence of any `DANGEROUS_PARAM_NAMES` substring in any URL).
6. `redacted_param_count`, `redacted_param_names`, `path_redacted` populated correctly for the `bookmarks_redaction.html` fixture.
7. `domain_policy.yml` parsed and applied: rows whose domain matches a policy entry have `content_visible=0`.

---

### Stream 2 — Scrape

```yaml
entity_type: work_stream
id: stream-2-scrape
owns_writes: [scrape_results]
also_updates: [bookmarks.enrichment_source]
reads: [bookmarks]
modules:
  - src/muninn/scrape/
  - data/scrape-cache/
  - data/http-cache/
depends_on: [stream-1-ingest-sanitize]
parallelizable_within: true (per-bookmark async pool, but per-domain serialized)
estimated_dvergar: 1
```

**Success criteria:**
1. `muninn scrape` runs the dual-pass (live + at_capture) for all `content_visible=1` bookmarks.
2. Per-domain politeness verified: ≤1 rps to live origins, ≤0.5 rps to IA endpoints (request timestamps in test).
3. `at_capture` window respects ±365 days of `bookmarks.captured_at`; outside-window rows get `scrape_status=no_archive`.
4. `recent_archive` fallback fires automatically when at_capture returns `no_archive`, recorded as a separate `scrape_results` row.
5. `bookmarks.enrichment_source` set per priority (`at_capture` → `recent_archive` → `live_fallback` → `none`).
6. Auth-wall detection: fixture page with login-form markers produces `scrape_status=auth_required`.
7. Re-running scrape is idempotent — `(bookmark_id, pass)` upsert, row count constant across re-runs.
8. HTTP cache prevents re-fetch on consecutive runs (request-count assertion).

---

### Stream 3 — Enrich

```yaml
entity_type: work_stream
id: stream-3-enrich
owns_writes: [enriched, qdrant_collection]
reads: [bookmarks, scrape_results]
modules:
  - src/muninn/enrich/
  - src/muninn/vector/
  - src/muninn/enrich/prompts/per_bookmark_v1.md
depends_on: [stream-2-scrape]
parallelizable_within: true (async API pool)
estimated_dvergar: 1
```

**Success criteria:**
1. `muninn enrich` calls Anthropic Haiku API with prompt caching enabled. Cache hit rate ≥80% on bulk pass after first 100 calls (verified via API response cache-hit headers).
2. `enriched` rows produced for every bookmark where `content_visible=1` AND `enrichment_source != 'none'`.
3. Idempotency: re-running with same `(enrichment_model, enrichment_prompt_version, content_hash)` triple performs zero API calls.
4. `enrichment_model`, `enrichment_prompt_version`, `content_hash` populated on every row (NOT NULL).
5. Qdrant collection populated; `count(qdrant_points) == count(enriched_rows)` after full enrich pass.
6. `scripts/reconcile-vector-index.py` correctly identifies and backfills `enriched` rows missing from Qdrant.

---

### Stream 4 — Synthesis

```yaml
entity_type: work_stream
id: stream-4-synthesis
owns_writes: [eras, analyses, cross_references, synthesis_runs]
also_updates: [enriched (deep-pass overwrites), enriched.key_quotes]
reads: [bookmarks, enriched, scrape_results]
modules:
  - src/muninn/synthesis/
  - containers/synthesis/
  - scripts/launch-synthesis.sh
  - schemas/json/
depends_on: [stream-3-enrich, saga-phase-1, saga-phase-2]
parallelizable_within: false (single Opus-1M instance at v1)
estimated_dvergar: 1
```

**Success criteria:**
1. `containers/synthesis/Dockerfile` builds; image extends Trail of Bits devcontainer with schemas + persona baked in.
2. Container starts with `saga-claude-credentials` mounted read-only; `/status` inside container reports `Login method: Claude Max Account` (subscription mode confirmed, NOT API).
3. `muninn synthesize era <era_label>` produces JSON validating against `schemas/json/era-narrative.schema.json`; `eras` row written with all required fields; `enrichment_model` records resolved Opus version.
4. `muninn synthesize deep-pass <bookmark_id>` produces JSON validating against `schemas/json/deep-pass.schema.json`; verbatim quote substring check passes for every entry in `key_quotes`.
5. `muninn synthesize analyze "<prompt>"` produces JSON validating against `schemas/json/ad-hoc-analysis.schema.json`; row inserted into `analyses` with `filter_query` preserved verbatim.
6. Single-retry self-correction loop: deliberately-broken first output triggers re-launch with `correction_instructions`; second failure → `validation_failed` in `synthesis_runs`, no DB write.
7. `synthesis_runs` audit log populated for every container launch; `status='cap_hit'` detectable from synthetic rate-limit scenario.
8. `cross_references` rows from deep-pass output insert with `created_by='deep_pass'`; `target_bookmark_id` validated against input materials.

---

### Stream 5 — Consumers

```yaml
entity_type: work_stream
id: stream-5-consumers
owns_writes: [] (read-only consumers; vault generator writes to external Gitea repo)
reads: [all tables]
modules:
  - src/muninn/consumers/mcp/
  - src/muninn/consumers/vault/
  - src/muninn/consumers/cli/
  - src/muninn/consumers/timeline/
  - src/muninn/consumers/parquet/
  - consumers/vault/templates/
depends_on: [stream-3-enrich, stream-4-synthesis]  # full surface; partial after streams 1+2
parallelizable_within: true (each consumer is independent)
estimated_dvergar: 1 (or split per-consumer if Phase 3 wants finer)
```

**Success criteria:**
1. `muninn` CLI registers as `[project.scripts]` entry; `muninn --help` works after `pip install -e .`.
2. `muninn triage` lists `scrape_status` failures or `enrichment_source='none'` rows; output human-readable + machine-parseable (JSON with `--json`).
3. `muninn deep-pass <id>` sets `deep_pass_requested=1` on `enriched` row; row picked up by `muninn synthesize deep-pass --pending`.
4. `muninn status` shows pipeline state: bookmark count, per-pass scrape counts, enriched count, recent `synthesis_runs` outcomes.
5. Vault generator produces one markdown page per `content_visible=1` bookmark in `MUNINN_VAULT_DIR`; cross-references render as Obsidian-compatible `[[bookmark-slug]]` links bidirectionally.
6. MCP server exposes `semantic_search`, `fts_search`, `get_bookmark`, `get_era`, `list_eras` (minimum); queries match SQLite/Qdrant ground truth.
7. Timeline builder emits JSON via DuckDB-attached-to-SQLite; per-era aggregations match `SELECT era_label, count(*) FROM bookmarks GROUP BY era_label`.
8. Parquet export (`muninn export parquet --out path.parquet`) readable by both DuckDB and pandas; JSON columns round-trip cleanly.

---

### Cross-stream contracts (must stay stable)

```yaml
entity_type: contract
name: cross-stream-contracts
```

- **SQLite schema** (§4 below): column names, types, constraints. Schema changes require spec amendment.
- **JSON Schemas** in `schemas/json/`: synthesis container's I/O contract.
- **`sanitize_url()` public signature** and `SanitizationResult` dataclass shape.
- **`domain_policy.yml` schema**: single `content_visible: false` toggle per entry; glob patterns.
- **`enrichment_prompt_version` naming convention**: `<task>_v<N>` matching the file name in `prompts/`.

### Decomposition shape for Odin

```
Stream 1 ──► Stream 2 ──► Stream 3 ──┬──► Stream 4
                                     └──► Stream 5
```

Streams 1–3 fully sequential. Streams 4 and 5 can begin in parallel once Stream 3 starts but cannot complete until Stream 3 is done. 5-stream decomposition is the natural granularity at v1 scale; finer splits don't reduce critical path.

---

## 4. Tables

Each table is one entity for the wiki compile step.

### Table: bookmarks

```yaml
entity_type: table
name: bookmarks
owned_by: stream-1-ingest-sanitize
purpose: Source-agnostic normalized row, one per ingested item. Survives even when content is hidden.
upsert_key: (source, source_id)
ingest_idempotent: true
```

```sql
CREATE TABLE bookmarks (
    bookmark_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source                  TEXT    NOT NULL,
    source_id               TEXT    NOT NULL,
    captured_at             INTEGER NOT NULL,
    title                   TEXT,
    url                     TEXT,                                       -- sanitized only
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

### Table: scrape_results

```yaml
entity_type: table
name: scrape_results
owned_by: stream-2-scrape
purpose: One row per scrape pass per bookmark. Upsert-by-(bookmark_id, pass); current state, not history.
upsert_key: (bookmark_id, pass)
fk_cascade: bookmark_id → bookmarks.bookmark_id ON DELETE CASCADE
```

```sql
CREATE TABLE scrape_results (
    scrape_result_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    bookmark_id         INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    pass                TEXT    NOT NULL,
    fetched_at          INTEGER NOT NULL,
    target_timestamp    INTEGER,
    actual_snapshot_at  INTEGER,
    archive_url         TEXT,
    final_url           TEXT,
    http_status         INTEGER,
    scrape_status       TEXT    NOT NULL,
    extraction_quality  TEXT,
    content_text        TEXT,
    content_html        TEXT,
    raw_html_path       TEXT,
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

### Table: enriched

```yaml
entity_type: table
name: enriched
owned_by: stream-3-enrich
also_written_by: stream-4-synthesis (deep-pass overwrites)
purpose: LLM-generated metadata, 1:1 with bookmarks. Idempotent via (enrichment_model, enrichment_prompt_version, content_hash) triple.
upsert_key: bookmark_id
fk_cascade: bookmark_id → bookmarks.bookmark_id ON DELETE CASCADE
```

```sql
CREATE TABLE enriched (
    bookmark_id                 INTEGER PRIMARY KEY REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    summary                     TEXT,
    tags                        TEXT    CHECK (tags IS NULL OR json_valid(tags)),
    entities                    TEXT    CHECK (entities IS NULL OR json_valid(entities)),
    content_type                TEXT,
    language                    TEXT,
    word_count                  INTEGER,
    enrichment_model            TEXT    NOT NULL,
    enrichment_prompt_version   TEXT    NOT NULL,
    content_hash                TEXT    NOT NULL,
    enriched_at                 INTEGER NOT NULL,
    deep_pass_requested         INTEGER NOT NULL DEFAULT 0 CHECK (deep_pass_requested IN (0, 1)),
    key_quotes                  TEXT    CHECK (key_quotes IS NULL OR json_valid(key_quotes))
);
CREATE INDEX idx_enriched_content_type ON enriched (content_type);
CREATE INDEX idx_enriched_deep_pass    ON enriched (deep_pass_requested) WHERE deep_pass_requested = 1;
```

### Table: eras

```yaml
entity_type: table
name: eras
owned_by: stream-4-synthesis
purpose: Per-era synthesis output (narrative, dominant topics/domains, year inference).
upsert_key: era_label
fk: none (era_label is mutable; do not FK)
```

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
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER
);
```

### Table: analyses

```yaml
entity_type: table
name: analyses
owned_by: stream-4-synthesis
purpose: Ad-hoc cross-corpus synthesis results. Append-only.
mode: append-only
```

```sql
CREATE TABLE analyses (
    analysis_id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    title                       TEXT    NOT NULL,
    prompt                      TEXT    NOT NULL,
    filter_query                TEXT,
    narrative                   TEXT,
    enrichment_model            TEXT,
    enrichment_prompt_version   TEXT,
    generated_at                INTEGER NOT NULL DEFAULT (unixepoch())
);
```

### Table: cross_references

```yaml
entity_type: table
name: cross_references
owned_by: stream-4-synthesis
also_written_by: cli (manual entry)
purpose: Asymmetric bookmark-to-bookmark relationships from deep-pass outputs and manual entry.
upsert_key: (source_bookmark_id, target_bookmark_id, created_by)
```

```sql
CREATE TABLE cross_references (
    cross_reference_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    source_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    target_bookmark_id  INTEGER NOT NULL REFERENCES bookmarks (bookmark_id) ON DELETE CASCADE,
    relationship        TEXT,
    rationale           TEXT,
    created_by          TEXT    NOT NULL,
    created_at          INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (source_bookmark_id, target_bookmark_id, created_by)
);
CREATE INDEX idx_cross_references_source ON cross_references (source_bookmark_id);
CREATE INDEX idx_cross_references_target ON cross_references (target_bookmark_id);
```

### Table: synthesis_runs

```yaml
entity_type: table
name: synthesis_runs
owned_by: stream-4-synthesis
purpose: Append-only audit log of every synthesis container launch attempt. Lets us audit "this era was synthesized 3 times across two prompt versions."
mode: append-only
```

```sql
CREATE TABLE synthesis_runs (
    synthesis_run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id                 TEXT    NOT NULL,
    task_type               TEXT    NOT NULL,
    attempt                 INTEGER NOT NULL,
    started_at              INTEGER NOT NULL,
    completed_at            INTEGER,
    status                  TEXT    NOT NULL,
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

### Virtual table: fts_bookmarks

```yaml
entity_type: virtual_table
name: fts_bookmarks
owned_by: stream-1-ingest-sanitize (creation); maintained by streams 2 + 3 (population)
purpose: Full-text search index. Contentless FTS5 — stores tokens only.
sync: application-layer (in same transaction as enriched/scrape_results writes); not via SQL triggers
```

```sql
CREATE VIRTUAL TABLE fts_bookmarks USING fts5 (
    title,
    summary,
    content_text,
    tags,
    content=''
);
```

### SQLite operational PRAGMA (set per connection)

```yaml
entity_type: db_settings
applies_to: every-connection
```

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;
PRAGMA foreign_keys = ON;
PRAGMA temp_store = MEMORY;
```

---

## 5. Containers

### Container: synthesis

```yaml
entity_type: container
name: muninn-synthesis
owned_by: stream-4-synthesis
base_image: trail-of-bits-devcontainer (sibling of saga-dvergr)
billing_mode: subscription (NOT api)
required_volumes:
  - saga-claude-credentials:/home/vscode/.claude:ro
required_env: []
firewall_whitelist:
  - api.anthropic.com (already in base)
  - web.archive.org
  - archive.org
launch_method: scripts/launch-synthesis.sh (docker run wrapper)
runtime_pattern: tmux + claude --dangerously-skip-permissions, NOT claude -p
workspace_layout:
  - /workspace/CLAUDE.md  (persona, baked into image)
  - /workspace/input/<task-id>.json  (written by orchestrator)
  - /workspace/output/<task-id>.json  (written by container)
  - /workspace/status/<task-id>.status.json  (optional progress)
  - /workspace/schemas/*.schema.json  (baked into image)
exit_pattern: ralph-loop completion-promise + --rm
keepalive: piggybacks on Saga's existing 4-hourly keepalive cron (no Muninn-specific keepalive needed)
```

---

## 6. Synthesis I/O schemas

Three JSON Schemas constitute the contract between orchestrator and synthesis container. Files baked into image at `/workspace/schemas/` and committed at `schemas/json/` in repo.

### Schema: era-narrative

```yaml
entity_type: json_schema
name: era-narrative.schema.json
written_by: synthesis container
read_by: stream-4-synthesis orchestrator
db_target: eras (per-era upsert)
```

Required output fields: `narrative` (50–4000 chars), `inferred_year` (int 1990–current+1), `dominant_topics` (1–10 items), `dominant_domains` (1–10 items), `synthesis_metadata.{model, prompt_version, input_token_count, output_token_count, neighboring_eras_used}`. Orchestrator stamps `start_date`, `end_date`, `bookmark_count` deterministically.

### Schema: deep-pass

```yaml
entity_type: json_schema
name: deep-pass.schema.json
written_by: synthesis container
read_by: stream-4-synthesis orchestrator
db_target: enriched (overwrite by bookmark_id) + cross_references (insert with created_by='deep_pass')
```

Required fields: `summary` (≥100 chars), `tags`, `entities[].{name,type∈[person,organization,place,concept,tool,paper,event]}`, `content_type∈[article,paper,video,tool,doc,talk,thread,other]`, `language`, `word_count`, `key_quotes` (≤5 items, ≤500 chars each, **must be verbatim substrings of content_text**), `cross_references[].{target_bookmark_id ∈ same_era_cluster, relationship, rationale}`, `synthesis_metadata`.

### Schema: ad-hoc-analysis

```yaml
entity_type: json_schema
name: ad-hoc-analysis.schema.json
written_by: synthesis container
read_by: stream-4-synthesis orchestrator
db_target: analyses (append)
```

Required fields: `narrative` (≥200 chars), `key_findings` (1–20 items), `referenced_bookmarks` (each must appear in input materials), `synthesis_metadata`.

### Validation and self-correction loop

```yaml
entity_type: process
name: synthesis-validation-loop
```

1. Container exits → orchestrator reads `/workspace/output/<task-id>.json`.
2. Validate against schema. If valid → write to DB.
3. If invalid → re-launch container with `correction_instructions` populated with validation errors.
4. If second attempt also invalid → mark `validation_failed` in `synthesis_runs`, surface in triage CLI. No further auto-retries.

Single-retry cap is intentional. If model can't produce schema-valid output after one corrected attempt, the issue is the prompt or schema, not the model.

---

## 7. Decisions (with rationale anchors)

Each decision is one entity. Full discussion in `SPEC.md`; this is the index.

```yaml
entity_type: decision
id: D1-scope-of-v1
resolution: bookmarks-only; schema source-agnostic
constrains: [stream-1-ingest-sanitize]
rejected_alternatives_index: [multi-source-ingest-v1]
```

```yaml
entity_type: decision
id: D2-delivery-shape
resolution: multi-language monorepo (Python primary); vault stays out of repo; two distinct vaults (compiled + personal) never the same vault
constrains: [stream-5-consumers, container-muninn-synthesis]
rejected_alternatives_index: [multiple-separate-repos, vault-inside-repo, single-vault-for-input-and-output]
```

```yaml
entity_type: decision
id: D3-scraper-strategy
resolution: HTTP-only v1, dual concurrent pass (live + at_capture from Wayback); scrape_results is upsert-by-(bookmark_id, pass) child table
constrains: [stream-2-scrape, table-scrape_results]
rejected_alternatives_index: [playwright-day-1, live-only-pass, archive-only-pass, screenshots-v1, append-mode-scrape-history]
```

```yaml
entity_type: decision
id: D4-dead-link-policy
resolution: ±365d at_capture window; recent_archive fallback on by default; live_fallback as canonical with enrichment_source flag making provenance explicit
constrains: [stream-2-scrape]
```

```yaml
entity_type: decision
id: D5-sensitive-data
resolution: Sanitize, don't exclude. Universal URL sanitization in code; per-domain content_visible toggle; auth-wall detection automatic. Original URL never stored after ingest.
constrains: [stream-1-ingest-sanitize, all-streams (no-raw-url-logging)]
rejected_alternatives_index: [opt-in-skiplist, exclude-from-normalized, four-toggle-domain-policy, sanitization-rules-in-yaml, generic-high-entropy-token-detection]
```

```yaml
entity_type: decision
id: D6-llm-tiering
resolution: Two-tier — Haiku 4.5 via API for bulk per-bookmark enrichment; Opus 4.6 + 1M context via subscription-mode container (sibling of dvergr) for synthesis. claude -p / CLAUDE_CODE_OAUTH_TOKEN are off-limits — they silently force API billing.
constrains: [stream-3-enrich, stream-4-synthesis, container-muninn-synthesis]
rejected_alternatives_index: [sonnet-everywhere, opus-everywhere, three-tier-haiku-sonnet-opus, claude-p-for-synthesis, multi-instance-synthesis-v1, silent-fallback-to-api-on-cap-hit]
```

---

## 8. URL sanitization rules

Highest-leakage-risk component. Defined in code at `src/muninn/sanitize/{url.py,rules.py,tokens.py}`.

```yaml
entity_type: leakage_critical_module
name: sanitize_url
public_function: sanitize_url(raw: str) -> SanitizationResult
return_dataclass: SanitizationResult { sanitized_url, redacted_param_names, path_redacted, userinfo_redacted, parse_error }
total_function: true (never raises; parse_error set when sanitized_url is None)
```

**Rule classes (full denylists in SPEC.md §URL sanitization rules):**
- **Rule 1:** Dangerous query parameter names (case-insensitive denylist): OAuth tokens, session IDs, credentials, magic links, AWS/GCS/Azure pre-signed signatures, Zoom join tokens.
- **Rule 1b:** Tracking params (utm_*, fbclid, gclid, mc_eid, _ga, ...): stripped silently, not recorded in `redacted_param_names`.
- **Rule 2:** JWT-shape detection in any param value: `\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b`.
- **Rule 3:** Path-as-credential domain patterns: Slack incoming webhooks, Discord webhooks, Telegram bot tokens, generic magic-link/passwordless/verify-email path shapes.
- **Rule 4:** Scheme handling: `http`/`https` allowed; `javascript`/`data`/`vbscript`/`file` rejected (sanitized_url=None); `mailto`/`tel`/`sms` passthrough.
- **Rule 5:** Userinfo (`user:pass@host`) stripped entirely; `userinfo_redacted=True`.
- **Rule 6:** Fragment sanitization (apply Rule 1 + Rule 2 to `#access_token=...` style fragments).
- **Rule 7:** Normalization: lowercase scheme/host, strip default ports, IDN→punycode, sort query params alphabetically.

**Test coverage requirement (CI gate):** ≥80 explicit table-driven test cases. Any change to `rules.py`/`tokens.py` requires test cases in same PR + reviewer ack of rationale-comment update.

**Defense-in-depth:** `raw/bookmarks.html` is the only place originals live; gitignored; never copied off host. `sanitize_url()` is the only path from raw → `bookmarks.url`. `redact_url_for_log()` wraps every log statement that would otherwise emit a URL.

---

## 9. Compute layer split

```yaml
entity_type: compute_topology
name: sqlite-canonical-duckdb-analytics
```

| Component | Engine | Reason |
|---|---|---|
| Pipeline writes (ingest, scrape_results, enriched) | SQLite (write) | Transactional, single-writer, simple |
| MCP point lookups | SQLite (read) | Low-latency, indexed |
| Vault generator | SQLite (read) | Per-bookmark page rendering |
| CLI ad-hoc queries (simple) | SQLite (read) | Default; promote to DuckDB only when query shape needs it |
| Timeline view | DuckDB (read over attached SQLite) | Time-bucketed aggregations, window functions |
| Ad-hoc analysis pre-pass (filter/aggregate) | DuckDB (read over attached SQLite) | Group-by + array aggregation across era × tag × domain |
| Parquet export | DuckDB | `COPY ... TO 'export.parquet'` is one line |

**v1 implementation gate:** JSON round-trip smoke test through DuckDB SQLite extension on every JSON column type. Block v1 on this passing.

**Vector store choice (Qdrant vs DuckDB-vss) is reassessable at first MCP integration**, not locked in v1.

---

## 10. Monorepo directory layout

See `SPEC.md` "Monorepo directory layout" for the full tree. Key invariants for compile-step extraction:

```yaml
entity_type: directory_layout
language: python
package_layout: src/
package_manager: uv
```

- `src/muninn/<package>/` for all importable code; modules grouped by stream.
- `containers/synthesis/` mirrors Saga's `containers/dvergr/` pattern.
- `schemas/json/` is the single source of truth for synthesis I/O contracts.
- `schemas/sql/001_initial.sql` seeds the future migration directory.
- `raw/` and `data/` gitignored at top level; `MUNINN_RAW_DIR` / `MUNINN_DATA_DIR` env vars override path.
- Prompts as versioned `.md` files in `src/muninn/enrich/prompts/`; `enrichment_prompt_version` references the filename.

---

## 11. Rejected alternatives (consolidated index)

Full reasoning in `SPEC.md` "Rejected alternatives" section. This is the index for cross-referencing from Decisions.

```yaml
entity_type: rejected_alternatives_index
```

| ID | Alternative | Decision context | Reason |
|---|---|---|---|
| sonnet-everywhere | Sonnet for all enrichment | D6 | 5–10× Haiku cost; not justified for tag/summary extraction |
| opus-everywhere | Opus for all enrichment | D6 | Far too expensive at scale; reasoning not needed for extraction |
| three-tier-haiku-sonnet-opus | Haiku → Sonnet → Opus tiers | D6 | 1M context made Sonnet tier unnecessary; collapse to two tiers |
| multi-source-ingest-v1 | Pocket/HN/Arena/Evernote in v1 | D1 | Too much surface before schema is proven; v2+ adapters |
| playwright-day-1 | Playwright for all scrapes | D3 | 10–50× per-page cost; readability covers article-shaped bulk |
| live-only-pass | Skip archive pass | D3 | Loses what the user actually saw; Muninn is memory not mirror |
| archive-only-pass | Skip live pass | D3 | Loses change-detection signal; faster on fresh URLs |
| screenshots-v1 | Capture screenshots | D3 | Requires browser; Playwright tax avoided |
| opt-in-skiplist | Allowlist sensitive domains | D5 | Tedious for 10k+ corpus; opt-out simpler |
| exclude-from-normalized | Drop sensitive bookmarks entirely | D5 | Loses event signal; user reframing was sanitize-don't-exclude |
| four-toggle-domain-policy | scrape/enrich/vault/mcp toggles independently | D5 | Unnecessarily granular; single content_visible toggle suffices |
| sanitization-rules-in-yaml | User-tunable sanitization config | D5 | Token regex tuning by users would leak; code+PR-review is right shape |
| generic-high-entropy-token-detection | Strip all 32+ char alphanumeric values | D5 | Too many false positives at v1 (YouTube IDs, Spotify IDs); defer to v2 |
| multiple-separate-repos | One repo per component | D2 | Schema churn requires single-commit changes; cross-repo dance |
| vault-inside-repo | Compiled vault committed here | D2 | Vault is data with own git lifecycle; mixes tool with data |
| single-vault-for-input-and-output | Same vault for compiled output and personal input | D2 | Ingest loops; authorship ambiguity. Hard rule |
| claude-p-for-synthesis | Use claude -p for headless subscription work | D6 | Silently forces API billing per SAGA_ARCHITECTURE.MD:141 |
| multi-instance-synthesis-v1 | Parallel synthesis dvergar at v1 | D6 | Total volume small; single Opus-1M instance fits; Phase 3 parallelism for v2 |
| silent-fallback-to-api-on-cap-hit | Auto-bill API when subscription cap hits | D6 | Surprise charges worse than explicit "resume after cooldown" |
| duckdb-vss-replacing-qdrant-v1 | Use DuckDB-vss instead of Qdrant | (compute layer) | Qdrant more mature, slot allocated; reassessable at first MCP integration |
| append-mode-scrape-history | scrape_results as append-only history | (table-scrape_results) | Adds complexity; separate scrape_attempts log table can be added later |
| tombstone-on-removal | Tombstone un-bookmarked rows | (table-bookmarks) | Hard delete simpler at v1; tombstones interesting at v2 |
| alembic-migrations-v1 | Schema migration framework at v1 | (storage) | Re-ingest is the recovery path; raw/ is source of truth |
| sql-trigger-fts5-sync | FTS5 sync via SQL triggers | (table-fts_bookmarks) | Multi-table denormalization triggers fiddly; app-layer cleaner |
| schema-migrations-from-day-one | Numbered migrations at v1 | (storage) | Overkill; "drop and re-ingest" is the recovery |

---

## 12. Saga dependency clarification

```yaml
entity_type: dependency_clarification
name: saga-dependency-shift
context: Original README.md framed Muninn as "blocked on Saga Phase 3". Spec design revised this on 2026-04-15.
```

- **Depends on Saga Phase 1+2 infrastructure (already done as of 2026-04-13):** credentials volume, keepalive cron, Trail of Bits devcontainer base image, dvergr container-launch pattern.
- **Does NOT depend on Saga Phase 3 orchestration (not yet shipped):** Odin's spec decomposition + multi-dvergr launch aren't needed at v1's single-instance synthesis volume.
- **Phase 3 is being used at implementation time** (this handoff IS the first Phase 3 decomposition run) — but the resulting Muninn artifact has no Phase 3 runtime dependency.
- **v2+ multi-source ingest** is when Saga Phase 3's parallel-dvergr orchestration becomes runtime-relevant for Muninn's synthesis side.

---

## 13. Open / deferred questions (acceptable to defer to consumer-stream time)

```yaml
entity_type: deferred_decisions
acceptable_to_defer: true
```

- **Embedding model choice** — deferred to first MCP integration (alongside Qdrant-vs-DuckDB-vss revisit).
- **Exact MCP tool surface** — sketched (`semantic_search`, `fts_search`, `get_bookmark`, `get_era`, `list_eras`); contract-specced when MCP consumer is built.
- **Vault page template** (`bookmark_page.md.j2` shape) — sketched in `README.md`; pinned when vault generator is built.
- **Timeline view JSON shape** — output shape spec'd when the consumer is built.

These don't block any of streams 1–4 starting.

---

## End notes for Odin

- Streams 1–3 are sequential; 4 + 5 fan out from Stream 3.
- Cross-stream contracts (schema, JSON Schemas, sanitize_url signature, domain_policy.yml schema, prompt-version naming) are stable and any change requires spec amendment, not silent stream-local override.
- Mimir's review focus on the decomposition: ensure each stream's success criteria are objectively verifiable from outside the stream (i.e., a black-box check), and that no stream silently relies on another stream's internal implementation details beyond the cross-stream contracts.
- This is the **first real Phase 3 run**; please record any template-format mismatches, decomposition friction, or Mimir verdict patterns in the post-run insights doc so future spec authoring can incorporate them.
