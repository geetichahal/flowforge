#!/usr/bin/env bash
# Start Ollama, warm the model, then serve FlowForge (API + website) on the Space port.
set -e
ollama serve > /tmp/ollama.log 2>&1 &
for i in $(seq 1 60); do curl -sf http://127.0.0.1:11434/api/tags > /dev/null && break; sleep 1; done
curl -sf http://127.0.0.1:11434/api/generate \
  -d "{\"model\": \"$FLOWFORGE_MODEL\", \"prompt\": \"hi\", \"stream\": false, \"options\": {\"num_predict\": 1}}" \
  > /dev/null 2>&1 &
exec uvicorn flowforge.api:app --host 0.0.0.0 --port 7860
