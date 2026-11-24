# User guide

## 1. Describe an automation

Write one sentence that says **when** it should run and **what** it should do. The planner recognises:

| Trigger | Example wording |
|---|---|
| Webhook | "when a webhook receives an order…", "every time our GitHub webhook fires…" |
| Form submitted | "when someone submits the contact form…", "after someone completes the interview form…" |
| Email received | "when I get an email with an invoice attached…", "for each new email with invoice in the subject…" |
| Schedule | "every Monday at 9am…", "each weekday at 7:30am…", "every 15 minutes…", "on the first day of every month…" |
| Manual | anything else ("summarize the text I give you and let me know") |

Actions include sending email, posting to Slack/Discord/Teams, SMS, appending or reading spreadsheet rows,
saving or listing Drive files, HTTP calls, calendar events, Jira tasks, summarising with an LLM, data
transformation ("convert the name to uppercase"), delays ("wait 10 minutes"), reminders and notifications.
Branches use `if <field> <is over | is less than | equals | contains> <value> … otherwise …`.

The rule-based template planner only knows these phrasings. The LLM planner (with Ollama) is more
flexible. See [EVALUATION.md](EVALUATION.md) for how often each one picks the right steps on new wording.

## 2. Dashboard (`/ui`)

Start it with `.venv/bin/python -m flowforge serve` and open <http://127.0.0.1:8000/ui>.

1. Type the request (or click an example).
2. Choose the **Planner**:
   - `auto`: LLM if available, else template
   - `llm`: LLM only
   - `template`: rule-based only
3. Optionally tick **Demo: inject a fault** to see self-correction repair a corrupted plan.
4. Click **Generate**. The status line says whether a valid automation was produced and in how many attempts.

| Tab | Shows |
|---|---|
| Plan | Correction history (each attempt, its planner, errors and repairs), step summary, structured plan JSON |
| Flowise | AgentFlow v2 export, post-import checklist, optional *Push to Flowise* (URL + API key) |
| n8n | n8n workflow JSON |
| Python | standalone script |
| Validation | every error and warning from all four validators |
| Simulation | dry-run trace: step, node, inputs, outputs, status |
| Downloads | `flowise-flow.json`, `n8n-workflow.json`, `automation.py`, `plan.json` |

If no valid plan is reached within 3 attempts, the status says so. The remaining errors are listed, and
only the plan is offered for download.

## 3. Command line

```bash
.venv/bin/python -m flowforge generate "every day at 8am call https://api.example.com/status and post it to discord" --out output/status
.venv/bin/python -m flowforge generate "<request>" --inject-fault      # show self-correction
.venv/bin/python -m flowforge validate output/status/plan.json
.venv/bin/python -m flowforge simulate output/status/plan.json
.venv/bin/python -m flowforge catalog
.venv/bin/python -m flowforge eval --mode template [--requests eval/requests-heldout2.json]
```

`generate` writes these files:

- `plan.json`
- `flowise-flow.json`
- `flowise-post-import.txt`
- `n8n-workflow.json`
- `automation.py`
- `validation.txt`
- `simulation.txt`

## 4. REST API

Interactive docs are at `/docs`. The main endpoints (JSON in, JSON out):

| Endpoint | Body | Returns |
|---|---|---|
| `POST /plan` | `{"request": "...", "mode": "template"}` | `{plan, source, note, validation}` |
| `POST /generate/flowise` | `{"plan": {...}}` | `{export, warnings, post_import}` |
| `POST /generate/n8n` | `{"plan": {...}}` | `{workflow, ...}` |
| `POST /generate/python` | `{"plan": {...}}` | `{code, env_vars, warnings}` |
| `POST /validate` | `{"plan": {...}}` | `{ok, errors, warnings, issues[]}` (plan + all three outputs) |
| `POST /simulate` | `{"plan": {...}, "trigger_payload": {...}?}` | `{ok, trace[], world, error}` |
| `POST /run` | `{"request": "...", "mode": "...", "inject_fault": false}` | full pipeline result incl. attempts |

```bash
curl -s localhost:8000/plan -H 'content-type: application/json' \
  -d '{"request":"when someone submits the contact form, email them a thank-you email","mode":"template"}'
```

## 5. Using the outputs

**Flowise** (`flowise-flow.json`)

- In Flowise's Agentflows (v2) section, import or load the file into a new agentflow. The exact menu
  wording differs between Flowise versions, and the UI route was not clicked through for this report.
- The verified route is the API: *Push to Flowise* in the dashboard, or
  `python -m flowforge push-flowise flowise-flow.json`. Both call `POST /api/v1/chatflows`.
- Then follow `flowise-post-import.txt`: attach credentials to Tool nodes, pick a chat model, and create any
  `$vars` (e.g. `SLACK_WEBHOOK_URL`). Steps Flowise has no node for (delay, log) appear as a sticky note.

**n8n** (`n8n-workflow.json`)

- In n8n, open *Workflows → Import from File*, or run `n8n import:workflow --input=n8n-workflow.json`.
- Select credentials on nodes that need them.
- `{{env.X}}` values become `$env.X`. Set those environment variables for n8n, and allow env access in
  nodes (`N8N_BLOCK_ENV_ACCESS_IN_NODE=false`).

**Python** (`automation.py`)

```bash
python automation.py                                   # dry run: prints what it would do, sends nothing
FLOWFORGE_DRY_RUN=0 python automation.py               # live: asks "Approve: ...? [y/N]" before each action
FLOWFORGE_DRY_RUN=0 python automation.py --payload trigger.json   # supply the trigger data
python automation.py --serve                           # webhook/form triggers: listen on http://localhost:8765
```

- The script's docstring lists the environment variables it needs.
- If a step fails, the script prints `[step] FAILED: <error>`, stops with exit code 1 and runs no later steps.
- Under `--serve`, a failed step returns HTTP 500 with the failing step's name.
