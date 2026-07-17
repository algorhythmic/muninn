# Skald Protocol — session capture and project-state collation

> **Lifecycle:** Draft v1 (2026-07-16). Tracked by `ROADMAP.md` — this document is the
> Phase 3 "session page template" item, pulled forward so the `/note` skill can emit
> before Bragi exists. Implementation status lives in the roadmap, not here.

Destination: `docs/skald-protocol.md` in the Muninn repo, sibling to the planned
`docs/mcp-api.md` and `docs/huginn-integration.md`.

---

## 1. Purpose and scope

This protocol is the executor for ADR-004: the vault is the source of truth for
project state, and sessions leave a trail without operator effort. It defines the
contract between **skalds** (anything that writes session narratives and project-state
updates into the vault) and **Muninn** (which owns everything that happens after a
page lands).

A skald is an emitter role, not a component. Bragi is the first-planned skald;
the `/note` skill is the first-shipped one. The protocol exists so that adding a
skald never requires touching Muninn, and evolving Muninn's vault-side processing
never requires touching a skald — the same contracts-over-coupling shape as the
Huginn promotion protocol (huginn-spec §9), which this document deliberately mirrors.

**In scope:** session pages (`wiki/sessions/`), project-state upserts
(`wiki/projects/`), the chronological log (`wiki/log.md`), git discipline, and
validation rules.

**Out of scope (non-goals):**

- Saga's operational telemetry. Per-iteration journals, verdicts, and guidance stay
  in `/opt/saga/vault` — different cadence, different consumer (ADR-003 logic applies).
- Repo-local documentation. This protocol never writes into project repos; it does
  not touch any `STATUS.md`, `ROADMAP.md`, or `DECISIONS.md` outside the vault.
- Synthesis pages (Phase 4 era narratives, deep passes). Those are
  `page_type: synthesis` and governed by SPEC §5, not this document.
- userMemories. Per ADR-004, memory edits remain fallback protection between vault
  updates; they are not part of this contract.
- Multi-operator anything.

## 2. Roles

| Role | Owner | Responsibility |
|---|---|---|
| **Skald** (emitter) | note-skill, stop-hook, Bragi, future emitters | Author session pages; upsert project pages; append `log.md` (bootstrap mode only); commit and push |
| **Vault** | Muninn repo (git, Gitea `.18`) | Storage, history, audit trail |
| **Vault-side processing** | Muninn ingest/watcher (Phase 2+) | Indexing into Qdrant, `index.md` maintenance, lint passes, cross-link suggestion |
| **Retrieval** | Muninn MCP (Phase 2) | `search`, `get_page`, `list_namespace` for session bootstrap by Claude Code, Claude.ai, Odin |
| **Operator** | You | Curates operator-owned page regions; ratifies decisions into repo ADR logs; reads via Obsidian |

## 3. Emitter registry

| Emitter id | What it is | Cadence | Status |
|---|---|---|---|
| `note-skill` | User-level Claude Code skill; operator-triggered (`/note`) or model-offered at session wrap-up | Per interactive session | Ships now |
| `stop-hook` | Optional ambient archive of raw session material into `raw/sessions/` (evidence only — never writes `wiki/`) | Per turn/session end | Optional |
| `bragi` | Saga's batch compiler; covers Saga *runs* (Odin, dvergar, Mimir activity) | Cron, per Saga session | Saga/Muninn Phase 3 |

New emitters register by adding a row here and using a lowercase-kebab `emitter` id.
One rule prevents double capture: **a session has exactly one narrative author.**
Saga runs belong to Bragi; the note-skill must decline inside Saga containers
(guard: `SAGA_ROLE` env or `/workspace/.dvergr` present).

## 4. Session page contract

### 4.1 Location and filename

```
wiki/sessions/YYYY-MM-DD-{primary-project}-{slug}.md
```

`{primary-project}` is the first entry of `projects:`. `{slug}` is a 2–5 word
kebab-case digest of the title. Same-day collision: append `-2`, `-3`.

