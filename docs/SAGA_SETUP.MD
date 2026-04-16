# Saga Setup Guide

Step-by-step walkthrough for installing Saga on a fresh Proxmox homelab.
If you're retrofitting onto an existing homelab, you can skip the
infrastructure sections and jump straight to "Phase 1: Credentials
volume and first dvergr."

## Prerequisites

| Item | Requirement |
|---|---|
| Hardware | A machine running Proxmox VE 8 or 9. Reference platform: Lenovo ThinkCentre M75s Gen 2, Ryzen 5 PRO 5650G, 32 GB RAM, 1 TB NVMe. |
| Network | Static IPs available. Saga uses `.17` (Docker host VM) and `.18` (Gitea). |
| Claude subscription | Anthropic Max plan. Pro will technically work but rate limits will bite fast with N parallel dvergar. |
| Browser | On any machine you can reach during the one-time OAuth dance. |
| Telegram | A bot token if you want Telegram control (Phase 2+). See [t.me/BotFather](https://t.me/BotFather). |

## Pre-Phase-1: Infrastructure

### 1. Docker host VM (IP `.17`)

Create a Debian 13 VM on Proxmox:

- VM ID: `106` (convention — matches the static IP reservation, next to Gitea CT 107 and Caddy CT 108)
- 8 GB RAM, 4 CPU cores, 100 GB disk
- Hostname: `saga-docker`
- Static IP: `192.168.86.17/24` (adjust for your subnet)
- Gateway: your router's LAN IP
- DNS: your Pi-hole at `192.168.86.11` (preferred — gives you `*.lab.daviddunn.dev` resolution via the homelab's wildcard DNS) or `1.1.1.1` as a fallback
- Search domain (optional): `lab.daviddunn.dev`

After install, on the VM:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y qemu-guest-agent curl ca-certificates gnupg tmux git sudo
sudo systemctl enable --now qemu-guest-agent
```

Lock down SSH (key auth only, no root login). Install Tailscale if you
want remote access:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --accept-dns=false
```

> **Why `--accept-dns=false`:** Tailscale's MagicDNS inserts
> `100.100.100.100` as the primary nameserver, which can't resolve
> `*.lab.daviddunn.dev` or any of the homelab's internal records.
> Saga containers today use bare IPs so this doesn't break anything,
> but a future dvergr PRD that does a hostname lookup from inside a
> container (inherits the VM's resolver) would silently fail. Opting
> out of MagicDNS here keeps the VM's resolver pointed at Pi-hole.
> See
> [`docs/HOMELAB_NETWORKING_PROJECT.md`](HOMELAB_NETWORKING_PROJECT.md)
> § "Tailscale DNS Override (Critical)" for the full writeup.

### 2. Docker Engine

Install the official Docker CE from Docker's repo (not the distro
`docker.io` package — it's usually a few versions behind):

```bash
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/debian/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] \
  https://download.docker.com/linux/debian trixie stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
```

Log out and back in, then verify:
```bash
docker run --rm hello-world
```

Create the Saga state directory:
```bash
sudo mkdir -p /opt/saga
sudo chmod 700 /opt/saga
```

### 3. Gitea LXC (IP `.18`)

On the Proxmox host, use the community helper script:
```bash
bash -c "$(wget -qLO - https://github.com/community-scripts/ProxmoxVE/raw/main/ct/gitea.sh)"
```

Advanced settings:
- Container ID: `107` (convention — dev VM is 106, Caddy is 108)
- Hostname: `gitea`
- CPU 2, RAM 1024 MB, disk 8 GB
- Unprivileged container
- Static IP: `192.168.86.18/24`
- DNS: `192.168.86.11` (Pi-hole); search domain `lab.daviddunn.dev`
- Enable Root SSH: yes (lock down later if desired)
- Container Protection: **yes** (data-holding container, worth the
  accidental-delete safety net)
- Everything else: no / default

After the LXC comes up, visit `http://192.168.86.18:3000` and complete
the first-run wizard. Use SQLite for the DB (simplest for single-user
homelab), uncheck self-registration, create the admin account.

Generate an SSH deploy key on the Docker host VM and add it to your
Gitea user:
```bash
ssh-keygen -t ed25519 -C "saga-docker-host" -f ~/.ssh/id_ed25519_gitea
cat ~/.ssh/id_ed25519_gitea.pub
# Paste into Gitea → Settings → SSH Keys
```

Verify:
```bash
ssh -T gitea@192.168.86.18
# expected: "Hi there, <you>! You've successfully authenticated..."
```

## Phase 1: Credentials volume and first dvergr

### 4. Clone the Saga repo onto the VM

```bash
mkdir -p ~/src
cd ~/src
git clone <your-saga-repo-url> saga
# or rsync from your Mac if the repo isn't on Gitea yet
cd saga
```

### 4b. Configure for your homelab (`.env`)

Saga's launcher scripts read deployment values (homelab IPs, image tags,
paths) from a repo-root `.env` file if it exists. The repo ships
`.env.example` as a template; copy it and fill in your homelab's
values:

```bash
cp .env.example .env
$EDITOR .env
```

At minimum, set `MQTT_HOST` to your Mosquitto broker's IP and
`SAGA_REPO_DIR` to wherever you cloned the repo (e.g.,
`/home/$USER/src/saga`). Other values default sensibly to the
reference homelab layout (`/opt/saga/...` for state, `:latest` image
tags) and only need overriding if your install diverges.

The `.env` file is git-ignored and stays per-host. The launcher
scripts source it automatically — no manual `export` needed.

If you also intend to run heimdall as a systemd unit (step 13 below),
you'll separately install `/etc/saga/heimdall.env` from the
`scripts/systemd/heimdall.env.example` template. That's a per-host
file too, with the same `SAGA_REPO_DIR` and queue-path values; kept
separate from the repo `.env` because systemd's `EnvironmentFile=`
needs an absolute, root-readable path.

### 5. Run the credentials bootstrap

This does three things in one shot: clones + builds the Trail of Bits
devcontainer at the pinned commit, creates the `saga-claude-credentials`
named volume, and walks you through the interactive `/login`.

**⚠️ Before running, disconnect Mullvad VPN if you use it.** The OAuth
callback breaks under Mullvad.

```bash
scripts/auth/bootstrap-credentials.sh --build
```

Follow the on-screen instructions:
1. When the container starts, run `claude`
2. Accept the bypass-permissions dialog
3. Run `/login` → "Claude account with subscription"
4. Open the printed OAuth URL on your Mac
5. Complete login against your Max-subscription account
6. Confirm with `/status` that it says `Login method: Claude Max Account`
7. `/exit`, then `exit` the container

After the script exits, verify the credentials landed:
```bash
docker run --rm \
  -v saga-claude-credentials:/home/vscode/.claude \
  saga-devcontainer:base \
  bash -c 'ls -la /home/vscode/.claude/.credentials.json'
```

### 6. Build the dvergr image

From the saga repo root on the VM:
```bash
docker build -f containers/dvergr/Dockerfile -t saga-dvergr:latest .
```

### 7. Phase 1 smoke test — launch a single dvergr

Create a minimal workspace:
```bash
sudo mkdir -p /opt/saga/workspaces/smoke/instance-1
sudo chown -R $USER:$USER /opt/saga/workspaces/smoke

cat > /opt/saga/workspaces/smoke/instance-1/PRD.md << 'EOF'
# Smoke test PRD

## Objective
Write a file at /workspace/done.txt containing exactly "smoke test passed".

## Success criteria
- /workspace/done.txt exists
- Its contents are exactly "smoke test passed"
- The task has been committed and recorded in .dvergr/journal.md
EOF
```

In a second SSH session on the VM, start watching MQTT:
```bash
mosquitto_sub -h 192.168.86.12 -t 'dvergr/#' -v
```

Back in the first session, launch the dvergr:
```bash
scripts/auth/launch-dvergr-template.sh smoke instance-1 \
  /opt/saga/workspaces/smoke/instance-1
```

Attach to watch Claude work:
```bash
docker exec -it saga-smoke-instance-1 tmux attach -t dvergr
```

Expected sequence:
1. `init-dvergr.sh` validates the credentials volume is mounted
2. tmux starts a pane running `claude --dangerously-skip-permissions`
3. `/plugin install ralph-loop@claude-plugins-official`
4. `/ralph-loop:ralph-loop "..."` kicks off
5. Claude writes `/workspace/done.txt`, then the Stop hook fires
6. `post-iteration.sh` publishes to `dvergr/smoke/instance-1/status` and
   `dvergr/smoke/instance-1/journal` — you see them in the `mosquitto_sub`
   session
7. Eventually the loop terminates because the completion promise is met

Detach from tmux with `Ctrl-b d`.

## Phase 2: Odin + Mimir + Telegram

### 8. Build the keepalive image and schedule credential refresh

Claude Code's refresh-token auto-rotation is fragile in headless
containers — we observed silent refresh failures within ~24 h during
Phase 1 (see [`CLAUDE.md`](../CLAUDE.md) "Authentication Flow"). The
keepalive container keeps the shared `saga-claude-credentials` volume
warm by starting a throwaway interactive session every few hours,
which exercises the refresh path before any agent would discover it
broken.

Build once:
```bash
docker build -f containers/keepalive/Dockerfile -t saga-keepalive:latest .
```

Schedule via cron on the Docker host VM:
```bash
sudo mkdir -p /var/log/saga
sudo chown "$USER":"$USER" /var/log/saga
crontab -e
# Add:
# 0 */4 * * * /home/david/src/saga/scripts/auth/run-keepalive.sh >> /var/log/saga/keepalive.log 2>&1
```

Run once manually first to confirm it exits 0 and the log contains
a successful trivial-prompt round-trip.

### 9. Build the Odin image

```bash
docker build -f containers/odin/Dockerfile -t saga-odin:latest .
```

The Odin Dockerfile bakes in Bun (required for the Telegram Channels
plugin's MCP server) and reuses the same
`containers/dvergr/claude-baseline.json` for onboarding bypass. Both
Odin and Mimir run as separate tmux panes in the same container,
billing to the same credentials volume.

### 10. Create the Telegram bot token

1. DM [`@BotFather`](https://t.me/BotFather) on Telegram → `/newbot`
   → follow the prompts.
2. Save the bot token to the host VM at `/opt/saga/secrets/telegram-bot-token`:
   ```bash
   sudo mkdir -p /opt/saga/secrets
   sudo chmod 750 /opt/saga/secrets
   # Paste the token via an interactive read so it doesn't land in shell history:
   read -rs TELEGRAM_TOKEN
   echo "$TELEGRAM_TOKEN" | sudo tee /opt/saga/secrets/telegram-bot-token > /dev/null
   unset TELEGRAM_TOKEN
   sudo chmod 600 /opt/saga/secrets/telegram-bot-token
   sudo chown root:root /opt/saga/secrets/telegram-bot-token
   ```
3. Authorize your own chat ID on first use — see step 12 below.

### 11. Create the Odin workspace directory and launch

```bash
sudo mkdir -p /opt/saga/odin-ws
sudo chown -R "$USER":"$USER" /opt/saga/odin-ws

sudo scripts/auth/launch-odin-template.sh \
  /opt/saga/odin-ws \
  /opt/saga/secrets/telegram-bot-token
```

`sudo` is needed because `/opt/saga/secrets` is root-only. After
launch, tail the logs:
```bash
docker logs -f saga-odin
```

Attach to the tmux session to see the three panes (Odin, Mimir,
ratatoskr):
```bash
docker exec -it saga-odin tmux attach -t odin
```

Detach with `Ctrl-b d`.

### 12. First Telegram pairing

DM your Telegram bot to trigger pairing. The first time, the
telegram plugin asks Odin to approve the user ID. Attach to Odin's
tmux pane (`docker exec -it saga-odin tmux attach -t odin`), run
`/telegram:access pair`, and answer the `approve`-write prompts.
Pairing persists inside the credentials volume and survives
container recreates.

Once paired, send a test message. Odin should reply via the MCP
reply tool.

### 13. Deploy the heimdall host daemon

`heimdall.py` is the container-launch daemon that runs on the Docker
host VM (outside any container). Odin writes YAML launch requests to
`/opt/saga/odin-ws/launch-requests/`; heimdall validates and
executes `docker run` on Odin's behalf. Odin never gets Docker
socket access — this is the security boundary.

First install the systemd env file (per-host config — references your
`SAGA_REPO_DIR` and other deployment values):
```bash
sudo install -d -m 0755 -o root -g root /etc/saga
sudo install -m 0644 -o root -g root \
     scripts/systemd/heimdall.env.example /etc/saga/heimdall.env
sudo $EDITOR /etc/saga/heimdall.env   # set SAGA_REPO_DIR for your install
```

Then deploy the unit:
```bash
sudo install -m 0644 -o root -g root \
     scripts/systemd/heimdall.service /etc/systemd/system/heimdall.service
sudo systemctl daemon-reload
sudo systemctl enable --now heimdall.service
sudo systemctl status heimdall.service
```

Verify it's running and watching the launch-requests directory with
`journalctl -u heimdall.service -f`. If the service fails to start
with "can't open file", `SAGA_REPO_DIR` in `/etc/saga/heimdall.env`
is wrong or unreachable to systemd — fix and `sudo systemctl restart
heimdall`.

### 14. Verify end-to-end

In one SSH session:
```bash
mosquitto_sub -h 192.168.86.12 -t 'dvergr/#' -v
```

DM Odin via Telegram with a simple question (e.g. "what is your
status"). Odin should reply. In a separate tmux pane, drop a dummy
Mimir request and verify the `pending/` → `verdicts/` flow works:
```bash
cat > /opt/saga/odin-ws/mimir/pending/test-review.md <<'EOF'
# Review request: dummy

Evaluate whether "hello world" is an adequate smoke test.
EOF
```
Mimir's pane should pick it up within a few seconds and write a
verdict into `/opt/saga/odin-ws/mimir/verdicts/test-review.md`.

## Phase 2b: Bragi skald container

Bragi compiles the fleet's raw outputs (dvergr journals, Mimir
verdicts, Odin guidance, project wikis) into an Obsidian-friendly
git-versioned vault at `/opt/saga/vault/`. Runs as a periodic batch
compiler, not a daemon.

### 15. Build the Bragi image

```bash
docker build -f containers/bragi/Dockerfile -t saga-bragi:latest .
```

### 16. Create the vault directory

```bash
sudo mkdir -p /opt/saga/vault
sudo chown -R 1000:1000 /opt/saga/vault  # uid 1000 = vscode in Trail of Bits base
```

### 17. Launch once to verify

```bash
scripts/auth/launch-bragi-template.sh
```

The default args point Bragi at `/opt/saga/odin-ws` (read-only) and
`/opt/saga/vault` (read-write). On first run, Bragi initializes
`/opt/saga/vault/` as a git repo and compiles whatever material is
currently in the Odin workspace. Watch the tmux pane:
```bash
docker exec -it saga-bragi tmux attach -t bragi
```

When the compile pass finishes and the ralph-loop's completion
promise fires, the container exits cleanly (`--rm` means no stale
container left behind).

### 18. Schedule periodic compilation

```bash
crontab -e
# Add:
# */30 * * * * /home/david/src/saga/scripts/auth/launch-bragi-template.sh >> /var/log/saga/bragi.log 2>&1
```

### 19. Read the vault from your workstation

Mount `/opt/saga/vault/` over SSHFS or a Tailscale filesystem share,
then open the mount as an Obsidian vault on your Mac. Bragi's
cross-links (`[[instance-1]]`, `[[nexus]]`) resolve natively in
Obsidian. `git log` inside the vault shows compilation history.

## Troubleshooting

### `/status` shows "Claude API" instead of "Claude Max"

You set `CLAUDE_CODE_OAUTH_TOKEN` somewhere. Don't. Unset it, restart
the container, and rely on the mounted credentials volume. See
[`saga-spec.md`](../saga-spec.md) §5.

### OAuth callback fails during `/login`

Disconnect Mullvad VPN. It blocks the loopback redirect.

### `apt update` in Docker build fails on `trixie`

If Docker hasn't published a `trixie` repo yet, point at `bookworm`
instead. It's ABI-compatible.

### Credentials file missing from volume after bootstrap

You skipped the `/login` step or exited Claude Code before the
credentials were written. Re-run `bootstrap-credentials.sh` and follow
the prompts.

### `mosquitto_sub` shows nothing during the smoke test

Check firewall: does the dvergr container have outbound access to
`192.168.86.12:1883`? The Trail of Bits devcontainer firewall
whitelists `api.anthropic.com` by default. Saga extends the whitelist
for bare IPs of the homelab services it touches:

```bash
iptables -A OUTPUT -d 192.168.86.12 -j ACCEPT  # Mosquitto
iptables -A OUTPUT -d 192.168.86.18 -j ACCEPT  # Gitea
iptables -A OUTPUT -d 192.168.86.15 -j ACCEPT  # Grafana + InfluxDB
```

Note that the homelab's reverse proxy at `192.168.86.20` (Caddy) is
NOT in the whitelist. Saga containers use bare IPs intentionally so
that firewall rules target specific services rather than funneling
everything through Caddy. See
[`CLAUDE.md`](../CLAUDE.md) and
[`docs/HOMELAB-INTEGRATION.md`](HOMELAB-INTEGRATION.md) § "Pi-hole
DNS" for the full rationale.

### Odin's Telegram pane shows `plugin not installed`

Bun is missing from the image. Rebuild `saga-odin:latest` from a
clean layer — the current Dockerfile bakes Bun in via
`BUN_INSTALL=/usr/local bash`. If you're on a stale image, a fresh
`docker build --no-cache -f containers/odin/Dockerfile -t saga-odin:latest .`
fixes it. `/mcp` inside Odin's pane should list `telegram` with
status `running`.

### Convergence alerts firing on a dvergr that is actually working

`check-convergence.sh` compares total word count of the *latest*
iteration entry. If your dvergr writes very short journal entries by
convention, raise the threshold:
```bash
docker run ... -e CONVERGENCE_WORD_THRESHOLD=30 -e CONVERGENCE_STREAK_THRESHOLD=3 saga-dvergr:latest
```
Defaults are ±10 words, k=2. See
[`dvergr-hooks/check-convergence.sh`](../dvergr-hooks/check-convergence.sh)
for the full parameter list.

### `monotonic_shrink_streak` on the metrics topic climbs steadily

Ratatoskr observed three-plus consecutive iterations where the
dvergr's journal shrank — the autoreason degradation signature (see
[`docs/INSIGHTS-2026-04-13.md`](INSIGHTS-2026-04-13.md) §2).
Usually means Mimir is over-critiquing. Check recent verdicts in
`/opt/saga/odin-ws/mimir/processed/` — `APPROVE_UNCHANGED` should be
the common verdict; if you see a cluster of `REJECT` or `FLAG`
verdicts with the same critique pattern, Mimir's prompt may need
attention. Restraint is a first-class Mimir behavior — see
[`odin/mimir/CLAUDE.md`](../odin/mimir/CLAUDE.md) "Restraint is the
default".

### Keepalive cron ran but `/status` still shows expired auth

Check `/var/log/saga/keepalive.log` for the outcome of the last run.
Common causes: credentials volume was rotated without re-running
`bootstrap-credentials.sh`, the refresh-token itself expired (the
refresh horizon is longer than the access token but not unlimited),
or a network blip. Re-bootstrap:
```bash
scripts/auth/bootstrap-credentials.sh
```
This recovers without needing `--build` if the base image is current.

### Bragi exits immediately after launch

Expected when the Odin workspace has no new material since the last
compile pass — Bragi's completion promise fires and the container
`--rm`s itself. Check the last commit in `/opt/saga/vault/` with
`git log -1`; if the timestamp is recent, Bragi ran successfully
and simply had nothing to do. If the vault git repo is empty or
corrupted, delete `/opt/saga/vault/.git` and re-run; init-bragi.sh
re-initializes on first run.

## Post-install hygiene

- Set a calendar reminder to re-run `bootstrap-credentials.sh` every
  ~60 days. Refresh tokens don't last forever.
- Configure a Proxmox backup schedule for the Gitea LXC and the
  Docker host VM.
- Add the `saga-claude-credentials` volume to your backup story —
  but only on the host. Never push it anywhere public.
