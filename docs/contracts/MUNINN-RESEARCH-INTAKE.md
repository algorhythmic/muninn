# Muninn Research Intake producer contract

Contract: muninn-research-intake, version 0.1.0-draft

> **Status:** draft, written 2026-10-21 for roadmap item RI-03. It has not
> been reviewed by the operator or by the Odin consumer side. The record
> schemas are **validated**: their valid fixtures pass and their invalid
> fixtures fail in muninn's own test run. Capture, assessment and the
> exporter are **not implemented**; nothing here has run against live X
> data, a model, or Odin. Section 12 gives the status of each part, and
> section 14 lists every open question.

**Producer.** Muninn, a standalone personal archive of saved material
(public repository `github.com/algorhythmic/muninn`). This file is
canonical at `docs/contracts/MUNINN-RESEARCH-INTAKE.md` in that repository.

**Authority.** Muninn owns this contract. Where muninn's decision log
(`docs/muninn-decisions.md`, ADR-009 and ADR-012) and this contract
disagree, the decision log wins and this contract is corrected under a new
version. A consumer requirement not stated here is a request to the
producer, not an obligation of muninn.

**Self-contained.** Odin copies this file byte-for-byte. It therefore uses
no relative links; repository paths are written as code and are relative
to the muninn repository root.

## 0. Reading this contract

### Status labels

| Label | Meaning |
|---|---|
| `proposed` | Specified here, with no executable evidence yet |
| `validated` | The schema exists, and its valid fixtures pass and its invalid fixtures fail in muninn's test run |
| `implemented` | Running code produces or enforces it, with tests |

Nothing is labelled `validated` or `implemented` without that evidence in
the repository at the pinned commit.

### Normative files

| File | Role |
|---|---|
| `docs/contracts/MUNINN-RESEARCH-INTAKE.md` | This contract |
| `schemas/json/intake/*.schema.json` | One JSON Schema per record, plus `defs.schema.json` (shared value types) |
| `tests/fixtures/intake/<record>/*.valid.json`, `*.invalid.json` | Synthetic fixtures. Each wraps a `record` with `fixture` metadata (`synthetic`, `expect`, `covers`, `why`; invalid fixtures also give `fails_at` and `fails_with`) |
| `tests/fixtures/intake/generate.py` | Regenerates the fixtures, which are interlinked by digest |
| `docs/contracts/MUNINN-RESEARCH-INTAKE-0.1.0-draft.sha256` | Release manifest: the sha256 of this file and of every schema and fixture (section 11) |

Informative, not part of the consumer contract: `schemas/sql/002_research_intake.sql`
(muninn's internal storage) and `src/muninn/consumers/packets/` (the
reference implementation of the digest, identity and resolution rules).

### What this contract is not

- It does not specify Odin's records. Applicability reviews, proposals,
  decisions and the human review surface (RI-07) are Odin-owned.
- It is not an API. Muninn exports files, and neither side reads the
  other's database.
- It does not describe running software. The exporter is a stub.

## 1. Scope and boundary

Research Intake is a muninn workflow (ADR-009). It captures selected
external material (in v1, the operator's X bookmarks with their linked
articles and repositories), assesses its suitability against an interest
profile, and exports **evidence packets**. Odin consumes those packets for
applicability review, proposals and human decisions.

- **Producer guarantees.** Every packet is schema-valid, content-addressed,
  and resolvable for as long as it is referenced. A withdrawn packet
  resolves to a tombstone, never to a dangling reference. No exported
  record contains a credential, a raw (unsanitized) locator, or content
  the operator has hidden.
- **Transport.** Plain files in a directory (section 6.3). The workflow runs
  without Odin deployed.
- **Consumer obligations.** Pin a snapshot of this contract. Store packets
  as `{ref, digest}` inputs. Verify digests. Treat `withdrawn` as a final
  state for that revision.

## 2. Conventions

- **Schemas.** JSON Schema draft 2020-12. Every object is closed
  (`additionalProperties: false`). The `$id` of each schema is
  `urn:muninn:research-intake:<record>:<MAJOR>.<MINOR>`, here `...:0.1`.
  Schemas reference each other by `$id`, so a validator needs all nine
  files loaded into one registry.
