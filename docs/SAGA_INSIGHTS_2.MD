# Insights & Project Strategy — 2026-04-15

Captures the organizational and pre-release decisions made between the
2026-04-15 networking-docs commit (`486d59e`) and the start of Phase 3
implementation. Three threads:

1. How to split related-but-distinct projects across repos
2. How to make Saga publishable without forcing generalization
3. The configuration-pattern refactor to lock in before Phase 3

Each thread has a corresponding action list at the end.

---

## 1. Repo strategy: platform vs applications vs portfolio

### The framing

The set of projects in flight (or planned) divides cleanly into three
layers, and the right repo cut follows the layer boundary, not the
skill-domain boundary:

- **Platform** — the substrate everything else runs on. Hardware,
  Proxmox, networking, DNS, firewall, monitoring (OPNsense / Suricata
  / ntopng). These components are co-designed and change together: a
  firewall rule edit cascades to Caddy's allowlist awareness; a DNS
  reorg cascades to every service in the homelab. No piece deploys
  independently of the others.
- **Applications** — things running on top of the platform. Saga,
  Muninn, the ESP32 surveillance system, Memex (future). Each has its
  own stack, language, lifecycle, and could in principle deploy to a
  different substrate.
- **Portfolio** — the public-facing site and blog narratives. Distinct
  from both.

### Why platform = one repo, not several

Even though "networking", "infrastructure", and "monitoring" feel like
separate skill domains, they live in one repo because:

- They change together. A DHCP reservation edit touches Pi-hole, Caddy
  configs, and firewall rules. One commit spanning all three is more
  honest than three PRs across three repos coordinating to stay
  consistent.
- The portfolio narrative "I built a homelab" is one story. Multiple
  blog posts come out of it, but the portfolio entry is singular.
- Future-you (or any collaborator) reads one repo, not five, to
  understand the platform.

Inside `homelab/`, top-level subdirectories can mirror the domains
(`networking/`, `monitoring/`, `backup/`, `proxmox/`, `dns/`,
`reverse-proxy/`) so the skill demonstration is still obvious to a
reader.

### Why applications = one repo each

- Different stacks, different dependencies, different lifecycles.
- Portability: Saga *could* run on someone else's Proxmox or AWS or
  bare metal. The moment homelab-specific config is committed into
  Saga's repo, that knot is tied.
- Portfolio clarity: "here are four things I built" reads better than
  "here is one huge thing I built" when the four are actually distinct.

### Concrete repo list

| Repo | Layer | Status | Notes |
|---|---|---|---|
| `homelab` | Platform | New — to be created | Holds HOMELAB_NETWORKING_PROJECT.md (moved out of saga/), Caddyfile, iptables rules, Pi-hole config notes, Proxmox layout, future OPNsense / Suricata / monitoring configs |
| `saga` | Application | Existing | AI orchestrator. Currently private. Goes public once Phase 3 lands and sanitization pass is done |
| `muninn` | Application | New — to be created | Bookmark semantic wiki. Sketch already exists at `docs/MUNINN.MD` in saga; move it out |
| `surveillance` | Application | Pre-existing | ESP32 firmware + per-node config. May already live in its own repo or in Home Assistant configs |
| `memex` | Application | Future | Homelab state wiki; depends on Bragi |
| `daviddunn-portfolio` | Portfolio | Future | The actual portfolio site. Created last so it has things to point at |

Six repos for everything currently in scope, with growth room for
future applications.

### Skills get showcased through narrative, not repo structure

A portfolio visitor looking for networking chops doesn't care whether
DNS configs live in `homelab/networking/` or `homelab-networking/`.
They care about a well-written post that starts with "I had a problem
with split-horizon DNS…" and ends with "here's what I shipped."

The portfolio site should let someone browse by skill domain:

- **Networking** — links to `homelab` repo + 3-4 posts
- **Containerization** — links to `homelab` (Proxmox) + `saga` (Docker) + 2-3 posts
- **AI/LLM systems** — links to `saga` + `muninn` + `memex` + ~5 posts
- **Embedded/IoT** — links to `surveillance` + 2 posts
- **Security** — links to `homelab` (firewall, TLS, secrets) + `saga` (heimdall gate) + 2-3 posts

The same repo appears under multiple skill domains because real
engineering work doesn't cleave neatly along skill lines. Repos are
about lifecycle boundaries; portfolio narratives are about the story
you want to tell.

### Public/private per repo

