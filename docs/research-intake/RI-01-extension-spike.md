# RI-01 — Research Intake extension spike and decision

Date: 2026-10-21

Status: **accepted 2026-10-21**, in the revised stage-native form (§1, and
the revision note below). The ADRs are recorded in
[`muninn-decisions.md`](../muninn-decisions.md):

- ADR-009: stage-native Research Intake and explicit migrations
- ADR-010: muninn as a standalone tool
- ADR-012: X bookmarks first

Basis: the Odin design handoff for Research Intake. It is kept privately and
originates in Odin's `docs/design/`. This spike ran against this checkout at
`45296bf`.

This record is muninn's development task for RI-01. It labels what was
observed locally, what was decided, and what remains open.

> **Revision 2026-10-21.** The first draft proposed three new packages
> (`muninn.archive`, `muninn.sources.x`, `muninn.intake`), an `intake` CLI
> group and a parallel item/revision model. On review that split was
> unnecessary. Muninn is already organised as pipeline stages
> (`ingest → scrape → enrich → consumers`) over one source-agnostic
> `bookmarks` table, and the existing stages already provide the isolation the
> split was meant to buy. Research Intake is therefore a **workflow** over the
> existing stages: one new ingest adapter, one new enrichment task, one new
> export consumer, and append-only history tables. "Research Intake" remains
> the name of the workflow and of the producer contract for Odin
> (`muninn-research-intake`).

## 1. Decision

1. **Research Intake is a workflow inside muninn, built from the existing
   stages.** It adds no new package, repository, service, daemon, broker,
   dashboard or vector dependency. §8 records why no extraction is
   justified.
2. **Stage-native design:**

   | Need | Where it lives |
   |---|---|
   | X capture | `src/muninn/ingest/x.py`, beside the Netscape parser. `muninn ingest x` writes `bookmarks` rows with `source='x'` and `source_id=<post id>`. Author, publication time and post text go in `source_metadata`; additive columns are used only if queries need them |
   | Linked articles and repos | Their own `bookmarks` rows (`source='x_link'`), joined to the post by a small `item_links` table (parent, child, relation, position). Every locator goes through `sanitize_url` |
   | Fetching linked material | The existing `muninn scrape`. The live pass is the current evidence, and the archive passes stay labelled historical (§5) |
   | Revisions | Two append-only tables. `scrape_history` holds one row per fetch with a content digest, extractor name and version, and coverage; SPEC already anticipated this ("a separate `scrape_attempts` log table can be added non-breakingly"). `assessments` holds one row per suitability run. The current-state tables (`scrape_results`, `enriched`) keep upserting, so the browser-bookmark lane is unchanged |
   | Suitability assessment | A second enrichment task, `enrich/prompts/suitability_v1.md`, run by `muninn enrich --task suitability`. Its idempotency key is the existing triple (model, prompt version, content hash) plus the interest-profile digest |
   | Interest profile | A versioned YAML file, like `domain_policy.yml`. Its sha256 digest is its revision |
   | Evidence packets | A consumer beside Parquet and the timeline, `src/muninn/consumers/packets/`. `muninn export packets` writes content-addressed JSON (sha256) validated by `schemas/json/intake/evidence-packet.schema.json` |
   | Vault and search | No extra work: X bookmarks compile to `wiki/bookmarks/` and are indexed like any other bookmark |
   | Schema changes | Explicit migrations (§4), run by `muninn db migrate` |

3. **Identifiers.** No new packages. The CLI is the existing stage commands
   plus `muninn ingest x`, `muninn enrich --task suitability`,
   `muninn export packets` and `muninn db migrate`. The producer contract is
   `muninn-research-intake`.
4. **Checkout.** Development happens in this repository on feature branches
   off `main`.
   - The GitHub remote is public, so only synthetic fixtures and design docs
     go there.
   - Private material (live X responses, the Odin handoff, account details)
     goes to the dotprivate sidecar (`.private/muninn/`).
5. **Prerequisites before capture code (RI-04):**
   - **P1:** the dependency fixes in §6. In progress on branch
     `adr-reset-x-first`.
   - **P2:** the migrator in §4. Next after P1; ADR-009 is accepted.

