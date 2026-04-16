# Insights & Next Moves — 2026-04-13

Captures three threads from the 2026-04-13 research session:
1. Dev-loop improvement (move dev Claude onto VM .17)
2. Autoreason (Nous Research) applied to Mimir
3. Karpathy's LLM wiki pattern applied to Saga's journal/principles

Plus a short list of new project concepts the above enables.

---

## 1. Dev Loop: Move Dev Claude onto VM .17

### Why

Current loop: Claude on Mac → human → VM → human relays output → Claude.
The human is a lossy relay. Running Claude Code directly on VM .17 gives
it bash, docker, git, container logs, iptables, and Gitea reach in one
session. Biggest single productivity win available.

### Key constraint — keep dev auth separate from agent auth

Dev Claude must use its own `~/.claude/.credentials.json` in the VM user's
home dir. **Do NOT** point it at the `saga-claude-credentials` Docker
volume. Agents are already wrestling the refresh-token auto-refresh bug;
a second client writing to the same credentials file would make auth
failures impossible to attribute.

Same Max subscription, separate credential files.

### Blast radius

Dev Claude on the VM can:
- `docker volume rm saga-claude-credentials` (nukes all agent auth)
- Stop Odin / Mimir / dvergar mid-iteration
- Force-push to any Saga repo in Gitea
- Touch iptables / systemd units

Mitigations:
- Run as a non-root user with `docker` group membership. `/opt/saga/secrets/`
  stays 0750-unreadable without explicit sudo — that's the correct default.
- Drop a VM-local `~/saga-dev/CLAUDE.md` with explicit guardrails mirroring
  the project CLAUDE.md: never `docker volume rm`, never force-push main,
  never `-f` anything in `/opt/saga/`, confirm before stopping a running
  Saga container.

### Implementation steps

1. SSH into VM .17 as the homelab user (via Tailscale).
2. Install Claude Code on the VM (same `curl .../install.sh` path the
   Trail of Bits Dockerfile uses — pins match).
3. Run `claude` → `/login` → "Claude account with subscription". Needs a
   browser on the Mac for the OAuth URL (Mullvad OFF). Confirm with
   `/status`: "Login method: Claude Max Account".
4. Generate a dedicated SSH keypair on the VM:
   `ssh-keygen -t ed25519 -C "saga-dev@vm17" -f ~/.ssh/saga_dev_ed25519`.
5. Add the public half to your Gitea account (Settings → SSH Keys).
6. Test: `git clone git@gitea.home.lab:david/saga.git ~/saga-dev` and
   `git push` on a throwaway branch.
7. Write `~/saga-dev/CLAUDE.md` with guardrails (copy the "Executing
   actions with care" block from the project CLAUDE.md, add VM-specific
   prohibitions listed above).
8. Start a persistent tmux session: `tmux new -s saga-dev`, launch
   `claude` inside it. Detach/reattach over Tailscale as needed.
9. Keep the Mac Claude session for: research, web reading, multi-repo
   work, anything not Saga-internal. Two seats, two roles.

### Follow-ups to watch

#### Subscription capacity — second seat vs multiplexing

"Multiplexing" here means **all Saga agents share one subscription's
usage pool**. Anthropic's Max plans meter usage in 5-hour rolling windows
plus a weekly cap, and the meter aggregates across every machine and
session logged into that account. Today that pool is: Odin (Opus,
long-running), Mimir (Sonnet, reactive), keepalive (Sonnet, every 4h,
trivial), and soon dev Claude (mixed, potentially heavy). Four clients,
one meter.

The contention failure modes are:
- **5-hour window exhaustion** — a long dev-Claude investigation
  consumes the Opus budget Odin needed for its next decomposition,
  and Odin silently stalls or drops to degraded responses.
- **Weekly cap exhaustion** — late in the week, a burst of dvergr
  iterations plus a long debugging session tips the account over
  and *everything* freezes until reset.
- **Attribution loss** — when something's wrong, you can't tell
  which client ate the budget without cross-referencing timestamps.

