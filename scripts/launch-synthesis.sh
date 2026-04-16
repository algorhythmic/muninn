#!/bin/bash
# Launch the muninn-synthesis container for a single task.
#
# Usage:
#   scripts/launch-synthesis.sh <task-id> [workspace-dir]
#
# The orchestrator (src/muninn/synthesis/orchestrator.py) normally invokes
# `docker run` directly via container.py. This shell wrapper exists for
# manual / interactive launches and as a reference implementation of the
# subscription-mode mount + env constraints.
#
# Constraints (binding):
#   - Mounts saga-claude-credentials at /home/vscode/.claude READ-ONLY.
#   - Refuses to launch if CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY are
#     set in the host environment (would leak into the container and force
#     API billing).
#   - Does NOT pass -p to claude. The container's entrypoint runs
#     `claude --dangerously-skip-permissions` under tmux.

set -euo pipefail

TASK_ID="${1:?Usage: launch-synthesis.sh <task-id> [workspace-dir]}"
WORKSPACE="${2:-/tmp/muninn-synthesis/${TASK_ID}}"

IMAGE="${MUNINN_SYNTHESIS_IMAGE:-muninn-synthesis:latest}"
CRED_VOLUME="${SAGA_CREDENTIALS_VOLUME:-saga-claude-credentials}"
CONTAINER_NAME="muninn-synthesis-${TASK_ID}"

# Refuse to launch if API-mode env vars are in the host shell.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo "Error: CLAUDE_CODE_OAUTH_TOKEN is set; refusing to launch (would force API billing)." >&2
    exit 1
fi
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo "Error: ANTHROPIC_API_KEY is set; refusing to launch (would force API billing)." >&2
    exit 1
fi

# Verify the credentials volume exists.
if ! docker volume inspect "$CRED_VOLUME" >/dev/null 2>&1; then
    echo "Error: Docker volume '$CRED_VOLUME' not found." >&2
    echo "Set up Saga Phase 1+2 (saga-claude-credentials) before running synthesis." >&2
    exit 1
fi

# Workspace dirs.
mkdir -p "$WORKSPACE/input" "$WORKSPACE/output" "$WORKSPACE/status"

if [ ! -f "$WORKSPACE/input/${TASK_ID}.json" ]; then
    echo "Error: expected input file at $WORKSPACE/input/${TASK_ID}.json" >&2
    exit 1
fi

# Build image lazily.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "Building synthesis image $IMAGE..."
    docker build -t "$IMAGE" -f containers/synthesis/Dockerfile .
fi

echo "Launching synthesis container: $CONTAINER_NAME"
echo "  workspace: $WORKSPACE"
echo "  image:     $IMAGE"
echo "  volume:    $CRED_VOLUME (read-only)"

# shellcheck disable=SC2086
exec docker run \
    --name "$CONTAINER_NAME" \
    --rm \
    -v "${CRED_VOLUME}:/home/vscode/.claude:ro" \
    -v "${WORKSPACE}:/workspace" \
    --cidfile "$WORKSPACE/container.cid" \
    -e "TASK_ID=${TASK_ID}" \
    -l "muninn.task.id=${TASK_ID}" \
    "$IMAGE" \
    "${TASK_ID}.json"