- **Numbers.** Integers only, within ±(2^53 − 1). There are no floats
  anywhere. Scores are integers 0–100, and money is integer micro-USD.
- **Times.** ISO-8601 UTC, second precision, `Z` suffix
  (`2026-10-20T08:00:00Z`). Muninn's database keeps epoch seconds; the
  export converts them.
- **Identifiers.** ULIDs (26 characters of Crockford base32) for
  revisions, assessments, packets, usage records and sync runs. Typed
  string IDs for items (section 3).
- **Digests.** Lowercase hex sha256.
- **References.** `{ref, digest}` objects, following Odin's convention:

  | Reference to | `ref` | `digest` |
  |---|---|---|
  | content revision | `revision_id` (ULID) | its `content_sha256` |
  | assessment | `assessment_id` (ULID) | sha256 of the assessment's canonical JSON |
  | interest profile | `profile_id` | sha256 of the profile's canonical JSON |
  | evidence packet | `<packet_id>@<revision>` | sha256 of the packet's canonical JSON |

- **Canonical JSON.** UTF-8 bytes of
  `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
  Muninn proposes RFC 8785 (JCS) as the canonicalization. On the subset
  these schemas allow, the two produce identical bytes:
  - Key order: JCS sorts by UTF-16 code units, Python by code points. The
    orders differ only for keys containing characters above U+FFFF. Every
    key is a schema-defined ASCII name, and the reference implementation
    rejects such keys.
  - Numbers: JCS uses ECMAScript number serialization, which for safe
    integers is the plain decimal form Python emits. Floats are excluded.
  - Strings: both escape only `"`, `\` and control characters, using the
    same short escapes and lowercase `\u00xx` for the rest.

  So a consumer may verify digests with any JCS implementation, or with
  `sha256sum` on an exported file, which is written in canonical form.
- **Text normalization `muninn-text/1`.** Unicode NFC; CRLF and CR become
  LF; trailing whitespace is stripped from each line; runs of three or
  more newlines collapse to two; leading and trailing whitespace is
  stripped. `content_sha256` is the sha256 of the normalized text's UTF-8
  bytes. A capture with no readable content has the empty-string digest
  `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`.
- **Locators.** Every URL in a record is the output of muninn's
  `sanitize_url`. That function lowercases the host, strips userinfo,
  tracking parameters and credential-shaped parameters, and sorts the
  query. The original URL is never stored after ingest. As a defence in
  depth, the `locator` schema rejects any URL that still carries a common
  credential parameter name.

## 3. Identity

### 3.1 Items

Internally an item is one `bookmarks` row, unique on `(source, source_id)`.
Each item has one typed export ID:

| Item | `bookmarks.source` | `bookmarks.source_id` | Export `item_id` | Status |
|---|---|---|---|---|
| X post | `x` | post ID | `x:post:<id>` | validated |
| X Article | `x_article` | article ID | `x:article:<id>` | **proposed** (open: RI-02a article body) |
| GitHub repository | `x_link` | sha256 of the sanitized canonical locator | `github:repo:<owner>/<name>` | validated |
| Web page | `x_link` | sha256 of the sanitized canonical locator | `web:<same sha256>` | validated |

Rules:

- **Canonical locator.** It is chosen from the X URL entity in the order
  `unwound_url`, then `expanded_url`, then the `t.co` `url`. The chosen
  value is passed through `sanitize_url`, and the result is the canonical
  locator. The item link records which field was used
  (`locator_resolution`).
- **GitHub.** A canonical locator of the form
  `https://github.com/<owner>/<name>` becomes `github:repo:<owner>/<name>`,
  lowercased. A trailing `/` or `.git` is ignored. Site sections such as
  `/topics` or `/orgs` are excluded. Any other GitHub URL, such as a file
  inside a repository, is a `web:` item.