This refines the roadmap's `YYYY-MM-DD-{slug}.md`: embedding the primary project
makes `ls wiki/sessions/` scannable and prevents cross-project collisions on busy
days without needing to open frontmatter.

### 4.2 Frontmatter schema v1

Additive-only. New optional fields may be added at any time; existing fields are
never renamed, retyped, or removed. `schema_version` bumps only on a breaking
change, which is never planned (this is the same habit as huginn-spec §15.2).

| Field | Req | Type | Notes |
|---|---|---|---|
| `schema_version` | ✓ | int | `1` |
| `page_type` | ✓ | enum | `session` |
| `title` | ✓ | str | Human title; filename slug derives from it |
| `date` | ✓ | ISO date | Session date (end date if it spanned midnight) |
| `projects` | ✓ | list[slug] | First entry = primary. Slugs must resolve to `wiki/projects/{slug}.md`; if the page doesn't exist, the skald creates a stub in the same commit (§5.1) |
| `emitter` | ✓ | enum | Registry id (§3) |
| `outcome` | ✓ | enum | `shipped` \| `partial` \| `blocked` \| `exploratory` — one per session; per-project nuance goes in the body |
| `session_id` | – | str | Claude Code session UUID when the emitter can know it (hooks, Bragi). Interactive sessions can't reliably self-identify; best-effort or omit |
| `model` | – | str | e.g. `claude-opus-4-6` |
| `agents` | – | list | Saga roster; Bragi populates, others omit — an example of additive evolution already in use |
| `started_at` / `ended_at` / `duration_min` | – | ts / ts / int | Optional timing |
| `decisions` | – | list[str] | **Candidates**, one line each. Ratification happens in the owning repo's `DECISIONS.md`/ADR log; lint may later annotate the session page with the ratified ADR id. The vault records that a decision was made; the repo owns whether it stands |
| `next_actions` | – | list[str] | Session-scoped suggestions for the next bootstrap. One-way: these never auto-edit any `ROADMAP.md` — roadmaps stay operator-curated |
| `files_touched` | – | list[str] | `repo: path` form when multiple repos involved |
| `sources` | – | list | Evidence: `raw/sessions/...` paths or external URLs (§4.4) |
| `tags` | – | list[str] | Cross-namespace linking hints for lint |

### 4.3 Body conventions

The body is the narrative: **what happened, why it matters, decisions with
rationale, open threads.** 300–600 words as a norm; long enough that a future
session needs no other context, short enough that ten of them are skimmable.
Never a transcript dump. `[[wikilinks]]` to project/concept/people pages are
encouraged — cross-namespace links are where the compounding value lives (ADR-001).

### 4.4 Evidence and `raw/` (a deliberate deviation from the roadmap wording)

The roadmap's Phase 3 text has Bragi writing to `raw/sessions/`. This protocol
refines that: **skalds write finished pages directly to `wiki/sessions/`;
`raw/sessions/` holds evidence, not pages.**

Rationale: ADR-006 defines the semantic split — `raw/` is immutable originals,
`wiki/` is synthesis. A bookmark drop genuinely needs enrichment (Huginn's
one-line summary → full wiki page), so it lands in `raw/inbox/`. A session
narrative is *already synthesis*: the skald is the enricher, exactly as Muninn's
Haiku worker is the enricher for bookmarks. Routing finished narratives through
`raw/` for a no-op "promotion" adds a pipeline stage that transforms nothing.

What survives from the original wording: skalds *may* drop an evidence bundle
(transcript extracts, journal excerpts, Mimir verdicts) into
`raw/sessions/YYYY-MM-DD-{primary-project}-{slug}/` and cite it via `sources:`.
This preserves ADR-006's auditability mandate — every wiki page's claims traceable
to raw originals — in exactly the form bookmark pages already use.

Consequence for Muninn: the "sessions ingest lane" becomes an **indexing watcher**
over `wiki/sessions/` (embed, update `index.md`, lint, append `log.md`), not an
enrichment pipeline. Simpler than the bookmark lane, and shippable independently.

## 5. Project page contract

### 5.1 Location and stub creation

