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
def test_catalog_has_24_nodes_with_mappings():
    assert len(catalog.CATALOG) == 24
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
