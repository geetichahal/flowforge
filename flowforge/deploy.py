"""One-click deploy: put a generated flow into a running Flowise, wire it up, and test it.

Steps (each reported back, nothing silent):
  1. create the flow (POST /api/v1/chatflows)
  2. create any Flowise variables the flow uses ({{ $vars.X }}) from values you supply
     or from environment variables of the same name
  3. attach credentials you already saved in Flowise (Credentials page) to the tools that need them
  4. run it once (chat question, webhook payload or form) and return the answer and each node's status

Credentials are attached only inside Flowise; the exported file never contains them.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time

from . import catalog, flowise_client
from .generators.flowise import specs

VAR_RE = re.compile(r"\{\{\s*\$vars\.([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
CRED_LABELS = {"gmailOAuth2": "Gmail", "googleDriveOAuth2": "Google Drive", "googleSheetsOAuth2": "Google Sheets",
               "googleCalendarOAuth2": "Google Calendar", "jiraApi": "Jira", "awsApi": "AWS"}


def _req(method, path, body=None, base=None, api_key=None, timeout=30):
    return flowise_client._request(method, path, body, timeout=timeout, base=base, api_key=api_key)


WEB_TOOL_NAME = "flowforge_web_reader_v2"  # bump the suffix when the function changes
WEB_TOOL = {
    "name": WEB_TOOL_NAME,
    "description": "Read the main text of a web page (first 3000 characters). Input: the full URL.",
    "color": "linear-gradient(rgb(14,110,116), rgb(47,95,158))",
    "schema": json.dumps([{"id": 0, "property": "url", "description": "Full URL of the page, starting with https://",
                           "type": "string", "required": True}]),
    "func": """const fetch = require('node-fetch');
