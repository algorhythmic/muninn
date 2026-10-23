# Vault lint

Lint is Karpathy's "Lint" operation (roadmap Phase 6): periodic hygiene for the compiled vault. It has two tiers:

| Tier | Command | Cost | What it finds |
|---|---|---|---|
| Deterministic | `muninn lint` | free, no model | orphan pages, missing cross-references, unresolved links |
| Model | `muninn synthesize lint --tag TAG` | budgeted API spend, or another harness | contradictions and superseded claims between compiled bookmark pages |

Code: `src/muninn/consumers/vault/lint.py` (deterministic), `src/muninn/synthesis/tasks/lint.py` (model tier), `src/muninn/consumers/vault/lint_notes.py` (rendering).

## The guarantee: authored pages are never edited

ADR-008 splits the vault into two lanes. `wiki/bookmarks/` is compiled from SQLite, and the compiler is its only writer. The authored lane (`wiki/concepts/`, `wiki/infra/`, `wiki/people/`, `wiki/projects/`, `wiki/log.md`) is written by hand, has no machine-managed regions and is never machine-edited.

Lint keeps to that:

- **`muninn lint` never writes, renames or deletes any vault file**, in either lane. Every finding is report-only, and the report goes to stdout or to a file outside the vault (`--report` refuses a path inside the vault or the personal vault). The tests hash the whole vault before and after.
- **The model tier writes only SQLite** (`lint_pair_checks`, `lint_findings`, `synthesis_runs`, `usage_records`). It reads only compiled bookmark pages, as rows from SQLite, and never an authored page.
- **Findings reach the vault only through the compiler**, and only in the compiled `wiki/bookmarks/` pages it writes anyway. A page without a current finding compiles byte-for-byte as before.

Both tiers refuse a vault that equals, contains or sits inside `MUNINN_PERSONAL_VAULT_DIR`. This is the compiler's never-same-vault guard (SPEC Decision 2), shared through `vault_fs.resolve_wiki_dir`.

## `muninn lint`

```
muninn lint [--vault DIR] [--json] [--report FILE] [--strict]
```

- `--vault DIR`: the compiled vault root (the directory that holds `wiki/`). Default: `MUNINN_VAULT_DIR`.
- `--json`: print the report as JSON instead of text.
- `--report FILE`: write the report (text, or JSON with `--json`) to `FILE`, and print a one-line summary. `FILE` must be outside the vault and the personal vault.
- `--strict`: exit 1 when there is any finding. Without it, lint exits 0 whatever it finds, so a scheduled run doesn't show as failed.

Errors (no vault configured, no `wiki/`, an overlap with the personal vault, a report path inside the vault) exit 1 with a one-line message and read nothing further.

### What is scanned

Every `.md` page under `wiki/`: the pages at its root and in every directory below it. Symlinks and dot-prefixed entries are skipped, as the MCP server skips them. A page larger than 1 MiB is skipped and listed under `skipped`. Files are opened read-only.

Each page is in one lane: `captured` for `wiki/bookmarks/`, `authored` for everything else.

### Link resolution

Links resolve the way Obsidian resolves them, case-insensitively:

- `[[target]]` by file name, with or without `.md`;
- `[[target|display text]]`, `[[target#Heading]]`, `[[target#^block-id]]` and combinations: only the part before `#` or `|` names the page;
- `![[target]]` embeds count as links;
- `[[concepts/target]]` and `[[wiki/concepts/target]]` by path, or by a path suffix;
- a page's frontmatter `aliases` (or `alias`), as an inline list `[a, b]` or a block list of `- a` lines;
- `[[target\|display]]` inside a table (the escaped pipe);
- relative Markdown links to `.md` files, `[text](../concepts/target.md#x)`, resolved from the linking page's directory;
- wikilinks in frontmatter values (Obsidian property links, such as `superseded_by: ["[[page]]"]`).

When two pages share a name, the one in the linking page's directory wins, then the shorter path, then path order. `[[#Heading]]` and a page's links to itself are not inbound links. Links inside code (fenced blocks and inline spans) and HTML comments are not links.

### Checks

**Orphans.** A page with no inbound link from any other page. Not orphan-checked:

- hub and index pages, by file name in any directory: `index`, `_index`, `readme`, `home` and `log` (so `wiki/log.md` is excluded);
- any page whose frontmatter has `lint_ignore: orphan` (or a list containing `orphan`). The operator adds this by hand; lint never does.

