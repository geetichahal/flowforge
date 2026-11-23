# FlowForge

**Natural-language automations, generated as a Flowise flow, an n8n workflow and a Python script, validated, self-corrected and dry-run.**

```
"when I get an email with an invoice attached, save it to Drive and ping me on Slack"
        │
   Planner agent ── Ollama (qwen3:4b) first, rule-based template fallback
        │  structured plan: trigger, steps, branches, {{step.field}} data mappings
        ▼
   Validator (plan) ──fail──► Self-correction: errors → planner revises (max 3 attempts)
        │ pass
        ├─► Flowise generator ─► AgentFlow v2 export  (import into Flowise; verified on Flowise 3.1.x)
        ├─► n8n generator     ─► n8n workflow JSON
        └─► Python generator  ─► standalone script (stdlib + requests, dry-run by default)
        ▼
   Validator (all three outputs) ─► Simulator (mock email/sheets/drive/messaging/HTTP) ─► step trace
```

Everything is free and local. No API key is needed to build, run, test or demo it.

**Try it in your browser:** https://geetichahal.github.io/flowforge/. No install needed; the engine runs in the page. Watch self-correction with [this demo link](https://geetichahal.github.io/flowforge/?q=when%20a%20webhook%20receives%20an%20order%2C%20if%20the%20amount%20is%20over%20100%20post%20to%20slack%20otherwise%20add%20a%20row%20to%20the%20spreadsheet&fault=1&tab=tab-sim).

## Quick start

```bash
cd ~/flowforge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

# one request -> all outputs in output/<name>/
.venv/bin/python -m flowforge generate "when I get an email with an invoice attached, save it to Drive and ping me on Slack"

# watch the self-correction loop repair a deliberately corrupted plan
.venv/bin/python -m flowforge generate "..." --inject-fault

# website + API  ->  http://127.0.0.1:8000   (API docs at /docs, older Gradio dashboard at /ui)
.venv/bin/python -m flowforge serve

# evaluation over the fixed request catalog -> eval/results-template.md
.venv/bin/python -m flowforge eval

# tests
.venv/bin/python -m pytest -q
```