## 2. Evidence gathered by this spike

All results below are local observations made on 2026-10-21. They involved
no credentials, no homelab contact and no real corpus.

Receipts:

- **Environment.** A scratch venv was built with
  `uv venv --python 3.12 && uv pip install -e '.[dev]'`. The fresh resolve
  pulled selectolax 1.0.1 (later replaced by 0.4.13), anthropic 2.0.0,
  mcp 2.3.0, duckdb 2.0.0, httpx 0.28.1 (plus httpx2 2.16.0, which
  anthropic and mcp pull in) and pytest 9.1.1. SQLite was 3.53.1.
- **Test command.**
  `env -u ANTHROPIC_API_KEY MUNINN_EMBEDDING_BACKEND=hash MUNINN_DATA_DIR=<scratch>/data MUNINN_RAW_DIR=<scratch>/raw MUNINN_DB_PATH=<scratch>/data/muninn.db python -m pytest -q -p no:cacheprovider`
- **Network.** On first run the DuckDB tests download the `sqlite_scanner`
  extension once.

| Check | Result |
|---|---|
| Test suite, fresh scratch venv (Python 3.12, `.[dev]`, `MUNINN_EMBEDDING_BACKEND=hash`) | **443 passed, 3 failed.** All three dual-pass scrape tests fail because of `selectolax` 1.0.1 (defect D1, §6) |
| Same suite with `selectolax<1.0` (0.4.13) | **446 passed, 0 skipped**, in about 34 s. This is the archival regression baseline |
| `import muninn.consumers.cli.main` (`-X importtime`) | About 80 ms. Loads only click, `config` and `db`. No qdrant, duckdb, pyarrow, mcp, anthropic, jinja2 or torch |
| `import muninn.sanitize` | Stdlib only. Pure, and reusable as-is |
| `import muninn.scrape` / `muninn.ingest` | About 0.5 s each. `scrape/__init__.py` eagerly imports client and pipeline, and `ingest` reaches it just to read `domain_policy` |
| `import muninn.consumers.mcp.server` on a fresh resolve | **Fails.** `mcp>=1.0` resolves to 2.x, which renamed `FastMCP` (D2) |
| `schema.sql` vs `schemas/sql/001_initial.sql` | Byte-identical. Only `schema.sql` is read (`db.py:13`) |
| Migration support | None. `user_version` is 0 and every statement is plain `CREATE`, so `init_db()` fails on an existing DB. Its docstring says "Idempotent" and then notes that a second call errors |
| Revision history in existing tables | None. `bookmarks`, `scrape_results` and `enriched` all upsert in place. Only `analyses` and `synthesis_runs` are append-only |
| DuckDB ATTACH of a DB with extra STRICT tables, a second FTS5 table and `user_version=2` | Works (DuckDB 2.0). New tables do not break the analytics consumers |

## 3. Reuse inventory

| Existing piece | Reuse | Change needed |
|---|---|---|
| `sanitize.sanitize_url` (`sanitize/url.py:42`) | As-is for every locator: X `unwound_url`/`expanded_url`, linked articles, repos | Redaction metadata records param names and path/userinfo flags, but not *why*. RI-03 decides whether auditability needs a rule identifier |
| `ingest/pipeline.py` upsert on `UNIQUE(source, source_id)` | The X adapter writes through the same upsert shape | Identity is the X post id, not a raw-URL hash |
| `scrape.client.ScrapeClient`, `HttpCache`, `RateLimiter`, `detect_auth_wall`, `extract.extract_with_quality`, `DomainPolicy` | Used as-is for linked items | **D9:** `HttpCache.get()` never expires an entry (`http_cache.py:40-48`), and live keys are just `live:{url}` (`client.py:119`), not the "URL+date" in SPEC.md@45296bf:314. Re-capturing a revision needs a cache bypass or a time-bucketed key. The extraction method and version are never recorded; `scrape_history` must record them |
| `scrape.pipeline.scrape_one` and its passes | Reused for `x_link` rows | Writes one `scrape_history` row per fetch, alongside the existing upsert. For research selection the live pass is canonical (§5) |
| `enrich` pipeline and idempotency triple (`enrich/idempotency.py`) | `--task suitability` reuses the worker pool, client and skip logic | The key gains the interest-profile digest. Output goes to append-only `assessments`, never to `enriched` |
| CLI registration (`cli.add_command`, lazy imports inside commands) | New subcommands are added the same way | None |
| `consumers/cli/triage.py` | Not reused. It is pipeline-health triage, not suitability | Avoid the name collision in docs and help text |
| Synthesis container and Saga credentials path | **Not used** | None |