`wiki/projects/{slug}.md`, one per project; the filename stem **is** the slug
registry. First reference to a new slug obliges the skald to create a stub from
the Appendix B template in the same commit — first-touch bootstrapping instead of
a separate registration step.

### 5.2 Managed regions

Project pages have mixed authorship — operator prose and machine upserts on one
file — so ownership is explicit, delimited by markers:

```
<!-- skald:begin current-state -->   ... <!-- skald:end current-state -->
<!-- skald:begin open-questions -->  ... <!-- skald:end open-questions -->
<!-- skald:begin recent-sessions --> ... <!-- skald:end recent-sessions -->
```

Rules:

- Skalds edit **only** inside markers. Everything outside (architecture summary,
  operator commentary) is operator-owned and untouchable.
- `current-state`: replace wholesale on each upsert. It answers "where does this
  project stand right now" in ≤150 words.
- `open-questions`: read-modify-write. Carry unresolved items forward, drop
  resolved ones (noting resolution in the session page, not here).
- `recent-sessions`: prepend a link line, cap at 10 entries.
- Operators never edit inside markers; if machine-written state is wrong, the fix
  is a correcting session page + upsert, so the correction is itself on the record.

Marker-delimited regions also make every upsert a clean, reviewable git diff —
the audit trail ADR-004 wants falls out of the mechanism.

### 5.3 Upsert triggers

Upsert `current-state` only on **material** change — phase completion, scope
shift, rename, decision ratified, blocker appearing/clearing (the roadmap's Phase
3 list). Routine sessions add a `recent-sessions` line and nothing else. This
keeps `git log wiki/projects/` a signal-dense history of state changes rather
than a mirror of session activity.

### 5.4 Layering rule (canonicality)

Within a project's own repo, its dashboards are and remain the sole authority —
nothing here changes any repo's internal contract, and no repo needs to know this
protocol exists. The vault is the one layer where per-repo truth and portfolio
truth coexist, so precedence is declared here and binds **vault pages only**:

- A repo-local dashboard (dbriefly's `STATUS.md`, Saga's spec/roadmap, Muninn's
  own `ROADMAP.md`) is authoritative for **intra-project build state**.
- `wiki/projects/{slug}.md` is authoritative for **portfolio-level state** — what
  phase, what's blocking, what changed lately, as visible across projects.