- **No merging.** A post, its article and its repository are always
  separate items, joined by explicit item-link records. Canonical-locator
  deduplication applies only to identical linked locators: two posts
  linking the same page share one `web:` item with two links.
- An `x_link` row's `source_id` and its `web:` ID are the same digest, so
  the internal and export identities can be converted in both directions
  without a lookup.

### 3.2 Revisions, assessments, packets

| Thing | Identity | Digest |
|---|---|---|
| Content revision | `revision_id` (ULID). Unique on `(item_id, content_sha256, coverage)` | `content_sha256` |
| Interest-profile revision | `(profile_id, digest)` | sha256 of the profile's canonical JSON |
| Assessment | `assessment_id` (ULID). Unique on (content revision, profile digest, `policy_version`, `model`, `prompt_version`) | sha256 of its canonical JSON |
| Evidence packet | `packet_id` (ULID) + integer `revision` ≥ 1 | sha256 of its canonical JSON = sha256 of the exported file |

## 4. Records

Field-level definitions are in the schemas. This section gives each
record's purpose, its internal home and the rules a schema cannot express
alone.

| Record | Schema `$id` | Internal home (draft 002) | Status |
|---|---|---|---|
| `source-item` | `urn:muninn:research-intake:source-item:0.1` | `bookmarks` row | validated |
| `item-link` | `urn:muninn:research-intake:item-link:0.1` | `item_links` row | validated |
| `bookmark-observation` | `urn:muninn:research-intake:bookmark-observation:0.1` | `bookmarks.captured_at` + `source_metadata` | validated |
| `content-revision` | `urn:muninn:research-intake:content-revision:0.1` | `scrape_history` row | validated |
| `interest-profile` | `urn:muninn:research-intake:interest-profile:0.1` | the profile YAML file; every used revision in `interest_profiles` | validated |
| `assessment` | `urn:muninn:research-intake:assessment:0.1` | `assessments` row | validated |
| `evidence-packet` (active and withdrawn forms) | `urn:muninn:research-intake:evidence-packet:0.1` | exported file + `evidence_packets` row | validated |
| `usage-record` | `urn:muninn:research-intake:usage-record:0.1` | `usage_records` row | validated |

Every record carries a constant `record_type` equal to its name.

### 4.1 source-item

One captured item: `kind` is `x_post`, `x_article`, `github_repo` or
`web_page`, and must agree with the `item_id` prefix. It carries the
sanitized `locator` (null if sanitization rejected every candidate), the
`title`, the source `published_at` (null when the source states none, and
never inferred from the bookmark time), and `redaction`. The redaction
object holds only the *names* of removed parameters and the path and
userinfo flags, never their values. X posts may add `author` (platform
ID, handle, display name) and `x_post` (`is_long_post` when the text came
from `note_tweet`, `conversation_id`, `has_media`, `possibly_sensitive`).
Repositories add `repository` (`owner`, `name`).

### 4.2 item-link

An explicit parent-to-child relation. The `relation` values are:

- `links_to`: a URL entity in the parent
- `quotes` and `replies_to`: a referenced post, by ID
- `article`: an X Article attached to the post

`position` is the 0-based URL-entity index for `links_to`, and 0
otherwise. `locator_resolution` is `unwound`, `expanded` or `short` for
`links_to`, and `api_reference` for `quotes` and `replies_to`.
`entity_status` is the HTTP status X reported on the entity, if any. The
parent and child must differ.

### 4.3 bookmark-observation

When the operator's bookmark of an item was observed. This is the third
of the three separate times (section 8). `observed_at_basis` is
`source_reported` when the source states the bookmark time (a browser
`ADD_DATE`), and `first_seen_by_sync` when it is muninn's first sighting.
For X the basis is always `first_seen_by_sync`: the X API gives no bookmark
time, so `observed_at` is only an upper bound on when the bookmark was
made. The optional fields are:

- `last_observed_at`
- `sync_run_id`
- `folder_ids` (open)
- `list_position` (open)

### 4.4 content-revision

One distinct captured content state of an item. Rules:

- **Unchanged content never creates a revision.** A re-fetch whose
  normalized text and coverage match an existing revision of the same item
  adds nothing.
