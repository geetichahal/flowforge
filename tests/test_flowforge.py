import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from flowforge import catalog, pipeline, planner
from flowforge.generators import flowise, n8n, python_gen
from flowforge.generators.flowise import specs
from flowforge.plan import normalize, refs_in
from flowforge.simulator import simulate
from flowforge.validator import validate_flowise, validate_n8n, validate_plan, validate_python

INVOICE = "when I get an email with an invoice attached, save it to Drive and ping me on Slack"
BRANCH = "when a webhook receives an order, if the amount is over 100 post to slack otherwise add a row to the spreadsheet"


def good_plan():
    return normalize(planner.template_plan(INVOICE))


# ------------------------------------------------------------------ catalog
def test_catalog_has_25_nodes_with_mappings():
    assert len(catalog.CATALOG) == 25
    for n in catalog.NODES:
        assert n.flowise in ("direct", "approximate", "none")
        assert n.type in python_gen.BODIES, f"python generator lacks {n.type}"


def test_flowise_specs_are_from_real_flowise():
    s = specs()
    assert s["startAgentflow"]["version"] == 1.4
    assert s["toolAgentflow"]["version"] == 1.2
    assert {"gmail", "googleDriveTool", "googleSheetsTool"} <= set(s)


# ------------------------------------------------------------------ planner
def test_template_plan_invoice_matches_report_example():
    p = good_plan()
    assert [n["type"] for n in p["nodes"]] == ["email_received", "extract_attachments", "drive_save", "slack_message"]
    assert validate_plan(p).ok
    assert p["nodes"][-1]["params"]["webhook_url"] == "{{env.SLACK_WEBHOOK_URL}}"


def test_template_plan_branches():
    p = normalize(planner.template_plan(BRANCH))
    cond = next(n for n in p["nodes"] if n["type"] == "condition")
    assert cond["params"]["operator"] == "greater_than" and cond["params"]["value"] == "100"
    branches = {e["branch"] for e in p["edges"] if e["from"] == cond["id"]}
    assert branches == {"true", "false"}


@pytest.mark.parametrize("text,cron", [
    ("every monday at 9am", "0 9 * * 1"), ("daily at 6:30 pm", "30 18 * * *"),
    ("every 15 minutes", "*/15 * * * *"), ("every hour", "0 * * * *"),
])
def test_schedule_parsing(text, cron):
    assert planner.parse_schedule(text) == cron


# ------------------------------------------------------------------ validator
@pytest.mark.parametrize("mutate,code", [
    (lambda p: p["nodes"].pop(0), "E_NO_TRIGGER"),
    (lambda p: p["edges"].append({"from": "save_to_drive", "to": "nowhere"}), "E_DANGLING_EDGE"),
    (lambda p: p["nodes"][2]["params"].pop("file_name"), "E_MISSING_PARAM"),
    (lambda p: p["edges"].append({"from": "notify_slack", "to": "extract_attachments"}), "E_CYCLE"),
    (lambda p: p["nodes"][3]["params"].update(webhook_url="/".join(["https://hooks.slack.com/services", "T0" + "FAKE", "B0" + "FAKE", "y" * 20])), "E_LITERAL_SECRET"),
    (lambda p: p["nodes"][3]["params"].update(text="key sk-abcdefghijklmnopqrstuvwxyz123456"), "E_EMBEDDED_CREDENTIAL"),
    (lambda p: p["nodes"][3]["params"].update(text="{{trigger.nonexistent}}"), "E_BAD_REF_FIELD"),
    (lambda p: p["nodes"][1]["params"].update(source="{{notify_slack.ok}}"), "E_REF_NOT_UPSTREAM"),
    (lambda p: p["nodes"][1].update(type="gmail_magic"), "E_UNKNOWN_TYPE"),
])
def test_validator_catches(mutate, code):
    p = good_plan()
    mutate(p)
    assert code in {i.code for i in validate_plan(p).errors}


