# Evaluation report

All numbers below were measured on 2026-11-24/25 on an 8 GB Apple-silicon MacBook (Python 3.14.4). Raw
per-request results are in `eval/results-*.md` and `eval/results-*.json`. Nothing here was estimated:
each figure comes from `python -m flowforge eval` or one of the `tools/` scripts, and failures are
listed as recorded.

## 1. Datasets

| Set | File | Requests | How it was written | Status |
|---|---|---|---|---|
| Development | `eval/requests.json` | 22 (R01–R22) | Alongside the template planner, during development | Tuned: measures correctness on known phrasings, not generalisation |
| Held-out 1 | `eval/requests-heldout.json` | 15 (H01–H15) | New wording, written before running anything against it | Was used **after** its baseline run to fix the template planner. Baseline kept in `results-heldout-template-baseline.*` |
| Held-out 2 | `eval/requests-heldout2.json` | 15 (G01–G15) | Fresh wording, written after those fixes | **Untuned**: run once, never used to change the planner |

Each request lists its expected trigger and expected node types (e.g. `form_submitted` +
`transform`, `sheets_append`). The requests cover email, form, webhook, schedule and manual triggers;
spreadsheets, cloud storage, email, Slack/Discord/Teams/SMS messaging, HTTP, conditions, transforms,
delays, calendar, tasks, LLM summaries and AI agents.

## 2. Metrics (defined in `flowforge/evaluate.py`)

Each metric is pass/fail per request.

| Metric | Passes when |
|---|---|
| `intent` | The plan's trigger is the expected one **and** every expected node type is present (did it build what was asked?) |
| `plan` | The plan passes the plan validator within at most 3 attempts (self-correction included) |
| `flowise` | The Flowise export passes `validate_flowise` |
| `n8n` | The n8n workflow passes `validate_n8n` |
| `py_parse` | The Python script parses and has one function per step plus `main()` (`validate_python`) |
| `py_dryrun` | The script **runs to completion** in a subprocess in dry-run mode (exit code 0) |
| `simulate` | The mock-connector simulation completes without an error |
| `repair` | Re-running with injected faults (missing required param, dangling edge, literal Slack secret, unknown node type) yields a valid plan within 3 attempts **with the same steps and the same dry-run trace** as the clean run |

`intent` is the only metric that checks meaning. All the others check that whatever was planned is
well-formed and runs. A plan can be valid and still be the wrong automation, which is why `intent` is
reported separately.

## 3. Results

### 3.1 Template (rule-based) planner

| Set | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair |
|---|---|---|---|---|---|---|---|---|
| Development (22) | 22/22 | 22/22 | 22/22 | 22/22 | 22/22 | 22/22 | 22/22 | 22/22 |
| Held-out 1, **baseline** before any fixes (15) | **5/15** | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 |
| Held-out 1 after fixes (15; no longer held-out) | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 |
| **Held-out 2, untuned (15)** | **11/15** | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 |

The template planner runs in about 0.1 s per request.

**Held-out 1 baseline failures (5/15).** The planner did not recognise:
- trigger wording: "each weekday", "completes the … form", "using the booking form", "attachments I receive by email";
- action verbs: "convert / reformat" (transform), "download / POST … to URL / forward it to URL" (HTTP), "wait 2 days", "remind me".

These became rules. The `transform` node also gained an optional `case` (upper/lower) parameter, implemented in
the simulator and all three generators. Regression tests were added: `test_planner_phrasings_from_heldout_set`
and `test_transform_case_in_all_outputs`.

**Held-out 2 failures (11/15), after those fixes:**

| ID | Request (abridged) | Got | Missing |
|---|---|---|---|
| G01 | "email … whose subject **mentions** refund, let the support team know on Slack" | email → slack | `condition` ("whose … mentions" is not an `if` clause) |
| G03 | "Announce every new volunteer registration from our sign-up form in the Discord server" | manual trigger | form trigger ("registration from our … form" not recognised) |
| G06 | "Turn each email from support@example.com into a Jira ticket" | manual trigger | email trigger ("each email from" not recognised) |
| G07 | "When the order form is submitted **and** the amount exceeds 500, send me an SMS, otherwise …" | form → sms, email | `condition` ("and the amount exceeds" is not an `if` clause) |

**Conclusion:** the rule-based planner is reliable only for phrasings it has rules for. On fresh
wording it chose the right steps for 73% of requests. Every plan it produced was still structurally
valid and ran: the failures were wrong automations, not broken artefacts.

### 3.2 LLM planner (Ollama `qwen3:4b`, temperature 0)

Each row's planner source is recorded. All 30 held-out requests were planned by the LLM; none fell back
to the template planner.