Compiled bookmark pages are checked too. An orphaned bookmark page usually has no tags and no era, and the operator might extract a concept from it.

**Suggested links.** A page mentions another page's title, alias or file name, as a whole word and case-insensitively, but doesn't link it. Lint suggests the link. Guards against false positives:

- **Targets** are authored pages in `concepts/`, `infra/`, `people/` and `projects/`. Bookmark titles are long and specific, so they are never targets.
- **Terms** are a target's title (frontmatter `title`, else its first `# ` heading, else the file name), its aliases, and its file name with `-` and `_` read as spaces.
  - A term shorter than 4 characters, or with no letter, is dropped.
  - So is a term made only of common words (`COMMON_WORDS`: function words and vault words such as "notes", "index" or "summary").
  - A term two pages share is dropped, since lint couldn't tell which page is meant.
- **Not scanned:** frontmatter, fenced and inline code, HTML comments, URLs, and the text of existing links.
- **Whole words only:** "rust" doesn't match "trust" or "rusty". Whitespace inside a term matches any run of whitespace, and the longest term wins at a position.
- **At most one suggestion per target per page**, at its first mention. None when the page already links the target (by any form above), and none for a page mentioning itself.
- `lint_ignore: suggest` turns off suggestions *on* that page.

The suggested link is the shortest one that resolves: `[[file-name]]`, or `[[file-name|Mention]]` when the mention differs. A suggestion on a compiled page is a signal only, since a hand edit there would be overwritten.

**Unresolved links.** A wikilink or relative `.md` link that resolves to no page.

### Output

Text (the default):

```
Vault lint (muninn-vault-lint/1): /path/to/muninn-vault
Scanned 214 page(s): 190 compiled (wiki/bookmarks/), 24 authored.
Report only: no vault file was changed.

Orphans (2): no inbound link from another page
  wiki/bookmarks/example-page-17.md [captured]
  wiki/infra/old-box.md [authored]

Suggested links (1): mentions another page without linking it
  wiki/concepts/retrieval.md:9 [authored] "prompt caching" -> [[prompt-caching|prompt caching]] (wiki/concepts/prompt-caching.md)

Unresolved links (1): resolve to no page
  wiki/concepts/retrieval.md:7 [authored] [[missing-page]]
…
```

JSON (`--json`) has the same content, with `report_version: "muninn-vault-lint/1"`:

| Key | Content |
|---|---|
| `vault`, `pages_scanned`, `pages_by_lane` | what was read |
| `counts` | `orphans`, `suggestions`, `unresolved_links`, `skipped` |
| `orphans[]` | `page`, `lane`, `namespace` |
| `suggestions[]` | `page`, `lane`, `line`, `mention`, `target`, `link` |
| `unresolved_links[]` | `page`, `lane`, `line`, `target` |
| `skipped[]` | `page`, `reason` |
| `exclusions` | the hub names, opt-out key, suggestion targets and minimum term length in force |

Paths are relative to the vault root, and lines are 1-based in the file. Output is deterministic: the same vault gives the same bytes, with no timestamp, so weekly reports diff cleanly. A report names vault pages and the terms they mention, so keep it private, like the vault.

## The model tier: `muninn synthesize lint`

```
muninn synthesize lint --tag TAG [--limit N] [--prompt-version lint_vN]
                       [--emit-task DIR] [--model M] [--max-cost-usd X]
```

This is a task on the synthesis contract (`muninn-synthesis/1`, task type `lint`; see [`synthesis-contract.md`](synthesis-contract.md)). It runs with the budgeted API runner by default. With `--emit-task DIR` it writes a task file for another harness, and `muninn synthesize ingest-result FILE` takes the result back.

**Input.** The candidates are the newest `--limit` (default 20, at most 40) visible bookmarks whose enrichment tags include `TAG`, case-insensitively, and that have a summary or captured text. Each has a page in `wiki/bookmarks/`.

The task holds only the **pending pairs** among the candidates (see Idempotency) as `materials.pairs`, and only the pages that appear in them as `materials.pages`. The model is told to judge those pairs and no others. So one new page among 20 candidates costs its 19 pairs and its 19 partners' pages, not all 190 pairs, and a pair already checked is never re-judged because an unrelated page changed.