def test_repair_fixes_injected_faults():
    broken, faults = planner.inject_fault(good_plan())
    assert len(faults) >= 3
    assert not validate_plan(normalize(broken)).ok
    r = pipeline.run(INVOICE, mode="template", plan_override=broken)
    assert r["ok"], r["report_text"]
    assert 2 <= len(r["attempts"]) <= 3
    assert "hooks.slack.com" not in json.dumps(r["plan"])


def test_loop_reports_failure_honestly():
    hopeless = {"nodes": [{"id": "x", "type": "teleport"}], "edges": []}
    r = pipeline.run("nonsense", mode="template", plan_override=hopeless, max_attempts=1)
    assert not r["ok"] and r["flowise"] is None and r["report"]["errors"] > 0


# ------------------------------------------------------------------ generators
def test_flowise_export_shape_and_conventions():
    out = flowise.generate(good_plan())
    exp = out["export"]
    assert set(exp) == {"AgentFlowV2"} and exp["AgentFlowV2"][0]["type"] == "AGENTFLOW"
    assert isinstance(exp["AgentFlowV2"][0]["flowData"], str)
    assert validate_flowise(exp).ok
    flow = json.loads(exp["AgentFlowV2"][0]["flowData"])
    names = [n["data"]["name"] for n in flow["nodes"]]
    assert names[0] == "startAgentflow" and "slack" not in json.dumps(names).lower()
    assert any(n["data"]["name"] == "httpAgentflow" for n in flow["nodes"])  # Slack goes through HTTP
    assert "FLOWISE_CREDENTIAL_ID" not in exp["AgentFlowV2"][0]["flowData"]
    assert "{{ $vars.SLACK_WEBHOOK_URL }}" in exp["AgentFlowV2"][0]["flowData"]


def test_flowise_condition_uses_two_output_anchors():
    flow = flowise.flow_data(flowise.generate(normalize(planner.template_plan(BRANCH)))["export"])
    handles = {e["sourceHandle"] for e in flow["edges"] if e["source"].startswith("conditionAgentflow")}
    assert handles == {"conditionAgentflow_0-output-0", "conditionAgentflow_0-output-1"}


def test_flowise_unsupported_step_is_reported_not_dropped():
    p = normalize(planner.template_plan("when someone submits the signup form, email them a thank-you email, wait 10 minutes, then post to slack"))
    out = flowise.generate(p)
    assert any(w["code"] == "W_FLOWISE_UNSUPPORTED" for w in out["warnings"])
    assert validate_flowise(out["export"]).ok


def test_n8n_workflow_valid_and_branches():
    wf = n8n.generate(normalize(planner.template_plan(BRANCH)))["workflow"]
    assert validate_n8n(wf).ok
    if_node = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.if")
    assert len(wf["connections"][if_node["name"]]["main"]) == 2
    assert all("credentials" not in n for n in wf["nodes"])


def test_python_script_parses_and_dry_runs(tmp_path):
    code = python_gen.generate(good_plan())["code"]
    assert validate_python(code, good_plan()).ok
    f = tmp_path / "a.py"
    f.write_text(code)
    env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWFORGE_")}
    r = subprocess.run([sys.executable, "-I", str(f)], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert "DRY RUN" in r.stdout and "[dry-run] would upload" in r.stdout


def test_python_live_run_hits_real_http_and_respects_approval(tmp_path):
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}/hook"
    plan = normalize(planner.template_plan(f"when a webhook receives a signup, send it to {url}"))
    f = tmp_path / "a.py"
    f.write_text(python_gen.generate(plan)["code"])
    (tmp_path / "payload.json").write_text(json.dumps({"body": {"email": "ada@example.com"}, "headers": {}, "query": {}}))
    base = {k: v for k, v in os.environ.items() if not k.startswith("FLOWFORGE_")}
    # live, but approval denied -> nothing sent
    r = subprocess.run([sys.executable, "-I", str(f), "--payload", "payload.json"], input="n\n", capture_output=True,
                       text=True, cwd=tmp_path, env={**base, "FLOWFORGE_DRY_RUN": "0"}, timeout=60)
    assert r.returncode == 0, r.stderr
    assert received == []
    # live and approved -> request really sent with the forwarded payload
    r = subprocess.run([sys.executable, "-I", str(f), "--payload", "payload.json"], capture_output=True, text=True,
                       cwd=tmp_path, env={**base, "FLOWFORGE_DRY_RUN": "0", "FLOWFORGE_AUTO_APPROVE": "1"}, timeout=60)
    srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert received == [{"email": "ada@example.com"}]


