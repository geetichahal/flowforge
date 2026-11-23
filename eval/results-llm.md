# FlowForge evaluation (llm planner)

18 requests · run 2026-11-24 03:24 · 1101.2s

| Metric | Passed | Rate |
|---|---|---|
| intent | 18/18 | 100% |
| plan | 18/18 | 100% |
| flowise | 18/18 | 100% |
| n8n | 18/18 | 100% |
| py_parse | 18/18 | 100% |
| py_dryrun | 18/18 | 100% |
| simulate | 18/18 | 100% |
| repair | 17/18 | 94% |

| ID | Pattern | intent | plan | flowise | n8n | py_parse | py_dryrun | simulate | repair | Attempts | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| R01 | email + storage + messaging | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R02 | form + email reply | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R03 | form + spreadsheet + task | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R04 | scheduled report | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R05 | webhook + branch | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| R06 | scheduled API call + messaging | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R07 | email + condition on subject | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | 1 | repair produced a valid plan but changed its steps |
| R08 | email logging to spreadsheet | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R09 | email summary to Teams | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R10 | webhook + calendar | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| R11 | multi-step chain with delay | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | Flowise skips ['wait_10_minutes'] (no equivalent node) |
| R12 | manual + storage listing | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R13 | hourly monitoring | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 | Flowise skips ['log_message'] (no equivalent node) |
| R14 | webhook forwarding | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R15 | form + branch on field | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 2 |  |
| R16 | email attachments to storage folder | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R17 | scheduled storage of API data | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |
| R18 | manual chat notify | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | 1 |  |

Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one.
