#!/usr/bin/env bash
# One command for the demo: starts Ollama, Flowise and FlowForge (skipping any already running),
# then opens the website.
#
#   ~/flowforge/tools/demo_start.sh        # Ctrl+C stops FlowForge (Ollama and Flowise keep running)
#
# Logs: ~/flowforge/output/logs/
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$ROOT/output/logs"; mkdir -p "$LOGS"
up() { curl -sf "$1" > /dev/null 2>&1; }
wait_for() {  # url, name, seconds
  for _ in $(seq 1 "$3"); do up "$1" && { echo "  ✓ $2 is ready"; return 0; }; sleep 1; done
  echo "  ✗ $2 did not start in $3 s. See $LOGS"; return 1
}

echo "1/3 Ollama (local AI model)"
if up http://127.0.0.1:11434/api/tags; then echo "  ✓ already running"
else
  command -v ollama > /dev/null || { echo "  ✗ Ollama is not installed: brew install ollama"; exit 1; }
  nohup ollama serve > "$LOGS/ollama.log" 2>&1 &
  wait_for http://127.0.0.1:11434/api/tags Ollama 30 || exit 1
fi
ollama list 2>/dev/null | grep -q '^qwen3:4b ' || echo "  ! model qwen3:4b missing: run  ollama pull qwen3:4b"

echo "2/3 Flowise (http://localhost:3000)"
if up http://127.0.0.1:3000/api/v1/ping; then echo "  ✓ already running"
else
  nohup "$ROOT/tools/flowise_start.sh" > "$LOGS/flowise.log" 2>&1 &
  wait_for http://127.0.0.1:3000/api/v1/ping Flowise 120 || echo "  (continuing without Flowise; steps 1-5 of the demo still work)"
fi

echo "3/3 FlowForge (http://127.0.0.1:8000)"
if up http://127.0.0.1:8000/health; then echo "  ✓ already running"; open http://127.0.0.1:8000; exit 0; fi
( wait_for http://127.0.0.1:8000/health FlowForge 60 && open http://127.0.0.1:8000 ) &
echo "  Starting… the browser opens when it's ready. Press Ctrl+C here to stop FlowForge."
cd "$ROOT" && exec .venv/bin/python -m flowforge serve