# ------------------------------------------------------------------ simulator
def test_simulation_trace_and_world_state():
    res = simulate(good_plan())
    assert res["ok"] and [s["status"] for s in res["trace"]] == ["ok"] * 4
    assert res["world"]["storage_files"]["drv-2"]["name"] == "INV-2041.pdf"
    assert "INV-2041" in res["world"]["messages_posted"][0]["text"]


def test_simulation_takes_false_branch():
    p = normalize(planner.template_plan(BRANCH))
    res = simulate(p, trigger_payload={"body": {"amount": 20}, "headers": {}, "query": {}})
    status = {s["node"]: s["status"] for s in res["trace"]}
    assert status["notify_slack"] == "skipped" and status["append_row"] == "ok"


# ------------------------------------------------------------------ API
def test_api_endpoints():
    from fastapi.testclient import TestClient
    from flowforge.api import create_app
    c = TestClient(create_app())
    plan = c.post("/plan", json={"request": INVOICE, "mode": "template"}).json()["plan"]
    for ep in ("/generate/flowise", "/generate/n8n", "/generate/python", "/validate", "/simulate"):
        r = c.post(ep, json={"plan": plan})
        assert r.status_code == 200, (ep, r.text)
    assert c.post("/validate", json={"plan": plan}).json()["ok"]
    assert c.post("/flowise/push", json={"export": {}, "confirm": False}).status_code == 400
    bad = c.post("/generate/n8n", json={"plan": {"nodes": [], "edges": []}})
    assert bad.status_code == 422


# ------------------------------------------------------------------ LLM path (fake Ollama server)
def test_llm_planner_against_fake_ollama(monkeypatch):
    """First answer has a bad node type; the LLM's revision (fed the validator errors) fixes it."""
    calls = []
    first = {"name": "Form to Slack", "nodes": [
        {"id": "trigger", "type": "form_submitted", "params": {"title": "Contact", "fields": "name,email"}},
        {"id": "post", "type": "slack", "params": {"text": "New lead {{trigger.name}}"}}],
        "edges": [{"from": "trigger", "to": "post"}]}
    fixed = json.loads(json.dumps(first))
    fixed["nodes"][1]["type"] = "slack_message"

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "qwen3:4b"}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append(body)
            plan = first if len(calls) == 1 else fixed
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"message": {"content": "<think>x</think>" + json.dumps(plan)}}).encode())

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("OLLAMA_HOST", f"http://127.0.0.1:{srv.server_port}")
    monkeypatch.setenv("FLOWFORGE_MODEL", "qwen3:4b")
    r = pipeline.run("when the contact form is submitted, post the lead to slack", mode="llm")
    srv.shutdown()
    assert r["ok"], r["report_text"]
    assert [a["source"] for a in r["attempts"]] == ["llm:qwen3:4b", "llm:qwen3:4b"]
    assert calls[0]["format"] == "json" and calls[0]["model"] == "qwen3:4b"
    assert "E_UNKNOWN_TYPE" in calls[1]["messages"][1]["content"]  # errors were fed back


def test_repair_keeps_the_same_steps():
    """Regression: an unknown node type used to be deleted, giving a 'valid' plan missing a branch."""
    clean = normalize(planner.template_plan(BRANCH))
    r = pipeline.run(BRANCH, mode="template", inject_fault=True)
    assert r["ok"]
    assert sorted(n["type"] for n in r["plan"]["nodes"]) == sorted(n["type"] for n in clean["nodes"])