"Second seat" means **buy another Max subscription on a separate
Anthropic account** (or move to Team/Enterprise, which exposes per-seat
metering natively). Dev Claude logs in as user-B; Saga agents stay on
user-A. Independent 5-hour windows, independent weekly caps, clean
attribution. Cost roughly 2x. The right trigger to pull it: the first
time a fleet stall gets traced back to dev Claude having eaten the
budget. Until then, single-seat multiplexing is cheaper and fine.

Cheaper alternatives worth trying before buying a second seat:
- Restrict dev Claude to Sonnet-class models for routine work; reserve
  Opus for decisions that demand it.
- Run the keepalive as a smaller Haiku-class session if it's only
  exercising the refresh path (verify it still hits the refresh code
  path in Haiku mode before committing to this).

#### Permanent dev-Claude footprint

If dev Claude earns its keep past the trial week:
- Create a dedicated `saga-dev` user on the VM (docker group, not
  root, no sudo to `/opt/saga/secrets/`).
- Minimal dotfiles: `.tmux.conf`, `.gitconfig`, the SSH key to Gitea,
  the VM-local `CLAUDE.md` with guardrails.
- Add `/home/saga-dev/` to the Proxmox backup scope so the credentials
  file, SSH key, and any in-flight work survive VM loss.
- Rotate the Gitea SSH key on the same cadence as other homelab keys.

### Does this make Saga "self-healing"?

Short answer: **no, not by itself.** A dev Claude on the VM that can
read logs, write code, and push to Gitea is a *closed development
loop* — a human still has to notice something's wrong and ask dev
Claude to fix it. Self-healing requires removing the human trigger.

The distinction matters. What dev-Claude-on-VM gets you:
- Faster human-triggered fixes (minutes instead of copy-paste hours).
- Ability for *you* to say "Odin is stuck, investigate and fix" and
  have it done without relay.
- Ability to run ad-hoc diagnostics without context-switching.

What it does NOT give you:
- Autonomous detection of problems.
- Autonomous authorization to act without you in the loop.
- Guardrails against a bad fix making things worse.
- Verification that the fix actually worked.

#### What full self-healing would entail

Five pieces, in roughly the order they have to exist:

1. **Health signals, machine-readable.** Today's signals are scattered:
   container exit codes, MQTT dead letters, auth 401s in logs, dvergr
   journal stall patterns, rate-limit 429s. Self-healing needs these
   normalized into a single stream — MQTT topic `saga/health/#` with a
   consistent schema, or a structured-log pipeline into InfluxDB. Without
   this, the healing agent has no idea anything's wrong.

2. **A detector agent, or a rules engine with an agent escalation path.**
   Not every anomaly deserves an LLM. Restarts for crashed containers
   are a systemd/docker-restart job, not an AI task. The agent gets
   involved when the fix requires *understanding* — a subtle bug, a
   misbehaving prompt, a config that needs revision. Good split:
   deterministic rules handle the known failure modes; the agent gets
   paged only on the unknowns.

3. **A scoped authorization model.** Self-healing without a scope is
   a footgun. Define the perimeter up front: what the healer is allowed
   to touch unilaterally (restart a dvergr, adjust a prompt, revert a
   journal entry) vs what requires human approval (modify Odin's
   persona, push to main, rebuild images, touch iptables). Encode this
   in the healer's CLAUDE.md and in the container's capabilities
   (read-only mounts for out-of-scope paths).

4. **Guardrails and rollback.** Every autonomous fix needs a safe
   undo. Concretely: tag the known-good container image before
   deploying a new one; capture the pre-fix git SHA and offer a
   one-command revert; canary any code change on a single dvergr
   before fleet-wide rollout; circuit-break on "healer has tried N
   times in M minutes without success, stop and page the human."

5. **Loop closure — verify the fix worked.** The healer has to
   re-read the health signal after acting. If the thing it was trying
   to fix is still red after a bounded retry window, it escalates to
   human rather than trying a bigger hammer. Without verification,
   you get the classic "self-healing" antipattern where the system
   thrashes indefinitely and the operator can't tell what's broken.

#### Where to start (if self-healing becomes a goal)

- **Phase 0 — restart-based.** docker `--restart unless-stopped` plus
  systemd units for non-container services. No AI involved. Handles
  90% of real-world crashes. Mostly done already.