- Every project page whose repo has a dashboard MUST carry a lifecycle pointer
  (dbriefly's own convention, adopted protocol-wide):
  `> **Lifecycle:** {repo dashboard} is authoritative for build state; on
  disagreement, it wins and this page needs an upsert.`

## 6. The log

`wiki/log.md` is append-only, one line per session, newest last:

```
- 2026-07-15 · [dbriefly] · shipped · [[sessions/2026-07-15-dbriefly-cascaded-ptt]] — cascaded PTT + BT route restore
```

Bootstrap mode: the skald appends. Once the watcher ships, appending moves
vault-side and skalds stop (the watcher derives the line from frontmatter).

## 7. Modes: bootstrap vs. watcher

| Obligation | Bootstrap (now) | Watcher live (Phase 2+) |
|---|---|---|
| Write session page | Skald | Skald |
| Project upsert | Skald | Skald (Bragi for Saga runs) |
| `log.md` append | Skald | Watcher |
| Qdrant indexing, `index.md` | — (absent) | Watcher |
| Lint / cross-link suggestions | — | Watcher (Phase 6) |

The protocol ships full value with zero Muninn-side code — consistent with
ADR-005's v1 discipline (no new ingest lane required to start). Skald obligations
only ever shrink as Muninn grows.

## 8. Git discipline

- Before writing: `git pull --rebase`.
- Commit message: `skald(<emitter>): <primary-project> <date> <short-title>`.
- One commit per session capture (session page + stubs + upserts + log line together).
- Push; on failure retry once, then leave the commit local and tell the operator.
  Single-operator, serial-session reality makes conflicts rare; git is the
  atomicity unit and the audit trail, so no locking protocol is warranted.
- Vault unreachable entirely (Mac session, Tailscale down): write the session page
  to `~/.skald/outbox/` and tell the operator; next `/note` with the vault present
  drains the outbox first.

## 9. Validation (`skald-lint`)

A small script in the Muninn repo (`scripts/skald_lint.py`); skalds SHOULD run it
pre-commit, the watcher MUST run it on every new page. Checks:

1. Frontmatter parses; required fields present; enums valid.
2. Filename agrees with `date` + primary project slug.
3. Every `projects:` slug resolves to `wiki/projects/{slug}.md` in the same tree.
4. Managed markers balanced on any touched project page.
5. `sources:` paths that point into `raw/` exist.
6. Bootstrap mode: `log.md` gained a matching line.

## 10. Forward compatibility

Saga's generalization Move 1 (typed journals) lands as **frontmatter extension,
never migration** (ADR-007 consequence): typed event fields appear as new optional
keys; the existing corpus stays valid untouched. If this protocol's shape fights
Bragi's natural output during Phase 3 implementation, log the incident — twice
counts toward generalization Trigger #3.

Per huginn-spec §15.5: "skald" is a name, not a type. Field names and enums stay
boring and semantic; nothing in code should switch on Norse-ness.

## 11. Design notes

- **Protocol, not module** — the ecosystem's repeated choice (ADR-003, ADR-007) is
  contracts over coupling; the only shared code this creates is `skald-lint`.
- **Emitters write `wiki/` directly** — see §4.4; `raw/` = originals, `wiki/` =
  synthesis, and a session narrative is synthesis at birth.
- **Managed regions** — mixed human/machine authorship fails silently without
  explicit ownership; markers make upserts idempotent, safe, and diff-reviewable.
- **`projects` is a list** — real sessions span projects; primary-first keeps
  filenames deterministic.
- **Decisions are candidates** — the vault records, repos ratify; prevents the
  vault from silently becoming a second ADR system.
- **One narrative author per session** — the note-skill/Bragi guard exists so the
  same session never produces two competing pages.

---

## Appendix A — session page template

```markdown
---
schema_version: 1
page_type: session
title: Cascaded PTT + Bluetooth route restoration
date: 2026-07-15
projects: [dbriefly]
emitter: note-skill
outcome: shipped
model: claude-opus-4-6
duration_min: 140
decisions:
  - "Manual PTT records one bounded WAV; no gpt-realtime warm-hold"
next_actions:
  - "iPhone + Bluetooth on-device pass incl. short/silent hold"
files_touched:
  - "dbriefly: src/audio/ptt.ts"
sources:
  - raw/sessions/2026-07-15-dbriefly-cascaded-ptt/
tags: [audio, bluetooth]
---

# Cascaded PTT + Bluetooth route restoration

**What happened.** …

**Why it matters.** …

**Decisions.** … (rationale, not just the verdict)

**Open threads.** …
```

## Appendix B — project page template

```markdown
---
schema_version: 1
page_type: project
project: dbriefly
status: active
phase: M1
repo: ["gitea:david/dbriefly"]
updated_at: 2026-07-16
updated_by: note-skill
---

# dbriefly

> **Lifecycle:** Repo dashboard `STATUS.md` is authoritative for build state; on
> disagreement, it wins and this page needs an upsert.

<!-- skald:begin current-state -->
One-paragraph portfolio-level state: phase, momentum, current blocker.
<!-- skald:end current-state -->

## Architecture summary

Operator-owned prose. Skalds do not edit outside markers.

<!-- skald:begin open-questions -->
- …
<!-- skald:end open-questions -->

<!-- skald:begin recent-sessions -->
- [[sessions/2026-07-15-dbriefly-cascaded-ptt]] — shipped — cascaded PTT
<!-- skald:end recent-sessions -->
```

## Appendix C — per-repo declaration (optional)

In a project repo's `CLAUDE.md`, so the note-skill resolves identity without
guessing:

```markdown
## Skald
- skald.project: dbriefly
- skald.vault: /opt/muninn/vault   # optional; default resolution in SKILL.md
```
