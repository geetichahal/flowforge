# FlowForge evaluation (llm planner, `requests-heldout.json`)

15 requests · run 2026-11-25 00:19 · 5822.7s

First-attempt planner per request: llm:qwen3:4b ×15

| Metric | Passed | Rate |
|---|---|---|
| intent | 14/15 | 93% |
| plan | 15/15 | 100% |
| flowise | 15/15 | 100% |
| n8n | 15/15 | 100% |
| py_parse | 15/15 | 100% |
| py_dryrun | 15/15 | 100% |
| simulate | 15/15 | 100% |
| repair | 15/15 | 100% |

| ID | Pattern | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair | Attempts | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H01 | form + transform + spreadsheet | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| H02 | schedule + http + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H03 | webhook + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H04 | email + drive | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H05 | form + condition + email/slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H06 | schedule + sheets + discord | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H07 | webhook + delay + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | Flowise skips ['wait_2_days'] (no equivalent node) |
| H08 | email + summarize + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H09 | form + sms | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['form_submitted', 'notify']; missing ['sms_message'] |
| H10 | schedule + http + drive | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H11 | webhook + condition + http | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H12 | email + teams + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H13 | form + email + calendar | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H14 | schedule + notify | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H15 | webhook + transform + http | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 3 |  |

Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one.