- **Coverage** is `full`, `partial`, `blocked`, `deleted` or `empty`.
  `blocked`, `deleted` and `empty` revisions carry no content: `char_count`
  is 0 and the digest is the empty-string digest. An auth-wall or error
  page is not the item's content. Coverage is part of the revision key, so
  the three empty states stay distinct.
- **Retrieval method** is `x_api` (the X API response), `live` (an HTTP
  fetch at sync time) or `archive` (a web-archive capture).
- **Freshness** is `current` for `x_api` and `live`, and `historical` for
  `archive`. An archive revision must carry `archive_snapshot_at`. Research
  selection uses current revisions; historical ones are comparisons only.
- `extractor` names the extractor and its version.
- `text` is optional. When present it is the normalized text, and its
  sha256 equals `content_sha256`.

### 4.5 interest-profile

One revision of the operator's interest profile: `interests`, each with a
`weight` from 0 to 100, plus optional `exclusions`. The profile is
authored as a YAML file. Its revision is the sha256 of the parsed
record's canonical JSON, not of the file bytes, so editing a comment or
whitespace does not trigger reassessment. This is a refinement of RI-01's
"file digest" wording. Every revision any assessment has used is kept, so
old assessments and packets stay resolvable.

### 4.6 assessment

One append-only suitability judgement.

- **Key.** Content revision + profile digest + `policy_version` + `model`
  + `prompt_version`. A changed profile, policy, model or prompt creates a
  new assessment, and earlier assessments stay in place.
  `prior_assessment` optionally points at the assessment this one
  re-evaluates.
- **Disposition** is one of `candidate`, `reference_only`,
  `needs_evidence`, `deferred` or `rejected`. Retrieval failures are
  operational states and are never dispositions.
- **Coverage** of the assessed revision is copied into the assessment, so
  the judgement always shows how much content it saw. An assessment of a
  `blocked`, `deleted` or `empty` revision may not be `candidate`. This
  rule is **proposed** here, and it is enforced by both the schema and the
  database.
- `relevance` and `confidence` are integers from 0 to 100.
  `matched_interests` are `interest_id` values from the referenced profile
  revision.

### 4.7 evidence-packet

Section 6.

### 4.8 usage-record

One paid call or batch of calls. `kind` is `x_api` or `model`; `provider`
is a slug such as `x` or `anthropic`. `operation` is an endpoint template
(`GET /2/users/:id/bookmarks`) or a task name (`enrich:suitability`). It
is never a full request URL, so it cannot carry a cursor or token.

The record also carries `request_count`, an optional `resource_count`
(billed posts and users), and token counts (model calls only). Cost is
`cost_micro_usd`, with `cost_basis` set to `estimated` (from a price
table) or `reported` (from the provider). A usage record can be linked to
its sync run or its assessment.

## 5. Revision and append-only rules

- `scrape_history` (content revisions), `assessments`, `interest_profiles`
  and `usage_records` are append-only. The one permitted change is
  **withdrawal**: the content is cleared and `withdrawn_at` and
  `withdrawal_reason` are set.
- The current-state tables (`bookmarks`, `scrape_results`, `enriched`)
  keep upserting, so the browser-bookmark lane behaves as before.
- An assessed revision cannot be deleted, only withdrawn. A deleted
  `bookmarks` row sets the history rows' link to NULL. The history rows
  keep the typed `item_ref`, so they still identify the item.
- **Known limitation.** Revisions form a content-addressed set, not a
  timeline. If content changes from A to B and back to A, no third
  revision is created, and A's `retrieved_at` stays its first retrieval.
  The current state is in `scrape_results`.

## 6. Evidence packets

### 6.1 Active form

A packet is about one `subject_item_id`, normally the bookmarked X post.
It carries the following, by value:

- `items` (the subject and its linked items)
- `item_links`
- `observations`
- `content_revisions`
- `interest_profiles` (every profile revision an included assessment
  references)
- `assessments`

It also carries:

