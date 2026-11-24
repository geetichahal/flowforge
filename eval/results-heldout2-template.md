# FlowForge evaluation (template planner, `requests-heldout2.json`)

15 requests · run 2026-11-24 22:51 · 3.0s

First-attempt planner per request: template ×15

| Metric | Passed | Rate |
|---|---|---|
| intent | 11/15 | 73% |
| plan | 15/15 | 100% |
| flowise | 15/15 | 100% |
| n8n | 15/15 | 100% |
| py_parse | 15/15 | 100% |
| py_dryrun | 15/15 | 100% |
| simulate | 15/15 | 100% |
| repair | 15/15 | 100% |

| ID | Pattern | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair | Attempts | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| G01 | email + condition + slack | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['email_received', 'slack_message']; missing ['condition'] |
| G02 | schedule + http + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G03 | form + discord | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | trigger ['manual'] |
| G04 | webhook + sheets + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G05 | schedule + drive list + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G06 | email + task | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | trigger ['manual'] |
| G07 | form + condition + sms/email | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | got ['form_submitted', 'sms_message', 'send_email']; missing ['condition'] |
| G08 | webhook + summarize + teams | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G09 | schedule + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G10 | email + attachments + drive + sheets | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G11 | webhook + delay + slack | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | Flowise skips ['wait'] (no equivalent node) |
| G12 | form + transform + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G13 | schedule + http + summarize + email | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G14 | webhook + calendar | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| G15 | email + condition + drive | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |

Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one.
