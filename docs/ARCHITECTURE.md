# System architecture

FlowForge turns a natural-language automation request into one structured plan, then renders that plan
three ways (a Flowise flow, an n8n workflow and a standalone Python script). It checks all four artefacts,
repairs the plan when a check fails, and dry-runs the result against mock services.

## Data flow

```
 request (plain English)
        │
        ▼
 ┌───────────────┐   plan.json: trigger, ordered steps, branches, {{step.field}} mappings
 │ Planner agent │── LLM (Ollama, qwen3:4b) → falls back to the rule-based template planner
 └──────┬────────┘
        ▼
 ┌───────────────┐  errors   ┌────────────────────────────────────────────┐
 │ Validator     │──────────►│ Self-correction (max 3 attempts in total)  │
 │ (plan)        │◄──────────│ plan + errors → planner.revise()           │
 └──────┬────────┘  revised  │ LLM revision, else deterministic repair    │
        │ ok                 └────────────────────────────────────────────┘
        ├──► Flowise generator ─► AgentFlow v2 export JSON ─┐
        ├──► n8n generator     ─► n8n workflow JSON         ├─► Validator (all three outputs)
        └──► Python generator  ─► automation.py             ┘      │ errors → self-correction again
                                                                   ▼ ok
                                                     Simulator (mock connectors) → step trace
```

Everything after the planner is deterministic and standard-library Python. Only the dashboard and API
need third-party packages (FastAPI, Gradio). Generated scripts need `requests` for live mode only.

## Modules

| Module | Responsibility |
|---|---|
| `flowforge/catalog.py` | 25 node types: kind, description, required/optional params, outputs, sample output, mock service, Flowise and n8n mapping status. See [NODE_CATALOG.md](NODE_CATALOG.md). |
| `flowforge/plan.py` | Plan model helpers: `{{ref}}` parsing and substitution, topological order, ancestors, trigger lookup, normalisation, text summary. |
| `flowforge/planner.py` | Planner agent: `plan()` (LLM first, template fallback), `revise()` (self-correction step), `repair()` (deterministic fixes per error code), `inject_fault()` (demo/eval). |
| `flowforge/llm.py` | Minimal Ollama client (stdlib `urllib`), JSON-mode chat, availability check. |
| `flowforge/validator.py` | Static checks for the plan and for each generated output; structured `Report` of `Issue(level, code, message, node, target)`. |
| `flowforge/pipeline.py` | Orchestrates plan → validate → generate ×3 → validate → revise loop → simulate. |
| `flowforge/generators/flowise.py` | Plan → Flowise AgentFlow v2 export, built from node specs dumped from a real Flowise 3.1.x build. |
| `flowforge/generators/n8n.py` | Plan → n8n workflow JSON (nodes, typeVersions, positions, parameters, connections). |
| `flowforge/generators/python_gen.py` | Plan → standalone script: one function per node, `main()`, env-var config, approval gate, error handling. |
| `flowforge/simulator.py` | Dry run with `MockEmail`, `MockSpreadsheet`, `MockMessaging`, `MockStorage`, `MockHTTP`; per-step trace. |
| `flowforge/api.py` | FastAPI app: `/plan`, `/generate/{flowise,n8n,python}`, `/validate`, `/simulate`, `/run`, `/catalog`, `/health`, opt-in Flowise push/deploy; mounts the Gradio dashboard at `/ui`. |
| `flowforge/ui.py` | Gradio dashboard (request input, plan, Flowise, n8n, Python, validation, simulation, downloads). |
| `flowforge/cli.py` | `python -m flowforge generate | simulate | validate | push-flowise | eval | serve | catalog`. |
| `flowforge/evaluate.py` | Evaluation harness over fixed request sets. |
| `flowforge/flowise_client.py`, `deploy.py` | Opt-in REST client for a running Flowise (create, read, trigger webhook, predict). |
| `tools/*.py` | Evidence scripts: Flowise runtime demo, Flowise import of all eval flows, n8n check, API smoke test, catalog doc. |

## The structured plan

The plan is the single source of truth shared by all generators.