- `primary_assessment`, a `{ref, digest}` to the included assessment that
  qualified the packet
- `evidence.supporting` and `evidence.contradictory`. Each entry is a
  statement plus a `{ref, digest}` to a content revision, with an optional
  verbatim `excerpt`.
- `repository_revisions`: the GitHub items and commit SHAs inspected
- `unresolved_questions`
- optionally, `usage`

Cross-record rules (checked by `packet_integrity_errors` in the reference
implementation):

- Every reference resolves inside the packet, with a matching digest.
- Each assessment's `item_id` and `coverage` equal those of its revision.
- `matched_interests` belong to the referenced profile revision.
- Included `text` is normalized and matches its digest.
- An `excerpt` occurs in its revision's text.
- The subject and every link endpoint are in `items`.

Excluded content: items the operator has hidden (`content_visible = 0`)
are never exported.

### 6.2 Revisions

`revision` starts at 1. A new packet revision is exported when its content
would change: a new content revision, a reassessment, or new evidence.
Revision n > 1 must carry `previous_revision` (a `{ref, digest}` to
revision n − 1), and revision 1 must not. Superseded revisions are not
deleted. They stay resolvable for as long as anything references them.

### 6.3 Files and resolution

The exporter writes canonical JSON bytes, with no trailing newline, so the
file's sha256 is the packet digest:

    <out>/<packet_id>/<revision>.json            active packet
    <out>/<packet_id>/<revision>.withdrawn.json  tombstone that replaced it

Resolving `{ref: "<packet_id>@<n>", digest}` works as follows:

1. If `<n>.json` exists, its digest must equal `digest`: the result is
   `active`. If the digests differ, the result is `digest_mismatch`.
2. Otherwise, if `<n>.withdrawn.json` exists and its `digest` field equals
   `digest`, the result is `withdrawn`.
3. Otherwise the result is `missing`, which is a producer defect.

Muninn also keeps each exported packet in its `evidence_packets` table, so
a lost file can be re-materialized byte-for-byte.

Planned CLI (**proposed**; registered with the exporter):

    muninn export packets [--out DIR] [--item ITEM_ID]... [--since DATE] [--limit N] [--dry-run]
    muninn export packets --withdraw PACKET_ID@REVISION --reason REASON

## 7. Withdrawal and tombstones

Source material is deleted when retention expires, at the operator's
request, when the source itself deletes it, or when the operator hides it
(`domain_policy`). Each packet revision that contains the material is then
**withdrawn**. Its content is removed, both the file and the stored body,
and a tombstone takes its place:

    {record_type: "evidence-packet", contract, contract_version,
     status: "withdrawn", packet_id, revision, digest, withdrawn_at, withdrawal_reason}

- `digest` is the digest of the withdrawn packet revision, not of the
  tombstone, so existing `{ref, digest}` references resolve to `withdrawn`.
- `withdrawal_reason` is one of `retention`, `operator_request`,
  `source_deleted` or `policy_hidden`.
- The tombstone carries no content and no free-text note.
- Withdrawing one revision does not withdraw the others; each needs its own
  tombstone.
- The underlying content revisions are withdrawn the same way (content
  cleared, row kept).

## 8. Times and freshness

Every content revision keeps three separate times:

- the source **publication** time (`published_at`)
- the **retrieval** time (`retrieved_at`)
- the **bookmark observation** time (`observed_at`), when known

"Recently bookmarked" never implies "newly published". Research selection
uses current captures: the X API response and a live fetch of linked
material at sync time. Archive captures are labelled `historical`. The
browser-bookmark lane keeps its at-capture-first behaviour unchanged.

## 9. Privacy and secrets

- Every locator passes through `sanitize_url`. Raw URLs, OAuth tokens,
  pagination cursors and response headers never appear in a record.
  `usage-record.operation` is a template, so it cannot carry them.
- X OAuth tokens are stored outside the database. The sync-checkpoint
  table stores only the opaque pagination cursor.
