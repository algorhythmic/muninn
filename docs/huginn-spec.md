# Huginn

**Spec for a multi-source information triage layer, built by Saga.**

*Named after the raven of thought — sibling to Muninn, the raven of memory. Where Muninn persists what you've chosen to remember, Huginn surveys what's emerging and decides what's worth your attention.*

This document is the source of truth for Huginn's design. It is intended for ingestion into Saga's Phase 3 spec pipeline, where Odin will decompose it into parallel dvergar workstreams.

For architectural rationale behind key decisions referenced here, see Muninn's [`DECISIONS.md`](../muninn/DECISIONS.md) — specifically ADR-003 (Huginn as separate service) and ADR-004 (vault as source of truth).

---

## 1. Overview

Huginn is a personal information triage dashboard. It consolidates the daily workflow of scanning GitHub trending, Hacker News, subreddits, and newsletters into a single information-dense feed, personalizes candidate items against the operator's knowledge graph (Muninn), enriches each with a short "why this surfaced" summary, and offers a one-click promotion action that writes durable items into Muninn's vault.

Huginn is *not* a knowledge base. It does not store anything durably. It operates between the world's noisy information streams and Muninn's curated long-term memory. Items that matter get promoted to Muninn; the rest evaporate after a TTL.

Huginn is the third named project in the Norse-named homelab ecosystem, after Saga (the coding orchestrator) and Muninn (the unified personal knowledge base). It will be built *by* Saga through Phase 3's spec ingestion pipeline, as one of Saga's first multi-workstream project deliveries.

## 2. Goals

- Consolidate information discovery across GitHub, Hacker News, Reddit, and newsletters into a single dashboard.
- Score candidate items against the operator's actual interests using signals from Muninn's knowledge graph — contributor overlap, concept overlap, topic/language match.
- Enrich each candidate with a short natural-language summary explaining *why it surfaced*, not *what it is*.
- Present the feed in an information-dense UI optimized for fast skim passes, with a low-friction promotion action.
- Ship v0.1 with GitHub-only ingestion as a proof-of-pattern; add adapters progressively in later phases.
- Stay within the homelab's existing stack — Python, FastAPI, DuckDB, Haiku via direct Anthropic API, Tailscale for remote access.

## 3. Non-goals

- Not a durable knowledge base. Muninn handles that.
- Not an RSS reader. Filtering and personalization are the product; raw consumption is explicitly not.
- Not an archive. Unpromoted items rot from the inbox after a TTL (default 30 days).
- Not X/Twitter ingestion. Nitter has collapsed to unreliability, third-party API access violates the Max-subscription spirit, maintenance burden is disproportionate. Deferred indefinitely; revisit only if a clear path emerges.
- Not a general-purpose tool. Personalized to the operator's specific knowledge graph via Muninn.
- Not multi-user. Single-operator system.

## 4. Architecture

Huginn runs as a standalone service in its own LXC. It consists of seven cooperating components, any of which is a plausible dvergr workstream for Saga's Phase 3 decomposition:

