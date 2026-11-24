"""Plan -> standalone Python script (standard library + ``requests``).

The script has one function per plan node, a ``run()`` that walks the steps in
dependency order (following condition branches), configuration read from
environment variables, and a safety model built in:

* ``FLOWFORGE_DRY_RUN`` defaults to ``1``: nothing external happens, each step
  prints what it would do and returns sample data.
* With ``FLOWFORGE_DRY_RUN=0`` every external side effect asks for approval on
  the terminal, unless ``FLOWFORGE_AUTO_APPROVE=1`` is set explicitly.
"""
from __future__ import annotations

import pprint
import textwrap

from .. import catalog
from ..plan import node_map, refs_in, topo_order, trigger_of

RUNTIME = r'''
import argparse
import base64
import json
import os
import re
import sys
import time
from email.message import EmailMessage

try:
    import requests
except ImportError:  # dry runs work without it
    requests = None

DRY_RUN = os.environ.get("FLOWFORGE_DRY_RUN", "1") != "0"
AUTO_APPROVE = os.environ.get("FLOWFORGE_AUTO_APPROVE") == "1"
TIMEOUT = 30
REF = re.compile(r"\{\{\s*([A-Za-z_]\w*)((?:\.[\w-]+)*)\s*\}\}")


def env(name):
    """Read configuration from the environment. Secrets never live in this file."""
    value = os.environ.get(name)
    if not value:
        if DRY_RUN:
            return "<" + name + ">"
        sys.exit("Missing environment variable " + name)
    return value


def lookup(ctx, root, path):
    if root == "env":
        return env(path[0])
    value = ctx.get(root, {})
    for key in path:
        if isinstance(value, dict):
            value = value.get(key, "")
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return ""
    return value


def render(value, ctx):
    """Resolve {{step.field}} references. A value that is exactly one reference keeps its type."""
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = REF.fullmatch(value.strip())
    if whole:
        return lookup(ctx, whole.group(1), [p for p in whole.group(2).split(".") if p])

    def one(m):
        v = lookup(ctx, m.group(1), [p for p in m.group(2).split(".") if p])
        return v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v)
    return REF.sub(one, value)


def approve(action):
    """Human approval gate for every real external action."""
    if DRY_RUN:
        print("  [dry-run] would " + action)
        return False
    if AUTO_APPROVE:
        return True
    answer = input("  Approve: " + action + "? [y/N] ").strip().lower()
    return answer in ("y", "yes")


def http(method, url, **kwargs):
    if requests is None:
        sys.exit("Install requests to run for real: pip install requests")
    response = requests.request(method, url, timeout=TIMEOUT, **kwargs)
    response.raise_for_status()
    return response


def google_headers():
    # An OAuth access token with the needed Google scopes (Gmail/Drive/Sheets/Calendar).
    return {"Authorization": "Bearer " + env("GOOGLE_ACCESS_TOKEN")}


def check(left, operator, right):
    def num(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return None
    l, r = ("" if left is None else str(left)), ("" if right is None else str(right))
    if operator == "contains":
        return r.lower() in l.lower()
    if operator == "not_contains":
        return r.lower() not in l.lower()
    if operator == "equals":
        return l.strip().lower() == r.strip().lower()
    if operator == "not_equals":
        return l.strip().lower() != r.strip().lower()
    if operator == "starts_with":
        return l.lower().startswith(r.lower())
    if operator == "ends_with":
        return l.lower().endswith(r.lower())
    if operator in ("greater_than", "less_than"):
        a, b = num(l), num(r)
        if a is None or b is None:
            return False
        return a > b if operator == "greater_than" else a < b
    if operator == "is_empty":
        return l.strip() == ""
    if operator == "is_not_empty":
        return l.strip() != ""
    raise ValueError("unknown operator " + operator)
'''