- Hidden items (`content_visible = 0`) are never exported.
- A canary test plants a synthetic secret in raw connector inputs
  (`tests/fixtures/intake/canary/raw-inputs.json`). It asserts that the
  secret appears in no valid fixture, no schema, and not in this file, and
  that `sanitize_url` removes it before identity is computed.
- All fixtures are synthetic. They use reserved example hosts and invented
  X IDs and handles, and contain no recorded data.

## 10. Internal storage (informative)

The draft `schemas/sql/002_research_intake.sql` is additive. It creates
seven STRICT tables and alters nothing that 001 created:

- `item_links`
- `scrape_history`
- `interest_profiles`
- `assessments`
- `evidence_packets`
- `sync_checkpoints`
- `usage_records`

It contains no transaction control, because the migrator wraps it. It has
been applied to a 001 database in tests, with the existing tables, indexes
and rows unchanged and the integrity and foreign-key checks passing. No
runtime path applies it yet.

The two design points RI-01 left open are settled as follows, without any
table rebuild:

1. **Where the X post's own text lives.** In the same place as every other
   item's content:
   - the current text is the post's `scrape_results` row with
     `pass = 'live'`
   - every distinct version is a `scrape_history` row with
     `retrieval_method = 'x_api'`

   `bookmarks.source_metadata` holds structured post metadata (author,
   publication time, language, conversation ID, flags, last observation),
   never the text. A new `scrape_results.pass` value was rejected: it would
   need a table rebuild, and the existing `live` value already means "the
   current retrieval".
2. **`enrichment_source` for API-sourced content.** `live_fallback`, the
   existing value that maps to the `live` pass. The existing enrich,
   vault and full-text paths therefore read X post text with no code or
   schema change. Provenance (`x_api` or an HTTP fetch) is recorded per
   revision in `scrape_history`, never inferred from `enrichment_source`.

   A dedicated `x_api` value would need a rebuild of `bookmarks`, which
   has foreign-key children and full-text dependants; that cost was
   judged not worth paying. The value's name is a browser-lane artefact:
   for X rows it reads as "live". A future migration that rebuilds
   `bookmarks` for another reason may add `x_api` (open question).

   Consequence for RI-04: `muninn scrape` must skip `source = 'x'` rows,
   or a live fetch of the post page would overwrite the API text.
   Deleted or blocked posts keep their earlier revisions, and their
   current row records the failure.

Text from the X API is normalized with `muninn-text/1`. Each `t.co` link
in the text is replaced by its entity's canonical locator, or left as-is
when sanitization rejects every candidate (**proposed**).

### 10.1 X adapter mapping (informative, for RI-04)

The X connector maps an X API v2 bookmarks response as follows. The field
names follow the documented response shapes, and items marked open depend
on RI-02a.