Rules that replace the dropped package boundaries:

- **Failure isolation comes from the stages.** Capture (`ingest x`,
  `scrape`) never calls a model. `enrich --task suitability` has its own
  `--limit` and budget flags. A model outage, an exhausted budget or a missing
  `anthropic` package cannot stop capture.
- **Bounded selection.** Ingest never triggers assessment. Assessment runs
  only on rows the command selects, using source, date range and limit.
  Older browser bookmarks can be selected the same way, but a 10k-row import
  never queues assessments.
- **Odin boundary.** Muninn exports content-addressed packet files. Odin
  stores them as `{ref, digest}` inputs, and neither side reads the other's
  database.

Open design points carried into RI-03:

- **Where the X post's own text lives.** Options are `source_metadata` or a
  new `scrape_results.pass` value. A new pass value would need a
  table-rebuild migration, because SQLite cannot alter a CHECK constraint.
- **The `enrichment_source` value for API-sourced content.**

## 4. Migration and rollback plan

SPEC.md@45296bf:50 and :225 rejected a migration framework because recovery was
"drop DB, re-ingest". That no longer holds: paid enrichments, Wayback
captures and revisioned intake evidence cannot be rebuilt from `raw/`.
SPEC.md@45296bf:1441–1443 ("Migration story") anticipated this: multi-source ingest
"will force the migration question — handle it then". The replacement is a
small built-in migrator of roughly 60 lines in `db.py`, not Alembic.

1. **Numbered migrations.**
   - `schemas/sql/NNN_*.sql` are canonical. `schema.sql` stays as a mirror of
     `001`, guarded by an identity test, or is retired in favour of `001`.
   - `PRAGMA user_version` records the applied version.
   - Each migration runs inside `BEGIN IMMEDIATE … COMMIT`, together with the
     `user_version` bump. `executescript` is not used, because it
     auto-commits.
2. **Bootstrap.** A database at `user_version=0` that already has
   `bookmarks` is stamped as version 1. `init_db()` becomes "create if
   empty, then migrate".
3. **Explicit, never implicit.** Migration runs only via
   `muninn db migrate [--dry-run]`. It never runs silently, because the CLI,
   MCP and DuckDB readers share the file under WAL.
   - When the schema is **behind** the code, `connect()` fails fast.
   - When the schema is **ahead** of the code (after a rollback to an older
     build), `connect()` warns and continues. Additive migrations are safe for
     older code to ignore. Any non-additive migration must bump a separate
     "minimum reader version", which older builds refuse.
4. **Backup first.** Before migrating, `conn.backup()` copies the file to
   `muninn.db.bak-v{old}-{ts}`. After migrating, `PRAGMA integrity_check` and
   `foreign_key_check` must pass.
5. **Rollback.** Restore the backup. For purely additive migrations, a
   pre-intake build can also simply ignore the extra tables and columns. There
   are no down-migrations.
6. **Constraints on new DDL.**
   - **No semantic changes to existing columns.** `ADD COLUMN` (nullable or
     defaulted) is allowed.
   - **Extending a CHECK enum needs a table-rebuild migration:** create the
     new table, copy the rows, verify row counts and foreign keys, then swap
     it in. The bookmark-lane regression checks (§10) must pass.
   - **Links to `bookmarks` from new tables** are nullable, with
     `ON DELETE SET NULL`.
   - **Full-text search.** If one is needed, it gets its own content-storing
     FTS5 table with application-layer sync. Never use `contentless_delete`,
     because it breaks DuckDB ATTACH (`schema.sql:162-166`).
   - **Conventions from 001 stay:** epoch ints, `json_valid` CHECKs, and TEXT
     enums with CHECK.

