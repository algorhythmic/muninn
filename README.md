# Muninn

> *"…and Muninn returns each day to whisper into Odin's ear what he
> has remembered."*

A personal archive and wiki of saved material, starting with X
bookmarks and a decade of browser bookmarks. Humans can query it
through an Obsidian-browsable wiki, and LLMs through an MCP server.
Named after Odin's raven of **memory**: the one whose return Odin
awaits more anxiously, because "thought" can always be re-derived but
**memory cannot**.

---

## The problem

A typical knowledge worker has thousands of bookmarks accumulated
across browsers, services, and exports — a corpus of curated reading
that should be one of their most valuable knowledge assets, but in
practice is:

- **Unsearchable beyond title.** Browser bookmark search matches
  page titles, not content, summaries, or themes.
- **Invisible to AI.** No LLM has access to it. Brainstorming with
  Claude or ChatGPT can't draw on the user's own curated reading.
- **Decaying.** Pages move, sites die, archive.org becomes the only
  surviving copy. Bookmarks rot silently.
- **Disconnected.** No cross-linking; no thematic grouping; no way
  to surface "what did I save five years ago about this topic."

Muninn turns the bookmark corpus into a **dual-interface knowledge
base**: a human-browsable wiki plus a vector-indexed MCP endpoint
that any LLM-aware tool can query.

---

## Architecture

There are two layers. A compiled wiki alone can't hold thousands of
pages ergonomically, and a vector index alone can't be browsed by a
human.

### Layer 1 — the vault

The vault has one markdown page per saved item, structured like this:

```markdown
# <page title>

- **URL:** sanitized-url
- **Source:** x | bookmarks_html | …
- **Captured:** YYYY-MM-DD
- **Last scraped:** YYYY-MM-DD
- **Tags:** tag1, tag2
- **Archive fallback:** web.archive.org link

## Summary
...

## Cross-references
- [[other-page-slug]]
```

It is Obsidian-browsable and git-versioned. You can navigate it by tag,
backlinks or full-text search inside Obsidian. The vault compiler
generates these pages from muninn's SQLite store. Curated notes (concepts,
infra, people, and reference project notes) are written by hand alongside
them.

### Layer 2 — semantic index

Embeddings of full text and summaries are indexed in Qdrant, on a
homelab LXC, behind a pluggable search interface. An MCP server exposes
the index so any Claude Code session can query it:

```
> What have I saved about distributed consensus algorithms?
[MCP returns the most relevant pages from the vault]
```

The vault is the **human interface**; the MCP server is the **LLM
interface**.

### Research Intake

Muninn can also assess selected saved items for suitability. It looks
at substance, evidence quality, novelty and topic relevance, then exports
**versioned, content-addressed evidence packets**. Odin, the operator's
project supervisor, consumes those packets to decide whether anything
applies to an active project. Muninn produces the evidence; it does not
make project decisions.

---

## How it runs

- **Capture and bulk enrichment:** source adapters (X bookmarks API and
  browser `bookmarks.html` export) feed an HTTP scraper (live + Wayback
  passes). The scraper feeds a Python async worker pool that calls the
  Anthropic API directly with Haiku. Every stage is a separate,
  idempotent CLI command.
- **Synthesis** (era narratives, deep passes, ad-hoc analyses) runs
  through a task-file → result-file contract. The default runner is a
  budgeted direct API call; any harness the operator chooses can run the
  same task.

Muninn is a standalone tool. It does not depend on any agent
orchestrator to run.

---

## Status

- **Implemented:** the browser-bookmark pipeline (ingest → sanitize →
  dual-pass scrape → enrich → vault compiler, MCP, timeline, Parquet),
  with a test suite. Real embeddings (EmbeddingGemma) and the Qdrant
  index are verified on a 100-bookmark smoke set.
- **In progress (v1):** X bookmarks end to end, plus Research Intake
  evidence-packet export. See `docs/muninn-roadmap.md`.
- **Next (v1.1):** the bulk run over the full browser-bookmark export.

Key documents:

- `SPEC.md`: pipeline design
- `docs/muninn-decisions.md`: ADR log
- `docs/research-intake/`: the Research Intake workflow
- `CLAUDE.md`: guidance for Claude Code sessions

---

## Relationship to other projects

| Project | Relationship |
|---|---|
| **Odin** | Consumes Research Intake evidence packets, and can use muninn as a recall source. It owns project state and decisions |
| **Skald** | Independent transcript archiver. A possible future source for session notes |
| **Braid** | Independent retrieval engine. A candidate backend for muninn's search layer |
| **Homelab** | Hosts the Qdrant vector DB, and hosts the vault git repo on Gitea |
| **Portfolio site** *(future)* | A possible public-facing variant: a curated subset of the vault published as a "things I've found worth remembering" reading list |

---

## Naming

Muninn (Old Norse: *Muninn*, "memory" or "the one who remembers") is
one of two ravens that fly across Midgard each day to bring news back
to Odin. Per Grímnismál:

> *"Huginn and Muninn fly each day over Jörmungandr; for Huginn I
> fear lest he come not back, but for Muninn my care is more."*

The naming fits: thought (Huginn) is a renewable resource, but
memory once lost is gone. A bookmark corpus IS personal memory —
losing it (link rot, service shutdown, lost browser profile) is
a real loss.

The name is historical. Muninn is a standalone tool, and Norse naming
isn't a requirement for anything new.

---

## What this is NOT

- **Not a generic bookmark manager.** Pinboard, Raindrop, Pocket,
  and others already do storage and tagging well. Muninn doesn't
  replace them; it builds *on top of* their exports and APIs.
- **Not a project tracker.** Project state, proposals and decisions
  belong to Odin. Muninn supplies the evidence.
- **Not a public service.** The corpus is personal; the value is in
  the curation. The vault stays private. Only individual entries
  worth sharing get republished elsewhere.
- **Not a general embeddings playground.** The architecture is
  optimized for "personal knowledge corpus, dozens to thousands of
  documents." Different design tradeoffs would apply for production
  RAG over millions of docs.

---

## Privacy

The corpus is personal. Bookmarks reveal interests, employment
history, projects considered, problems debugged — quasi-identifying
information. The vault and vector index stay private; only this
README and project-level design docs are public.
