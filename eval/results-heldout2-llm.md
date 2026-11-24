# FlowForge evaluation (llm planner, `requests-heldout2.json`)

15 requests · run 2026-11-25 00:51 · 1907.8s

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
| G01 | email + condition + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G02 | schedule + http + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G03 | form + discord | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G04 | webhook + sheets + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| G05 | schedule + drive list + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G06 | email + task | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G07 | form + condition + sms/email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| G08 | webhook + summarize + teams | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G09 | schedule + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G10 | email + attachments + drive + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G11 | webhook + delay + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | Flowise skips ['wait_5_minutes'] (no equivalent node) |
| G12 | form + transform + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| G13 | schedule + http + summarize + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G14 | webhook + calendar | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| G15 | email + condition + drive | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['email_received', 'drive_save']; missing ['condition'] |

Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one.
