# Saga Architecture

This document is a high-level tour of Saga's moving parts. For the
canonical design spec including MQTT schema, journal format, principle
evolution, and phase plan, read [`saga-spec.md`](../saga-spec.md) in
the repo root. For development guardrails and terminology, read
[`CLAUDE.md`](../CLAUDE.md).

## The problem Saga solves

You want to run multiple long-running "Ralph-style" coding loops at the
same time, managed by an AI orchestrator that knows about your whole
portfolio of work rather than the individual streams each dvergr is
focused on. You want to:

- Submit project specs (via Telegram, to your phone, anywhere).
- Have an AI decompose them into parallel workstreams.
- Have each workstream run autonomously in its own sandboxed container.
- Get insights, alerts, and approval requests pushed back to you
  asynchronously — no sitting at a terminal watching output scroll.
- Pay a fixed monthly subscription, not per-token.

Saga is the orchestrator for that.

## The agents

| Agent | Model | What it does | Where it runs |
|---|---|---|---|
| **Odin** | Opus | Ingests specs, decomposes into dvergr streams, monitors progress, cross-pollinates insights, talks to the co-pilot over Telegram | One persistent tmux pane in the `saga-odin` container |
| **Mimir** | Sonnet | Second set of eyes on every Odin decision — catches mistakes Opus makes. Reviews decompositions, guidance drafts, and principle proposals. Restraint-biased: `APPROVE_UNCHANGED` is the preferred verdict when no concrete improvement is identified (autoreason §2 Phase A) | Separate tmux pane in the same `saga-odin` container |
| **Dvergar** | Sonnet | Workers. Each runs the `/ralph-loop` plugin against a specific PRD inside its own `saga-dvergr` container. Per-iteration hooks publish status, journal, and convergence signal | N containers, one per work stream |
| **Bragi** | Sonnet | Skald. Compiles the fleet's raw outputs (journals, verdicts, guidance, project wikis) into an Obsidian-friendly git-versioned vault. Periodic batch compiler, not a daemon | `saga-bragi` container, runs on cron (default every 30 min) |
| **Heimdall** | — (Python) | Host VM daemon. Validates YAML launch requests from Odin and executes `docker run` on Odin's behalf. The security boundary between Odin and Docker | `heimdall.service` systemd unit on the host VM, outside any container |
| **Ratatoskr** | — (Python) | Sidecar. The only MQTT client in Odin's container; translates filesystem events ↔ MQTT, nudges Mimir/Odin tmux panes, emits journal-word-count metrics | Third tmux pane in the `saga-odin` container |
| **Co-pilot** | Human | Submits specs, approves decompositions, responds to alerts; browses Bragi's vault over Obsidian | Their phone or laptop, via Telegram + Obsidian |

Odin drives, Mimir critiques, dvergar work, Bragi chronicles, Heimdall
gates the docker API, Ratatoskr ferries messages, the co-pilot
navigates.

## The layers