**Optional LLM planner:** install [Ollama](https://ollama.com), run `ollama pull qwen3:4b`, and FlowForge uses it automatically (`--mode auto`, the default). Override the model with `FLOWFORGE_MODEL`, the host with `OLLAMA_HOST`. Without Ollama, the template planner is used and the UI/CLI say so.

## Website

`python -m flowforge serve` serves the website at `http://127.0.0.1:8000`. Type a request, or click an example. The site draws the plan as a flow diagram and shows the self-correction attempts. It has tabs for the Flowise flow, n8n workflow, Python script, checks, dry-run trace and plan JSON, each with copy and download buttons. With the server, the Ollama planner and **Create in Flowise** are available.

The same site also runs **with no server at all**:

```bash
python tools/build_site.py          # writes site/
python -m http.server -d site 8080  # or upload site/ to GitHub Pages / Netlify
```

In that mode the Python engine runs inside the browser through Pyodide 0.26.4, loaded from jsDelivr. The rule-based planner, all three generators, the validator, self-correction and the dry run work. The LLM planner and pushing to Flowise need the server. Add `?engine=browser` to force browser mode while the server is running.

Shareable links: `?q=<request>&fault=1&tab=tab-sim` pre-fills the request, turns on fault injection and opens a tab.

Checked in headless Chrome: server mode, browser-only mode, light and dark themes, and a 390px-wide viewport with no horizontal overflow.

## Using the outputs

| Output | How to use it |
|---|---|
| `flowise-flow.json` | Flowise → Agentflows → **Import**, or `python -m flowforge push-flowise flowise-flow.json` (needs `FLOWISE_URL`, `FLOWISE_API_KEY`), or **Push to Flowise** in the dashboard. Then follow `flowise-post-import.txt`: connect credentials, pick a model (ChatOllama works locally), create any `$vars` it uses, save. It's a starting canvas, not a one-click automation. |
| `n8n-workflow.json` | n8n → Workflows → **Import from File**. Select credentials on the nodes that ask for them. `{{env.X}}` references become `$env.X`. |
| `automation.py` | `python automation.py` dry-runs it (prints every action, touches nothing). `FLOWFORGE_DRY_RUN=0 python automation.py` runs it live and **asks for approval before every external action**. Webhook/form triggers: `--serve`. Saved trigger data: `--payload file.json`. Env vars it needs are listed in its docstring. |

## Project layout

```
flowforge/
  catalog.py          24-node catalog: params, outputs, sample data, Flowise/n8n mapping status
  plan.py             plan model, {{ref}} parsing, graph helpers, normalization
  planner.py          LLM planner, template planner, deterministic repair, fault injection
  llm.py              Ollama client (stdlib)
  validator.py        static checks for the plan + Flowise / n8n / Python outputs
  pipeline.py         plan → validate → generate ×3 → validate → revise loop → simulate
  simulator.py        mock connectors + step trace
  generators/
    flowise.py        Python port of Flowise's canvas initNode(); AgentFlow v2 export
    n8n.py            n8n workflow JSON
    python_gen.py     standalone script generator
  data/flowise_node_specs.json   exact node definitions dumped from the Flowise build
  api.py, ui.py, cli.py, evaluate.py, flowise_client.py
tools/dump_flowise_specs.js      regenerates the specs from a built Flowise checkout
eval/requests.json               18 fixed requests; results-template.md is the latest run
examples/                        committed outputs for two requests
tests/test_flowforge.py          31 tests
web/                             the website (index.html, app.js, styles.css)
tools/build_site.py              static, server-free build of the website
```

## How the Flowise output was made exact, and verified

The Flowise generator doesn't hand-write node JSON. `tools/dump_flowise_specs.js` instantiates the compiled node classes from the local Flowise build (`~/Flowise/packages/components/dist`) and dumps their real definitions. `generators/flowise.py` then rebuilds each node the way the canvas does (`packages/ui/src/utils/genericHelper.js#initNode`), so node names, versions, `inputParams` ids and output anchors match the platform. Variable syntax (`{{ question }}`, `{{ $form.x }}`, `{{ $webhook.body.x }}`, `{{ $vars.X }}`, `{{ nodeId }}`, `{{ nodeId.output.x }}`) and the Tool node's `toolAgentflowSelectedToolConfig` / `toolInputArgs` contract were read from `buildAgentflow.ts` and `Tool.ts`.

**Verified against a running Flowise (local build, port 3999, scratch database):**

- 7 generated agentflows were created via `POST /api/v1/chatflows` and read back. One was created via the UI's import endpoint `POST /api/v1/export-import/import` (`{"message":"success"}`).
- **Execution in Flowise's runtime:** the request *"when a webhook receives an order, if the amount is over 100 post it to …/big-orders otherwise post it to …/small-orders"* was generated and triggered through `POST /api/v1/webhook/<flowId>`. Every node reported `FINISHED`. The receiving server got `{"order_id":"A-250","amount":250}` on `/big-orders` and `{"order_id":"A-40","amount":40}` on `/small-orders`. (Flowise blocks requests to localhost by default; `HTTP_SECURITY_CHECK=false` was set on the test instance only.)

**Not verified:** flows that need Google/Slack/Jira credentials or a chat model were imported, but not executed. The canvas UI rendering was not checked in a browser. **n8n output was checked only by FlowForge's own structural validator** (there's no n8n instance here). Node types/versions follow n8n's published formats, so import it once before the demo.

### Corrections to `FLOWISE-INTEGRATION.md` (found in the source)

| Doc says | Flowise source (Start node v1.4) says | Effect on FlowForge |
|---|---|---|
| No webhook-start node; use the prediction API | `startInputType: "webhookTrigger"`, route `/api/v1/webhook/<flowId>`, payload as `{{ $webhook.body.* }}` | `webhook` maps **directly** |
| Schedule payload unconfirmed; set in UI | `startInputType: "scheduleInput"` with `scheduleType: "cronExpression"`, `scheduleCronExpression`, `scheduleTimezone` | `schedule` maps **directly** (enable it on the Start node after import) |
| Condition / CustomFunction / DirectReply params unconfirmed | `conditions[{type, value1, operation, value2}]` with outputs `-output-0` / `-output-1`; `customFunctionInputVariables` (as `$name` in JS) + `customFunctionJavascriptFunction`; `directReplyMessage` (terminal node) | all emitted with confirmed params |
| Agentflow variable syntax unconfirmed | confirmed (list above) | data mappings work across nodes |
| Tool arguments `APPROXIMATE_SEE_DOC` | Gmail `sendMessage(to, subject, body)`, Drive `createFile(name, content, mimeType, parents)`, Sheets `appendValues(spreadsheetId, range, values)` from the tools' zod schemas | exact `toolInputArgs` |