MAIN = r'''

MESSAGE = ""


def run(trigger_output=None):
    """Execute the steps in order. A step runs when at least one incoming edge is live."""
    ctx = {}
    for step_id, fn, incoming in STEPS:
        if not incoming:
            ctx[step_id] = trigger_output if trigger_output is not None else fn(ctx)
            print("[" + step_id + "] trigger fired")
            continue
        live = any(src in ctx and (branch is None or bool(ctx[src].get("result")) == (branch == "true"))
                   for src, branch in incoming)
        if not live:
            print("[" + step_id + "] skipped (branch not taken)")
            continue
        print("[" + step_id + "] running")
        ctx[step_id] = fn(ctx)
    return ctx


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--payload", help="JSON file used as the trigger output (skips the trigger)")
    parser.add_argument("--serve", action="store_true", help="for webhook/form triggers: listen for requests")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    parser.add_argument("--chat", action="store_true", help="manual trigger: keep asking for messages until you type exit")
    parser.add_argument("message", nargs="*", help="manual trigger: the input text")
    args = parser.parse_args()
    global MESSAGE
    MESSAGE = " ".join(args.message)
    print("Mode: " + ("DRY RUN (set FLOWFORGE_DRY_RUN=0 to run for real)" if DRY_RUN else "LIVE"))
    if args.serve:
        serve(args.port)
        return
    if args.chat:
        while True:
            try:
                MESSAGE = input("you> ").strip()
            except EOFError:
                break
            if MESSAGE.lower() in ("exit", "quit", ""):
                break
            ctx = run()
            last = list(ctx.values())[-1]
            print("bot> " + str(last.get("answer", last.get("message", json.dumps(last, default=str)))))
        return
    payload = None
    if args.payload:
        with open(args.payload) as fh:
            payload = json.load(fh)
    ctx = run(payload)
    print(json.dumps(ctx, indent=2, default=str))


if __name__ == "__main__":
    main()
'''

SERVE_NONE = r'''

def serve(port):
    sys.exit("This automation's trigger does not listen for HTTP requests; run it directly"
             + (" or from cron: " + CRON if CRON else "."))
'''

SERVE_HTTP = r'''

def serve(port):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import parse_qs, urlparse

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body, ctype="application/json"):
            data = body.encode() if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if FORM_FIELDS:
                inputs = "".join('<p><label>' + f + '<br><input name="' + f + '"></label></p>' for f in FORM_FIELDS)
                return self._reply(200, "<h1>" + FORM_TITLE + '</h1><form method="post">' + inputs
                                   + '<button>Submit</button></form>', "text/html")
            self._reply(405, '{"error": "use ' + WEBHOOK_METHOD + '"}')

        def do_POST(self):
            url = urlparse(self.path)
            if WEBHOOK_PATH and url.path.strip("/") != WEBHOOK_PATH.strip("/"):
                return self._reply(404, '{"error": "unknown path"}')
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode() or "{}"
            if FORM_FIELDS:
                fields = {k: v[0] for k, v in parse_qs(raw).items()}
                trigger = {**{f: fields.get(f, "") for f in FORM_FIELDS},
                           "submitted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            else:
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = {"raw": raw}
                trigger = {"body": body, "headers": dict(self.headers), "query": {k: v[0] for k, v in parse_qs(url.query).items()}}
            ctx = run(trigger)
            self._reply(200, json.dumps({"ok": True, "steps": list(ctx)}, default=str))

    print("Listening on http://localhost:%d/%s" % (port, WEBHOOK_PATH or ""))
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
'''

# ---------------------------------------------------------------------------
# per-node function bodies. `p` = rendered params, `ctx` = outputs so far.