```
┌──────────────────────────────────────────────────────────────────────┐
│ Human plane                                                          │
│   Co-pilot ←→ Telegram ←→ Odin (Claude Code + Telegram MCP plugin)   │
│   Co-pilot ←→ Obsidian  ←→ /opt/saga/vault (Bragi's output, SSHFS)   │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
                              │ spec files, commands, approvals
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Orchestration plane (all inside the saga-odin container)             │
│   Odin       (Opus, interactive Claude Code session)                 │
│   Mimir      (Sonnet, interactive Claude Code session, same container)│
│   ratatoskr  (Python sidecar: MQTT + inotify + metrics + send-keys)  │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
                              │ MQTT (retained topics), git
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Worker plane                                                         │
│   Dvergr #1 │ Dvergr #2 │ Dvergr #3 │ …                              │
│   Each = one Claude Code session running /ralph-loop in its own      │
│   Docker container, writing code, running tests, committing to Gitea.│
│   Per-iteration hooks: post-iteration → status + journal on MQTT;    │
│   check-convergence → converged signal on k=2 no-delta streak.       │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
                              │ docker API (heimdall-gated)
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Chronicle plane (independent, read-only consumer of the others)      │
│   Bragi (Sonnet, saga-bragi container, cron-scheduled)               │
│     reads  /opt/saga/odin-ws (RO)                                    │
│     writes /opt/saga/vault  (RW, git-versioned, Obsidian vault)      │
└──────────────────────────────────────────────────────────────────────┘
                              ▲
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────────┐
│ Infrastructure plane                                                 │
│   heimdall   (systemd service on HOST VM 192.168.86.17, outside      │
│               any container — the security boundary between Odin     │
│               and Docker)                                            │
│   keepalive  (cron-driven short-lived credential-refresh container)  │
│   Docker Engine on the Saga Docker host VM 106 (192.168.86.17)       │
│   Pi-hole    CT 100 (192.168.86.11) — DHCP + DNS for the homelab     │
│   Mosquitto  CT 101 (192.168.86.12) — MQTT broker                    │
│   Grafana    CT 104 (192.168.86.15) — metrics dashboards (Phase 4+)  │
│   Gitea      CT 107 (192.168.86.18) — git server                     │
│   Caddy      CT 108 (192.168.86.20) — reverse proxy, wildcard TLS    │
│                                       for `*.lab.daviddunn.dev`;     │
│                                       Saga containers bypass it and  │
│                                       use bare IPs per firewall      │
│                                       allowlist. See HOMELAB-        │
│                                       INTEGRATION.md for rationale.  │
└──────────────────────────────────────────────────────────────────────┘
```

## Three rules for message passing

Every communication channel in Saga follows one of three patterns,
chosen by where the sending and receiving ends are:

1. **Hooks** — for session-local events within ONE Claude Code session.
   Example: a dvergr's `post-iteration.sh` Stop hook runs `mosquitto_pub`
   after each iteration. The hook belongs to the same session it's
   reacting to.

2. **Sidecars** — for cross-session IO. Claude Code sessions are not
   daemons; they only run when prompted. Anything that needs to
   continuously watch a directory or topic runs as a long-lived
   process in its own tmux pane. Example: `ratatoskr.py` nudges
   Mimir via `tmux send-keys` when `mimir/pending/` gets a new file.

3. **MCP servers** — for external chat channels. The Telegram plugin
   runs an MCP server inside Odin's session that translates Telegram
   messages into prompts for Claude Code and vice versa.

**Never use a hook to reach across sessions, and never use a sidecar
for something that could be a tool call.** Hook-to-other-session
doesn't work (hooks can't talk to another session's stdin); tool-call
for everything is cleaner than sidecar proliferation.

## The credentials story (why this matters)

The single most surprising thing about Saga is how every container is
authenticated against your Max subscription.

**The short version:**
- One Docker named volume: `saga-claude-credentials`
- One file in that volume: `/home/vscode/.claude/.credentials.json`
- Populated ONCE by a human running
  [`scripts/auth/bootstrap-credentials.sh`](../scripts/auth/bootstrap-credentials.sh)
  which walks through an interactive `/login`
- Every Odin, Mimir, and dvergr container mounts the volume at
  `/home/vscode/.claude` — instant subscription auth, zero OAuth
  prompts per container