Still true: there's no email-arrival trigger (FlowForge emits a 5-minute schedule plus an Agent with the Gmail tool, and warns). There's no Slack node (HTTP to an incoming webhook), no delay node, and no log node. Those steps are flagged `W_FLOWISE_UNSUPPORTED`, shown as a sticky note on the canvas, and still performed by the n8n and Python outputs. Gmail has no attachment-download action, so binary attachments in the Flowise path are flagged.

## Safety model

- **Simulation is the default.** The simulator never touches the network, and generated scripts dry-run unless `FLOWFORGE_DRY_RUN=0`.
- **Human approval gate.** In live mode every external side effect prompts `Approve: …? [y/N]`. Skipping the prompt requires explicitly setting `FLOWFORGE_AUTO_APPROVE=1`. A test proves a denied approval sends nothing and an approved one sends the real request.
- **No credentials in output.** Secret parameters must be `{{env.NAME}}` references (`E_LITERAL_SECRET`). All outputs are scanned for token patterns (Slack/Discord webhooks, `sk-`, AWS, GitHub, Google keys, bearer tokens, private keys, `password=`). No Flowise credential ids or n8n `credentials` blocks are emitted.
- **Flowise push is opt-in.** The API requires `confirm: true`, and the CLI/UI only push on an explicit command or button.

## Evaluation (template planner, `eval/results-template.md`)

18 requests covering email, storage, spreadsheets, messaging, schedules, webhooks, forms, branches, delays and multi-step chains. All 18 pass every metric: intent, plan validity, Flowise/n8n validity, Python parse, **Python dry-run executed in a subprocess**, simulation, and repair of injected faults. A repair counts only if the repaired plan has the same steps *and* the same dry-run behavior as the unfaulted plan.

An earlier version of this metric counted any valid result as repaired. Under the stricter definition, that version of the repairer scored **10/18**: it deleted steps it couldn't identify, and it refilled a lost `greater_than` with the default `contains`, which flipped a branch. Both are fixed and have regression tests. The 18/18 above is the stricter number.

Read that number carefully: **the requests were written alongside the template planner, so 100% measures the pipeline's correctness on known phrasings, not generalization.** Six extra phrasings tried during development exposed planner gaps (missing an email trigger without "when", misordered fetch → post, "translate … forward"). Those were fixed, so they're no longer a held-out test either. Open-ended phrasing is what the LLM planner is for. Its plumbing (request format, JSON parsing, error feedback in revisions) is covered by a test against a fake Ollama server, but **its plan quality hasn't been measured, because Ollama isn't installed on this machine.** Run `python -m flowforge eval --mode llm` once it is, and report those numbers too.

## Node catalog (24)

Triggers: `manual`, `webhook`, `schedule`, `email_received`, `form_submitted`.
Actions/logic: `send_email`, `slack_message`, `discord_message`, `teams_message`, `sms_message`, `notify`, `sheets_append`, `sheets_read`, `drive_save`, `drive_list`, `extract_attachments`, `http_request`, `condition`, `transform`, `delay`, `llm_summarize`, `create_task`, `calendar_create`, `log_message`.
`python -m flowforge catalog` prints params and outputs. `GET /catalog` returns them with each node's Flowise/n8n mapping status.

## API

`POST /plan`, `/generate/flowise`, `/generate/n8n`, `/generate/python`, `/validate`, `/simulate`, `/run` (full pipeline, optional `inject_fault`), `/flowise/push` (needs `confirm: true`), `GET /catalog`, `/health`.

## Re-generating Flowise node specs

If you upgrade Flowise: `pnpm build` it, then `node tools/dump_flowise_specs.js /path/to/Flowise`. The script shims `SlowBuffer`, which Node ≥ 25 removed but one of Flowise's dependencies still reads.