def test_repair_restores_missing_params_from_the_request():
    """Regression: a removed 'greater_than' was refilled with the default 'contains', flipping the branch."""
    r = pipeline.run(BRANCH, mode="template", inject_fault=True)
    cond = next(n for n in r["plan"]["nodes"] if n["type"] == "condition")
    assert cond["params"]["operator"] == "greater_than"
    taken = {s["node"]: s["status"] for s in r["simulation"]["trace"]}
    assert taken["notify_slack"] == "ok"  # sample amount 250 > 100


# ------------------------------------------------------------------ public server (Hugging Face Space)
def test_public_server_blocks_private_push_targets(monkeypatch):
    from fastapi.testclient import TestClient
    import flowforge.api as api_mod
    monkeypatch.setattr(api_mod, "PUBLIC", True)
    c = TestClient(api_mod.create_app())
    export = flowise.generate(good_plan())["export"]
    for target in ("http://localhost:3000", "http://127.0.0.1:3000", "http://169.254.169.254", "http://10.0.0.5:3000", "file:///etc/passwd"):
        r = c.post("/flowise/push-to", json={"export": export, "confirm": True, "url": target})
        assert r.status_code == 400, target
    assert c.post("/flowise/push", json={"export": export, "confirm": True}).status_code == 403
    assert c.get("/health").json()["public"] is True


