# RI-02 / RI-03 — bounded work plan

Date: 2026-10-21
Status: **accepted plan** (2026-10-21). P1 is in progress on branch
`adr-reset-x-first`; everything else is not started. It builds on the
stage-native RI-01 decision ([`RI-01-extension-spike.md`](RI-01-extension-spike.md))
and ADR-009/ADR-012. "Handoff" below means the Odin design handoff for
Research Intake (kept privately; it originates in Odin's `docs/design/`).

Each workstream below is written as a bounded assignment with these fields,
following handoff §7:

- inputs
- owned files and interfaces
- acceptance evidence
- budget and stop conditions
- reviewer

Three things can run in parallel:

- RI-02b (synthetic fixtures)
- RI-03 (records and contract)
- P (prerequisite maintenance)

RI-02a needs the operator, because it spends money and uses their X account.
RI-03 owns the shared schemas, and other workers do not redefine them.

```
P1 (deps) ──► P2 (migrator) ──────────────────┐
RI-02b (fixtures) ──┐             ├─► RI-04 (capture) ─► RI-05 … RI-06
RI-03 (records) ────┴─────────────┘
RI-02a (operator live qualification) ── feeds back into RI-03 v0.2 / RI-04 config
```

## P — prerequisite maintenance (muninn hygiene, not RI scope)

P is split in two so the dependency fixes can land before the migrator.
ADR-009 is accepted, so P2 starts as soon as P1 is merged.

### P1 — dependency and bug fixes (no decision needed)

- **Inputs:** RI-01 §6.
- **Owned:**
  - `pyproject.toml` and `uv.lock`
  - `src/muninn/scrape/{__init__,extract}.py`
  - `src/muninn/ingest/pipeline.py` (the `domain_policy` import)
  - `src/muninn/enrich/pipeline.py:309`
- **Work:** fix D1–D4 and D7 as separate commits, and add the
  import-isolation test.
  - D1 also protects every scrape, including the later browser-bookmark
    bulk run.
  - On D2, pin `mcp<2` and leave the 2.x port for later.
- **Acceptance evidence:**
  - The full suite passes on a fresh `uv sync`, without manual pins (438 tests after P1; the 17 `skald_lint` tests were retired with ADR-011).
  - `muninn mcp` imports.
  - The import-isolation test passes.
  - A test proves an extraction-backend import failure is fatal rather than
    returning `""`.
- **Budget/stop:** no network beyond package resolution. Stop and report if
  any existing test changes meaning, as opposed to being fixed.
- **Reviewer:** operator, or a fresh review agent, before merge.

### P2 — schema migrator (next after P1)

- **Owned:**
  - `src/muninn/db.py`
  - `schemas/sql/`
  - `schema.sql`
  - a new `muninn db migrate` command
  - `tests/conftest.py`
  - `scripts/init-db.py`
- **Work:** implement RI-01 §4: user_version, transactional numbered
  migrations, backup, `--dry-run`, the bootstrap stamp, and the
  behind/ahead checks. `conftest` creates test databases via migrate.
- **Acceptance evidence:**
  - The migration regression tests listed in RI-01 §10 pass.
  - The existing suite passes on migrated databases.
- **Budget/stop:** no network. Stop and report if any existing test has to
  change meaning to pass on a migrated database.

## RI-02 — qualify X access and capture strategy

### RI-02a — live qualification (operator-run)

- **Inputs:** desk research (§A below) and the operator's X account.
- **Authorization needed:**
  - creating an X developer app
  - buying credits. The console's minimum pack is expected to be about
    $5–10 and is unverified. The purchase is not the same as usage spend.
  - a **$5 usage-spend limit** set in the console. The expected usage for
    this procedure is under $1.
- **Procedure:**
  1. Create the app under the operator's own account, which owned-read
     pricing requires. Record the date, the required plan, the minimum
     purchase, and the per-endpoint prices the console shows.
  2. Run a local OAuth 2.0 PKCE flow with the scopes
     `tweet.read users.read bookmark.read offline.access`. Record the
     access-token TTL, whether the refresh token rotates on use, and the
     refresh-token TTL.
  3. Call `GET /2/users/me`, then fetch one bookmarks page
     (`max_results=100`, full field set, `expansions=author_id,attachments.media_keys,referenced_tweets.id`).
     Try both the `tweet.fields` and `post.fields` spellings. Record which
     names are accepted, all `x-rate-limit-*` headers, and the credit
     consumed, which shows whether `includes.users` is billed separately.
  4. Paginate to the end. Record the total count (to settle whether the
     historical 800 cap still applies), the ordering, and any short pages
     that still carry a `next_token`.
  5. Probe the folder endpoints. Record their shape (IDs only?) and how they
     are billed.
  6. Check edge cases against the X web UI:
     - Does a deleted, protected, or suspended-author bookmark appear in
       `data`, in `errors`, or not at all?
     - Is the full X Article body present?
     - Is `note_tweet` returned for long posts?
  7. Re-run the same UTC day and confirm deduplication leaves the bill at
     zero.
  8. In parallel, request the X data archive and record whether it contains
     bookmarks.
- **Record:** a results table in the private sidecar (`.private/muninn/evals/x-api-qualification/`, via dotprivate), not this
  public repo. Scrub tokens from raw responses before saving them, and keep
  those privately too. This repo gets only a summary of access result,
  limits and prices, with no corpus content.
- **Stop conditions:**
  - Usage spend reaches $5, or the minimum credit purchase exceeds $10.
  - Any step requires a paid subscription tier rather than pay-per-use.
  - The ToS would require scraping instead of the API.

  On any of these, stop and return to the operator with the findings.

### RI-02b — synthetic fixtures and connector contract (dev-run, no live access)

- **Inputs:** §A below and the documented X v2 response shapes.
- **Owned:**
  - `tests/fixtures/x/` (synthetic JSON pages plus header sidecars)
  - `src/muninn/ingest/x.py`, an X API client and page-iterator stub only
    (`BookmarkPage` iterator, checkpoint type, error taxonomy). It does not
    yet write to the database; RI-04 adds the upsert into `bookmarks` and
    `item_links`.
  - respx-based tests
- **Fixtures (all IDs and handles synthetic, labelled "documentation-derived, not recorded"):**
  - **Pages:** three pages chained by `next_token`, including a short page
    that still has a token and a terminal page.
  - **Post variants:** plain post; long post with `note_tweet`; post with an
    `article` object (with and without a body); quote post with
    `includes.tweets`; post with media.
  - **URL entities:** fully unwound; `unwound_url` null; t.co only;
    `status: 404`; a GitHub repository link; an `x.com/i/article` link.
  - **Errors:**
    - a 200 response carrying `errors[]` of types `resource-not-found`,
      `not-authorized-for-resource` and `resource-unavailable`
    - a 429 `rate-limit-exceeded` with reset headers
    - a 429 `usage-capped`, which is a hard stop rather than a retry
    - a 401 expired token, followed by a refresh that returns a rotated
      token
    - a 403 `client-forbidden`
  - **Folders:** a folder list, and an ID-only folder page.
- **Acceptance evidence:** tests prove that the connector stub:
  - treats only a missing `next_token` as the end
  - resumes from a persisted checkpoint after an injected mid-run failure
  - never logs a token (a secret-canary test)
  - turns `usage-capped` into an exhaustion status, not a retry loop
  - selects locators in the order `unwound_url` → `expanded_url` → `url`,
    and then runs them through `sanitize_url`
  - takes the 800 cap and both field-name spellings as configuration, not
    constants
- **Budget/stop:** no network and no credentials. Stop when RI-03 has not
  yet fixed a record field the stub needs. Use a local placeholder type and
  flag it; do not invent the field in the shared schema.
- **Reviewer:** the RI-03 owner, for record compatibility.

## RI-03 — specify records and the producer interface

- **Inputs:** handoff §5 and §8; RI-01 §3–§5; Odin's contract conventions
  (summarized in §B).
- **Owned:**
  - `docs/contracts/MUNINN-RESEARCH-INTAKE.md`, the producer contract.
    Header line: `Contract: muninn-research-intake, version 0.1.0-draft`.
    No relative Markdown links, because Odin copies it byte-for-byte.
  - `schemas/json/intake/*.schema.json`: JSON Schema **draft 2020-12**,
    `additionalProperties: false`, and an `$id` of the form
    `urn:muninn:research-intake:<record>:0.1`. Odin has no `$id`
    convention yet, so this sets one.
  - Valid and invalid fixtures under `tests/fixtures/intake/`.
  - A validation test in `tests/`.
  - A **draft** `schemas/sql/002_research_intake.sql`, validated only by
    applying it to an in-memory database; no runtime path uses it until
    RI-04. In the stage-native design (RI-01 §1) it adds:
    - `item_links`
    - append-only `scrape_history` and `assessments`
    - a sync-checkpoint table for the X adapter
    - any additive `bookmarks` columns that queries need

    It also settles the two open design points in RI-01 §3: where the
    post's own text lives, and the `enrichment_source` value for
    API-sourced content.
  - The packet exporter's interface, `src/muninn/consumers/packets/`, as a
    stub only, plus the `muninn export packets` CLI signature.
- **Records.** These are export-contract records. Each maps onto
  stage-native tables: a source item is a `bookmarks` row, and a content
  revision is a `scrape_history` row.
  - `source-item`
  - `item-link`
  - `bookmark-observation`
  - `content-revision`
  - `interest-profile`
  - `assessment`
  - `evidence-packet`
  - `usage-record`

  Applicability reviews, proposals and decisions are Odin-owned, and are
  **not** specified here.
- **Identity and revision rules to fix:**
  - **Item identity.** Internally, identity stays `UNIQUE(source, source_id)`
    on `bookmarks`: `source='x'` with the post id, and `source='x_link'`
    with the sha256 of the sanitized canonical locator. In exports, each item
    has a typed stable ID:
    - `x:post:<id>` for X posts
    - `github:repo:<owner>/<name>` for repositories
    - `web:<sha256 of sanitized canonical locator>` for web pages

    Parent/child links are explicit records, so canonical-URL dedup never
    collapses a post, its article and its repository into one item.
  - **Content revision** (a `scrape_history` row). Identity is
    `(item, content_sha256)`, where the
    digest covers the normalized extracted content. The revision records
    extractor name and version, retrieval method (`x_api`, `live`,
    `archive`), coverage (`full`, `partial`, `blocked`, `deleted`, `empty`),
    and the publication, retrieval and observation times. Unchanged content
    never creates a new revision.
  - **Assessment.** Append-only. Its key is content revision + profile
    revision + policy version + model + prompt version. A changed profile or
    policy creates a new assessment and leaves earlier ones in place.
  - **Disposition** is one of `candidate`, `reference_only`, `needs_evidence`,
    `deferred` or `rejected`. Retrieval failures are operational states and
    are never dispositions.
  - **Evidence packet.** Identity is a ULID plus an integer revision. The
    digest is sha256 over canonical JSON. ULIDs and sha256 match Odin's
    conventions; using RFC 8785 (JCS) for canonicalization is muninn's own
    proposal. A packet references content and assessment revisions
    by ID and digest, carries supporting and contradictory evidence,
    repository revisions inspected (commit SHAs) and unresolved questions,
    and must stay resolvable while referenced.
  - **Tombstone.** When source material is deleted (retention, or the
    operator's request), the packet's content is removed. A tombstone stays
    in its place: packet id, revision, digest, withdrawal reason and time.
    A reference to the packet then resolves to "withdrawn" instead of
    dangling.
- **Compatibility policy:**
  - SemVer, staying at `0.x-draft` until reviewed.
  - MINOR versions add optional fields only. MAJOR versions are breaking.
  - Every release gets an entry in a never-deleted Releases table with the
    sha256 of the contract and of every schema and fixture.
  - Status vocabulary: `proposed`, `validated` (its valid fixtures pass and
    invalid ones fail in this repo's test run), `implemented`.
- **Acceptance evidence:**
  - Every schema has at least one valid and one invalid fixture, and the
    test proves both behave correctly.
  - Fixtures cover a changed-content revision, a changed-profile
    reassessment, an old packet that still resolves, a blocked or partial
    capture assessed with coverage shown, and a canary secret that must not
    appear in any export.
  - The 002 DDL applies to a 001 database without changing existing tables.
- **Budget/stop:**
  - No model calls and no network.
  - Where a field depends on an RI-02a unknown (Article body, folder
    semantics), make it optional and list it as open.
  - Where a choice in RI-01 §9 is unresolved, stop and ask; don't assume.
- **Reviewer:** the operator, plus the Odin consumer side. Odin needs to
  generalize its `SNAPSHOT.schema.json`, which hard-codes the producer
  `saga`, before it can pin this contract. That is Odin's task and is not
  changed from here.

## §A — X API desk research summary (2026-10-21, unverified live)

These facts come from docs.x.com pages fetched on 2026-10-21. Those pages
are undated, so read them as "current on that date". Nothing below was
tested against a live account.

- **Endpoints:**
  - `GET /2/users/:id/bookmarks` and the two bookmark-folder endpoints
  - user-context auth only (OAuth 2.0 PKCE, or OAuth 1.0a user token)
  - no app-only bearer
- **Tokens:** an access token lasts 2 h. A refresh token requires
  `offline.access`.
- **Pagination:** `max_results` 1–100 with a `pagination_token`. There is no
  bookmark webhook or stream, so the connector must poll.
- **Rate limits:** 180 requests per 15 min per user for bookmarks; 50 for
  folders.
- **Pricing:** pay-per-use credits are now the default, with no free tier
  for this use.
  - Owned reads (the user owns the app) cost $0.001 per resource.
  - Standard reads cost $0.005 per post and $0.010 per user.
  - Billing is deduplicated per UTC day ("soft guarantee").
  - The cap is 3M post reads per month.
- **URL entities:** each carries `url` (t.co), `expanded_url`,
  `unwound_url` and `status`, all nullable.
- **Partial errors:** a 200 response can carry both `data` and `errors[]`.
- **429 errors:** a 429 is either `rate-limit-exceeded` (wait for the
  `x-rate-limit-reset` epoch) or `usage-capped` (a hard stop until the next
  cycle or a top-up).
- **Alternatives:**
  - Browser-extension and SaaS exporters scrape the logged-in web app,
    which conflicts with X's ToS, so we will not build on them.
  - The official data archive's bookmark coverage is unconfirmed.
- **Unverified (RI-02a settles):**
  - whether the historical 800-bookmark cap still applies
  - refresh-token TTL and rotation
  - how expansions and folders are billed
  - the minimum credit purchase
  - whether the full X Article text comes back
  - whether bookmarks are in the data archive
  - how deleted or protected bookmarks surface in responses

## §B — Odin consumer conventions this producer must meet

These are summarized from Odin's public `docs/contracts/README.md` as it
stood on 2026-10-21.

- **Ownership:** the producer owns the contract. Odin pins a byte-for-byte
  snapshot under `<producer>/<snapshot-id>/` with a `SNAPSHOT.json` receipt
  recording the producer path, commit and sha256.
- **Snapshot ID:** Odin chooses the snapshot identifier when it pins;
  muninn only needs to provide a commit and sha256 digests.
- **Status:** status is the producer's to declare. Nothing is labelled
  implemented or tested without evidence in the pinned copy.
- **Record conventions:** Odin's planned records use ULIDs, `rev` with
  `expected_revision` optimistic concurrency, sha256 hex digests and
  ISO-8601 UTC timestamps. Its assignments take inputs as `{ref, digest}`.
  The packet's export envelope therefore uses ISO-8601 UTC, even though the
  SQLite internals keep epoch ints.
- **No existing schema:** Odin has no evidence-packet or proposal schema.
  Muninn's packet schema is the first one.
