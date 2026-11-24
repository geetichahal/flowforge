# Demonstration procedure

A 15-20 minute live demonstration for evaluators. Steps 1-5 need only Python. Steps 6-7 need the optional
Flowise and n8n installs (see [SETUP.md](SETUP.md)).

Everything shown is backed by a script that writes its evidence to `docs/evidence/`, so each step can be
repeated with the same commands.

## 0. Before the demo (5 min)

```bash
cd ~/flowforge
.venv/bin/python -m pytest -q                       # 50 passed
tools/flowise_start.sh &                            # only for step 6 (start with HTTP_SECURITY_CHECK=false)
ollama serve &                                      # only if showing the LLM planner
.venv/bin/python -m flowforge serve                 # http://127.0.0.1:8000/ui
```

On an 8 GB machine, close browsers and other heavy apps first.

## 1. Planner → structured plan (objective 1)

In the dashboard, enter:

> when a webhook receives an order, if the amount is over 100 post to slack otherwise add a row to the spreadsheet

Choose planner `template` (instant), or `auto` if Ollama is running (30-90 s). Click **Generate** and open
the **Plan** tab. Point out:

- the trigger (`webhook`)
- the condition node with `true`/`false` edges
- the two branch actions
- the data mappings (`{{trigger.body.amount}}`)
- the Slack URL given as `{{env.SLACK_WEBHOOK_URL}}`, not a literal

## 2. The three outputs (objectives 3, 4, 5)

- **Flowise tab:** Start node (`webhookTrigger`), Condition node with two output anchors, HTTP nodes; post-import checklist.
- **n8n tab:** `n8n-nodes-base.webhook` → `if` (v2) → `httpRequest` / `googleSheets`, `connections` keyed by node name.
- **Python tab:** commented `step_*` functions, `STEPS` table, `run()` with error handling, `main()`, env-var docstring.
- **Downloads tab:** download all four files.

## 3. Validation and self-correction (objectives 6, 7)

Tick **Demo: inject a fault** and click Generate again. The Plan tab shows:

- **Fault injected:** a required param removed, an edge to `ghost_step`, a literal Slack webhook pasted, a
  node renamed to an unknown type.
- **Attempt 1:** ❌ with the structured errors (`E_MISSING_PARAM`, `E_DANGLING_EDGE`,
  `E_EMBEDDED_CREDENTIAL`/`E_LITERAL_SECRET`, `E_UNKNOWN_TYPE`), followed by the repair actions.
- **Attempt 2:** ✅

To show an honest failure, run the unit test `test_loop_reports_failure_honestly`
(`.venv/bin/python -m pytest -q -k reports_failure_honestly`). It gives the loop a plan it cannot repair.
The result comes back `ok: false`, with the remaining errors listed and no generated outputs.

## 4. Simulation (objective 8)

Open the **Simulation** tab. The trace shows step number, node, inputs, outputs and status for each step.
With the sample amount (250) the `true` branch runs and the spreadsheet step is `skipped`.

Optionally show that the simulator is offline:

```bash
.venv/bin/python -m pytest -q -k never_touches_the_network
```

## 5. API (objective 9)

Open <http://127.0.0.1:8000/docs> and run `POST /plan`, then `/validate`, then `/simulate`. Or run the
recorded smoke test:

```bash
.venv/bin/python tools/api_smoke.py          # starts its own server on :8011; writes docs/evidence/api-smoke.md
```

## 6. Flowise: import and run in Flowise's runtime (objectives 3, 9)

```bash
.venv/bin/python tools/flowise_demo.py
```

The script does the following:

1. Starts a local receiver.
2. Generates the flow for "when a webhook receives an order, if the amount is over 100 post it to
   …/big-orders otherwise post it to …/small-orders".
3. Validates it, imports it with `POST /api/v1/chatflows`, and reads it back.
4. Triggers Flowise's webhook endpoint twice (amount 250, amount 40).

Expected output:

- every node `FINISHED`
- the receiver gets `/big-orders` for 250 and `/small-orders` for 40
- `Result: PASS`

Open the printed canvas URL in Flowise to show the imported flow.

To show that all generated flows are accepted by Flowise:

```bash
.venv/bin/python tools/flowise_import_all.py   # 52 flows created, read back, compared, deleted
```

## 7. n8n: import and execute (objective 4)

```bash
N8N_DIR=/path/to/n8n-install .venv/bin/python tools/n8n_check.py
```

The check covers all 52 evaluation workflows:

- every workflow is checked against n8n's own node registry (type, typeVersion, parameter names);
- every workflow is imported with `n8n import:workflow` and exported again for comparison;
- one workflow (HTTP GET → Slack message) is executed with `n8n execute` against a local receiver.

## 8. Evaluation (objective 10)

```bash
.venv/bin/python -m flowforge eval --mode template
.venv/bin/python -m flowforge eval --mode template --requests eval/requests-heldout2.json
```

Show [EVALUATION.md](EVALUATION.md). Make three points:

- the rule-based planner is perfect on the requests it was developed against;
- it was 5/15 on the first held-out set before fixes and 11/15 on a fresh, untuned set;
- the LLM planner's held-out results are reported alongside.

## Fallbacks if something fails live

| Problem | Fallback |
|---|---|
| Ollama slow or out of memory | use the `template` planner; LLM results are recorded in `eval/results-*-llm.md` |
| Flowise not running | show `docs/evidence/flowise-runtime-demo.md` and `flowise-import-all.md` (recorded runs) |
| n8n not installed | show `docs/evidence/n8n-check.md` |