BODIES: dict[str, str] = {
    "manual": '''
        return {"input": MESSAGE or p["input"]}
    ''',
    "webhook": '''
        # Normally the payload arrives via --serve or --payload; this is the sample request.
        return SAMPLE
    ''',
    "schedule": '''
        # Run this script from cron to get the schedule:  {cron}
        return {{"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}}
    ''',
    "form_submitted": '''
        # Submissions normally arrive via --serve (which shows the form) or --payload.
        return SAMPLE
    ''',
    "email_received": '''
        if DRY_RUN:
            print("  [dry-run] would fetch the newest Gmail message matching " + repr(p["query"]))
            return SAMPLE
        base = "https://gmail.googleapis.com/gmail/v1/users/me/messages"
        listing = http("GET", base, headers=google_headers(), params={"q": p["query"], "maxResults": 1}).json()
        if not listing.get("messages"):
            sys.exit("No email matches " + repr(p["query"]))
        msg_id = listing["messages"][0]["id"]
        msg = http("GET", base + "/" + msg_id, headers=google_headers(), params={"format": "full"}).json()
        headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
        body, attachments, stack = "", [], [msg["payload"]]
        while stack:
            part = stack.pop()
            stack.extend(part.get("parts", []))
            data = part.get("body", {})
            if part.get("filename") and data.get("attachmentId"):
                att = http("GET", base + "/" + msg_id + "/attachments/" + data["attachmentId"], headers=google_headers()).json()
                attachments.append({"name": part["filename"], "mime_type": part.get("mimeType", ""),
                                    "size": data.get("size", 0), "content": base64.urlsafe_b64decode(att["data"])})
            elif part.get("mimeType") == "text/plain" and data.get("data") and not body:
                body = base64.urlsafe_b64decode(data["data"]).decode("utf-8", "replace")
        return {"message_id": msg_id, "from": headers.get("from", ""), "subject": headers.get("subject", ""),
                "body": body, "has_attachments": bool(attachments), "attachments": attachments}
    ''',
    "send_email": '''
        if not approve("send email to " + str(p["to"]) + " with subject " + repr(p["subject"])):
            return SAMPLE
        mail = EmailMessage()
        mail["To"], mail["Subject"] = p["to"], p["subject"]
        mail.set_content(str(p["body"]))
        raw = base64.urlsafe_b64encode(mail.as_bytes()).decode()
        sent = http("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                    headers=google_headers(), json={"raw": raw}).json()
        return {"message_id": sent.get("id", ""), "status": "sent"}
    ''',
    "slack_message": '''
        if not approve("post to Slack: " + repr(str(p["text"])[:80])):
            return SAMPLE
        http("POST", p["webhook_url"], json={"text": p["text"]})
        return {"ok": True}
    ''',
    "discord_message": '''
        if not approve("post to Discord: " + repr(str(p["content"])[:80])):
            return SAMPLE
        http("POST", p["webhook_url"], json={"content": p["content"]})
        return {"ok": True}
    ''',
    "teams_message": '''
        if not approve("post to Microsoft Teams: " + repr(str(p["text"])[:80])):
            return SAMPLE
        http("POST", p["webhook_url"], json={"text": p["text"]})
        return {"ok": True}
    ''',
    "sms_message": '''
        if not approve("send SMS to " + str(p["to"])):
            return SAMPLE
        sid = env("TWILIO_ACCOUNT_SID")
        res = http("POST", "https://api.twilio.com/2010-04-01/Accounts/" + sid + "/Messages.json",
                   auth=(sid, env("TWILIO_AUTH_TOKEN")),
                   data={"From": env("TWILIO_FROM_NUMBER"), "To": p["to"], "Body": p["body"]}).json()
        return {"sid": res.get("sid", "")}
    ''',
    "notify": '''
        print("  NOTIFY: " + str(p["message"]))
        return {"message": p["message"]}
    ''',
    "sheets_append": '''
        values = p["values"]
        row = ([v.strip() for v in values.split(",")] if isinstance(values, str)
               else list(values) if isinstance(values, (list, tuple)) else [values])
        if not approve("append row " + repr(row) + " to sheet " + str(p["spreadsheet_id"])):
            return {**SAMPLE, "row": row}
        url = ("https://sheets.googleapis.com/v4/spreadsheets/" + p["spreadsheet_id"] + "/values/"
               + p["range"] + ":append")
        res = http("POST", url, headers=google_headers(), params={"valueInputOption": "USER_ENTERED"},
                   json={"values": [row]}).json()
        return {"updated_range": res.get("updates", {}).get("updatedRange", ""), "row": row}
    ''',
    "sheets_read": '''
        if DRY_RUN:
            print("  [dry-run] would read " + str(p["range"]) + " from sheet " + str(p["spreadsheet_id"]))
            return SAMPLE
        url = "https://sheets.googleapis.com/v4/spreadsheets/" + p["spreadsheet_id"] + "/values/" + p["range"]
        rows = http("GET", url, headers=google_headers()).json().get("values", [])
        return {"rows": rows, "row_count": len(rows)}
    ''',
    "drive_save": '''
        if not approve("upload " + repr(p["file_name"]) + " to Google Drive"):
            return SAMPLE
        content = p["content"] if isinstance(p["content"], bytes) else str(p["content"]).encode()
        meta = {"name": p["file_name"]}
        if p.get("folder_id"):
            meta["parents"] = [p["folder_id"]]
        boundary = "flowforge-boundary"
        body = (("--" + boundary + "\\r\\nContent-Type: application/json\\r\\n\\r\\n" + json.dumps(meta)
                 + "\\r\\n--" + boundary + "\\r\\nContent-Type: " + p["mime_type"] + "\\r\\n\\r\\n").encode()
                + content + ("\\r\\n--" + boundary + "--").encode())
        res = http("POST", "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart&fields=id,webViewLink",
                   headers={**google_headers(), "Content-Type": "multipart/related; boundary=" + boundary},
                   data=body).json()
        return {"file_id": res.get("id", ""), "link": res.get("webViewLink", "")}
    ''',
    "drive_list": '''
        if DRY_RUN:
            print("  [dry-run] would list Drive files matching " + repr(p["query"]))
            return SAMPLE
        res = http("GET", "https://www.googleapis.com/drive/v3/files", headers=google_headers(),
                   params={"q": p["query"] or None, "fields": "files(id,name)"}).json()
        return {"files": res.get("files", []), "count": len(res.get("files", []))}
    ''',
    "extract_attachments": '''
        items = p["source"] if isinstance(p["source"], list) else []
        types = [t.strip().lower() for t in str(p["file_types"]).split(",") if t.strip()]
        keep = [a for a in items if not types or any(str(a.get("name", "")).lower().endswith("." + t) for t in types)]
        first = keep[0] if keep else {}
        return {"files": keep, "count": len(keep), "first_name": first.get("name", ""),
                "first_content": first.get("content", "")}
    ''',
    "http_request": '''
        if not approve(p["method"] + " " + str(p["url"])):
            return SAMPLE
        kwargs = {}
        if p.get("body"):
            kwargs["json"] = json.loads(p["body"]) if isinstance(p["body"], str) else p["body"]
        res = http(p["method"], p["url"], **kwargs)
        try:
            body = res.json()
        except ValueError:
            body = res.text
        return {"status": res.status_code, "body": body}
    ''',
    "condition": '''
        result = check(p["field"], p["operator"], p.get("value", ""))
        print("  condition " + repr(p["field"])[:60] + " " + p["operator"] + " " + repr(p.get("value", "")) + " -> " + str(result))
        return {"result": result}
    ''',
    "transform": '''
        return dict(p["fields"])
    ''',
    "delay": '''
        seconds = float(p["seconds"])
        if DRY_RUN:
            print("  [dry-run] would wait %s seconds" % seconds)
        else:
            time.sleep(seconds)
        return {"waited": seconds}
    ''',
    "llm_summarize": '''
        if DRY_RUN:
            print("  [dry-run] would ask Ollama model " + p["model"] + ": " + repr(str(p["instruction"])[:60]))
            return SAMPLE
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        res = http("POST", host + "/api/generate", json={
            "model": p["model"], "stream": False,
            "prompt": str(p["instruction"]) + "\\n\\n" + str(p["text"])}).json()
        return {"summary": res.get("response", "").strip()}
    ''',
    "ai_agent": '''
        if DRY_RUN:
            print("  [dry-run] would ask the agent (" + p["model"] + "): " + repr(str(p["input"])[:80]))
            return SAMPLE
        return {"answer": run_agent(p["instruction"], str(p["input"]), p["model"],
                                    [t.strip() for t in str(p["tools"]).split(",") if t.strip()], p.get("url", ""))}
    ''',
    "create_task": '''
        if not approve("create Jira task " + repr(p["title"]) + " in project " + str(p["project"])):
            return SAMPLE
        base = env("JIRA_BASE_URL").rstrip("/")
        res = http("POST", base + "/rest/api/2/issue", auth=(env("JIRA_EMAIL"), env("JIRA_API_TOKEN")),
                   json={"fields": {"project": {"key": p["project"]}, "summary": p["title"],
                                    "description": p["description"], "issuetype": {"name": "Task"}}}).json()
        return {"task_id": res.get("key", ""), "url": base + "/browse/" + res.get("key", "")}
    ''',
    "calendar_create": '''
        if not approve("create calendar event " + repr(p["title"]) + " at " + str(p["start"])):
            return SAMPLE
        from datetime import datetime, timedelta
        start = datetime.fromisoformat(str(p["start"]).replace("Z", "+00:00"))
        end = start + timedelta(minutes=int(p["duration_minutes"] or 30))
        attendees = [{"email": a.strip()} for a in str(p["attendees"]).split(",") if a.strip()]
        res = http("POST", "https://www.googleapis.com/calendar/v3/calendars/primary/events", headers=google_headers(),
                   json={"summary": p["title"], "start": {"dateTime": start.isoformat()},
                         "end": {"dateTime": end.isoformat()}, "attendees": attendees}).json()
        return {"event_id": res.get("id", ""), "link": res.get("htmlLink", "")}
    ''',
    "log_message": '''
        print("  LOG: " + str(p["message"]))
        return {"logged": True}
    ''',
}

