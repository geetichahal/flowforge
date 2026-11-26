"""One-click deploy into a running n8n: create the workflow, attach saved credentials, publish it, run it once.

Configuration: N8N_URL (default http://localhost:5678) and N8N_API_KEY (n8n → Settings → n8n API),
or "n8n_url" / "n8n_api_key" in ~/.flowforge/config.json (private, chmod 600). Nothing is sent unless
the user asks for a real run.

Steps (each reported back, nothing silent):
  1. attach credentials already saved in n8n to the nodes that need them (matched by credential type)
  2. create the workflow (POST /api/v1/workflows)
  3. publish it (POST /api/v1/workflows/{id}/publish) so its form / webhook is live
  4. submit the form or call the webhook once with the user's test input
  5. read the execution back (GET /api/v1/executions) and report each node's status

Secrets never go into the workflow file: n8n keeps them in its own credential store, and
{{ $env.NAME }} values come from the n8n process environment.
"""
from __future__ import annotations

import copy
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import flowise_client  # shares ~/.flowforge/config.json
from .deploy import _apply_test_input, sample_input

# n8n node type -> credential type it needs (only nodes FlowForge generates)
NODE_CREDENTIALS = {
    "n8n-nodes-base.gmail": "gmailOAuth2",
    "n8n-nodes-base.gmailTrigger": "gmailOAuth2",
    "n8n-nodes-base.googleSheets": "googleSheetsOAuth2Api",
    "n8n-nodes-base.googleDrive": "googleDriveOAuth2Api",
    "n8n-nodes-base.googleCalendar": "googleCalendarOAuth2Api",
    "n8n-nodes-base.jira": "jiraSoftwareCloudApi",
    "n8n-nodes-base.twilio": "twilioApi",
    "@n8n/n8n-nodes-langchain.lmChatOllama": "ollamaApi",
}
CRED_LABELS = {"gmailOAuth2": "Gmail", "googleSheetsOAuth2Api": "Google Sheets", "googleDriveOAuth2Api": "Google Drive",
               "googleCalendarOAuth2Api": "Google Calendar", "jiraSoftwareCloudApi": "Jira", "twilioApi": "Twilio",
               "ollamaApi": "Ollama"}
ENV_RE = re.compile(r"\$env\.([A-Za-z_][A-Za-z0-9_]*)")


def base_url() -> str:
    return (os.environ.get("N8N_URL") or flowise_client._config().get("n8n_url") or "http://localhost:5678").rstrip("/")


def configured_key() -> str | None:
    return os.environ.get("N8N_API_KEY") or flowise_client._config().get("n8n_api_key")


def _request(method: str, path: str, body: dict | None = None, timeout: float = 30,
             base: str | None = None, api_key: str | None = None) -> dict:
    key = api_key or configured_key()
    if not key:
        raise RuntimeError("No n8n API key: create one in n8n → Settings → n8n API and save it as n8n_api_key")
    headers = {"Content-Type": "application/json", "Accept": "application/json", "X-N8N-API-KEY": key}
    req = urllib.request.Request((base or base_url()) + "/api/v1" + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"n8n {method} {path} -> HTTP {exc.code}: {exc.read().decode()[:300]}") from None


def _post_form(url: str, fields: dict, timeout: float = 60) -> int:
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n')
    data = ("".join(parts) + f"--{boundary}--\r\n").encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _post_json(url: str, payload: dict, timeout: float = 60) -> int:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status


def _list_credentials(base=None, api_key=None) -> list[dict]:
    out, cursor = [], None
    for _ in range(10):
        q = "?limit=100" + (f"&cursor={urllib.parse.quote(cursor)}" if cursor else "")
        page = _request("GET", "/credentials" + q, base=base, api_key=api_key)
        out += page.get("data") or []
        cursor = page.get("nextCursor")
        if not cursor:
            break
    return out


def _fresh_triggers(workflow: dict) -> dict:
    """Give every form/webhook trigger a fresh id/path so a new copy never clashes with one already live."""
    suffix = uuid.uuid4().hex[:8]
    for n in workflow["nodes"]:
        if n["type"] in ("n8n-nodes-base.formTrigger", "n8n-nodes-base.webhook"):
            n["webhookId"] = str(uuid.uuid4())
        if n["type"] == "n8n-nodes-base.webhook":
            n["parameters"]["path"] = f"{n['parameters'].get('path', 'flowforge-hook')}-{suffix}"
    return workflow


def _wait_for_execution(workflow_id: str, since: float, base=None, api_key=None, timeout: float = 60) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        page = _request("GET", f"/executions?workflowId={workflow_id}&includeData=true&limit=1", base=base, api_key=api_key)
        items = page.get("data") or []
        if items and items[0].get("status") not in ("running", "new", "waiting") and items[0].get("finished") is not None:
            return items[0]
        time.sleep(1)
    return None


def _summarise(execution: dict) -> dict:
    result = ((execution.get("data") or {}).get("resultData") or {})
    nodes = []
    for name, runs in (result.get("runData") or {}).items():
        run = runs[-1] if runs else {}
        status = run.get("executionStatus") or ("error" if run.get("error") else "success")
        nodes.append({"node": name, "status": "FINISHED" if status == "success" else status.upper()})
    err = result.get("error") or {}
    last_output = ""
    last = result.get("lastNodeExecuted")
    try:
        last_output = json.dumps(result["runData"][last][-1]["data"]["main"][0][0]["json"], indent=2)[:1500]
    except (KeyError, IndexError, TypeError):
        pass
    message = err.get("message") if isinstance(err, dict) else None
    if isinstance(err, dict) and err.get("description"):
        message = f"{message} – {err['description']}"
    ok = execution.get("status") == "success" and not message
    return {"ok": ok, "nodes": nodes, "answer": last_output if ok else "", "error": message}


