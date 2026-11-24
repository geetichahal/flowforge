# Safety model

FlowForge generates automations that can send email, post messages, write spreadsheets and call APIs.
Its safety rules are about **not taking real actions by accident** and **not leaking secrets**.

## 1. Simulation by default; no real accounts in the simulator

- `simulate()` uses in-memory mock connectors only:
  - `MockEmail`
  - `MockSpreadsheet`
  - `MockMessaging` (Slack, Discord, Teams, SMS, notifications)
  - `MockStorage`
  - `MockHTTP`
- It never opens a network connection. `test_simulator_never_touches_the_network` replaces
  `socket.connect` and `socket.create_connection` with a function that fails the test, then simulates a plan
  that uses email, Drive, Sheets and Slack.
- The dashboard, API `/simulate`, CLI `simulate` and the evaluation all use this simulator.

## 2. Generated Python scripts: dry run and human approval

- `FLOWFORGE_DRY_RUN` defaults to `1`. A dry run prints `[dry-run] would …` for each external action,
  returns sample data, and sends nothing.
- In live mode (`FLOWFORGE_DRY_RUN=0`), **every** external side effect goes through
  `approve("…")`, which asks `Approve: …? [y/N]`. Anything but `y`/`yes` skips the action.
  Skipping the prompts requires explicitly setting `FLOWFORGE_AUTO_APPROVE=1`.
- `test_python_live_run_hits_real_http_and_respects_approval` shows two things against a real local HTTP
  server: a denied approval sends nothing, and an approved one sends the real request.
- A failing step stops the run (`StepFailed`), so later steps never act on missing data.
  `test_python_step_failure_is_reported_and_stops_the_run` covers this.

## 3. No embedded credentials

- **Plan rule:** secret parameters (webhook URLs, tokens) must be `{{env.NAME}}` references. A literal value
  is `E_LITERAL_SECRET`.
- **Pattern scan:** the plan and all three outputs are scanned for credential-shaped strings:
  - Slack, Discord and Teams webhook URLs
  - Slack tokens, `sk-…` keys, AWS access keys, GitHub tokens, Google API keys and OAuth tokens
  - bearer tokens, private-key blocks, `password=` / `api_key=` / `secret=` assignments
  Any match is `E_EMBEDDED_CREDENTIAL` (shown working over the real API in `docs/evidence/api-smoke.md`).
- **Flowise:** no credential ids are emitted (`E_FW_CREDENTIAL` if present). Credentials are attached in
  Flowise after import.
- **n8n:** no `credentials` blocks are emitted (`E_N8N_CREDENTIAL` if present). `{{env.X}}` becomes `$env.X`.
- **Python:** configuration comes from `os.environ` via `env()`. In live mode a missing variable is a
  reported `ConfigError`, never a hard-coded fallback.
- **Self-correction:** the repairer replaces a pasted secret with an `{{env.…}}` reference and reports
  "redacted an embedded credential" in the history.
- **Repository:** `~/.flowforge/config.json` (Flowise URL and API key) lives outside the repository. The
  fault injector builds its fake Slack URL at runtime, so the source holds no webhook-shaped literal.

## 4. Writing to real Flowise / n8n is opt-in

- `/flowise/push` and `/flowise/deploy` require `"confirm": true` in the request body. The dashboard and
  CLI only push on an explicit button or command.
- **Public server guard:** a request arriving through the public tunnel (`Cf-Connecting-Ip` header) is
  treated as public. Pushes to private, loopback and link-local addresses are then refused, so visitors
  cannot use the server to reach the local network. Tests: `test_public_server_blocks_private_push_targets`
  and `test_tunneled_requests_get_the_public_guard`.
- The evidence scripts touch only:
  - the local Flowise named by `FLOWISE_URL` (the import-all check deletes every flow it creates);
  - a temporary n8n data folder;
  - local receivers on 127.0.0.1.

## 5. Generated code safety

- Generated Python uses only the standard library and `requests` (`E_PY_DEPENDENCY` otherwise).
- The AI-agent calculator tool evaluates arithmetic through an AST whitelist (numbers and `+ - * / ** % //`).
  It never uses `eval`. Test: `test_python_agent_calculator_is_safe`.
- **Flowise message bodies** are built with `JSON.stringify` in a Custom Function, so quotes or objects in
  user data cannot break the JSON (`test_flowise_message_bodies_are_built_safely`).
- **Python and n8n message fields:** when a value is structured data, it is sent as JSON text rather than an
  object that Slack/Discord/Teams would reject (`test_python_messages_send_structured_data_as_text`, and the
  n8n execution check).

## 6. Known limits of this model

- The approval gate protects the Python script only. Workflows imported into Flowise or n8n run under
  those platforms' own permission models once credentials are attached.
- The secret scanner is pattern-based. A secret in an unusual format may not be detected.
- The local Flowise used for the runtime demo ran with `HTTP_SECURITY_CHECK=false` so it could post to a
  127.0.0.1 receiver. Do not use that setting on a shared or internet-facing Flowise.
- Generated webhook/form listeners (`--serve`) bind to 127.0.0.1 and have no authentication. Put them
  behind your own authentication before exposing them.