AGENT_RUNTIME = r'''

# ---- agent: a small tool-calling loop over Ollama's /api/chat
import ast
import operator
from datetime import datetime, timezone

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.Mod: operator.mod, ast.FloorDiv: operator.floordiv,
        ast.USub: operator.neg, ast.UAdd: operator.pos}


def _calc(expr):
    """Evaluate arithmetic safely (numbers and + - * / ** % // only; no names, no calls)."""
    def ev(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("only arithmetic is allowed")
    return str(ev(ast.parse(str(expr).replace("^", "**"), mode="eval").body))


def _web(url):
    text = http("GET", url, headers={"User-Agent": "FlowForge-agent"}).text
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))[:4000]


def _arxiv(query):
    xml = http("GET", "http://export.arxiv.org/api/query", params={"search_query": "all:" + query, "max_results": 3}).text
    titles = re.findall(r"<title>(.*?)</title>", xml, re.S)[1:]
    summaries = re.findall(r"<summary>(.*?)</summary>", xml, re.S)
    return "\n\n".join(t.strip() + ": " + s.strip()[:400] for t, s in zip(titles, summaries)) or "no results"


AGENT_TOOLS = {
    "calculator": ({"name": "calculator", "description": "Evaluate an arithmetic expression, e.g. 2480*0.15",
                    "parameters": {"type": "object", "properties": {"expression": {"type": "string"}}, "required": ["expression"]}},
                   lambda a, url: _calc(a.get("expression", ""))),
    "datetime": ({"name": "current_datetime", "description": "Get the current date and time (UTC and local)",
                  "parameters": {"type": "object", "properties": {}}},
                 lambda a, url: datetime.now(timezone.utc).isoformat() + " UTC / local " + datetime.now().isoformat()),
    "web": ({"name": "read_web_page", "description": "Read the text of a web page",
             "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}},
            lambda a, url: _web(a.get("url", ""))),
    "http": ({"name": "call_api", "description": "GET the configured API and return its response",
              "parameters": {"type": "object", "properties": {}}},
             lambda a, url: http("GET", url).text[:4000]),
    "arxiv": ({"name": "arxiv_search", "description": "Search Arxiv research papers",
               "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
              lambda a, url: _arxiv(a.get("query", ""))),
}


def run_agent(instruction, message, model, tools, url="", max_steps=6):
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    chosen = {AGENT_TOOLS[t][0]["name"]: AGENT_TOOLS[t] for t in tools if t in AGENT_TOOLS}
    messages = [{"role": "system", "content": instruction + "\nUse your tools whenever they help. Answer briefly."},
                {"role": "user", "content": message}]
    for _ in range(max_steps):
        res = http("POST", host + "/api/chat", json={
            "model": model, "messages": messages, "stream": False, "think": False, "options": {"num_ctx": 8192},
            "tools": [{"type": "function", "function": spec} for spec, _ in chosen.values()]}).json()
        msg = res.get("message", {})
        messages.append(msg)
        calls = msg.get("tool_calls") or []
        if not calls:
            return re.sub(r"(?s)<think>.*?</think>", "", msg.get("content", "")).strip()
        for call in calls:
            fn = call.get("function", {})
            name, args = fn.get("name"), fn.get("arguments") or {}
            try:
                result = chosen[name][1](args, url) if name in chosen else "unknown tool"
            except Exception as exc:
                result = "tool error: " + str(exc)
            print("  agent used " + str(name) + "(" + json.dumps(args)[:80] + ")")
            messages.append({"role": "tool", "content": str(result)})
    return "Stopped after " + str(max_steps) + " tool steps without a final answer."
'''

