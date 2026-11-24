#!/usr/bin/env bash
# Start your own Flowise (the ~/Flowise build) on http://localhost:3000 with its normal data folder (~/.flowise).
#
#   tools/flowise_start.sh            # Ctrl+C stops it
#
# First time only: open http://localhost:3000, create your admin account, then
# Settings → API Keys → Create key, ticking the agentflows, chatflows and variables permissions
# and credentials:view. Then start FlowForge with that key so "Deploy & run" works without pasting it:
#   FLOWISE_API_KEY=<your key> .venv/bin/python -m flowforge serve
set -euo pipefail
FLOWISE_DIR="${FLOWISE_DIR:-$HOME/Flowise}"
PORT="${FLOWISE_PORT:-3000}"
[ -x "$FLOWISE_DIR/packages/server/bin/run" ] || { echo "No Flowise build at $FLOWISE_DIR (set FLOWISE_DIR)"; exit 1; }
if curl -sf "http://127.0.0.1:$PORT/api/v1/ping" > /dev/null; then echo "Flowise is already running on port $PORT"; exit 0; fi

# Node >= 25 removed SlowBuffer; an old dependency of Flowise still reads it.
SHIM="$(mktemp -t flowise-shim).js"
echo "const b = require('buffer'); if (!b.SlowBuffer) b.SlowBuffer = b.Buffer" > "$SHIM"

echo "Starting Flowise on http://localhost:$PORT (data in ~/.flowise)…"
cd "$FLOWISE_DIR/packages/server"
PORT="$PORT" NODE_OPTIONS="--require $SHIM" exec ./bin/run start