| Repo | Public or private? | Why |
|---|---|---|
| `homelab` | Private + sanitized public mirror | Real IPs, MAC addresses, Cloudflare scopes, secrets paths — sensitive. Keep a private `INFRASTRUCTURE.md` with real values; publish a sanitized `README.md` and `docs/` for the mirror |
| `saga` | Public-ready after sanitization pass | Design-document-heavy; secrets never committed |
| `muninn` | Public | Reference-implementation value is high; corpus is personal but code is general |
| `surveillance` | Probably private | Device IDs, camera positioning, timing patterns — homelab-OPSEC-adjacent |
| `memex` | Follows `homelab`'s privacy stance | It's literally a wiki *of* the homelab |
| `daviddunn-portfolio` | Public | That's the whole point |

Pattern for the private repos: each gets a public sanitized mirror
(`{repo}-public` or `{repo}-reference`) so the public repo count
reflects portfolio breadth without exposing operational detail.

### Sequencing

1. **Carve `homelab` out of saga first.** Move `docs/HOMELAB_NETWORKING_PROJECT.md` to a fresh `homelab` repo. That repo will accumulate scope over time (monitoring, backup scripts, Proxmox automation).
2. **Carve `muninn` out of saga next.** Light touch — just the sketch doc, establish the repo, let it sit until scoped.
3. **Update saga's HOMELAB-INTEGRATION.md and ARCHITECTURE.md** to link to the external repos by URL instead of internal doc paths. One small commit.
4. **Delete the originals from saga** (`docs/HOMELAB_NETWORKING_PROJECT.md`, `docs/MUNINN.MD`).
5. **Build the portfolio site** later, once the others have material to point at.

### One CC session per project, not concurrent

Mac has 8 GB RAM; two concurrent CC instances already caused a kernel
panic. Pattern instead:

- One dev CC session at a time, per project
- Context-switch by `cd ~/src/<project> && claude` (auto-memory dir
  follows the cwd slug)
- Cross-project knowledge that genuinely belongs to "you" (homelab
  IPs, workflow preferences) goes in user-level memory at
  `~/.claude/CLAUDE.md`, which all sessions load

`claude --resume <id>` recovers any session's state without needing a
multiplexer to keep it alive.

---

## 2. Saga's public-release stance: idiosyncratic by design, share the thinking

### The reframe

Saga's primary public value isn't "clone and deploy it" — it's "read
the design and think." `saga-spec.md`, the Norse agent model,
file-based Mimir handoff, the autoreason `APPROVE_UNCHANGED` insight,
the heimdall security boundary, the credentials-volume pattern: every
one of those transfers to any multi-agent system someone else might
build. None depends on `192.168.86.X` being specifically those IPs.

The code is a **reference implementation**: "here's what these designs
look like when they actually run on my specific homelab." Nobody needs
to run your exact code. They need to see it exists and works, so they
can trust the design isn't vaporware.

This is how most homelab/infrastructure projects work on GitHub.
Pi-hole isn't generalized to every DNS setup; ntfy isn't deployable to
arbitrary Kubernetes. Public repos, well-documented, honest about
prerequisites — and people still fork, learn, and contribute.

### What "idiosyncratic" actually means in the current saga repo

Mostly cosmetic, not structural. Concretely:

**Already good:**
- No credentials in the repo (token files are 0600 root on the VM, not committed)
- Architecture docs already frame things generically ("Docker host VM", "Gitea instance")
- No PII beyond email (which lives in memory files, not in repo)
- Firewall rules live in scripts, not baked into code

**Idiosyncrasy surface that reads wrong publicly:**
- Hardcoded `192.168.86.X` sprinkled through CLAUDE.md, SETUP.md
- Hardcoded `192.168.86.18:3000` for Gitea in launcher scripts
- `/home/david/src/saga` hardcoded in `scripts/systemd/heimdall.service`
- MAC addresses in network docs (those are moving to the `homelab` repo)

None are security issues post-token-rotation. They're presentation
issues — the repo reads as "this person's homelab" rather than "this
design, implemented on this person's homelab."

### What NOT to generalize

- Don't refactor to support multiple deployment environments
  (Kubernetes, AWS, bare metal). Scope creep, delays Phase 3+, and the
  docs have to caveat every claim.
- Don't make the stack swappable (Postgres instead of Gitea, NATS
  instead of MQTT). Same problem.
- Don't write "installation scripts for arbitrary Linux distros."
  You're not shipping software; you're sharing a reference.

The stance: **"I built this for my homelab. The design is general, the
deployment is specific. Here are both. Learn from it, steal from it,
or fork it and port it — that's on you, not me."**