| X response field | Internal | Contract record field |
|---|---|---|
| `data[].id` | `bookmarks(source='x', source_id=id)` | `source-item.item_id` = `x:post:<id>` |
| `data[].author_id` + `includes.users[]` (`id`, `username`, `name`) | `source_metadata.author` | `source-item.author` |
| `data[].created_at` | `source_metadata.published_at`, `scrape_history.published_at` (epoch) | `published_at` |
| `data[].note_tweet.text`, else `data[].text` | `scrape_results(pass='live').content_text`, `scrape_history(retrieval_method='x_api')` | `content-revision` (`x_post.is_long_post` = note_tweet present) |
| `data[].lang`, `conversation_id`, `possibly_sensitive`, `attachments.media_keys` | `source_metadata` | `language`, `x_post.*` |
| `data[].entities.urls[i]` (`url`, `expanded_url`, `unwound_url`, `status`) | `bookmarks(source='x_link', source_id=sha256(locator))` + `item_links(relation='links_to', position=i)` | `item-link` (`locator_resolution`, `entity_status`); child `source-item` |
| URL entity that is an X post URL | `item_links` to that post's `x:post` item (not a `web:` item) | `item-link.child_item_id` = `x:post:<id>` |
| `data[].referenced_tweets[]` (`quoted`, `replied_to`) | `item_links(relation='quotes' or 'replies_to')` | `locator_resolution` = `api_reference` |
| `data[].article` (shape and body open) | `bookmarks(source='x_article')` + `item_links(relation='article')` | `x:article:<id>` (proposed) |
| `errors[]` type `resource-not-found` for a bookmarked ID | `scrape_history` coverage `deleted`; `scrape_results.scrape_status = 'failed'` | `content-revision.coverage` = `deleted` |
| `errors[]` type `not-authorized-for-resource` or `resource-unavailable` | `scrape_history` coverage `blocked`; `scrape_results.scrape_status = 'auth_required'` | `content-revision.coverage` = `blocked` |
| first sync that sees the ID | `bookmarks.captured_at` | `bookmark-observation.observed_at` (`first_seen_by_sync`) |
| later syncs | `source_metadata.last_observed_at` | `last_observed_at` |
| folder endpoints (open) | `source_metadata.folder_ids` | `folder_ids` |
| the run itself | `sync_checkpoints` (`run_ulid`, `source='x'`, `account_ref` = user ID from `/2/users/me`, `collection` = `bookmarks` or `bookmark_folder:<id>`, `status`, `next_token`, `pages_fetched`, `items_seen`, `items_new`, `resume_after` from `x-rate-limit-reset`, `error_code`, `config_json`) | `sync_run_id` |
| each page fetched | `usage_records(kind='x_api', operation='GET /2/users/:id/bookmarks', resource_count, cost_micro_usd, cost_basis='estimated')` | `usage-record` |

Sync status values:

- `paused`: rate-limited and resumable
- `exhausted`: `usage-capped`, a hard stop
- `failed`
- `completed`: only when no `next_token` is returned

`config_json` holds `max_results`, the accepted field-name spelling
(`tweet.fields` or `post.fields`) and the item cap (historically 800), as
configuration rather than constants.

## 11. Compatibility policy

- **Versioning.** SemVer, `MAJOR.MINOR.PATCH`, with a `-draft` suffix until
  the operator and the Odin consumer side have reviewed it. The contract
  stays at `0.x-draft` until then. Muninn applies the MINOR and MAJOR rules
  below from 0.1 on, rather than using SemVer's latitude for 0.x.
  - **PATCH:** editorial changes and added fixtures, with no change to any
    schema's meaning.
  - **MINOR:** adds optional properties or new record types only. Every
    rule a 0.N record satisfies still holds in 0.(N+1), so a consumer that
    moves its pin to the newer schemas never sees an older record rejected.
    The newer schemas also accept the newer `contract_version`; the older
    schemas do not.
  - **MAJOR:** anything else, including new required properties, new enum
    values, narrower patterns or ranges, a changed digest, identity or
    normalization rule, or a changed meaning.
- **Schema IDs** carry `MAJOR.MINOR`. The 0.1 schemas accept
  `contract_version` values `0.1.x` and `0.1.x-draft`. A new MINOR
  publishes new `$id`s. Older schema files remain available at their
  release commits, which Odin's snapshots pin.
- **Releases table.** Rows are never deleted. Each row names a manifest
  file in `sha256sum` format, with one line for this contract and one for
  every schema and fixture. The digests live in the manifest, not in this
  table, because this file cannot contain its own digest. The manifest is
  checked by `sha256sum -c` from the repository root, and by the test
  `test_release_manifest_matches_the_files`. A draft row's manifest is
  regenerated (`python scripts/intake-release-manifest.py`) whenever a
  listed file changes. Once a version is released, its manifest is frozen
  and never edited again. The release commit is recorded by the
  consumer's snapshot receipt, alongside that commit's sha256 of this file.

### Releases

| Version | Date | Status | Manifest | Notes |
|---|---|---|---|---|
| 0.1.0-draft | 2026-10-21 | draft, unreleased: schemas validated; exporter, capture and assessment not implemented | `docs/contracts/MUNINN-RESEARCH-INTAKE-0.1.0-draft.sha256` | First draft (RI-03). Open questions in section 14 |

## 12. Status by part

