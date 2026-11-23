#!/usr/bin/env bash
# Run the FlowForge server on this Mac and give it a public https link (free Cloudflare quick tunnel).
#
#   tools/serve_public.sh            # Ctrl+C stops everything
#
# Needs: brew install ollama cloudflared   and   ollama pull qwen3:4b
# The public link changes every time you start this. Anyone with the link can use the server
# while it runs; requests through the tunnel cannot reach your home network (see flowforge/api.py).
set -euo pipefail
cd "$(dirname "$0")/.."

PORT="${PORT:-8000}"
MODEL="${FLOWFORGE_MODEL:-qwen3:4b}"
PY=".venv/bin/python"
LOG_DIR="$(mktemp -d -t flowforge)"
PIDS=()

cleanup() {
  echo; echo "Stopping FlowForge…"
  for pid in ${PIDS[@]+"${PIDS[@]}"}; do kill "$pid" 2>/dev/null || true; done
}
trap cleanup EXIT INT TERM

need() { command -v "$1" > /dev/null || { echo "Missing $1. Install it with: brew install $2"; exit 1; }; }
need ollama ollama
need cloudflared cloudflared
[ -x "$PY" ] || { echo "Missing .venv. Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"; exit 1; }

# 1. Ollama (reuse one that is already running)
if ! curl -sf http://127.0.0.1:11434/api/tags > /dev/null; then
  echo "Starting Ollama…"
  ollama serve > "$LOG_DIR/ollama.log" 2>&1 &
  PIDS+=($!)
  for _ in $(seq 1 30); do curl -sf http://127.0.0.1:11434/api/tags > /dev/null && break; sleep 1; done
fi
if ! ollama list | awk '{print $1}' | grep -qx "$MODEL"; then
  echo "Downloading $MODEL (first run only)…"
  ollama pull "$MODEL"
fi

# 2. FlowForge (API + website)
if curl -sf "http://127.0.0.1:$PORT/health" > /dev/null; then
  echo "Port $PORT is already in use. Stop the other server or run: PORT=8001 $0"; exit 1
fi
echo "Starting FlowForge on port ${PORT}…"
FLOWFORGE_MODEL="$MODEL" "$PY" -m flowforge serve --port "$PORT" > "$LOG_DIR/flowforge.log" 2>&1 &
PIDS+=($!)
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/health" > /dev/null && break; sleep 1; done
curl -sf "http://127.0.0.1:$PORT/health" > /dev/null || { echo "FlowForge did not start; see $LOG_DIR/flowforge.log"; exit 1; }

# 3. Public tunnel
echo "Opening a public link…"
cloudflared tunnel --no-autoupdate --url "http://127.0.0.1:$PORT" > "$LOG_DIR/tunnel.log" 2>&1 &
PIDS+=($!)
URL=""
for _ in $(seq 1 40); do
  URL="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_DIR/tunnel.log" | head -1 || true)"
  [ -n "$URL" ] && break; sleep 1
done
[ -n "$URL" ] || { echo "Could not open the tunnel; see $LOG_DIR/tunnel.log"; exit 1; }
# the tunnel hostname takes a few seconds to resolve worldwide
for _ in $(seq 1 30); do curl -sf "$URL/health" > /dev/null && break; sleep 2; done

ENC="$("$PY" -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe=""))' "$URL")"
cat <<EOF

  FlowForge is online (LLM planner: $MODEL on this Mac)

  Public site:      $URL
  GitHub Pages:     https://geetichahal.github.io/flowforge/?api=$ENC
  On this Mac:      http://127.0.0.1:$PORT   (can also push to a Flowise on this Mac)

  Logs: $LOG_DIR
  Press Ctrl+C to stop. The public link stops working when you do.

EOF
wait
