# Viva preparation

How to explain FlowForge to an examiner, and how to answer the questions they are likely to ask.
Every number here comes from a recorded run in `eval/` or `docs/evidence/`. Do not quote a number that
is not in those files.

---

## 1. The 30-second explanation

> Tools like n8n and Flowise let you build automations by connecting nodes, but you have to know which
> nodes, how to wire them and what parameters they need. FlowForge removes that step. You type a sentence
> such as *"when I get an email with an invoice attached, save it to Drive and ping me on Slack"*. An
> agent turns it into a structured plan, and from that one plan we generate three runnable outputs: a
> Flowise flow, an n8n workflow and a Python script. A validator checks all of them. If a check fails, the
> agent repairs its own plan, up to three times. A simulator then dry-runs the automation against fake
> email, Drive, Sheets and Slack services, so you can see every step run without touching a real account.
> It all runs locally on a free model through Ollama, with no API key.

## 2. The one idea to remember: **one plan, many views**

The heart of the project is the **plan** (`plan.json`). Everything else either *produces* it or *reads* it.

```
sentence ──► PLANNER ──► plan ──► VALIDATOR ──fail──► SELF-CORRECTION (back to planner, max 3)
                                    │ pass
                                    ├──► Flowise generator ─► flowise-flow.json
                                    ├──► n8n generator     ─► n8n-workflow.json
                                    └──► Python generator  ─► automation.py
                                    ▼
                                 VALIDATOR (all 3 outputs) ─► SIMULATOR ─► step-by-step trace
```

A plan is just: **a trigger**, **ordered steps**, optional **branches** (if/else), and **data mappings**
such as `{{trigger.body.amount}}`, meaning "use the `amount` field that the trigger produced".

Because all three outputs come from the same plan, they cannot disagree. That is "consistent by
construction" (report §4.1). The alternative would be asking an LLM three separate times and getting
three different guesses.

## 3. What each part does, in one line each

| Part | File | One-line explanation |
|---|---|---|
| Node catalog | `flowforge/catalog.py` | The vocabulary: 25 building blocks (5 triggers, 20 actions). Each has its required parameters and its real Flowise and n8n equivalent. |
| Planner | `flowforge/planner.py`, `llm.py` | Sentence → plan. First tries the local LLM (Ollama `qwen3:4b`). If Ollama isn't running, a rule-based template planner is used instead. |
| Validator | `flowforge/validator.py` | Checks: a trigger exists, every edge points to a real node, required params are present, no cycles, no passwords or API keys written into the output. Returns a list of errors with codes, not just pass/fail. |
| Self-correction | `flowforge/pipeline.py` | Errors + plan go back to the planner, which revises. Max 3 attempts. If it still fails, it says so and shows no output. |
| Flowise generator | `generators/flowise.py` | Plan → Flowise **AgentFlow v2** export JSON (Start, Agent, Tool, HTTP, Condition nodes). |
| n8n generator | `generators/n8n.py` | Plan → n8n workflow JSON (`nodes` + `connections`). |
| Python generator | `generators/python_gen.py` | Plan → a script with one function per step and a `main()`. Config comes from environment variables. Dry-run by default. |
| Simulator | `flowforge/simulator.py` | Runs the plan against mock Email, Spreadsheet, Messaging, Storage and HTTP services. Network sockets are blocked in the test, so it provably touches nothing real. |
| API + UI | `api.py`, `ui.py` | FastAPI endpoints `/plan`, `/generate/flowise`, `/generate/n8n`, `/generate/python`, `/validate`, `/simulate`. Gradio dashboard at `/ui`, plus the website at `/`. |
| Evaluation | `flowforge/evaluate.py`, `eval/` | Runs fixed request sets and records plan validity, Flowise/n8n validity, Python parse, Python dry-run, simulation and repair. |

## 4. Objective → proof table (have this open during the viva)

