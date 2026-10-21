#!/bin/bash
# Entrypoint for muninn-synthesis. Runs Claude in INTERACTIVE mode inside a
# tmux session against the saga-claude-credentials volume.
#
# Hard rule: this script uses `claude --dangerously-skip-permissions`.
# It does NOT use `claude -p` (found in April 2026 to force API billing;
# under re-test).
#
# FROZEN: kept only until the harness-agnostic synthesis contract (ADR-013)
# replaces this container. Do not extend it.

set -euo pipefail

# 1. Subscription-mode preflight. Refuses API-mode env vars + missing creds.
echo "=== Synthesis container status check ==="
/usr/local/bin/check-status || exit 1
echo "=== Status OK ==="

# 2. Workspace setup (idempotent).
/usr/local/bin/workspace-init

# 3. Optional task hint passed as an argument: "<task-id>.json".
TASK_FILE="${1:-}"
if [ -n "$TASK_FILE" ] && [ -f "/workspace/input/$TASK_FILE" ]; then
    echo "Task file: /workspace/input/$TASK_FILE"
fi

# 4. Launch Claude in subscription / interactive mode under tmux so the host
#    can capture pane output for cap-hit detection.
#    NEVER `claude -p` — that path forces API billing.
exec tmux new-session -s synthesis \
    "claude --dangerously-skip-permissions" \; \
    pipe-pane -t synthesis -o "cat >> /workspace/status/tmux.log" \; \
    attach-session -t synthesis