ENV_HINTS = {
    "email_received": ["GOOGLE_ACCESS_TOKEN"], "send_email": ["GOOGLE_ACCESS_TOKEN"],
    "sheets_append": ["GOOGLE_ACCESS_TOKEN"], "sheets_read": ["GOOGLE_ACCESS_TOKEN"],
    "drive_save": ["GOOGLE_ACCESS_TOKEN"], "drive_list": ["GOOGLE_ACCESS_TOKEN"],
    "calendar_create": ["GOOGLE_ACCESS_TOKEN"],
    "sms_message": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"],
    "create_task": ["JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"],
    "llm_summarize": ["OLLAMA_HOST (optional)"],
    "ai_agent": ["OLLAMA_HOST (optional)"],
}


def func_name(node_id: str) -> str:
    return f"step_{node_id}"


def _sample(n: dict) -> dict:
    nt = catalog.get(n["type"])
    sample = dict(nt.sample_output) if nt else {}
    if n["type"] == "form_submitted":
        fields = [f.strip() for f in str(n["params"].get("fields", "")).split(",") if f.strip()]
        sample = {f: sample.get(f, f"sample {f}") for f in fields} | {"submitted_at": sample.get("submitted_at", "")}
    return sample


def generate(plan: dict) -> dict:
    nodes = node_map(plan)
    trig = trigger_of(plan) or {}
    order = topo_order(plan)
    env_vars: set[str] = set()
    for n in nodes.values():
        env_vars.update(path[0] for root, path in refs_in(n.get("params", {})) if root == "env" and path)
        env_vars.update(ENV_HINTS.get(n["type"], []))

    request = plan.get("request", "").replace('"""', "'''")
    doc = [f'"""{plan.get("name", "FlowForge automation")}', "",
           "Generated by FlowForge" + (f" from the request:\n    {request}" if request else "."), "",
           "Usage:",
           "    python this_script.py                       # dry run (default): prints actions, changes nothing",
           "    FLOWFORGE_DRY_RUN=0 python this_script.py   # live run; asks before each external action",
           "    python this_script.py --payload trigger.json  # use a saved trigger payload"]
    if trig.get("type") in ("webhook", "form_submitted"):
        doc.append("    python this_script.py --serve               # listen for the trigger over HTTP")
    if trig.get("type") == "schedule":
        doc.append(f"    cron: {trig['params'].get('cron')}  python /path/to/this_script.py")
    if env_vars:
        doc += ["", "Environment variables:"] + [f"    {v}" for v in sorted(env_vars)]
    doc.append('"""')

    parts = ["#!/usr/bin/env python3", "\n".join(doc), RUNTIME.strip("\n")]
    if any(n["type"] == "ai_agent" for n in nodes.values()):
        parts.append(AGENT_RUNTIME.strip("\n"))

    cfg = [
        f"WEBHOOK_PATH = {trig['params'].get('path', '')!r}" if trig.get("type") == "webhook" else "WEBHOOK_PATH = ''",
        f"WEBHOOK_METHOD = {trig['params'].get('method', 'POST')!r}" if trig.get("type") == "webhook" else "WEBHOOK_METHOD = 'POST'",
        "FORM_FIELDS = " + repr([f.strip() for f in str(trig.get("params", {}).get("fields", "")).split(",") if f.strip()]
                                if trig.get("type") == "form_submitted" else []),
        f"FORM_TITLE = {trig.get('params', {}).get('title', '')!r}",
        f"CRON = {trig['params'].get('cron', '')!r}" if trig.get("type") == "schedule" else "CRON = ''",
    ]
    parts.append("\n".join(cfg))

    for nid in order:
        n = nodes[nid]
        nt = catalog.get(n["type"])
        if nt is None:
            continue
        body = textwrap.dedent(BODIES[n["type"]]).strip("\n")
        if n["type"] == "schedule":
            body = body.format(cron=n["params"].get("cron", ""))
        params_src = pprint.pformat(n["params"], indent=4, width=100, sort_dicts=False)
        label = n.get("label") or nt.label
        fn = [f"def {func_name(nid)}(ctx):",
              f'    """{label}: {nt.description}"""',
              "    PARAMS = " + textwrap.indent(params_src, "    ").lstrip(),
              "    SAMPLE = " + textwrap.indent(pprint.pformat(_sample(n), indent=4, width=100, sort_dicts=False), "    ").lstrip(),
              "    p = render(PARAMS, ctx)",
              textwrap.indent(body, "    ")]
        parts.append("\n".join(fn))

    steps = ["STEPS = ["]
    for nid in order:
        if nid not in nodes or catalog.get(nodes[nid]["type"]) is None:
            continue
        incoming = [(e["from"], e.get("branch")) for e in plan["edges"] if e["to"] == nid]
        steps.append(f"    ({nid!r}, {func_name(nid)}, {incoming!r}),")
    steps.append("]")
    parts.append("\n".join(steps))

    serve = SERVE_HTTP if trig.get("type") in ("webhook", "form_submitted") else SERVE_NONE
    parts.append(serve.strip("\n"))
    parts.append(MAIN.strip("\n"))
    code = "\n\n\n".join(parts) + "\n"
    return {"code": code, "env_vars": sorted(env_vars), "warnings": []}
