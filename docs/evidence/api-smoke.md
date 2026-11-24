# API smoke test (real server over HTTP)

Run 2026-11-24 22:20 · `python -m flowforge serve --port 8011` · request: “when someone submits the contact form, email them a thank-you email and add a row to the spreadsheet”

| Endpoint | HTTP | Result | Detail |
|---|---|---|---|
| `POST /plan` | 200 | PASS | 3 nodes: form_submitted, send_email, sheets_append |
| `POST /generate/flowise` | 200 | PASS | keys: ['export', 'warnings', 'post_import'] |
| `POST /generate/n8n` | 200 | PASS | 3 n8n nodes: n8n-nodes-base.formTrigger, n8n-nodes-base.gmail, n8n-nodes-base.googleSheets |
| `POST /generate/python` | 200 | PASS | 343 lines, compiles: True |
| `POST /validate` | 200 | PASS | ok=True, errors=0, warnings=0 |
| `POST /validate (embedded secret)` | 200 | PASS | ok=False, error codes=['E_EMBEDDED_CREDENTIAL'] |
| `POST /simulate` | 200 | PASS | trigger:ok → send_email:ok → append_row:ok |
| `GET /ui/ (Gradio dashboard)` | 200 | PASS | 38327 bytes of HTML |

**All endpoints passed.**