## 5. Freshness policy

Research selection uses the **current** capture: the X API response, plus a
live fetch of linked material at sync time. Wayback passes are available
only as explicitly labelled historical comparisons. Every history row
records three separate times:

- the source publication time
- the retrieval time
- the bookmark observation time, when known

"Recently bookmarked" never implies "newly published". The browser-bookmark
lane keeps its existing at-capture-first behaviour unchanged.

## 6. Dependencies and pre-existing defects

These defects were found during the spike. Each is fixed as a separate
commit under P1, except D5, which P2 supersedes, and D6/D9, which are
deferred as noted.

| ID | Defect | Severity | Fix |
|---|---|---|---|
| D1 | `selectolax>=0.3` resolves to 1.0.x, which removed `selectolax.parser.HTMLParser`. `scrape/extract.py:25-28` catches the ImportError, and `extract_text()` then returns `""` for every page. Fresh installs silently degrade every scrape to `partial` or `none` | **High** (silent data loss) | Pin `<1`, or port to `selectolax.lexbor`. Make the import failure fatal instead of degrading |
| D2 | `mcp>=1.0` resolves to 2.x, where `mcp.server.fastmcp` no longer exists, so `muninn mcp` cannot import | High | Pin `mcp>=1.0,<2`, then port deliberately |
| D3 | `enrich --prompt-version vN` records `vN` but sends the v1 prompt (`enrich/pipeline.py:309` omits `prompt_version`) | Medium (corrupts the idempotency record) | Pass the version through, and add a test |
| D4 | No lockfile. `anthropic` resolves to 2.x on a fresh install, which is untested and uses a different httpx stack from the scraper | Medium | Commit `uv.lock`, and bound `anthropic` to the tested major version |
| D5 | The `init_db()` docstring contradicts itself ("Idempotent", yet a second call errors), and `scripts/init-db.py --force` deletes the DB | Low | Superseded by §4 |
| D6 | The deep pass overwrites `enriched`'s idempotency triple, so the next `muninn enrich` re-enriches | Low (pre-existing) | Out of scope for Research Intake. Noted in Known gaps |
| D7 | `aiosqlite` is declared but unused, and `pyarrow` is used only by tests | Low | Drop `aiosqlite`, move `pyarrow` to `dev` |
| D8 | The DuckDB tests download `sqlite_scanner` once from DuckDB's extension server | Info | Document it for offline runs |
| D9 | The live HTTP cache never expires and is keyed by URL only, not URL+date as SPEC.md@45296bf:314 states | Low for bookmarks; blocks intake re-capture | Time-bucketed key or explicit bypass. Fix in RI-04 |

Dependency direction for the X slice:

- **Needed:** `click`, `pydantic`, `pyyaml`, `httpx`, `selectolax` and
  `jsonschema`, plus `anthropic` for enrichment and assessment.
- **Not needed for capture, assessment or export:** `qdrant-client`,
  `sentence-transformers`, `duckdb`, `pyarrow`, `jinja2` and `mcp`. They
  remain needed for indexing, vault compile and MCP.

**Sequencing:**

1. **Now (P1).**
   - Make `scrape/__init__.py` lazy, or move `domain_policy` to
     `muninn/policy.py`.
   - Add an import-isolation test. It imports the CLI, `muninn.ingest`,
     `muninn.enrich` and the packets consumer, then asserts that none of
     `qdrant_client`, `duckdb`, `pyarrow`, `mcp`, `jinja2`,
     `sentence_transformers` or `torch` is in `sys.modules`.
   - Keep the default dependency set unchanged for backwards compatibility.
2. **Later, optionally.** Slim the core into extras: `[llm]`, `[vector]`,
   `[export]`, `[vault]`, `[mcp]`, and `[pipeline]` (equal to today's
   install).

## 7. Reconciliation with muninn's recorded decisions

The changes below are recorded in [`muninn-decisions.md`](../muninn-decisions.md)
as of 2026-10-21.

