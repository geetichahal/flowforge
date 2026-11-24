# FlowForge evaluation (template planner, `requests-heldout.json`)

15 requests · run 2026-11-24 22:07 · 1.8s

| Metric | Passed | Rate |
|---|---|---|
| intent | 5/15 | 33% |
| plan | 15/15 | 100% |
| flowise | 15/15 | 100% |
| n8n | 15/15 | 100% |
| py_parse | 15/15 | 100% |
| py_dryrun | 15/15 | 100% |
| simulate | 15/15 | 100% |
| repair | 15/15 | 100% |

| ID | Pattern | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair | Attempts | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| H01 | form + transform + spreadsheet | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['form_submitted', 'sheets_append']; missing ['transform'] |
| H02 | schedule + http + email | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['manual', 'http_request']; missing ['send_email'] |
| H03 | webhook + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H04 | email + drive | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['manual', 'drive_save']; missing ['extract_attachments'] |
| H05 | form + condition + email/slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H06 | schedule + sheets + discord | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H07 | webhook + delay + email | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['webhook', 'ai_agent']; missing ['delay', 'send_email'] |
| H08 | email + summarize + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H09 | form + sms | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | trigger ['manual'] |
| H10 | schedule + http + drive | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['schedule', 'drive_save']; missing ['http_request'] |
| H11 | webhook + condition + http | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['webhook', 'condition', 'notify']; missing ['http_request'] |
| H12 | email + teams + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| H13 | form + email + calendar | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | trigger ['manual'] |
| H14 | schedule + notify | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['schedule', 'ai_agent']; missing ['notify'] |
| H15 | webhook + transform + http | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['webhook', 'send_email']; missing ['transform', 'http_request'] |

Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one.