| Part | Status | Evidence |
|---|---|---|
| Record schemas and fixtures (section 4) | validated | `tests/unit/test_intake_schemas.py` |
| Canonical JSON, digests, ULIDs, item IDs, text normalization | implemented | `src/muninn/consumers/packets/canonical.py`; same tests |
| Packet file writing, withdrawal, resolution, integrity checks | implemented (library only; no CLI) | `src/muninn/consumers/packets/export.py`; same tests |
| Draft 002 storage | validated (applies additively; constraints tested) | `tests/unit/test_intake_migration.py`; no runtime writer |
| `export_packets` (database to packets), `muninn export packets` | proposed | interface stub raises `NotImplementedError` |
| X capture (`muninn ingest x`), suitability assessment | proposed | RI-04, RI-05 |
| No-candidate-without-content rule; `x:article` identity; t.co replacement in post text | proposed | specified here; schema and DDL enforce the first |

## 13. Producer conformance checks

These run in muninn's test suite, offline and without credentials:

1. Every schema is valid draft 2020-12, with the contract `$id`, closed
   objects and no `number` type.
2. Every record has at least one valid and one invalid fixture. Valid
   fixtures pass. Each invalid fixture fails at its stated path for its
   stated reason.
3. The fixtures cover:
   - a changed-content revision
   - a changed-profile reassessment
   - an old packet revision that still resolves after a newer one
   - blocked and partial captures assessed with coverage shown
   - a withdrawn-packet tombstone
   - the canary
4. Packet integrity rules, digest stability, and file sha256 equal to the
   packet digest.
5. 002 applies to a 001 database without changing it, and its
   append-only, coverage, disposition and SET NULL rules hold.
6. The packets package imports no heavy dependency (Qdrant, embeddings,
   DuckDB, Parquet, MCP, Jinja, the model SDK, the HTTP client).
7. The release manifest matches the files.

## 14. Open questions

Carried from RI-01 section 9 (each value below is **proposed**, adopted
with the operator's authorization to proceed):

- **X access method, cost and limits.** Proposed: the official X API,
  pay-per-use, OAuth 2.0 PKCE in user context. Qualified by RI-02a.
- **Evaluation model and spending caps.** Proposed: the Anthropic API,
  with the model and the per-run and monthly caps set in RI-05 from
  fixture calibration. `assessment.model` is a free string, so the
  contract does not depend on the choice.
- **Raw-content retention.** Proposed: keep the extracted text and digests
  for every revision a packet references. Raw HTML follows the existing
  spill rules. Deleting source material removes it from derived packets
  and leaves a tombstone (section 7).

Dependent on RI-02a live qualification. The fields involved are optional,
or the rules informative:

- Whether the full X Article body is returned. This determines whether
  the `x:article` item and its revisions are real, and what an
  article-only coverage means.
- Bookmark-folder semantics and billing (`folder_ids`).
- Retrievable history depth (the historical 800 cap) and list ordering
  (`list_position`).
- How deleted, protected and suspended-author bookmarks surface: in
  `data`, in `errors`, or not at all. This affects the `errors[]` mapping
  in section 10.1.
- Whether `note_tweet` is returned for long posts.
- Whether expansions (`includes.users`) are billed separately
  (`usage-record.resource_count`).

Design questions for review:

- Whether `sanitize_url` redaction needs a rule identifier for audit. 0.1
  records names and flags only.
- Un-bookmark detection. 0.1 records `last_observed_at` only. Whether a
  removed bookmark should withdraw packets is open.
- A dedicated `enrichment_source = 'x_api'` value, deferred until a
  `bookmarks` rebuild is needed for another reason (section 10).
- Database-enforced append-only behaviour (triggers), deferred until the
  migrator's statement handling of trigger bodies is settled. 0.1 enforces
  it in the application, plus foreign keys and CHECKs.
- Retweet references (`referenced_tweets` type `retweeted`) are not
  modelled.
- The A-to-B-to-A revision limitation (section 5).
- Odin's `SNAPSHOT.schema.json` hard-codes the producer `saga`. Odin must
  generalize it before it can pin this contract. That is Odin's task.
