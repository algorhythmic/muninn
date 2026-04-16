# Muninn Synthesis Container
# Sibling of saga-dvergr; subscription billing via the saga-claude-credentials
# Docker named volume mounted read-only at /home/vscode/.claude.
#
# Build (from repo root):
#   docker build -t muninn-synthesis:latest -f containers/synthesis/Dockerfile .
#
# Launch:
#   scripts/launch-synthesis.sh <task-id>

ARG BASE_IMAGE=ghcr.io/trailofbits/devcontainer:latest
FROM ${BASE_IMAGE}

LABEL maintainer="muninn"
LABEL component="synthesis"
LABEL billing="subscription"

# Workspace skeleton — actual per-task input/output/status dirs are
# (re)created by workspace-init.sh at container start since the host
# usually bind-mounts /workspace itself.
RUN mkdir -p /workspace/input \
             /workspace/output \
             /workspace/status \
             /workspace/schemas

# Bake the JSON Schemas the model validates its output against.
COPY schemas/json/task-input.schema.json   /workspace/schemas/
COPY schemas/json/era-narrative.schema.json /workspace/schemas/
COPY schemas/json/deep-pass.schema.json     /workspace/schemas/
COPY schemas/json/ad-hoc-analysis.schema.json /workspace/schemas/

# Bake the synthesis persona.
COPY containers/synthesis/CLAUDE.md /workspace/CLAUDE.md

# Helper scripts.
COPY containers/synthesis/scripts/check-status.sh   /usr/local/bin/check-status
COPY containers/synthesis/scripts/workspace-init.sh /usr/local/bin/workspace-init
COPY containers/synthesis/scripts/entrypoint.sh     /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/check-status \
             /usr/local/bin/workspace-init \
             /usr/local/bin/entrypoint.sh

# Subscription credentials are mounted at runtime, READ-ONLY:
#   -v saga-claude-credentials:/home/vscode/.claude:ro
# We deliberately do NOT set CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY,
# and the entrypoint refuses to start if either is present in the env.

WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