| Set | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair | Run time |
|---|---|---|---|---|---|---|---|---|---|
| Held-out 1 (15) | **14/15** | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | ≈ 20 min of compute (the file records 5,823 s of wall time because the Mac slept for 77 min on a flat battery mid-run) |
| Held-out 2, first run (15) | **14/15** | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | **14/15** | 15/15 | 1,355 s |
| **Held-out 2, re-run after simulator fix (15)** | **14/15** | 15/15 | 15/15 | 15/15 | 15/15 | 15/15 | **15/15** | 15/15 | 1,908 s |

Each request is run twice (clean, and with injected faults for `repair`), so planning one request takes
roughly 40–90 s.

**Failures:**

| ID | Request (abridged) | What happened |
|---|---|---|
| H09 | "Text me whenever somebody books an appointment using the booking form" | Built form → `notify` instead of `sms_message`. A valid plan, but the wrong channel. |
| G15 | "If an incoming email is from boss@example.com, save its contents to Google Drive" | Put the sender filter into the Gmail query (`from:boss@example.com`) instead of a separate `condition` node, in both runs (checked by re-planning). This arguably still does what was asked, but it does not match the expected node list, so it counts as a failure. |
| G10 (first run) | "When a receipt email with an attachment arrives, upload the file to Google Drive and log it in a spreadsheet" | The plan was correct and passed every validator and the Python dry run, but the **simulator** failed: its mock inbox held one invoice email, and the trigger query `subject:receipt` matched nothing. This was a test-harness limitation, fixed by letting the simulator create a test email matching the trigger's query (and noting this in the trace); see `test_simulated_email_trigger_matches_any_query`. The first run's results are kept unchanged in `results-heldout2-llm-prefix.*`. |

**Self-correction with the real LLM.** Some LLM plans failed validation and were repaired inside the loop:
- held-out 1: H01 took 2 attempts and H15 took 3;
- held-out 2: G04, G07, G12 and G14 each took 2 attempts, in both the first run and the re-run.

All ended valid. The `repair` metric, with faults injected on purpose, passed 15/15 on both sets.

**Historical, development set:** `eval/results-llm.md` (2026-11-24 03:24) ran the LLM planner on the 18
development requests that existed then, using an earlier code version: intent 18/18, every validity metric
18/18, repair 17/18. That run did not yet record per-request planner source, so it cannot rule out template
fallbacks. It is kept for reference only. The development set was not re-run with the LLM in this pass.

### 3.3 External compatibility checks (not part of `eval`, recorded in `docs/evidence/`)

| Check | Result |
|---|---|
| Flowise 3.1.4: all 52 evaluation flows created via `POST /api/v1/chatflows`, read back, compared, deleted (`flowise-import-all.md`) | 52/52 identical on read-back |
| Flowise 3.1.4 runtime: webhook → condition → HTTP flow executed via Flowise's webhook endpoint, both branches (`flowise-runtime-demo.md`) | PASS: every node `FINISHED`, correct payload at each endpoint |
| n8n 2.49.5: all 52 workflows against n8n's node registry: type, typeVersion, parameter names (`n8n-check.md`) | 52/52 |
| n8n 2.49.5: `n8n import:workflow` + `export:workflow` round trip | 52/52 unchanged |
| n8n 2.49.5: `n8n execute` of a generated HTTP → Slack workflow against a local receiver | PASS. This run found a real bug: the Slack `text` was an object, which Slack rejects. It is fixed in the n8n and Python generators and covered by `test_python_messages_send_structured_data_as_text`. |
| FastAPI over HTTP: `/plan`, `/generate/*`, `/validate` (incl. secret detection), `/simulate`, `/ui` (`api-smoke.md`) | All PASS |
| Gradio dashboard via `gradio_client`: Generate with fault injection, every panel populated, 4 downloads (`gradio-check.md`) | PASS |

## 4. Threats to validity

- **Small sets.** 15 requests per held-out set gives coarse percentages: one request is 6.7 points.
- **Single author.** The same person wrote the requests and the expected node lists. G15 shows that the
  "expected" structure can be debatable.
- **Validity metrics use FlowForge's own validators.** External import checks (section 3.3) were done
  separately to address this. Real credentialed services (Gmail, Sheets, Drive, Slack, Jira, Twilio) were
  never called, so their behaviour is covered only by mocks and dry runs.
- **One LLM, one machine.** Results are for `qwen3:4b` at temperature 0 on one Mac. Other models or
  settings may differ.
- **Development set.** Its 100% results reflect tuning, not generalisation. Held-out 2 is the fairest
  estimate for the template planner.

## 5. Reproduce

```bash
.venv/bin/python -m flowforge eval --mode template
.venv/bin/python -m flowforge eval --mode template --requests eval/requests-heldout.json
.venv/bin/python -m flowforge eval --mode template --requests eval/requests-heldout2.json
.venv/bin/python -m flowforge eval --mode llm --requests eval/requests-heldout2.json     # needs Ollama + qwen3:4b
```