```json
{
  "name": "Invoice email to Drive and Slack",
  "request": "when I get an email with an invoice attached, save it to Drive and ping me on Slack",
  "nodes": [
    {"id": "trigger", "type": "email_received", "params": {"query": "has:attachment subject:invoice"}},
    {"id": "extract_attachments", "type": "extract_attachments", "params": {"source": "{{trigger.attachments}}"}},
    {"id": "save_to_drive", "type": "drive_save",
     "params": {"file_name": "{{extract_attachments.first_name}}", "content": "{{extract_attachments.first_content}}"}},
    {"id": "notify_slack", "type": "slack_message",
     "params": {"webhook_url": "{{env.SLACK_WEBHOOK_URL}}",
                "text": "New email from {{trigger.from}} (saved: {{save_to_drive.link}})"}}
  ],
  "edges": [
    {"from": "trigger", "to": "extract_attachments"},
    {"from": "extract_attachments", "to": "save_to_drive"},
    {"from": "save_to_drive", "to": "notify_slack"}
  ]
}
```

- **Trigger:** exactly one node whose catalog kind is `trigger`, conventionally with id `trigger`.
- **Ordered action steps:** the nodes reachable from the trigger. Execution order is the topological order of `edges`.
- **Conditional branches:** a `condition` node (`field`, `operator`, `value`) whose outgoing edges carry
  `"branch": "true"` or `"branch": "false"`.
- **Data mappings:** `{{node_id.field}}` references to an upstream node's declared outputs, and
  `{{env.NAME}}` for configuration and secrets. The validator checks that each reference points to an
  existing, upstream node and a field that node actually outputs.

## Planner agent

`planner.plan(request, mode)` with `mode` = `auto` (default), `llm` or `template`:

1. **LLM planner.** The system prompt lists the whole catalog (types, params, outputs), the plan format,
   rules (one trigger, `{{env.X}}` for secrets, branch labels) and one worked example. The model is
   called in JSON mode (`qwen3:4b` by default, `FLOWFORGE_MODEL` to change). If Ollama is unreachable or the
   answer is unusable, the template planner is used and the reason is returned as `planner_note`.
2. **Template planner.** A rule-based parser for triggers (webhook, form, email, schedule with a cron
   parser, manual), ~18 action patterns, an `if … otherwise …` condition splitter and data-flow rules
   (later steps receive the most recent useful output). Requests with no recognisable automation become an
   `ai_agent` step.

## Self-correction loop (`pipeline.run`)

```
attempt 1..3:
    report = validate(plan) + validate(generate_all(plan))
    if report.ok: stop
    if attempt == 3: stop and report failure (ok = false, all errors listed)
    plan = planner.revise(plan, report)      # LLM gets plan + error list; else deterministic repair()
```

Each attempt is recorded with its planner source (`llm:…`, `template`, `repair`), its errors and the
revision actions. The dashboard, CLI and API all show this history. `inject_fault=True` corrupts the first
plan (removes a required param, adds an edge to a missing node, pastes a fake literal Slack webhook, renames
a node to an unknown type) to demonstrate the loop. An LLM revision is accepted only if it has fewer errors
than the plan it replaces. A time budget (`FLOWFORGE_LLM_BUDGET`, 60 s) switches later revisions to the
instant deterministic repairer.

## Generators

- **Flowise** (`AgentFlowV2` export): node definitions come from `flowforge/data/flowise_node_specs.json`,
  which `tools/dump_flowise_specs.js` dumps from a compiled Flowise build. The canvas's `initNode()` is
  ported, so node names, versions, input-param ids and anchors match Flowise. Triggers map to the Start
  node (`chatInput`, `formInput`, `webhookTrigger`, `scheduleInput`). Gmail, Sheets, Drive, Calendar and
  Jira map to Tool nodes with exact `toolInputArgs`. Webhook messaging (Slack, Discord, Teams) uses
  Custom Function + HTTP. Steps with no Flowise equivalent (delay, log) become a sticky note and a
  `W_FLOWISE_UNSUPPORTED` warning.