| Report objective | Where to show it | Evidence |
|---|---|---|
| 1 Planner | Dashboard → Plan tab | `tests` `test_template_plan_*`, `test_llm_planner_against_fake_ollama`; LLM eval in `eval/results-heldout*-llm.md` |
| 2 Catalog (~20 nodes) | `docs/NODE_CATALOG.md` | 25 nodes, `test_catalog_has_25_nodes_with_mappings` |
| 3 Flowise generator | Flowise tab → import into Flowise | `docs/evidence/flowise-import-all.md` (52/52 accepted by Flowise 3.1.4), `flowise-runtime-demo.md` (flow **executed** in Flowise, every node `FINISHED`) |
| 4 n8n generator | n8n tab | `docs/evidence/n8n-check.md`: 52/52 imported into n8n 2.49.5 and exported unchanged; one executed |
| 5 Python generator | Python tab | Every eval script is run in a subprocess (`py_dryrun`) |
| 6 Validator | Checks tab | `test_validator_catches` (one case per error type) |
| 7 Self-correction | Tick "inject a fault" | `test_repair_*`, `test_loop_reports_failure_honestly`; `repair` metric |
| 8 Simulator | Dry-run tab | `test_simulator_never_touches_the_network` |
| 9 UI + API | `python -m flowforge serve` | `docs/evidence/api-smoke.md`, `gradio-check.md` |
| 10 Evaluation (≥15 requests) | `docs/EVALUATION.md` | 22 development + 15 + 15 held-out requests |
| 11 Documentation | `docs/` | This file, ARCHITECTURE, SETUP, USER_GUIDE, DEMO, SAFETY, EVALUATION |

## 5. Results to quote (and how to say them honestly)

- **Development set (22 requests), template planner:** every metric 22/22. *Say:* "This set was written
  while building the planner, so 100% shows it works, not that it generalises."
- **Held-out set 2 (15 new requests, never tuned on), template planner:** right steps for **11/15
  (73%)**. Every one of the 15 still produced valid, runnable outputs. *Say:* "When the rule-based planner
  fails, it builds the wrong automation, not a broken file."
- **LLM planner (`qwen3:4b`) on held-out sets:** right steps for **14/15** on each set. All validity
  metrics 15/15. Self-correction repaired real LLM mistakes during the run (e.g. H15 took 3 attempts).
  *Say:* "The LLM generalises better than the rules. The rules are the offline safety net."
- **External checks:** 52/52 flows accepted by real Flowise 3.1.4, 52/52 workflows by real n8n 2.49.5.
  *Say:* "We didn't only trust our own validator. We imported every output into the real tools."
- **A real bug the evaluation found:** n8n execution showed the Slack `text` was being sent as an object,
  which Slack rejects. It was fixed and a regression test was added. *Say:* "This is why we test against
  the real platforms."

## 6. Likely questions, with answers

**Q: Why is this "agentic" and not just a prompt?**
It plans, acts and observes. The validator's report is the observation. The agent revises its own plan
based on it, up to 3 times. A single prompt just returns whatever the model says, with no checking.

**Q: Why three outputs? Isn't one enough?**
Different users: Flowise users get a flow on the canvas, n8n users get an importable workflow, developers
get plain code they can version-control. Technically it also proves the plan is an abstract model of the
automation, not memorised syntax for one tool.

**Q: Why Flowise, if its repository is archived?**
It is a stable, released, Apache-2.0 platform (we pin v3.1.4). It runs locally and supports Ollama, so it
fits our free, local, private design. Archived means "no longer changing", which for a pinned base is
fine. The n8n and Python outputs keep the project useful without Flowise.

**Q: Can Flowise really do "when an email arrives"?**
No, and we say so. Flowise has no email-arrival trigger. We map it to a 5-minute schedule plus an Agent
with the Gmail tool that fetches the newest matching email. The validator prints this as a warning
(`W_FLOWISE_POLLING`). n8n has a real Gmail trigger, so the n8n output uses it.

**Q: Flowise has no Slack node. How do you post to Slack?**
Through Flowise's HTTP node, posting to a Slack incoming-webhook URL. The URL is read from a variable
(`SLACK_WEBHOOK_URL`) and is never written into the file.

**Q: What does the validator check?**
A trigger is present. Every edge points to an existing node. Required parameters exist. No unintended
cycles. No embedded credentials (it pattern-matches things like `sk-…` keys and literal Slack webhook
URLs). Unknown node types are rejected. Each problem has a code, e.g. `E_MISSING_PARAM` or
`E_DANGLING_EDGE`.

**Q: How do you know self-correction actually works?**
We inject faults on purpose: delete a required param, add an edge to a node that doesn't exist, paste a
secret, rename a node to an unknown type. The loop must repair them **and** end with the same steps and
the same dry-run behaviour as the clean plan. That is the `repair` metric. Demo: tick "inject a fault".

**Q: What happens if it can't fix the plan?**
It returns `ok: false` with the remaining errors and no outputs. It never shows a broken workflow as if it
worked (`test_loop_reports_failure_honestly`).

