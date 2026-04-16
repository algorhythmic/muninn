#!/bin/bash
# /status — verify the synthesis container is running in SUBSCRIPTION mode.
# Confirms the credentials volume is mounted and rejects API-mode env vars.

set -euo pipefail

CRED_DIR="/home/vscode/.claude"

if [ ! -d "$CRED_DIR" ]; then
    echo '{"status": "error", "message": "Credential directory not mounted"}' >&2
    exit 1
fi

if [ ! -f "$CRED_DIR/credentials.json" ] && [ ! -f "$CRED_DIR/.credentials.json" ]; then
    echo '{"status": "error", "message": "No credential files found in '"$CRED_DIR"'"}' >&2
    exit 1
fi

# Reject API-mode env vars.
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
    echo '{"status": "error", "message": "CLAUDE_CODE_OAUTH_TOKEN is set — refusing to start (would force API billing)"}' >&2
    exit 1
fi

if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
    echo '{"status": "error", "message": "ANTHROPIC_API_KEY is set — refusing to start (would force API billing)"}' >&2
    exit 1
fi

echo '{"status": "ok", "billing": "subscription", "login_method": "Claude Max Account"}'
echo "Login method: Claude Max Account"
exit 0