- **n8n**: native node types with explicit `typeVersion`s (e.g. `n8n-nodes-base.gmail` v2.1, `if` v2,
  `httpRequest` v4.2), a left-to-right layout, `connections` keyed by node name with `main` outputs (IF
  nodes use output 0 = true, 1 = false), `{{ref}}` → `{{ $('Node').item.json.field }}`, `{{env.X}}` →
  `$env.X`. No `credentials` blocks are emitted.
- **Python**: a runtime header (`env()`, `render()`, `approve()`, `http()`, `as_text()`, `StepFailed`),
  then one commented `step_<id>(ctx)` function per node, a `STEPS` table with each step's incoming edges
  (and branch), `run()`, an optional HTTP listener for webhook/form triggers, and `main()`.

## Validator

`validate_plan`, `validate_flowise`, `validate_n8n`, `validate_python` and `validate_all` each return a
`Report` (`ok`, error/warning counts, issues with code, message, node and target). Main checks:

| Check | Plan | Flowise | n8n | Python |
|---|---|---|---|---|
| Trigger present / single | `E_NO_TRIGGER`, `E_MULTIPLE_TRIGGERS`, `E_TRIGGER_INCOMING` | `E_FW_START` | `E_N8N_TRIGGER` | `E_PY_STRUCTURE` (a function for every step, trigger included) |
| Connections reference existing nodes | `E_DANGLING_EDGE` | `E_FW_EDGE` | `E_N8N_CONNECTION` | – |
| Valid input/output connections | `E_BRANCH_LABEL`, `W_STRAY_BRANCH`, `W_CONDITION_NO_BRANCH`, `E_UNREACHABLE`, `E_SELF_LOOP` | `E_FW_EDGE` (anchor ids), `E_FW_UNREACHABLE` | `E_N8N_CONNECTION` (type), `E_N8N_UNREACHABLE` | – |
| Required parameters | `E_MISSING_PARAM`, `E_BAD_ENUM`, `E_BAD_NUMBER`, `E_BAD_MAPPING` | `E_FW_PARAM_ID`, `E_FW_TOOL_ACTION` | `E_N8N_NODE` | – |
| Known node types | `E_UNKNOWN_TYPE` | `E_FW_UNKNOWN_NODE`, `E_FW_UNKNOWN_TOOL`, `W_FW_VERSION` | (registry check in `tools/n8n_check.py`) | – |
| Data mappings | `E_BAD_REF`, `E_REF_NOT_UPSTREAM`, `E_BAD_REF_FIELD`, `E_BAD_ENV_REF` | – | – | – |
| No unintended cycles | `E_CYCLE` | `E_FW_CYCLE` | `E_N8N_CYCLE` | – |
| No embedded credentials | `E_LITERAL_SECRET`, `E_EMBEDDED_CREDENTIAL` | `E_FW_CREDENTIAL`, `E_EMBEDDED_CREDENTIAL` | `E_N8N_CREDENTIAL`, `E_EMBEDDED_CREDENTIAL` | `E_EMBEDDED_CREDENTIAL` |
| Syntax / structure | `E_PLAN_SHAPE`, `E_DUPLICATE_ID`, `E_BAD_ID` | `E_FW_SHAPE`, `E_FW_FLOWDATA`, `E_FW_NODE_ID` | `E_N8N_SHAPE`, `E_N8N_DUP_NAME`, `E_N8N_DUP_ID` | `E_PY_SYNTAX` (`ast.parse`), `E_PY_STRUCTURE` (one function per node + `main`), `E_PY_DEPENDENCY` (stdlib + `requests` only) |

## Simulator

`simulate(plan, trigger_payload=None)` walks the plan in topological order. It uses the trigger's sample
output (or a supplied payload), renders each node's params against earlier outputs, and calls a mock
connector:

- `MockEmail`: inbox and sent box
- `MockSpreadsheet`: rows per sheet
- `MockMessaging`: Slack, Discord, Teams, SMS and notifications
- `MockStorage`: Drive files, with fake links
- `MockHTTP`: canned responses

Condition nodes evaluate for real, so only the branch taken runs; the other branch's steps are marked
`skipped`. Each trace entry holds step number, node id, node type, rendered inputs, outputs, status and a
human-readable detail. The final mock world state (emails sent, rows added, files saved, messages posted)
is returned too. No connector opens a socket; a test enforces this.