// identity encoding: node-fetch in Flowise's sandbox fails on some compressed responses
const res = await fetch($url, { headers: { 'User-Agent': 'FlowForge-agent', 'Accept-Encoding': 'identity' } });
let html = await res.text();
const main = html.match(/<main[\\s\\S]*?<\\/main>/i) || html.match(/<article[\\s\\S]*?<\\/article>/i) || html.match(/<body[\\s\\S]*?<\\/body>/i);
if (main) html = main[0];
const text = html.replace(/<(script|style|nav|footer|header)[\\s\\S]*?<\\/\\1>/gi, ' ')
  .replace(/<[^>]+>/g, ' ').replace(/&nbsp;|&#160;/g, ' ').replace(/&amp;/g, '&').replace(/\\s+/g, ' ').trim();
return text.slice(0, 3000);""",
}


def ensure_web_tool(base=None, api_key=None) -> str:
    """Install (once) a compact web-page reader as a Flowise custom tool; returns its id.
    Flowise's built-in web scraper returns whole pages, which overflows a small local model's context."""
    tools = _req("GET", "/api/v1/tools", base=base, api_key=api_key) or []
    tools = tools if isinstance(tools, list) else tools.get("data", [])
    found = next((t for t in tools if t.get("name") == WEB_TOOL_NAME), None)
    if found:
        return found["id"]
    return _req("POST", "/api/v1/tools", WEB_TOOL, base=base, api_key=api_key)["id"]


def _tool_credential_names(tool: str) -> list[str]:
    cred = specs().get(tool, {}).get("credential") or {}
    return list(cred.get("credentialNames") or [])


def sample_input(plan: dict) -> dict:
    """What to send for the test run, based on the trigger."""
    trig = plan["nodes"][0] if plan.get("nodes") else {}
    t = trig.get("type")
    nt = catalog.get(t) if t else None
    if t == "webhook":
        return {"kind": "webhook", "payload": nt.sample_output["body"]}
    if t == "form_submitted":
        fields = [f.strip() for f in str(trig["params"].get("fields", "")).split(",") if f.strip()]
        return {"kind": "form", "form": {f: nt.sample_output.get(f, f"sample {f}") for f in fields}}
    if t in ("schedule", "email_received"):
        return {"kind": "chat", "question": "Scheduled run"}
    return {"kind": "chat", "question": "Hello! In one sentence, what can you help me with?"}


def deploy(plan: dict, export: dict, base: str | None = None, api_key: str | None = None,
           variables: dict | None = None, test_input: str | None = None, run: bool = True) -> dict:
    variables = dict(variables or {})
    steps: list[dict] = []
    todo: list[str] = []
    flow_entry = copy.deepcopy(export["AgentFlowV2"][0])
    flow = json.loads(flow_entry["flowData"])

    # -- variables
    needed = sorted(set(VAR_RE.findall(flow_entry["flowData"])))
    if needed:
        existing = {v.get("name") for v in (_req("GET", "/api/v1/variables", base=base, api_key=api_key) or [])}
        made, missing = [], []
        for name in needed:
            if name in existing:
                continue
            value = variables.get(name) or os.environ.get(name)
            if not value:
                missing.append(name)
                continue
            _req("POST", "/api/v1/variables", {"name": name, "value": value, "type": "static"}, base=base, api_key=api_key)
            made.append(name)
        steps.append({"step": "Variables", "ok": not missing,
                      "detail": (f"created {', '.join(made)}; " if made else "") +
                                (f"already set: {', '.join(n for n in needed if n in existing)}" if any(n in existing for n in needed) else "") +
                                (f"missing: {', '.join(missing)}" if missing else "")})
        todo += [f"Give a value for {m} (Flowise → Variables, or enter it here and deploy again)." for m in missing]

    # -- credentials
    creds = None
    attached, needs = [], []

    def find(names):
        nonlocal creds
        if creds is None:
            data = _req("GET", "/api/v1/credentials", base=base, api_key=api_key) or []
            creds = data if isinstance(data, list) else data.get("data", [])
        return next((c for c in creds if c.get("credentialName") in names), None)

    for node in flow["nodes"]:
        inp = node["data"].get("inputs", {})
        targets = []
        if node["data"]["name"] == "toolAgentflow" and inp.get("toolAgentflowSelectedTool"):
            targets.append((inp["toolAgentflowSelectedTool"], inp.setdefault("toolAgentflowSelectedToolConfig", {})))
        for t in inp.get("agentTools") or []:
            targets.append((t.get("agentSelectedTool"), t.setdefault("agentSelectedToolConfig", {})))
        for tool, cfg in targets:
            names = _tool_credential_names(tool)
            if not names:
                continue
            c = find(names)
            if c:
                cfg["FLOWISE_CREDENTIAL_ID"] = c["id"]
                attached.append(f"{node['data']['label']} → {c.get('name')}")
            else:
                label = CRED_LABELS.get(names[0], names[0])
                needs.append(label)
                todo.append(f"Connect a {label} credential in Flowise (Credentials → Add), then deploy again.")
    if attached or needs:
        steps.append({"step": "Credentials", "ok": not needs,
                      "detail": "; ".join(attached) + ("; " if attached and needs else "") +
                                (f"not connected yet: {', '.join(sorted(set(needs)))}" if needs else "")})

    # -- compact web reader for agents
    web_cfgs = [t for n in flow["nodes"] for t in (n["data"].get("inputs", {}).get("agentTools") or [])
                if t.get("agentSelectedTool") == "webScraperTool"]
    if web_cfgs:
        try:
            tool_id = ensure_web_tool(base, api_key)
            for t in web_cfgs:
                t["agentSelectedTool"] = "customTool"
                t["agentSelectedToolConfig"] = {"selectedTool": tool_id, "returnDirect": False, "agentSelectedTool": "customTool"}
            steps.append({"step": "Web reader", "ok": True, "detail": f"compact page reader installed ({WEB_TOOL_NAME})"})
        except Exception as exc:  # keep Flowise's own scraper; it works, just slower on long pages
            steps.append({"step": "Web reader", "ok": True,
                          "detail": f"kept Flowise's built-in scraper (could not install the compact one: {str(exc)[:80]})"})

    # -- create
    flow_entry["flowData"] = json.dumps(flow)
    created = flowise_client.create_flow({"AgentFlowV2": [flow_entry]}, base=base, api_key=api_key)
    steps.insert(0, {"step": "Created in Flowise", "ok": True, "detail": created["id"]})
    models = {n["data"]["inputs"].get("agentModelConfig", n["data"]["inputs"].get("llmModelConfig", {})).get("modelName")
              for n in flow["nodes"] if n["data"]["name"] in ("agentAgentflow", "llmAgentflow")}
    models.discard(None)
    if models:
        steps.insert(1, {"step": "Model", "ok": True, "detail": "Ollama · " + ", ".join(sorted(models))})
    start = next(n for n in flow["nodes"] if n["data"]["name"] == "startAgentflow")["data"]["inputs"]
    if start.get("startInputType") == "scheduleInput":
        todo.append(f"Turn on the schedule ({start.get('scheduleCronExpression')}) on the Start node in Flowise.")
    todo = list(dict.fromkeys(todo))

    result = {"id": created["id"], "url": created["url"], "ready": not todo, "steps": steps, "todo": todo,
              "chat": start.get("startInputType") == "chatInput", "run": None}
    if not run:
        return result
    if needs or any(s["step"] == "Variables" and not s["ok"] for s in steps):
        result["run"] = {"ok": False, "skipped": True, "detail": "Not run yet: finish the to-do items first."}
        return result
    result["run"] = test_run(created["id"], plan, base, api_key, test_input)
    return result


def name_from_email(email: str) -> str:
    """'rajesh.shrivastava@bennett.edu.in' -> 'Rajesh Shrivastava'; falls back to 'there' ("Hi there")."""
    words = [w for w in re.split(r"[._+\-\d]+", email.split("@")[0]) if w]
    return " ".join(w.capitalize() for w in words) or "there"


def _apply_test_input(sample: dict, test_input: str | None) -> dict:
    """Put the user's own test text into a form/webhook sample: a JSON object is merged field by field,
    an email address goes into the 'email' field, other text into the 'message' field (or the last
    field if there is none)."""
    if not test_input or sample["kind"] not in ("form", "webhook"):
        return sample
    key = "form" if sample["kind"] == "form" else "payload"
    data = dict(sample[key])
    try:
        given = json.loads(test_input)
    except ValueError:
        given = None
    if isinstance(given, dict):
        data.update(given)
    elif "email" in data and (addr := re.search(r"[^@\s,;<>()\"']+@[^@\s,;<>()\"']+\.[A-Za-z]{2,}", test_input)):
        # "rao@x.in", "Dr. Rao, rao@x.in", "Dr. Rao <rao@x.in>", "rao@x.in - Dr. Rao"
        data["email"] = addr.group(0)
        rest = (test_input[:addr.start()] + " " + test_input[addr.end():]).strip(" \t,;:|<>()-–\"'")
        rest = re.sub(r"\s*[,;|]\s*", " ", rest).strip()
        if "name" in data:  # never keep the sample person's name next to a real address
            data["name"] = rest or name_from_email(data["email"])
    else:
        field = "message" if "message" in data or not data else list(data)[-1]
        data[field] = test_input
        if field != "name" and data.get("name") == sample[key].get("name"):
            data["name"] = "Guest"  # the user typed something but no name: don't show the sample person
    return {**sample, key: data}


def test_run(flow_id: str, plan: dict, base=None, api_key=None, test_input: str | None = None,
             chat_id: str | None = None) -> dict:
    sample = _apply_test_input(sample_input(plan), test_input)
    t0 = time.time()
    try:
        if sample["kind"] == "webhook":
            out = flowise_client.trigger_webhook(flow_id, sample["payload"], base=base, api_key=api_key)
            sent = json.dumps(sample["payload"])
        elif sample["kind"] == "form":
            out = flowise_client.predict(flow_id, "", form=sample["form"], base=base, api_key=api_key)
            sent = json.dumps(sample["form"])
        else:
            q = test_input or sample["question"]
            out = flowise_client.predict(flow_id, q, chat_id=chat_id, base=base, api_key=api_key)
            sent = q
    except Exception as exc:
        return {"ok": False, "sent": None, "error": str(exc)[:500], "seconds": round(time.time() - t0, 1)}
    executed = [{"node": e.get("nodeLabel"), "status": e.get("status")} for e in out.get("agentFlowExecutedData") or []]
    ok = bool(executed) and all(e["status"] == "FINISHED" for e in executed) or (not executed and bool(out.get("text")))
    return {"ok": ok, "sent": sent, "answer": (out.get("text") or "").strip(), "nodes": executed,
            "chat_id": out.get("chatId"), "seconds": round(time.time() - t0, 1)}