### Timing gates for going public

Three checks before flipping the saga repo public:

1. **Phase 3 works end-to-end** — submit-spec-via-Telegram → Odin
   decomposes → Mimir reviews → human approves → dvergr spawns and
   iterates. Until that's demonstrated, the repo reads as
   aspirational, which is a worse first impression than being honest
   about what's done.
2. **Sanitization pass done** — placeholder substitution in operational
   code (see §3 below). Documentation prose stays concrete because it
   *is* the user's specific narrative.
3. **One polished `BLOG.md` or top-level writeup** — not the full
   spec, but a 1500-word "here's what Saga is, why it exists, what's
   interesting about it" doc at the root. The portfolio entry for Saga
   in prose form.

Cumulative effort across all three: ~1-2 days of focused work.

### The deeper point

The unspoken assumption that public-facing code must be deployable by
arbitrary strangers is a 2010s open-source mindset. Modern reality for
portfolio/homelab/research work:

- **Deployability is optional.** Even Anthropic's Claude Code plugins
  don't try to run in every environment.
- **Honesty about context is the feature, not the limitation.** "Built
  for my specific homelab, here's how" reads grounded and authentic.
  "Works for everyone everywhere" reads naive or marketing.
- **The reader is a peer, not a user.** They're looking at Saga to
  understand the thinking, not to deploy it tonight.

---

## 3. Configuration pattern: env vars + structured YAML, before Phase 3

### What Saga already does right

Most runtime code is already env-var driven:

```bash
# init-dvergr.sh
MQTT_HOST="${MQTT_HOST:-192.168.86.12}"
MAX_ITERATIONS="${MAX_ITERATIONS:-20}"

# heimdall.py
LAUNCH_REQUESTS_DIR = Path(os.environ.get("SAGA_LAUNCH_REQUESTS",
                                          "/opt/saga/odin-ws/launch-requests"))

# heimdall.service
Environment=SAGA_LAUNCH_REQUESTS=/opt/saga/odin-ws/launch-requests
```

Every operational `192.168.86.X` is already a *default value* an env
var can override. The "hardcoding" is just the default for the user's
specific homelab. The leakage pattern: a reader thinks "this is
hardcoded" when actually it's "this is the default; here's how to
override."

### Format choice: `.env` for deployment, YAML for structured data

| Format | Best for | Verdict for Saga |
|---|---|---|
| **`.env` (env vars)** | Flat key-value, deployment config | Use this — bash/Docker/Python/systemd all speak it natively, no parser dependency |
| **YAML** | Hierarchical config, lists, nested | Already used correctly for `project-registry.yml`, `launch-requests/*.yml` (domain data with structure) — keep that |
| **TOML** | Python tooling, PEP-standard | Reserve for `pyproject.toml`-style use; not needed here |
| **JSON** | Machine-to-machine | No comments, rigid syntax — bad for human-edited config |

The split worth keeping: **env vars for deployment / environment
values; YAML for structured domain data.**

### The five concrete changes

1. **Add `.env.example` at repo root** documenting all configurable
   env vars with placeholder values:

   ```env
   MQTT_HOST=192.168.X.X
   MQTT_PORT=1883
   GITEA_HOST=git.your-homelab.local
   GITEA_SSH_PORT=22
   SAGA_REPO_DIR=/home/youruser/src/saga
   SAGA_LAUNCH_REQUESTS=/opt/saga/odin-ws/launch-requests
   SAGA_LAUNCH_RESULTS=/opt/saga/odin-ws/launch-results
   SAGA_WORKSPACES_ROOT=/opt/saga/workspaces
   SAGA_CREDENTIALS_VOLUME=saga-claude-credentials
   ```

2. **Add `.env` and `.env.local` to `.gitignore`.**

3. **Have launcher scripts source `.env` if present:**

   ```bash
   SAGA_REPO_DIR="${SAGA_REPO_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
   [[ -f "$SAGA_REPO_DIR/.env" ]] && source "$SAGA_REPO_DIR/.env"
   ```

   The launchers transparently pick up overrides without anyone
   remembering to `export` things.

4. **Update `heimdall.service` to use `EnvironmentFile`:**

   ```ini
   [Service]
   EnvironmentFile=/etc/saga/heimdall.env
   ExecStart=/usr/bin/python3 ${SAGA_REPO_DIR}/odin/scripts/heimdall.py
   ```

   `/etc/saga/heimdall.env` is per-deployment, not committed. Removes
   the `/home/david/src/saga` hardcode.