1. **Source adapter framework.** An abstract `SourceAdapter` interface. Each adapter returns a stream of canonical `Item` objects. v0.1 ships one concrete adapter (GitHub); later phases add more.
2. **Normalizer.** Converts adapter output to the canonical `Item` schema, handles cross-source deduplication, tags with fetch timestamp and source provenance.
3. **Scorer.** Computes a per-item relevance score from three independent signals: velocity (rate of attention relative to baseline), graph overlap (contributors and concepts matching Muninn's graph), topic match (languages and subjects aligning with operator profile).
4. **Enricher.** Python async worker pool generating one-line "why this surfaced" summaries per item via the Anthropic API using Haiku 4.5. Pattern established by Muninn's bulk bookmark enrichment (Muninn SPEC §4).
5. **Operational store.** DuckDB holding item staging, scoring cache, fetch history, per-source rate-limit state. Ephemeral and prunable. No durable wisdom here.
6. **API server.** FastAPI endpoints serving the dashboard, receiving promote/reject actions, exposing a metrics endpoint.
7. **Dashboard.** HTMX + minimal CSS. Information-dense card feed with keyboard navigation and one-click promote.

Data flow:

```
  GitHub API     HN API     Reddit RSS     IMAP
      │            │            │            │
      └────────────┴────┬───────┴────────────┘
                        ▼
                 Source adapters
                        │
                        ▼
                   Normalizer
                        │
                        ▼                    ┌──────────────────┐
                    Scorer ───── queries ───▶│ Muninn MCP (.19) │
                        │                    └──────────────────┘
                        ▼
                   Enricher (Haiku 4.5 async pool)
                        │
                        ▼
                   DuckDB (ops state)
                        │
                        ▼
                   FastAPI ─── HTMX ─────▶ Browser (via Tailscale)
                        │
                        │ on promote
                        ▼
              ┌──────────────────────────┐
              │  Muninn vault (.17 NFS)  │
              │  raw/inbox/huginn/*.md   │
              └──────────────────────────┘
```

Muninn's MCP is a soft dependency — Huginn degrades gracefully to rule-based scoring when unavailable. Muninn's vault is a hard dependency for the promote action — failure holds promotion in a pending state for retry.

## 5. Data model

### Canonical Item

Every source adapter produces items in a common shape:

```python
@dataclass
class Item:
    # Source identity
    id: str                       # deterministic hash, stable across refetches
    source: str                   # "github", "hn", "reddit", "newsletter"
    source_id: str                # adapter-local identifier (repo full name, HN story id, etc.)

    # Content
    title: str
    url: str
    author: str | None
    published_at: datetime
    raw_text: str | None          # body if available; else None

    # Source metadata (pass-through)
    metadata: dict[str, Any]      # preserved from adapter for later enrichment/display

    # Pipeline state
    fetched_at: datetime
    score: float | None           # populated by scorer
    score_breakdown: dict         # {"velocity": 0.8, "graph": 0.6, "topic": 0.9}
    score_reasons: list[str]      # human-readable labels for UI chips
    summary: str | None           # populated by enricher

    # Operator state
    state: Literal["inbox", "promoted", "rejected", "expired"]
    state_changed_at: datetime
```

### Scoring

Three independent signals, summed with configurable weights (default equal):

- **Velocity.** How much attention is this item getting relative to its source's baseline? Z-score against source-appropriate history (e.g. for a GitHub repo: stars-in-last-24h vs. 30-day baseline). Normalized to [0, 1].
- **Graph overlap.** Does this item connect to the operator's Muninn graph via contributors, linked concepts, or already-bookmarked references? Queries Muninn's MCP. Normalized to [0, 1].
- **Topic match.** Does this item match the operator's declared interests (top languages, tracked topics, bookmarked domains)? Computed from a profile vector. Normalized to [0, 1].

Items below a configurable threshold are not surfaced at all. Items above threshold are sorted by composite score for dashboard display.

### Operational tables (DuckDB)

| Table | Purpose | Pruning |
|---|---|---|
| `items` | Current inbox + recent history | TTL 30d unpromoted, 90d promoted |
| `fetch_history` | Per-source last-successful-fetch timestamps | Permanent |
| `rate_limit_state` | Current backoff per source | In-memory; persists only for restart resilience |
| `contributor_cache` | GitHub contributor lists, cached by repo | TTL 7d |
| `velocity_baselines` | Per-repo 30-day star baselines | TTL 7d, recomputed |
| `operator_profile` | Computed interest vector | Recomputed weekly |

## 6. Source adapter interface

Every source adapter implements:

```python
class SourceAdapter(ABC):
    source_name: str
    fetch_interval: timedelta

    @abstractmethod
    async def fetch_candidates(self, since: datetime | None) -> list[Item]:
        """Return candidate items since the given timestamp (or initial batch if None)."""

    @abstractmethod
    async def enrich_metadata(self, item: Item) -> Item:
        """Fetch additional metadata not available in the initial fetch (e.g. contributors)."""
```

Rate-limit state is tracked per adapter in DuckDB. Adapters are expected to back off on 429s with exponential delay, log failures to a per-source MQTT topic (`huginn/adapters/{source_name}/#`), and never block the pipeline — a failing adapter yields an empty list, not an exception.

Adapter configuration (auth, thresholds, polling intervals) lives in environment variables or a YAML config file; not hardcoded.

## 7. v0.1 adapter: GitHub

GitHub is the first and most important adapter. It proves the pattern end-to-end.

### Fetch strategy

Two complementary sources:

1. **github.com/trending** — HTML scrape, daily. No API rate limit. Provides a curated top-of-noise list.
2. **GitHub Search API** — `created:>N_days_ago stars:>M` scoped to operator's top languages. Authenticated via PAT (5000 req/hr). Catches rising items before they hit trending.

### Metadata enrichment

Per item, the adapter fetches:
- Top 20 contributors (via `/repos/{owner}/{repo}/contributors`)
- Repo metadata (description, topics, license, primary language)
- Star history for velocity baseline (first encounter only; cached 7d)

### Scoring inputs

- **Velocity:** stars gained in last 24h vs. 30-day baseline. First-day repos get a flat high score (no baseline possible).
- **Graph:** contributors overlap with Muninn's `wiki/bookmarks/**/*.md` frontmatter `contributors:` field; topics overlap with `wiki/concepts/*.md`.
- **Topic:** primary language matches operator's top-5 languages (from profile); topics match tracked topic list.

### Configuration

```yaml
huginn:
  github:
    pat_env: GITHUB_PAT
    trending_languages: [python, rust, typescript, go]
    search_min_stars: 5
    search_max_age_days: 30
    poll_interval: 1h
```

## 8. Enrichment pipeline

Per-item enrichment is a stateless transformation: candidate Item in, Item with `summary` out. Muninn's SPEC §4 established this pattern for bookmark enrichment — Huginn reuses the same architecture.

### Worker pool

- Python asyncio, configurable concurrency (default 8 workers)
- Calls `anthropic.messages.create()` with Haiku 4.5
- Fixed prompt template including item title, URL, score breakdown, and the raw text if under 2KB
- Retries on transient errors (5xx) with exponential backoff
- Rate-limit aware — respects Anthropic's per-minute token and request limits

### Prompt template (abbreviated)

```
You are summarizing why a specific item surfaced in a personal information
triage feed for a homelab/AI/engineering audience.

Item:
  Title: {title}
  URL: {url}
  Source: {source}

Score reasons: {score_reasons}
Score breakdown: {score_breakdown}

{contributor_overlap_section if graph_score_applied}

Write one sentence explaining why this item is worth the reader's attention
given these reasons. Be concrete. Do not describe what the item is — that's
in the title. Describe why it rose above noise today specifically.

Examples:
- "Gained 412 stars in 3 days (4.3σ above baseline); contributors alice
  and bob also maintain [bookmark-linked repo]."
- "First release of a tool that overlaps with 3 concepts in your graph:
  MQTT orchestration, agent fleets, Norse-themed naming."
- "HN front page for 6+ hours with 400+ comments; author is the maintainer
  of a project you've bookmarked."

Return only the one sentence. No preamble.
```

### What this is not

Not a persistent Claude Code session. Not a dvergr. Not using OAuth credentials or tmux. Not sharing `saga-claude-credentials`. This is a direct, stateless, billable API call — cheap for Haiku, fast, and operationally trivial.

The persistent Claude Code session pattern (Saga's dvergar) is reserved for long-running coding tasks that need to converge on a spec. Huginn's enrichment is stateless per-item summarization. Different shape, different tool.

## 9. Muninn integration

Huginn talks to Muninn over two interfaces.

### Read — MCP

Huginn's scorer queries Muninn's MCP endpoint (running at homelab `.19`) for graph signals:

- `muninn.search_contributors(names: list[str]) -> list[dict]` — which of these contributors appear in Muninn's bookmarks or projects? Returns matching pages with links.
- `muninn.search_concepts(text: str, top_k: int = 5) -> list[dict]` — does this text overlap with concepts in Muninn's wiki? Returns concept pages with similarity scores.
- `muninn.has_bookmark(url: str) -> bool` — is this URL already bookmarked? Avoids surfacing duplicates.

MCP endpoint URL is configured via `HUGINN_MUNINN_MCP_URL`. Huginn's scorer has a circuit breaker: if MCP is unreachable or slow (>500ms p99), graph signals are skipped for that fetch cycle and rule-based scoring carries the load. Logged to MQTT; not a user-visible failure.

### Write — vault

The promote action writes a markdown file into Muninn's `raw/inbox/huginn/{item_id}.md`:

```markdown
---
source: github
source_id: owner/repo
url: https://github.com/owner/repo
title: Example repository title
fetched_at: 2026-04-19T14:30:00Z
promoted_at: 2026-04-19T15:45:00Z
huginn_score: 0.87
score_breakdown:
  velocity: 0.92
  graph: 0.78
  topic: 0.91
score_reasons:
  - velocity
  - contributor-overlap
  - language-match
huginn_summary: "Gained 412 stars in 3 days..."
contributors: [alice, bob, charlie]
primary_language: Rust
topics: [mqtt, orchestration, homelab]
---

# {title}

{raw_text if available, else the item's description}
```

Muninn's normal ingest pipeline picks up the file and processes it through its standard bookmark enrichment flow (see Muninn SPEC §4).

Vault path is configured via `HUGINN_MUNINN_VAULT_PATH` (mounted via NFS or similar). On write failure, the item stays in Huginn's inbox with a `promote_pending` flag; retry on next fetch cycle.

## 10. Dashboard UX

The dashboard is the product. It must be information-dense enough to replace ten browser tabs without losing signal.

### Layout

- Single scroll view, highest-score-first (tie-break by most recent)
- Each item renders as a card: source icon, title (truncated), score chip, score-reason chips, summary (one line), promote/reject buttons
- Hover (or tap on mobile) reveals: full score breakdown, contributor list, source-specific metadata, published timestamp
- Keyboard navigation: `j`/`k` next/previous, `p` promote, `r` reject, `/` filter, `.` refresh
- Filters: source chips (click to toggle), score threshold (slider), time window (today / week / month)
- No infinite scroll — cap at last N items (configurable, default 200) with a "load more" affordance at the bottom
- Promoted and rejected items disappear from the feed immediately with a 3-second undo toast

### Refresh

- Initial pull on page load
- Background refresh via HTMX polling every 5 minutes (new items appear at top with a subtle "N new" indicator; do not displace current view until operator scrolls)
- No SSE or websocket in v0.1 — HTMX polling is simpler and sufficient

### Auth

- v0.1: Tailscale-only. Dashboard binds to `tailscale0` interface; no public exposure; no authentication layer.
- v0.2+: optional basic auth for LAN access; Tailscale still preferred for remote.

### Styling

- HTMX + custom CSS, no framework (keep it simple; dashboard is a personal tool)
- Dense: ~20 cards visible without scrolling on a 1440p display
- Dark mode default; light mode toggle in header
- No JavaScript frameworks; HTMX + vanilla JS for keyboard shortcuts

### Explicitly out of scope

- Search. If the operator wants to search, they should use Muninn's MCP. Huginn is the triage layer; finding things again is not its job.
- Analytics dashboard. Promotion rates and score distributions go to Grafana in v0.4.
- Mobile-first responsive design. Desktop-primary in v0.1; responsive is nice-to-have, not required.

## 11. Phase plan

### v0.1 — GitHub only, rule-based scoring, local dashboard

**Scope:** Prove the pattern end-to-end. No Muninn dependency. Rule-based scoring (stars velocity + language match + keyword match against a static `interests.yml`). Haiku enrichment. Promotion writes to a local `promotions/` directory for inspection (not yet to Muninn's vault). Tailscale-only dashboard.

**Success criteria:** Operator can skim the dashboard daily, promote items, and observe sensible items being surfaced.

**Estimated workstreams for Odin:** 5-7 dvergar in parallel (adapter framework, GitHub adapter, scorer+enricher, FastAPI+scheduler, dashboard, deployment).

### v0.2 — Muninn integration

**Scope:** MCP client library. Graph-signal scoring (contributor overlap, concept overlap). Vault writes on promotion. Requires Muninn's MCP stable (Muninn Phase 2 complete).

**Success criteria:** Promoted items appear in Muninn's vault and are discoverable via Muninn's MCP. Graph-informed scoring demonstrably differs from rule-based scoring on the same item set.

### v0.3 — Additional adapters

**Scope:** HN API adapter, Reddit RSS adapter, Newsletter IMAP adapter. Each is an independent dvergr workstream. Scoring weights tuned per source (HN velocity curve differs from GitHub's; newsletter items have no velocity concept).

**Newsletter specifics:** dedicated email alias (e.g. `david+news@...`), IMAP polling, Haiku-based section extraction from newsletter HTML, each extracted section becomes an Item.

### v0.4 — Observability

**Scope:** Telegraf → InfluxDB → Grafana. Per-adapter fetch success rates, enrichment latency, promotion rate, score distribution. Shares Saga's TELEGRAF-WIRING work once that lands.

**Success criteria:** Dashboard at `grafana.homelab/huginn` shows the health of every adapter and the operator's promotion rate over time.

### Deferred

- X/Twitter adapter (blocked on Nitter viability; revisit only if a clear path emerges)
- Discord adapter (requires bot-per-server; manual toil not worth v0 scope)
- Multi-user support (may never happen)
- Cross-device dashboard sync (Muninn handles durable state; no need to replicate inbox)
- Learned scoring weights from promote/reject feedback (v0.3+ at earliest; requires enough data)

## 12. Open questions

These should be resolved during Odin's spec decomposition and may be answered differently per workstream.

- **Promotion side-effects.** When an item is promoted, should Huginn delete the raw inbox entry or mark it as `promoted`? Recommendation: mark promoted, let TTL purge. The promoted file in Muninn is the durable copy.
- **Rate-limit recovery.** If an adapter gets throttled hard (403 from GitHub, IP-banned from Reddit), should Huginn back off permanently until operator intervention, or retry with hourly jitter? Recommendation: exponential to 1 hour, then hourly retries, logged prominently.
- **Profile bootstrap.** v0.1's interest profile needs an initial seed. Options: (a) operator writes `interests.yml` manually, (b) Huginn bootstraps from operator's GitHub stars on first run, (c) both. Recommendation: (c). Starred repos seed the profile; manual yaml overrides and extends.
- **Pruning cadence.** How long do unpromoted items stay in DuckDB before purge? Default 30 days. Revisit if storage becomes a concern.
- **Score calibration.** Initial scoring weights are guesses. Feedback loop that adjusts weights based on promote/reject actions is scoped for v0.3 at earliest.
- **Dashboard identity.** v0.1 is strictly single-page (one view, no navigation). A "promoted items history" view may be needed in v0.2+ for debugging personalization — defer until the need is observed.
- **Dedupe across sources.** Same URL surfaced by multiple adapters should merge. Key: `url` after canonicalization (strip utm params, normalize case). Implementation: in the normalizer, not individual adapters.

## 13. Deployment

**Target:** new LXC on Proxmox, Debian 13. Operator selects IP from the available safe range (current allocations: `.10`–`.18` occupied by homelab services, `.19` is Muninn's Qdrant, Saga's Docker host is at `.17`). IP choice is not load-bearing; documented in the Muninn `projects/huginn.md` page once committed.

**Container layout:**
```
/opt/huginn/
├── src/                    # application code
├── data/                   # DuckDB file, fetch caches
├── config/
│   ├── interests.yml       # operator-maintained
│   └── .env                # secrets (PATs, API keys)
└── logs/
```

systemd unit `huginn.service` runs FastAPI + scheduler in one process. Health endpoint at `/healthz`.

**Environment variables:**
```
HUGINN_DUCKDB_PATH=/opt/huginn/data/huginn.duckdb
HUGINN_MUNINN_MCP_URL=http://192.168.86.19:PORT/mcp
HUGINN_MUNINN_VAULT_PATH=/mnt/muninn-vault
HUGINN_INTERESTS_PATH=/opt/huginn/config/interests.yml
ANTHROPIC_API_KEY=<operator's API credit allowance>
GITHUB_PAT=<read-only PAT>
```

**Homelab dependencies:**
- Muninn's MCP server (soft — degrades gracefully)
- Muninn's vault filesystem (hard for promotion, soft for operation)
- Mosquitto at `.12` (optional, for observability only)
- Tailscale (required for remote dashboard access)

**Monitoring:**
- Uptime Kuma at `.16` monitors `http://<huginn-ip>:8080/healthz`
- Grafana at `.15` (v0.4+) reads per-adapter metrics from MQTT

## 14. Relationship to other projects

- **Built by Saga.** Huginn is Saga's second real spec-ingestion project (after Muninn). Odin decomposes this spec into dvergar workstreams; Mimir audits the decomposition; Bragi compiles session journals; Eir converts retrospectives into PRs. Saga's existing infrastructure (credentials volume, devcontainer, container-launch pattern, Gitea) applies directly during the build.
- **Depends on Muninn.** Soft at the MCP layer (graceful degradation), hard at the vault layer for the promote action. See ADR-003 in Muninn's DECISIONS.md for why these stay separate services.
- **Reads Saga's patterns.** The Haiku worker pool pattern, the DuckDB + FastAPI stack, the LXC deployment convention, the MQTT observability pattern all come from existing Muninn and Saga work. No new stack components.

Huginn's existence completes a loop: Saga builds Huginn; Huginn discovers things worth attention; Muninn remembers what Huginn promotes; Saga queries Muninn for context on future builds. Each project earns its place in the ecosystem.

## 15. Forward-compatibility requirements

Saga has a deliberate generalization roadmap (`saga/docs/GENERALIZATION.md`) that will eventually convert its Norse-specific implementation into a reusable cooperative-agent framework. That generalization is gated on triggers that have not yet fired — Huginn does not need to be built with generalization in mind. But a handful of cheap habits keep Huginn forward-compatible without spending current scope on abstractions.

These are **build-time requirements**, not aspirations. Dvergar building any Huginn component should treat them as in-scope from day one. Each costs near zero at implementation time and avoids a schema break or prompt rewrite later.

### 15.1 MQTT topic routing

Every topic string used for publish or subscribe must go through a topic-builder helper. No bare f-string concatenations like `f"huginn/adapters/{source}/status"` scattered through adapter code. Topic construction lives in a single module (e.g. `huginn/mqtt/topics.py`) that exposes typed functions:

```python
huginn.mqtt.topics.adapter_status(source="github")  # → "huginn/adapters/github/status"
huginn.mqtt.topics.scoring_done(run_id="...")       # → "huginn/scoring/done"
huginn.mqtt.topics.promotion_event(action="promote") # → "huginn/promotions/promote"
```

Topic schema is documented in `huginn/mqtt/TOPIC-SCHEMA.md` (mirrors Saga's `mqtt/TOPIC-SCHEMA.md` convention). Parallel to Saga's ratatoskr topic-builder pattern.

### 15.2 Typed event payloads

Every event Huginn emits on MQTT or otherwise is a typed JSON object with an inline `schema_version` field. Free-form dictionaries are not acceptable. Event schemas live in `huginn/events/schemas/*.json` and are imported by publishers. Example envelope:

```json
{
  "schema_version": "1.0",
  "event_type": "scoring_completed",
  "run_id": "...",
  "timestamp": "2026-04-19T14:30:00Z",
  "source": "github",
  "payload": { "items_scored": 42, "above_threshold": 8 }
}
```

This is the cheapest down payment on Saga's generalization Move 1 (typed journals). Extending schemas later is additive; starting with free-form dicts forces a migration.

### 15.3 Run identifier threading

Each polling cycle, enrichment batch, or promotion action has a `run_id` (UUID v4) that threads through every event emitted during that operation. Consumers can join events by `run_id` to reconstruct a logical operation end-to-end (fetch → score → enrich → promote).

When Huginn eventually consumes signals from Claude Code sessions (none in v0.1; could appear in later phases for operator-in-the-loop scoring review), the Claude session UUID goes in a separate `claude_session_id` field, not merged into `run_id`.

### 15.4 Surface-agnostic operator notification

Huginn has no operator notification surface in v0.1. If one is added later (e.g. Telegram alert when a high-score item surfaces, email digest for daily summary), the capability is named `notify_operator` and abstracted behind a single function. Adapter code and scoring logic must not contain Telegram-specific, email-specific, or Slack-specific strings. This parallels Saga's generalization Move 5 (chat-surface adapter).

### 15.5 Norse names are names, not types

`agent_role == "auditor"` beats `is_mimir` in a conditional. `from_knowledge_base` beats `from_muninn`. `source_name == "github"` beats `is_github_source`. Naming conventions for the ecosystem can change without refactoring logic; do not encode Norse lineage into type checks, variable names used for dispatch, or schema field names. Norse names appear in prose, documentation, and as human-facing labels, not in control flow.

---

## 16. Naming and lineage

Huginn (*"thought"*) and Muninn (*"memory"*) are the two ravens Odin sends each day to bring back news from across the world. Muninn stores what matters; Huginn observes what's emerging. Odin — Saga's orchestrator — is fed by both.

The Norse convention is canonical for this homelab. Future services pulled from the pantheon should serve clearly distinct roles, not duplicate existing ones. Ratatoskr is already taken (Saga's Python MQTT sidecar); Heimdall is already taken (Saga's container launcher); Eir is already taken (Saga's retrospective-to-PR healer).

---

## Appendix A — Saga workstream decomposition hints

For Odin's use when ingesting this spec. These are suggestions, not mandates.

Candidate parallel workstreams (dvergar):

1. **`huginn-core`** — SourceAdapter ABC, Item dataclass, Normalizer, DuckDB schema + migrations. Smallest dvergr; blocking for others.
2. **`huginn-github`** — GitHub adapter, including trending scrape + Search API + metadata enrichment. Depends on `huginn-core`.
3. **`huginn-scorer`** — Scoring engine, velocity baseline calc, profile vector computation. Initially rule-based; later adds MCP-based graph signals. Depends on `huginn-core`.
4. **`huginn-enricher`** — Haiku worker pool, prompt template, retry logic. Depends on `huginn-core`.
5. **`huginn-api`** — FastAPI + scheduler, endpoints, promote/reject handlers. Depends on `huginn-core` and `huginn-scorer`.
6. **`huginn-dashboard`** — HTMX views, CSS, keyboard shortcuts, filter logic. Depends on `huginn-api` contract.
7. **`huginn-deploy`** — Dockerfile, systemd unit, LXC provisioning script, environment config. Depends on all others being built.

Workstream 1 blocks the rest; 2–5 can parallelize; 6 depends on 5's API contract; 7 is last.

For v0.2 and later phases, additional workstreams:

- **`huginn-muninn-client`** (v0.2) — MCP client library, circuit breaker, graph signal integration into scorer.
- **`huginn-muninn-writer`** (v0.2) — vault write path, frontmatter generation, retry-on-fail.
- **`huginn-hn`**, **`huginn-reddit`**, **`huginn-newsletter`** (v0.3) — additional adapters; each independent.
- **`huginn-observability`** (v0.4) — Telegraf integration, MQTT metrics topic publishing, Grafana dashboard definition.

## Appendix B — Configuration skeleton

Example `interests.yml` for v0.1:

```yaml
languages:
  - python
  - rust
  - typescript
  - go

topics:
  - mqtt
  - homelab
  - agent-orchestration
  - proxmox
  - llm
  - observability

domains_of_interest:
  - github.com
  - news.ycombinator.com
  - old.reddit.com/r/selfhosted
  - old.reddit.com/r/homelab
  - old.reddit.com/r/LocalLLaMA

score_weights:
  velocity: 1.0
  graph: 1.0
  topic: 1.0

score_threshold: 0.5
```