def deploy(plan: dict, workflow: dict, test_input: str | None = None, base: str | None = None,
           api_key: str | None = None, run: bool = True) -> dict:
    base = (base or base_url()).rstrip("/")
    wf = _fresh_triggers(copy.deepcopy(workflow))
    steps: list[dict] = []
    todo: list[str] = []

    # 1. credentials, matched by type
    saved = _list_credentials(base, api_key)
    attached, missing = [], []
    for n in wf["nodes"]:
        ctype = NODE_CREDENTIALS.get(n["type"])
        if not ctype:
            continue
        cred = next((c for c in saved if c.get("type") == ctype), None)
        if cred:
            n["credentials"] = {ctype: {"id": cred["id"], "name": cred["name"]}}
            attached.append(f"{n['name']} → {cred['name']}")
        else:
            missing.append(CRED_LABELS.get(ctype, ctype))
    if attached or missing:
        steps.append({"step": "Credentials", "ok": not missing,
                      "detail": "; ".join(attached) + (f"; not connected yet: {', '.join(sorted(set(missing)))}" if missing else "")})
    todo += [f"Connect a {m} credential in n8n (Credentials → Create credential), then run again." for m in sorted(set(missing))]

    env_needed = sorted(set(ENV_RE.findall(json.dumps(wf))))
    if env_needed:
        steps.append({"step": "Environment", "ok": True,
                      "detail": "uses " + ", ".join(env_needed) + " from the n8n process (set them in ~/n8n-local/start.sh)"})

    # 2. create
    body = {"name": wf.get("name", "FlowForge workflow")[:120], "nodes": wf["nodes"], "connections": wf["connections"],
            "settings": wf.get("settings") or {"executionOrder": "v1"}}
    created = _request("POST", "/workflows", body, base=base, api_key=api_key)
    wid = created["id"]
    steps.append({"step": "Created in n8n", "ok": True, "detail": wid})
    result = {"platform": "n8n", "id": wid, "url": f"{base}/workflow/{wid}", "steps": steps, "todo": todo, "chat": False}

    trigger = next((n for n in wf["nodes"] if n["type"] in ("n8n-nodes-base.formTrigger", "n8n-nodes-base.webhook")), None)
    if trigger and trigger["type"] == "n8n-nodes-base.formTrigger":
        result["form_url"] = f"{base}/form/{trigger['webhookId']}"

    if missing or not run:
        result["run"] = {"skipped": True, "detail": "Not run: finish the steps above first." if missing else "Not run."}
        return result

    # 3. publish
    try:
        _request("POST", f"/workflows/{wid}/publish", {}, base=base, api_key=api_key)
        steps.append({"step": "Published", "ok": True, "detail": "live – its form/webhook now accepts submissions"})
    except RuntimeError as exc:
        steps.append({"step": "Published", "ok": False, "detail": str(exc)[:300]})
        result["run"] = {"ok": False, "error": str(exc)[:500], "seconds": 0}
        return result

    if not trigger:
        result["run"] = {"skipped": True, "detail": "This workflow has no form or webhook trigger; open it in n8n and run it there."}
        return result

    # n8n registers the form/webhook a moment after publishing: wait until it answers
    if trigger["type"] == "n8n-nodes-base.formTrigger":
        for _ in range(40):
            try:
                with urllib.request.urlopen(result["form_url"], timeout=5) as r:
                    if r.status == 200:
                        break
            except Exception:  # noqa: BLE001 - not registered yet
                pass
            time.sleep(0.5)
    else:
        time.sleep(2)

    # 4. submit once with the user's input
    sample = _apply_test_input(sample_input(plan), test_input)
    t0 = time.time()
    try:
        if trigger["type"] == "n8n-nodes-base.formTrigger":
            labels = [f["fieldLabel"] for f in trigger["parameters"]["formFields"]["values"]]
            values = sample.get("form") or {}
            fields = {f"field-{i}": str(values.get(label, "")) for i, label in enumerate(labels)}
            _post_form(result["form_url"], fields)
            sent = json.dumps({label: values.get(label, "") for label in labels})
        else:
            payload = sample.get("payload") or {}
            _post_json(f"{base}/webhook/{trigger['parameters']['path']}", payload)
            sent = json.dumps(payload)
    except Exception as exc:  # noqa: BLE001 - report any failure to the user
        result["run"] = {"ok": False, "sent": None, "error": f"Could not trigger the workflow: {exc}"[:500],
                         "seconds": round(time.time() - t0, 1)}
        return result

    # 5. read the execution back
    execution = _wait_for_execution(wid, t0, base, api_key)
    if not execution:
        result["run"] = {"ok": False, "sent": sent, "error": "No finished execution after 60 s – open the workflow in n8n → Executions.",
                         "seconds": round(time.time() - t0, 1)}
        return result
    run_info = _summarise(execution)
    run_info.update({"sent": sent, "seconds": round(time.time() - t0, 1)})
    result["run"] = run_info
    return result