5. **Document the `.env` workflow in SETUP.md** as an early step:
   "Copy `.env.example` to `.env`, fill in your homelab values."

### What NOT to placeholderize

Three categories of hardcoded values that should stay hardcoded:

1. **CLAUDE.md, saga-spec.md, ARCHITECTURE.md, HOMELAB-INTEGRATION.md**
   — these are *narrative documentation* of *this user's specific
   system*. Not deployment templates. Concrete IPs make prose readable
   ("Pi-hole at 192.168.86.11"); placeholders (`${PIHOLE_IP}`) make it
   stilted and harder to follow.
2. **The IP allowlist in firewall extensions** — runtime-enforced
   security rules tuned to specific service IPs. Could be env-driven,
   but the marginal win is small and the misconfiguration surface
   grows. Concrete values document the threat model.
3. **`heimdall.py`'s `ALLOWED_IMAGES` allowlist** — explicitly
   hardcoded for security. Adding env-var override defeats the
   purpose. Bumping the allowlist is supposed to be a deliberate
   operator action requiring a code edit.

The pattern: **make values configurable when their override is part of
normal deployment workflow; keep them hardcoded when override is a
security-relevant operator decision or when the value is pedagogical
(in docs/narratives).**

### Why this is locked in BEFORE Phase 3

Phase 3 introduces more deployment values: per-project workspaces,
launch templates, decomposition outputs, multi-dvergr launches. You
want every new path to flow through the `.env` from the start, not
retrofit later. Three-to-four hours of work now saves a much larger
retrofit pass after Phase 3 has accumulated more hardcoded values.

---

## Action list (sequenced)

### Before Phase 3 starts

1. **Implement the `.env` refactor** — five changes in §3 above. ~3-4
   hours.
2. **Carve `homelab` out of saga** — create the new repo on Gitea +
   GitHub mirror, move `docs/HOMELAB_NETWORKING_PROJECT.md` into it as
   `README.md` (or split into README + docs/), write a small
   `CLAUDE.md` for it.
3. **Carve `muninn` out of saga** — create the new repo, move
   `docs/MUNINN.MD` into it, write a small `CLAUDE.md`.
4. **Update saga's HOMELAB-INTEGRATION.md and ARCHITECTURE.md**
   cross-references to point at the external repo URLs instead of
   internal doc paths. Delete the moved files from saga.

### During Phase 3

5. Build out spec ingestion → decomposition → per-instance PRD →
   launch-request pipeline (the actual Phase 3 work, see saga-spec.md
   §12 and the post-Phase-2 wiki restructure).
6. Use `.env`-driven config from the start for any new deployment
   values introduced.

### After Phase 3 lands

7. **Saga sanitization pass** — operational scripts only (the
   placeholder substitution; narrative docs stay concrete).
8. **Write `BLOG.md` for saga** — 1500-word top-level orientation
   doc.
9. **Flip saga repo public** on GitHub.
10. **Continue homelab repo** — fix the cross-browser TLS issue
    (Chromium/mobile blocked while Safari works), then sanitize and
    flip its public mirror live.
11. **Build out the portfolio site** at `daviddunn.dev` once there
    are 2-3 polished repos to point at. Cloudflare Tunnel + static
    site, served through the existing Caddy setup.

### Future / opportunistic

12. **Muninn implementation** — when scoped and prioritized; depends
    on Saga Phase 3 (so dvergar can do the parallel scrape work).
13. **Memex** — depends on Bragi being mature and the homelab repo
    being structured enough to compile against.
14. **Surveillance write-up / repo reorg** — if not already in its own
    repo, separate it; sanitized public mirror following the homelab
    pattern.

---

## Notes for future-me

- The **single biggest insight** from this conversation: don't try to
  make the repo structure carry the skill taxonomy. Repos are about
  lifecycle boundaries; portfolio narratives are the skill story. Two
  separate concerns; conflating them creates artificial structure.
- The **idiosyncratic-feeling problem** is mostly a presentation issue
  solved by env-var defaults + `.env.example`, not a structural
  problem requiring generalization.
- The **public/private split per repo** is more nuanced than "all
  public" or "all private." Sanitized public mirrors of private repos
  give portfolio breadth without OPSEC cost.
- **One CC session per project, serially**, not concurrent — the Mac
  RAM constraint (and likely the VM's too once Phase 3 ramps up agent
  count) won't tolerate multiple parallel dev sessions.
- **`claude --resume <id>`** is a safety net that lets you context-
  switch between projects without needing a multiplexer to keep
  sessions alive.