| Earlier decision | Tension | Resolution |
|---|---|---|
| ADR-003 (Huginn separate) | Research Intake does intake and triage inside muninn | **ADR-015:** Huginn is deferred indefinitely. Its cadence and failure-isolation argument survives as a design rule in ADR-009 (separate stages, separate budgets) rather than as a service boundary. Automatic discovery returns only as a new source, under its own ADR |
| ADR-005 (v1 = browser bookmarks only) | X is the first source | **ADR-012:** v1 is X bookmarks end to end. The browser-bookmark bulk run moves to v1.1 |
| ADR-004/007 (Bragi-authored project state; muninn as a Saga workload) | Odin owns project state, Skald archives transcripts, and Research Intake must not require Saga | **ADR-011:** muninn holds no canonical project or session state. **ADR-010:** muninn is a standalone tool, with no Saga dependency at runtime, build or test time |
| ADR-008 (two lanes) | Where intake data lives | **ADR-008 (accepted, revised):** intake data is in the captured lane. SQLite is canonical and packets are derived exports. No new lane |
| SPEC "drop DB, re-ingest" (`@45296bf` :50, :225, :1441–1443) | Revisioned evidence is not reproducible from `raw/` | **ADR-009:** explicit migrations (§4) |
| SPEC "append-mode scrape history rejected at v1" (:223) | Revisions are required | **ADR-009:** append-only `scrape_history` and `assessments`. `scrape_results` keeps upserting |
| Huginn spec "Not X/Twitter ingestion" (`docs/history/huginn-spec.md`) | X is the first source | Its Nitter and third-party-access reasons do not apply: this uses X's official API with user-context OAuth. Its "maintenance burden" reason does apply, and is managed through the RI-02a stop conditions, the bounded slice, and RI-08's maintenance-time measurement |
| SPEC "Respect robots.txt" / Known gap 4 | Linked-article capture fetches arbitrary origins | robots.txt handling becomes an RI-04 acceptance item for the live-fetch path |

## 8. Extraction justification

No extraction is proposed. None of the handoff's reasons for a separate
evaluator applies today:

- There is no consumer besides Odin.
- There is no separate deployment requirement.
- There is no measured maintenance cost.

Revisit only if one of those appears.

## 9. Choices

| Choice | Status |
|---|---|
| Module and CLI identifiers | **Resolved:** no new packages. The CLI is the existing stage commands plus `muninn ingest x`, `muninn enrich --task suitability`, `muninn export packets` and `muninn db migrate` |
| First-release source scope | **Resolved:** explicit X bookmarks only, with no automatic discovery (ADR-012, ADR-015) |
| Consumers besides Odin in release 1 | **Resolved:** none required. Packets are plain files any consumer can read |
| Sequencing against the browser-bookmark v1 | **Resolved:** X first (ADR-012) |
| X access method, cost and limits | **Open.** Proposed: the official API, pay-per-use, OAuth 2.0 PKCE in user context. Qualified per RI-02 |
| Evaluation model and spending caps | **Open.** Proposed: Anthropic API, with the model and per-run and monthly caps set in RI-05 from fixture calibration |
| Raw-content retention | **Open.** Proposed: keep extracted text and digests for every revision a packet references. Raw HTML follows the existing spill rules. Deleting source material removes the content from derived packets but leaves a **tombstone** holding the packet id, revision, digest, withdrawal reason and time. RI-03 fixes the tombstone shape |
| Human review surface and the meaning of approval | **Open,** and owned by Odin (RI-07). Outside muninn's scope |

## 10. Archival regression checks (must stay green from P2 onward)

- The full existing suite (438 tests after P1: the 446-test baseline, minus 17 retired `skald_lint` tests, plus 9 new P1 tests) passes on a migrated-to-head DB, with
  `conftest` creating databases via migrate.
- A migration test applies 001, seeds bookmarks, enriched rows and FTS rows,
  migrates to head, and asserts that row counts and contents are unchanged
  and that a second migrate is a no-op.
- Fresh-to-head and 001-plus-migrations produce identical `sqlite_master`.
- `test_duckdb_roundtrip` runs against the head DB.
- The import-isolation test (§6) passes.
- `muninn status`, `triage`, `vault compile` and `export` produce unchanged
  output on the fixture corpus.