def test_cors_allows_the_pages_site_only():
    from fastapi.testclient import TestClient
    from flowforge.api import create_app
    c = TestClient(create_app())
    ok = c.options("/run", headers={"Origin": "https://geetichahal.github.io", "Access-Control-Request-Method": "POST",
                                    "Access-Control-Request-Headers": "content-type"})
    assert ok.headers.get("access-control-allow-origin") == "https://geetichahal.github.io"
    bad = c.options("/run", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


def test_tunneled_requests_get_the_public_guard():
    from fastapi.testclient import TestClient
    from flowforge.api import create_app
    c = TestClient(create_app())
    export = flowise.generate(good_plan())["export"]
    via_tunnel = c.post("/flowise/push-to", headers={"Cf-Connecting-Ip": "203.0.113.9"},
                        json={"export": export, "confirm": True, "url": "http://127.0.0.1:3000"})
    assert via_tunnel.status_code == 400
    assert c.get("/health", headers={"Cf-Connecting-Ip": "203.0.113.9"}).json()["public"] is True
    assert c.get("/health").json()["public"] is False  # same machine: local Flowise pushes allowed


# ------------------------------------------------------------------ AI agents and one-click deploy
AGENT_REQ = "a study assistant that helps engineering students with calculations, deadlines and reading web articles"


def test_open_ended_requests_become_agents_with_matching_tools():
    p = normalize(planner.template_plan(AGENT_REQ))
    assert [n["type"] for n in p["nodes"]] == ["manual", "ai_agent"]
    assert p["nodes"][1]["params"]["tools"] == "calculator,datetime,web"
    p = normalize(planner.template_plan("what is the EMI on a 5 lakh loan at 9% for 3 years"))
    assert p["nodes"][1]["type"] == "ai_agent" and "calculator" in p["nodes"][1]["params"]["tools"]
    p = normalize(planner.template_plan("every morning at 8am research the latest AI news and email me a summary"))
    assert [n["type"] for n in p["nodes"]] == ["schedule", "ai_agent", "llm_summarize", "send_email"]


def test_agent_generates_in_all_three_formats():
    p = normalize(planner.template_plan(AGENT_REQ))
    fw = flowise.generate(p)
    assert validate_flowise(fw["export"]).ok
    agent = next(n for n in flowise.flow_data(fw["export"])["nodes"] if n["data"]["name"] == "agentAgentflow")
    inp = agent["data"]["inputs"]
    assert inp["agentModelConfig"]["modelName"] == "qwen3:4b-instruct" and inp["agentModelConfig"]["think"] is False
    assert [t["agentSelectedTool"] for t in inp["agentTools"]] == ["calculator", "currentDateTime", "webScraperTool"]
    assert inp["agentEnableMemory"] is True  # chat agent
    wf = n8n.generate(p)["workflow"]
    assert validate_n8n(wf).ok
    assert any(n["type"] == "@n8n/n8n-nodes-langchain.agent" for n in wf["nodes"])
    code = python_gen.generate(p)["code"]
    assert validate_python(code, p).ok and "def run_agent(" in code


def test_python_agent_calculator_is_safe():
    ns = {}
    exec(python_gen.AGENT_RUNTIME.split("AGENT_TOOLS = {")[0].replace("import ast", "import ast, re, os, json"), ns)
    assert ns["_calc"]("2480*0.15") == "372.0"
    with pytest.raises(Exception):
        ns["_calc"]("__import__('os').system('echo hi')")


def test_flowise_message_bodies_are_built_safely():
    p = normalize(planner.template_plan(BRANCH))
    flow = flowise.flow_data(flowise.generate(p)["export"])
    builders = [n for n in flow["nodes"] if n["data"]["name"] == "customFunctionAgentflow"]
    assert builders and "JSON.stringify" in builders[0]["data"]["inputs"]["customFunctionJavascriptFunction"]
    http = next(n for n in flow["nodes"] if n["data"]["name"] == "httpAgentflow")
    assert http["data"]["inputs"]["body"] == "{{ " + builders[0]["id"] + " }}"


def _fake_flowise(state):
    class H(BaseHTTPRequestHandler):
        def _send(self, obj, code=200):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(obj).encode())

        def do_GET(self):
            if self.path == "/api/v1/variables":
                return self._send(state["variables"])
            if self.path == "/api/v1/credentials":
                return self._send(state["credentials"])
            if self.path == "/api/v1/tools":
                return self._send(state["tools"])
            self._send({}, 404)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            state["calls"].append((self.path, body))
            if self.path == "/api/v1/variables":
                state["variables"].append(body)
                return self._send(body)
            if self.path == "/api/v1/tools":
                state["tools"].append({**body, "id": "tool-1"})
                return self._send({"id": "tool-1"})
            if self.path == "/api/v1/chatflows":
                state["flow"] = json.loads(body["flowData"])
                return self._send({"id": "flow-1", "name": body["name"], "type": body["type"]})
            if self.path.startswith("/api/v1/prediction/") or self.path.startswith("/api/v1/webhook/"):
                return self._send({"text": "fake answer", "chatId": "chat-1",
                                   "agentFlowExecutedData": [{"nodeLabel": "Start", "status": "FINISHED"}]})
            self._send({}, 404)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_deploy_wires_variables_credentials_and_tools():
    from flowforge import deploy
    state = {"variables": [], "credentials": [{"id": "cred-g", "name": "My Gmail", "credentialName": "gmailOAuth2"}],
             "tools": [], "calls": []}
    srv = _fake_flowise(state)
    base = f"http://127.0.0.1:{srv.server_port}"
    # agent: compact web tool is installed and swapped in; test run happens
    p = normalize(planner.template_plan(AGENT_REQ))
    d = deploy.deploy(p, flowise.generate(p)["export"], base=base, api_key="k")
    assert d["ready"] and d["run"]["ok"] and d["run"]["answer"] == "fake answer"
    tools = next(n for n in state["flow"]["nodes"] if n["data"]["name"] == "agentAgentflow")["data"]["inputs"]["agentTools"]
    assert {"agentSelectedTool": "customTool"}.items() <= tools[-1].items() and tools[-1]["agentSelectedToolConfig"]["selectedTool"] == "tool-1"
    # automation needing Gmail + Drive + Slack variable: Gmail attaches, Drive and the variable become to-dos, no run
    p = normalize(planner.template_plan(INVOICE))
    d = deploy.deploy(p, flowise.generate(p)["export"], base=base, api_key="k")
    srv.shutdown()
    assert not d["ready"] and d["run"]["skipped"]
    assert any("SLACK_WEBHOOK_URL" in t for t in d["todo"]) and any("Google Drive" in t for t in d["todo"])
    assert "cred-g" in json.dumps(state["flow"])  # attached inside Flowise only
    assert "cred-g" not in json.dumps(flowise.generate(p)["export"])  # never in the exported file