- A `content_visible = 0` row is never included.
- An X post that links a policy-hidden item gets the vault's treatment (`x_pages.py`). The hidden locators are masked out of its text and title as `[hidden link]`, and enrichment that quotes one is withheld whole.
- Each page carries its title, summary, captured text (clipped to 4,000 characters), its dates and a content hash.
- `dated_at` is the publication time when muninn knows it (X posts), else the bookmark or capture time.

**Findings.** The model compares the claims of each pair it was given and returns zero or more findings. Each has:

- a relation: `contradicts`, or `supersedes` (the newer page's claim replaces the older page's);
- the two bookmark ids;
- a rationale of at most 500 characters;
- quoted evidence.

The result must validate against `schemas/json/lint.schema.json`. Muninn then checks, before writing anything:

- both ids are pages of the task, and they differ;
- the pair is one of `materials.pairs`: a finding on any other pair, even between two pages of the task, is rejected;
- for `supersedes`, the newer page's `dated_at` is strictly later than the older page's;
- every evidence quote names one of the pair, and occurs in that page's `text` or `summary`, either exactly or with different whitespace (the stored span is kept). A quote across `[hidden link]` is rejected;
- each of the two pages is quoted at least once.

A failure gets the contract's single correction retry, as the other tasks do.

**Storage** (migration 003):

- **`lint_pair_checks`**: one row per pair the task asked about (`materials.pairs`), finding or not. It records the pair, both content hashes, the model, the prompt version, the run's `task_id` and the tag. A pair the task did not ask about is never recorded, so a task cannot replace the verdict on it.
- **`lint_findings`**: one row per finding. It records the relation, both pages and their content hashes, the rationale, the verified evidence (`evidence_json`), the model, the prompt version, the `task_id` and the tag. Contradictions are stored with the lower id first.

Both tables are append-only, and their rows cascade away with a deleted bookmark.

**Idempotency.** Before building a task, muninn checks every pair of the candidates. A pair is done when it was already checked with the same two content hashes and the same prompt version, under any tag: the check is keyed by content, so two pages that share two tags are checked once, not once per tag. For an API run, the model must match too (the requested id, or a dated snapshot of it that the API served); an emitted task matches any model. Every other pair is pending, and only pending pairs go into the task. When none is pending, muninn makes no call and builds no task, and the command reports `up to date`. A changed page (new text, new summary, a link newly hidden), a new prompt version or another model re-opens the pairs it affects. Re-running with unchanged inputs never pays twice.

**Budget.** The same as every synthesis task: the per-run `--max-cost-usd` (default $3.00), and the required monthly `MUNINN_SYNTHESIS_MONTHLY_BUDGET_USD`. The worst case is checked before the call, and a call that doesn't fit is never made (`cap_hit`). Usage is recorded as `synthesize:lint`.

### Rendering

`muninn vault compile` renders a finding while it is **current**:

- both pages are still visible;
- both pages' content hashes still match;
- the finding belongs to the latest check of that pair with those hashes (a later re-check that finds nothing clears it).

| Relation | Rendered on | What |
|---|---|---|
| `contradicts` | both pages | an inline `<!-- contradiction: {other_page} -->` comment, then a visible `> [!warning] Possible contradiction` callout linking the other page, with the rationale |
| `supersedes` | the older page | `superseded_at:` (the earliest superseding page's `dated_at`) and `superseded_by: ["[[newer-page]]"]` in frontmatter, plus a `> [!note] Superseded` callout |

The callouts go under the page's `# ` heading. Both pages stay, and the older claim is not removed. The vault's git history keeps every compiled version. A hidden page takes its findings, and their model-written rationale, off the other page too.

## Cadence

- **On demand:** run `muninn lint` at any time. It is free and read-only.
- **Weekly:** `deploy/systemd/` has an example systemd user service and timer, `muninn-lint.service` and `muninn-lint.timer`. Nothing installs them. They run the deterministic lint only, writing `~/.local/state/muninn/lint/latest.txt` and `latest.json` every Monday at 07:00, and catch up after downtime. The unit's file system is read-only except its state directory. To install, edit the paths marked `ADJUST`, then:

  ```
  cp deploy/systemd/muninn-lint.{service,timer} ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now muninn-lint.timer
  systemctl --user list-timers muninn-lint.timer
  ```

- **The model tier stays manual,** because it spends money. Run `muninn synthesize lint --tag TAG` for the tags you care about, then `muninn vault compile`.