**Why not `CLAUDE_CODE_OAUTH_TOKEN`?** Because it doesn't work. Tested
2026-04-10: the env var path silently forces API billing mode and
returns 401 on every request. See
[`anthropics/claude-code#16238`](https://github.com/anthropics/claude-code/issues/16238)
and the "Critical Constraints" section of
[`CLAUDE.md`](../CLAUDE.md).

**Why not bake credentials into the image?** Because `docker push` to
Gitea would leak them. Named volumes stay on the host.

## Filesystem layout

### Inside the Odin container

```
/workspace/
├── odin/                    ← Odin's persistent state
│   ├── master-journal.md    ← Odin's memory across sessions
│   ├── project-registry.yml ← projects and their dvergar
│   ├── scripts/state.json   ← live state snapshot, written by sidecar
│   ├── incoming-journals/   ← journal entries from dvergar, written
│   │                          by sidecar from MQTT journal topics
│   ├── incoming-alerts/     ← alerts + convergence signals from sidecar
│   ├── outgoing-guidance/   ← Odin writes here, sidecar publishes
│   │   └── sent/            ← archive after publish
│   ├── outgoing-alerts/     ← Odin writes here for human-facing alerts
│   ├── outgoing-commands/   ← Odin writes container commands here;
│   │                          heimdall picks them up
│   ├── projects/<project>/  ← per-project wiki (raw/ + wiki/ layers;
│   │                          see odin/specs/templates/project-template/
│   │                          for the schema)
│   └── prds/<project>/      ← per-instance PRDs generated by Odin
└── mimir/
    ├── pending/             ← Odin writes review requests here
    ├── verdicts/            ← Mimir writes verdicts here
    └── processed/           ← reviewed requests, archived by Mimir
```

### Inside the Bragi container

```
/workspace/
├── CLAUDE.md  → /opt/saga/bragi/CLAUDE.md  (baked in, symlinked)
├── inputs/    ← /opt/saga/odin-ws mounted READ-ONLY
│   ├── odin/
│   └── mimir/
└── vault/     ← /opt/saga/vault mounted READ-WRITE (git repo)
    ├── README.md
    ├── index.md
    ├── log.md
    ├── projects/<project>/{journals,guidance,dvergar,decisions}/
    ├── decisions/
    ├── principles.md
    └── glossary.md
```

### Inside a dvergr container

```
/workspace/
├── PRD.md                  ← dvergr's specific PRD (bind-mounted in)
├── CLAUDE.md               ← dvergr's persona (either injected by Odin
│                             or seeded with a fallback by init-dvergr.sh)
├── .claude/settings.json   ← Saga hook registrations (written at startup)
├── .dvergr/
│   ├── journal.md          ← dvergr writes iteration entries here
│   ├── status.json         ← dvergr overwrites with current state
│   └── latest-guidance.md  ← pulled from MQTT by pre-iteration hook
└── <project source code, cloned from Gitea>
```

### On the HOST VM (outside any container)

```
/opt/saga/
├── .env.DEPRECATED.DO-NOT-USE      ← tripwire for the failed env var approach
├── secrets/
│   └── telegram-bot-token          ← 0600 root — mounted RO into saga-odin
├── workspaces/<project>/<instance>/ ← per-dvergr bind-mount targets
├── odin-ws/                        ← Odin's bind-mount target
│   ├── odin/ …                     ← see "Inside the Odin container"
│   ├── mimir/ …
│   ├── launch-requests/            ← heimdall input queue (dropped by Odin)
│   └── launch-results/             ← heimdall output queue (read by Odin)
└── vault/                          ← Bragi's output, git-versioned
                                     (co-pilot reads via Obsidian+SSHFS)

Docker named volumes:
  saga-claude-credentials            ← subscription creds shared by all containers

Systemd units on the host:
  heimdall.service                   ← container-launch daemon
```

## What runs where — the full matrix

| Component | Location | Lifetime | Restartable? |
|---|---|---|---|
| Odin (Claude Code Opus session) | tmux pane in saga-odin container | Days-weeks | Yes; master-journal.md restores context |
| Mimir (Claude Code Sonnet session) | tmux pane in saga-odin container | Same as Odin | Stateless; nothing to restore |
| `ratatoskr.py` sidecar | tmux pane in saga-odin container | Same as Odin | Yes; reconnects to MQTT, re-reads filesystem on start |
| Telegram MCP server (Bun) | Plugin process launched by Odin's session | Same as Odin's session | Restarts with Claude Code |
| Dvergr (Claude Code Sonnet session) | saga-dvergr container, one per work stream | Hours-days per PRD | Yes; journal.md restores context |
| `/ralph-loop` Stop hook | Inside each dvergr's session | Same as dvergr's session | N/A — part of plugin |
| Saga hooks (pre-iteration, post-iteration, check-convergence) | Inside each dvergr's session | Same as dvergr's session | N/A — fired on Stop |
| Bragi (Claude Code Sonnet session) | saga-bragi container, cron-scheduled | Minutes per invocation | `--rm` per run; state in `/opt/saga/vault/.git` |
| `heimdall.py` daemon | systemd service on HOST VM (.17) | VM uptime | Yes |
| `keepalive` container | Short-lived, cron-driven on HOST VM | Seconds per invocation | `--rm` per run |
| Mosquitto broker | CT 101 on Proxmox | Independent of Saga | Yes |
| Gitea | LXC on Proxmox | Independent of Saga | Yes |
| Grafana / InfluxDB | CT 104 on Proxmox | Independent of Saga | Yes |

## Phase plan at a glance

| Phase | Delivers | Status |
|---|---|---|
| Pre-Phase-1 | Docker host VM, Gitea, credentials-volume validation, Trail of Bits build | ✅ Done (2026-04-10) |
| Phase 1 | Single dvergr end-to-end: image built, MQTT publishing verified, subscription billing confirmed | ✅ Done (2026-04-13) |
| Phase 2 | Odin + Mimir + ratatoskr + heimdall all live; Telegram wired up; file-based Mimir flow working; keepalive cron for credential refresh | ✅ Done (2026-04-13) |
| Post-Phase-2 | Dev Claude on VM; Mimir `APPROVE_UNCHANGED` + metrics; spec-as-wiki restructure; Bragi skald; convergence-aware stuck detection | ✅ Done (2026-04-14) |
| Phase 3 | Spec ingestion through the wiki pipeline + decomposition + multi-dvergr launch | ⏳ Not started |
| Phase 4+ | See [`saga-spec.md`](../saga-spec.md) §12. Next-up: autoreason tournament for Odin strategic decisions | ⏳ Not started |

## Further reading

- [`saga-spec.md`](../saga-spec.md) — full design spec, canonical
- [`CLAUDE.md`](../CLAUDE.md) — development guardrails
- [`mqtt/TOPIC-SCHEMA.md`](../mqtt/TOPIC-SCHEMA.md) — MQTT topic/payload reference (including the `metrics` and `converged` topics)
- [`journal/JOURNAL-FORMAT.md`](../journal/JOURNAL-FORMAT.md) — journal structure
- [`odin/mimir/CLAUDE.md`](../odin/mimir/CLAUDE.md) — Mimir's persona and the restraint-biased verdict schema
- [`bragi/CLAUDE.md`](../bragi/CLAUDE.md) — Bragi's persona and the vault schema
- [`odin/specs/templates/project-template/CLAUDE.md`](../odin/specs/templates/project-template/CLAUDE.md) — spec-as-wiki schema and ingest workflow
- [`docs/SETUP.md`](SETUP.md) — step-by-step installation on a fresh homelab
- [`docs/HOMELAB-INTEGRATION.md`](HOMELAB-INTEGRATION.md) — how Saga
  ties into existing services
- [`docs/HOMELAB_NETWORKING_PROJECT.md`](HOMELAB_NETWORKING_PROJECT.md) — full homelab network buildout (Pi-hole DHCP/DNS, Caddy reverse proxy with wildcard TLS, Cloudflare split-horizon DNS); Saga's view of what it touches is in HOMELAB-INTEGRATION.md above
- [`docs/INSIGHTS-2026-04-13.md`](INSIGHTS-2026-04-13.md) — post-Phase-2 research log (autoreason, Karpathy wiki pattern, self-healing considerations)
- [`docs/INSIGHTS-2026-04-15.md`](INSIGHTS-2026-04-15.md) — repo strategy, Saga public-release stance, .env refactor plan
- [`docs/FEATURE-VOICE-TELEGRAM.md`](FEATURE-VOICE-TELEGRAM.md) — Phase 4+ candidate: STT/TTS sidecar for voice notes over Telegram
- [`docs/FEATURE-SPEC-DEVELOPMENT.md`](FEATURE-SPEC-DEVELOPMENT.md) — three-tier proposal for capturing design rationale during spec authoring (Tier 1 should land during Phase 3)
- [`docs/FEATURE-SECRETS-MANAGEMENT.md`](FEATURE-SECRETS-MANAGEMENT.md) — three-tier proposal for agent-safe secrets handling (file-on-disk → reference-based .env → per-project sops); Tier 1 should land during Phase 3
