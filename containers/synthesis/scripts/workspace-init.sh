#!/bin/bash
# /usr/local/bin/workspace-init — ensure /workspace/{input,output,status}
# exist with sane perms when the host bind-mounts /workspace at run time.

set -euo pipefail

mkdir -p /workspace/input /workspace/output /workspace/status

# If schemas dir was bind-mounted over by the host, fall back to a copy of
# the baked schemas so the persona's contract is always satisfied.
if [ ! -f /workspace/schemas/task-input.schema.json ] && \
   [ -d /opt/muninn/schemas ]; then
    mkdir -p /workspace/schemas
    cp /opt/muninn/schemas/*.schema.json /workspace/schemas/ 2>/dev/null || true
fi

# Make sure vscode user owns the workspace (devcontainer base runs as vscode).
if [ "$(id -u)" = "0" ] && id vscode >/dev/null 2>&1; then
    chown -R vscode:vscode /workspace 2>/dev/null || true
fi

exit 0
