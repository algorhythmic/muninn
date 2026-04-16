# Muninn

> *"…and Muninn returns each day to whisper into Odin's ear what he
> has remembered."*

A personal knowledge base built from a decade of accumulated
bookmarks, made queryable by both humans (via an Obsidian-browsable
wiki) and LLMs (via an MCP server). Named after Odin's raven of
**memory** — the one whose return Odin awaits more anxiously, because
"thought" can always be re-derived but **memory cannot**.

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

## Architecture (planned)

Two layers, because a compiled wiki alone can't hold 10k+ pages
ergonomically and a vector index alone is unbrowsable by a human:

### Layer 1 — Bragi-style vault

One markdown page per bookmark, structured as:

```markdown
# <page title>

- **URL:** original-url
- **Captured:** YYYY-MM-DD
- **Last scraped:** YYYY-MM-DD
- **Tags:** tag1, tag2
- **Archive fallback:** web.archive.org link

## Summary
...

## Key quotes
...

## Cross-references
- [[other-bookmark-slug]]
```

Obsidian-browsable, git-versioned. The user can navigate by tag,
backlinks, or full-text search inside Obsidian. The vault structure
mirrors what Saga's Bragi agent already produces for fleet
journals — same tooling, different corpus.

### Layer 2 — Semantic index

Embeddings of full-text + summaries indexed in a local vector DB
(Qdrant on a homelab LXC at `.19`, an unused slot in the Pi-hole DHCP
range). Exposed via an MCP server so any Claude Code session can
query during brainstorms:

```
> What have I saved about distributed consensus algorithms?
[MCP returns 5 most relevant bookmark pages from the vault]
```

The vault is the **human interface**; the MCP is the **LLM interface**.

---

## How it gets built

The original framing assumed Muninn was an **exercise of Saga at
scale** — submit a `bookmarks.html` export to Saga, Odin decomposes
it into N parallel scrape-and-summarize streams, each dvergr handles
its slice. That framing made Muninn dependent on Saga **Phase 3**
(spec decomposition + multi-dvergr launch), which isn't shipped yet.

The spec design (see `SPEC.md`) revised this. Two-tier execution:

- **Bulk per-bookmark enrichment (scrape + summary + tags):**
  Python async worker pool calling the Anthropic API directly with
  Haiku 4.5. No Claude Code instances, no dvergar, no Saga
  orchestration. The bottleneck is HTTP wait + LLM API latency — a
  worker pool handles it; a fleet would be over-engineering at this
  shape.
- **Synthesis (era narratives, flagged deep-passes, ad-hoc
  analysis):** Opus 4.6 with 1M context, run in a single Saga-style
  container that mounts the existing `saga-claude-credentials`
  volume for subscription-mode auth. This is a **sibling of dvergr**,
  not a consumer of Odin/heimdall.

This shifts the Saga dependency:

- Depends on Saga **infrastructure (Phase 1+2, already done)** —
  the credentials volume, the keepalive cron, the Trail of Bits
  devcontainer base image, the dvergr container-launch pattern.
- Does **not** depend on Saga **orchestration (Phase 3, not yet
  shipped)** — Odin's spec decomposition isn't needed at v1's
  single-instance synthesis volume.

Net: **v1 Muninn is no longer blocked on Saga Phase 3.** It can be
built immediately on top of the Saga infrastructure that already
exists. Multi-instance synthesis batching becomes a v2 question
when corpus size or simultaneous-task volume actually requires it —
that's when Saga's parallel-dvergr pattern earns its keep on
Muninn's synthesis side.

Muninn is still a **product** (the knowledge base) and is still an
eventual stress-test for Saga's fleet capabilities (v2+ multi-source
ingest will want them) — but it's now a near-term standalone build,
not a forcing function for an unshipped Saga phase.

---

## Status

Spec drafted (`SPEC.md`), implementation pending. Currently this repo
contains:

- This README (project framing and architecture sketch)
- `MUNINN.MD` — original brainstorming context
- `SPEC.md` — working specification, with the six core design
  decisions resolved
- `CLAUDE.md` — project-specific guidance for Claude Code sessions
- `docs/` — Saga architecture, setup, and insights documents that
  ground the synthesis-container design

Implementation can begin against the existing Saga Phase 1+2
infrastructure (credentials volume, keepalive cron, container
patterns) — no longer blocked on Saga Phase 3.

---

## Relationship to other projects

| Project | Relationship |
|---|---|
| **Saga** | Builds Muninn — Saga's dvergar handle the parallel scrape/summarize work; Saga's Bragi handles the vault compilation |
| **Homelab** | Hosts the Qdrant vector DB at `.19`, hosts the vault git repo on Gitea |
| **Memex** *(future)* | Sibling project — same architecture pattern but applied to homelab state instead of bookmarks |
| **Portfolio site** *(future)* | Possible public-facing variant: a curated subset of the vault published as a "things I've found worth remembering" reading list |

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

The project sits in a Norse-named ecosystem alongside Saga (the
goddess of history and storytelling) and follows the same
naming convention.

---

## What this is NOT

- **Not a generic bookmark manager.** Pinboard, Raindrop, Pocket,
  and others already do storage + tagging well. Muninn doesn't
  replace them; it builds *on top of* their export formats.
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
