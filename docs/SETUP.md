# Setup and installation

## Requirements

| Component | Needed for | Version used for the recorded results |
|---|---|---|
| Python | everything | 3.14.4 (macOS, Apple silicon). The code targets 3.10+, but only 3.14 was tested. |
| pip packages (`requirements.txt`) | API, dashboard, tests, live scripts | fastapi, uvicorn, gradio, requests, pytest, httpx |
| Ollama + `qwen3:4b` | optional LLM planner | Ollama with `qwen3:4b` and `qwen3:4b-instruct` |
| Node.js | optional Flowise / n8n checks | v26.13.1 |
| Flowise | optional import + runtime demo | 3.1.4, built from source in `~/Flowise` |
| n8n | optional import + execution check | 2.49.5 from npm |

No API key or cloud account is needed to build, test, run the dashboard, evaluate or simulate.

## 1. Core install (required)

```bash
cd ~/flowforge
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q          # expect: 47 passed
```

## 2. Run the dashboard and API

```bash
.venv/bin/python -m flowforge serve            # http://127.0.0.1:8000
```

- Gradio dashboard: <http://127.0.0.1:8000/ui>
- API docs (Swagger): <http://127.0.0.1:8000/docs>
- Website front-end: <http://127.0.0.1:8000/>

## 3. Optional: LLM planner (Ollama)

```bash
brew install ollama            # or https://ollama.com/download
ollama serve &                 # if not already running as a service
ollama pull qwen3:4b
ollama pull qwen3:4b-instruct  # used inside generated AI steps
```

FlowForge finds it automatically (`--mode auto`). Environment overrides:

- `FLOWFORGE_MODEL` (planner model)
- `FLOWFORGE_AGENT_MODEL` (model inside generated AI steps)
- `OLLAMA_HOST`
- `FLOWFORGE_OLLAMA_TIMEOUT` (availability check, default 5 s)
- `FLOWFORGE_LLM_BUDGET` (seconds of LLM self-correction before switching to the deterministic repairer, default 60)

On an 8 GB machine, close other heavy apps. Flowise, n8n and Ollama together can exhaust memory.

## 4. Optional: Flowise (for the import and runtime demonstration)

```bash
git clone https://github.com/FlowiseAI/Flowise ~/Flowise
cd ~/Flowise && pnpm install && pnpm build
cd ~/flowforge && tools/flowise_start.sh       # http://localhost:3000
```

The first time, create the admin account in the browser, then go to *Settings → API Keys* and create a
key with the chatflows/agentflows permissions. Give it to FlowForge in one of two ways:

- `export FLOWISE_URL=http://localhost:3000 FLOWISE_API_KEY=<key>`, or
- `~/.flowforge/config.json` containing `{"flowise_url": "http://localhost:3000", "flowise_api_key": "<key>"}`
  (chmod 600; it is outside the repository and never committed).

The runtime demo posts to a local receiver on 127.0.0.1. Flowise's HTTP node refuses loopback targets
unless the Flowise server was started with `HTTP_SECURITY_CHECK=false`. Set that only on a local test
instance.

## 5. Optional: n8n (for the import and execution check)

```bash
mkdir -p /tmp/n8n-check && cd /tmp/n8n-check && npm init -y && npm install n8n@2.49.5
cd ~/flowforge && N8N_DIR=/tmp/n8n-check .venv/bin/python tools/n8n_check.py
```

The check uses its own temporary n8n data folder (SQLite), so it never touches an existing n8n instance.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Dashboard says "LLM planner unavailable" | Ollama is not running or the model is not pulled; the template planner is used (this is expected without Ollama). |
| `Flowise POST /api/v1/chatflows -> HTTP 401` | Missing or wrong `FLOWISE_API_KEY`. |
| Flowise demo: HTTP node error mentioning a private/loopback address | Restart the local Flowise with `HTTP_SECURITY_CHECK=false`. |
| Flowise fails to start on Node ≥ 25 (`SlowBuffer`) | Use `tools/flowise_start.sh`, which installs a shim. |
| LLM evaluation is very slow | Expected on CPU or 8 GB RAM; each request also re-runs with injected faults. |
