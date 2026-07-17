---
name: note
description: Persist the current work session into the Muninn vault as a Skald Protocol session page, and upsert the touched project pages. Use when the operator invokes /note, asks to "note this session", "write this up", "persist a summary", or when a substantial work session is clearly wrapping up and its outcome should be recorded. Not for general note-taking, not for writing docs inside project repos, and never inside Saga agent containers (Bragi owns those sessions).
---

# /note — session skald

Install at `~/.claude/skills/note/SKILL.md` on the homelab VM (user-level, so it
applies in every repo). If a given Claude Code version only discovers
project-level skills, symlink this directory into each repo's `.claude/skills/`.

Canonical contract: `docs/skald-protocol.md` in the Muninn repo. If this skill
and the protocol disagree, **the protocol wins** — then update this skill.

## Guard: refuse inside Saga containers

If `$SAGA_ROLE` is set or `/workspace/.dvergr/` exists, do not write anything.
Tell the operator this session belongs to Bragi (one narrative author per
session, protocol §3) and stop.

## Step 0 — resolve configuration

Resolve in order; first hit wins:

1. **Vault path:** `$SKALD_VAULT_PATH` → `~/.skald/config` (`vault=` line) →
   default `/opt/muninn/vault`. Verify it exists and contains `wiki/`. If not
   reachable, switch to **outbox mode** (below).
2. **Project slug(s):** `skald.project` in the repo's `CLAUDE.md` → mapping from
   the git remote / directory name → ask the operator. A session that touched
   several projects gets all of them; ask which is primary if unclear. Slugs are
   lowercase-kebab and must match `wiki/projects/{slug}.md` stems.

## Step 1 — gather

From the conversation and working tree, collect: what was attempted, what
shipped or failed, decisions made **and their rationale**, open threads, files
touched (`git status` / `git diff --stat` helps), rough duration, model in use.
Ask the operator only if the outcome is genuinely ambiguous — prefer inferring
from the session over interrogating.

## Step 2 — draft the session page

Follow protocol §4. Frontmatter (schema v1):

```yaml
---
schema_version: 1
page_type: session
title: <human title>
date: <YYYY-MM-DD>
projects: [<primary>, ...]
emitter: note-skill
outcome: shipped | partial | blocked | exploratory
model: <if known>
duration_min: <estimate, optional>
decisions: [<one-liners, optional>]
next_actions: [<optional>]
files_touched: ["<repo>: <path>", ...]
sources: [<raw/ paths or URLs, optional>]
tags: [<optional>]
---
```

Body: **What happened / Why it matters / Decisions / Open threads.** 300–600
words. Narrative, not transcript. Write it so a future session needs no other
context. Use `[[wikilinks]]` to project and concept pages where natural.
`session_id` is optional — include it only if reliably determinable; never guess.

Write to `wiki/sessions/YYYY-MM-DD-{primary}-{slug}.md`. On filename collision,
append `-2`.

## Step 3 — upsert project pages

For each slug in `projects:`:

- Page missing → create the stub from protocol Appendix B in this same commit.
- Edit **only** inside `<!-- skald:begin/end ... -->` markers:
  - `current-state`: replace wholesale, ≤150 words — but **only** if this session
    materially changed state (phase completion, scope shift, rename, decision
    ratified, blocker appeared/cleared). Routine sessions skip this.
  - `open-questions`: read-modify-write; carry unresolved items forward, remove
    resolved ones.
  - `recent-sessions`: prepend `- [[sessions/<page>]] — <outcome> — <5-word gist>`;
    trim to 10 entries.
- Never touch anything outside markers. Never edit files in project repos.
- Update `updated_at` / `updated_by` frontmatter on any page you changed.

## Step 4 — log line (bootstrap mode)

Append to `wiki/log.md`:

```
- <date> · [<projects>] · <outcome> · [[sessions/<page>]] — <short gist>
```

(Once Muninn's watcher owns the log, the protocol will say so and this step is
dropped.)

## Step 5 — validate, commit, push

1. If `scripts/skald_lint.py` exists in the vault repo, run it; fix findings.
2. `git pull --rebase`
3. `git add` the touched paths; one commit:
   `skald(note-skill): <primary> <date> <short-title>`
4. `git push`. On failure: retry once; then leave the commit local and tell the
   operator explicitly that the vault has an unpushed commit.

## Step 6 — report

Reply with the session page path, a one-line summary, and which project pages
were upserted (or "link-only"). Do not paste the full page back into chat.

## Outbox mode (vault unreachable)

Write the finished session page to `~/.skald/outbox/` with its intended
filename, tell the operator, and stop. On any later `/note` run with the vault
reachable, drain the outbox first (move files in, upsert, log, commit) before
handling the current session.

## Style constraints

- Facts over vibes: name commits, files, error messages, measured results.
- Decisions carry rationale — "chose X because Y over Z" — one sentence each.
- No praise, no filler, no restating the roadmap; only what this session changed.
- British understatement over enthusiasm; the reader is future-you at bootstrap.