**Q: Why a local model and not GPT/Claude?**
No cost, no API key, no internet dependency, and the user's request stays on their machine. The cost is
speed: about 40–90 s per request on an 8 GB Mac.

**Q: Why keep a rule-based planner if you have an LLM?**
Three reasons: it works offline, it is instant, and it is deterministic, so evaluation runs are
reproducible.

**Q: Is it safe? Could it send real emails?**
The default path is simulation only. Generated Python scripts are dry-run unless live mode is explicitly
turned on, and real actions need approval. Credentials are never written into outputs or logs. See
`docs/SAFETY.md`.

**Q: What are the limitations?** (Examiners respect honest answers here.)
- The template planner only understands phrasings it has rules for (73% on fresh wording).
- Real Gmail, Drive, Sheets and Slack accounts were never called. Those paths are tested with mocks and
  dry runs only.
- Flowise has no event triggers, no delay node and no attachment-download action, so some steps are
  approximate there (reported as warnings, never hidden).
- Small evaluation sets: one request is about 6.7 percentage points.
- One model and one machine were tested.

**Q: What would you do next?**
Add more catalog nodes. Test against real accounts in a sandbox. Try larger local models. Add a
human-approval step in the UI before any live run.

**Q: What did *you* personally build?**
Each team member should prepare their own answer to this. Pick one module you can explain line by line
(for example the validator or the simulator) and open it during the viva.

## 7. Five-minute live demo script

```bash
cd ~/flowforge
.venv/bin/python -m pytest -q                 # "50 tests, all passing"
.venv/bin/python -m flowforge serve           # open http://127.0.0.1:8000
```

1. Type: *when a webhook receives an order, if the amount is over 100 post to slack otherwise add a row to the spreadsheet*. Click Generate.
2. **Plan tab:** point to the trigger, the condition with true/false branches, and `{{trigger.body.amount}}`.
3. **Flowise / n8n / Python tabs:** "same plan, three formats".
4. **Tick "inject a fault", Generate again:** Attempt 1 ❌ with error codes, Attempt 2 ✅. *"The agent fixed its own mistake."*
5. **Dry-run tab:** each step's inputs, outputs and status. The `false` branch shows as `skipped`.
6. *(If Flowise is running)* Download the Flowise JSON → Flowise → Add New → Import → it appears on the canvas.

### If the examiner gives you their own request

Use the **local server with Ollama running** (`ollama serve`, then `flowforge serve`), planner **Auto**.
Do **not** use the GitHub Pages site for this: it has no model, so it only runs the rule-based planner,
which understands just the phrasings it has rules for.

Measured on this Mac with requests the system had never seen (LLM planner, `qwen3:4b`):

| Request | Plan it built | Time |
|---|---|---|
| "every friday at 6pm send my manager an email with a summary of the sales sheet" | schedule → read sheet → summarise → email. Correct on attempt 1. | 79 s |
| "when someone fills the feedback form and the rating is below 3, create a jira ticket and text me" | form → **condition** → Jira → notify. Attempt 1 had an error, and **self-correction fixed it on attempt 2**. "Text me" became a generic notify, not SMS. | 100 s |

The rule-based planner got both of these wrong (it appended to the sheet instead of reading it, and it
dropped the condition). That is the honest reason the report calls the planner **LLM-first, with rules
as the offline fallback**.

What to say while it runs (about 1–2 minutes): *"It's running a 4-billion-parameter model locally on an
8 GB laptop, with no cloud and no API key. Watch the attempts: if the plan fails validation, the agent
revises it."*

If it gets something wrong, don't panic. Say so, show the validator output, and explain the limitation
(section 6). For example, "text me" became notify instead of SMS: the plan is valid, but the channel is
wrong. An honest answer scores better than pretending.

What it **cannot** do, by design (report §4.2): services outside the 25-node catalog. "Book a cab"
becomes a generic AI-agent step, because there is no cab node. Any service with a web API can still be
reached through the `http_request` node. Adding a new service means adding one catalog entry plus its
Flowise and n8n mapping.

Backup if anything breaks live: the website works with no server at
https://geetichahal.github.io/flowforge/, and every step has recorded evidence in `docs/evidence/`.

**Before the demo:** close Chrome tabs and other heavy apps. 8 GB RAM is tight with Flowise and Ollama running together. Use planner mode `template` for the prepared example (instant), and **Auto (LLM)** for any request the examiner makes up.