- **Phase 1 — detection only.** `saga/health/#` MQTT topics, a
  Grafana dashboard, Telegram alerts routed to Odin via the existing
  channels plugin. Human still decides; Odin is now aware.
- **Phase 2 — human-on-the-loop healing.** Odin proposes fixes in
  Telegram, human approves, dev-Claude-style executor applies them.
  Still supervised, but much faster.
- **Phase 3 — human-out-of-the-loop for narrow scopes.** Pick one
  well-understood failure class (e.g., auth token refresh failure
  → re-bootstrap credentials) and let an agent handle it end-to-end
  with rollback on failure. Expand scope only after each class proves
  reliable in supervised mode for weeks.

Prerequisite for any of this: the §2 Phase A Mimir work. If Mimir
still over-critiques in the current single-critic mode, a self-healing
loop inherits that bias and makes *confident wrong fixes* — which is
strictly worse than not healing at all.

---

## 2. Autoreason Applied to Mimir

### The finding (Nous Research, autoreason paper)

Single-critic iterative refinement has three structural failure modes:
- **Prompt bias** — asked to critique, models hallucinate flaws
- **Scope creep** — outputs expand unchecked each pass
- **Lack of restraint** — models never say "no changes needed"

Empirically: naive critique-and-revise shrank Haiku 3.5 outputs 59–70%
over 15 passes. Refinement actively degrades weaker models.

Autoreason's fix: each iteration produces three competing versions —
incumbent **A**, adversarial revision **B**, synthesis **AB** — judged
by fresh agents with no shared context via blind Borda count. "Do
nothing" is a first-class option. Convergence: A wins twice.

### Why Saga cares

Mimir is exactly the single-critic pattern the paper shows is broken.
The Odin → Mimir → dvergr guidance loop likely has scope-creep risk —
every verdict nudges toward *more* changes. Plausible that current
Mimir is making dvergar output worse over long projects, not better.

The paper's transition point (Haiku 4.5 at ~60% accuracy) matters for
where to invest: autoreason's gains concentrate at the Odin *strategic*
layer (Opus decisions, wide generation/evaluation gap), not at every
dvergr iteration.

### Implementation steps

**Phase A — cheap instrumentation (do first)**
1. Add `"approve_unchanged"` as a first-class value in Mimir's verdict
   schema (see `odin/mimir/CLAUDE.md`). Document it as the *preferred*
   verdict when no concrete improvement is identified.
2. Instrument dvergr journal word-count deltas in ratatoskr. Publish
   to `dvergr/<project>/<instance>/metrics`. Watch for monotonic
   shrinkage over iterations — that's the paper's degradation signature.
3. Reword Mimir's persona prompt to explicitly reward restraint.
   Current prompt likely over-incentivizes finding flaws.

**Phase B — autoreason tournament for Odin strategic decisions**
4. Identify the decision types worth the compute: spec decomposition,
   delegation structure, principle revisions. Per-iteration dvergr
   guidance is NOT on this list.
5. Extend Odin's container with a third and fourth tmux pane:
   `revisor` (fresh Opus) and `synthesizer` (fresh Sonnet). File-based
   handoff, same pattern as Mimir.
6. Build a 3-judge Borda panel: three ephemeral Sonnet sessions reading
   A/B/AB blind, returning rank tuples. Converge on "A wins twice."
7. Gate: only invoke the tournament when Odin flags a decision above a
   complexity threshold (configurable in Odin's CLAUDE.md).

Phase A is low-risk and can ship this week. Phase B is a genuine
architectural change and belongs after Phase 3 of the spec.

---

## 3. Karpathy's Wiki Pattern Applied to Saga

### The finding

Instead of RAG-at-query-time, have the LLM **incrementally compile a
persistent wiki** from raw sources. Three layers:
- `raw/` — immutable sources
- Wiki — LLM-owned markdown, cross-linked, backlinked
- Schema (`CLAUDE.md`) — conventions and workflows

Two navigational files: `index.md` (content-oriented catalog),
`log.md` (chronological append-only). Obsidian as IDE frontend.

### Why Saga cares

Odin's master journal + evolving principles is embryonic wiki:
- `principles.md` ≈ `index.md` (distilled, content-oriented)
- `journal.md` ≈ `log.md` (chronological)

Currently specs flow raw from co-pilot → Odin → dvergar. Karpathy's
`raw/` → `wiki/` pipeline suggests: co-pilot spec lands in `raw/`,
Odin *compiles* it into a project wiki (entity pages: components,
constraints, open questions, glossary), dvergar consume the wiki.
Mimir verdicts file back into the wiki as decision records.

At fleet scale, cross-project learnings (from N completed projects'
journals) beg for a compiled wiki the next Odin can read.

### Implementation steps

1. Before Phase 3 (spec ingestion), restructure `odin/specs/templates/`
   so a spec is *ingested into* a project wiki directory, not read as
   a monolith markdown.
2. Define the project-wiki schema: `raw/` (co-pilot input), `wiki/`
   (Odin-compiled), `wiki/index.md`, `wiki/log.md`, entity pages.
3. Add an ingest workflow to Odin's CLAUDE.md: on spec arrival, read
   into `raw/`, discuss with co-pilot, compile initial wiki, commit.
4. Have dvergar read `wiki/index.md` first on session start, drill into
   relevant entity pages. Their journal entries append to `wiki/log.md`.
5. Mimir verdicts become wiki pages: `wiki/decisions/<date>-<slug>.md`,
   backlinked from affected entity pages.
6. Set up an Obsidian vault on the Mac pointing at the Saga Gitea
   project repos. Co-pilot browses the wiki while Odin writes it.
7. Spin this into its own agent — see **Bragi** below.

---

## 4. New Project Concepts (brief)

### Bragi — the skald agent

Dedicated compiler agent, separate container. Job: compile Saga's raw
journals + Mimir verdicts + spec docs into an Obsidian-viewable wiki.
Same ratatoskr/inotify pattern — watches journal files, compiles on
change, commits to Gitea. Obsidian vault on Mac opens the repo directly.
Norse-consistent (Bragi = god of poetry, Saga's husband; Saga herself
chronicles at Sökkvabekkr).

**Starter step:** new `containers/bragi/` skeleton; persona prompt in
`bragi/CLAUDE.md`; ratatoskr watcher added to Odin container that pushes
journal deltas to Bragi's input queue.

### Autoreason-Mimir (Mimisbrunnr tournament)

Upgrade Mimir from single critic to A/B/AB tournament with blind Borda
panel. Covered in §2 Phase B above.

### Convergence-aware stuck detection

Adopt autoreason's "do nothing wins k=2" as an explicit dvergr completion
signal. Add to ratatoskr as "no material delta two iterations in a row"
— catches silent stalls the completion promise misses.

**Starter step:** new `dvergr-hooks/check-convergence.sh` invoked by
post-iteration; publishes `dvergr/<project>/<instance>/converged` retained
topic; Odin subscribes and tears down the loop.

### Homelab Memex

Separate project, not Saga-internal. LLM-maintained Obsidian wiki of
homelab state fed from MQTT retained topics, Uptime Kuma, Grafana alerts,
docker-compose trees, Proxmox config. Entity page per CT/VM/service with
auto-updated IP / cron / token-expiry facts. Solves CLAUDE.md and
saga-spec.md drifting out of sync with actual state.

**Starter step:** reuse the Bragi engine once it exists; point it at a
new `homelab-wiki` Gitea repo with a different schema file.

### Spec-as-wiki template

Restructure `odin/specs/templates/` from monolith to wiki. Covered in
§3 steps 1–2 above.

---

## Priority Order

1. **Dev Claude on VM .17** (§1) — unblocks everything else.
2. **Mimir verdict schema + journal word-count instrumentation** (§2 Phase A)
   — cheap, addresses a named failure mode in a running system.
3. **Spec-as-wiki + Bragi skeleton** (§3, §4 Bragi) — architectural
   alignment before Phase 3 lands.
4. **Convergence-aware stuck detection** (§4) — small, self-contained.
5. **Autoreason tournament for Odin** (§2 Phase B) — post-Phase-3,
   genuine architectural change.
6. **Homelab Memex** (§4) — separate project, reuses Bragi.
