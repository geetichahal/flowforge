"""Plan -> n8n workflow JSON (importable via n8n's "Import from File" / REST API).

Shape: ``{"name", "nodes": [{id, name, type, typeVersion, position, parameters}],
"connections": {"<node name>": {"main": [[{node, type, index}], ...]}}, "settings", ...}``.

Credentials are never emitted: n8n asks the user to pick a credential for each
node that needs one after import. Secrets referenced as ``{{env.X}}`` in the
plan become ``{{ $env.X }}`` expressions.
"""
from __future__ import annotations

import copy
import json
import re
import uuid

from .. import catalog
from ..plan import node_map, refs_in, substitute, topo_order, trigger_of

X_STEP, Y_ROW = 240, 180

# catalog output field -> path inside the n8n node's item JSON ("" = the whole item)
N8N_FIELDS = {
    "email_received": {"from": "from.value[0].address", "body": "text", "message_id": "id",
                       "attachments": "attachments"},
    "send_email": {"message_id": "id"},
    "llm_summarize": {"summary": "text"},
    "ai_agent": {"answer": "output"},
    "drive_save": {"file_id": "id", "link": "webViewLink"},
    "http_request": {"body": "", "status": "statusCode"},
    "sheets_read": {"rows": "*all*", "row_count": "*count*"},
    "drive_list": {"files": "*all*", "count": "*count*"},
    "create_task": {"task_id": "key"},
    "calendar_create": {"event_id": "id", "link": "htmlLink"},
}

def _uid(*parts: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "flowforge-n8n:" + ":".join(parts)))


class N8nBuilder:
    def __init__(self, plan: dict):
        self.plan = plan
        self.nodes: list[dict] = []
        self.connections: dict = {}
        self.names: dict[str, str] = {}  # plan id -> n8n node name (emits outgoing edges)
        self.entry: dict[str, str] = {}  # plan id -> n8n node name receiving incoming edges
        self.warnings: list[dict] = []

    def warn(self, node, code, message, level="warning"):
        self.warnings.append({"level": level, "code": code, "node": node, "message": message, "target": "n8n"})

    def unique_name(self, label: str) -> str:
        used = {n["name"] for n in self.nodes}
        name, k = label, 2
        while name in used:
            name, k = f"{label} {k}", k + 1
        return name

    def expr(self, value):
        """Plan references -> n8n expressions. Strings containing refs get the '=' prefix."""
        def fn(root, path):
            if root == "env":
                return "{{ $env." + (path[0] if path else "UNSET") + " }}"
            name = self.names.get(root, root)
            nt = catalog.get(node_map(self.plan).get(root, {}).get("type", ""))
            if nt and path and path[0] in N8N_FIELDS.get(nt.type, {}):
                mapped = N8N_FIELDS[nt.type][path[0]]
                q = "$('" + name.replace("'", "\\'") + "')"
                if mapped == "*all*":  # one n8n item per row/file
                    return "{{ JSON.stringify(" + q + ".all().map(i => i.json)) }}"
                if mapped == "*count*":
                    return "{{ " + q + ".all().length }}"
                path = ([mapped] if mapped else []) + path[1:]
                access = "".join("." + seg if not seg.startswith("[") else seg for seg in path)
                return "{{ $('" + name.replace("'", "\\'") + "').item.json" + access + " }}"
            access = "".join(f"[{json.dumps(p)}]" if not p.isidentifier() else f".{p}" for p in path)
            if nt and nt.type == "form_submitted":
                # n8n form trigger outputs fields keyed by their labels
                access = "".join(f"[{json.dumps(p)}]" for p in path)
            return "{{ $('" + name.replace("'", "\\'") + "').item.json" + access + " }}"

        def wrap(v):
            if isinstance(v, str) and refs_in(v):
                return "=" + substitute(v, fn)
            if isinstance(v, dict):
                return {k: wrap(x) for k, x in v.items()}
            if isinstance(v, list):
                return [wrap(x) for x in v]
            return v
        return wrap(value)

    @staticmethod
    def as_text(val):
        """A message field that is exactly one expression may evaluate to an object: send it as JSON text."""
        if isinstance(val, str) and val.startswith("={{ ") and val.endswith(" }}") and val.count("{{") == 1:
            inner = val[4:-3]
            return "={{ typeof (" + inner + ") === 'string' ? (" + inner + ") : JSON.stringify(" + inner + ") }}"
        return val

    def build(self) -> dict:
        plan = self.plan
        nodes = node_map(plan)
        order = topo_order(plan)
        depth: dict[str, int] = {}
        row: dict[str, int] = {}
        for nid in order:
            ins = [e for e in plan["edges"] if e["to"] == nid]
            depth[nid] = max((depth.get(e["from"], 0) + 1 for e in ins), default=0)
            row[nid] = max((row.get(e["from"], 0) + (1 if e.get("branch") == "false" else 0) for e in ins), default=0)
        for nid in order:
            n = nodes[nid]
            nt = catalog.get(n["type"])
            if nt is None:
                continue
            label = n.get("label") or nt.label
            name = self.unique_name(label)
            self.names[nid] = name
            x, y = 240 + depth[nid] * X_STEP * 2, 300 + row[nid] * Y_ROW
            specs = self._emit(n, nt, name)
            if not specs:
                continue
            # first spec receives incoming edges; the spec named `name` emits outgoing ones
            self.entry[nid] = specs[0]["name"]
            for i, spec in enumerate(specs):
                spec.setdefault("id", _uid(plan.get("name", ""), nid, spec["name"]))
                if spec["type"].endswith("lmChatOllama"):
                    spec.setdefault("position", [x, y + 200])
                else:
                    spec.setdefault("position", [x + i * (X_STEP // 2) if len(specs) > 1 else x, y])
                self.nodes.append(spec)

        for e in plan["edges"]:
            if e["from"] not in self.names or e["to"] not in self.entry:
                continue
            src = self.names[e["from"]]
            idx = 1 if (nodes[e["from"]]["type"] == "condition" and e.get("branch") == "false") else 0
            outs = self.connections.setdefault(src, {}).setdefault("main", [])
            while len(outs) <= idx:
                outs.append([])
            outs[idx].append({"node": self.entry[e["to"]], "type": "main", "index": 0})

        return {
            "name": plan.get("name", "FlowForge automation"),
            "nodes": self.nodes,
            "connections": self.connections,
            "active": False,
            "settings": {"executionOrder": "v1"},
            "pinData": {},
            "meta": {"templateCredsSetupCompleted": False, "generator": "FlowForge"},
            "tags": [],
        }

    # ---------------------------------------------------------------- nodes
    def _emit(self, n: dict, nt, name: str) -> list[dict]:
        t, nid = n["type"], n["id"]
        raw = n["params"]
        p = self.expr(raw)

        def node(type_, version, params, **extra):
            return [{"name": name, "type": type_, "typeVersion": version, "parameters": params, **extra}]

        if t == "manual":
            return node("n8n-nodes-base.manualTrigger", 1, {})
        if t == "webhook":
            path = str(raw.get("path", "flowforge-hook"))
            return node("n8n-nodes-base.webhook", 2,
                        {"httpMethod": raw.get("method", "POST"), "path": path, "responseMode": "onReceived", "options": {}},
                        webhookId=_uid("webhook", path))
        if t == "schedule":
            return node("n8n-nodes-base.scheduleTrigger", 1.2,
                        {"rule": {"interval": [{"field": "cronExpression", "expression": raw.get("cron", "0 9 * * *")}]}})
        if t == "email_received":
            return node("n8n-nodes-base.gmailTrigger", 1.2,
                        {"pollTimes": {"item": [{"mode": "everyMinute"}]}, "simple": False,
                         "filters": {"q": raw.get("query", "is:unread")},
                         "options": {"downloadAttachments": True}})
        if t == "form_submitted":
            fields = [f.strip() for f in str(raw.get("fields", "")).split(",") if f.strip()]
            return node("n8n-nodes-base.formTrigger", 2.2,
                        {"formTitle": raw.get("title", "Form"),
                         "formFields": {"values": [{"fieldLabel": f, "requiredField": True} for f in fields]},
                         "options": {}}, webhookId=_uid("form", nid))
        if t == "send_email":
            return node("n8n-nodes-base.gmail", 2.1,
                        {"sendTo": p.get("to", ""), "subject": p.get("subject", ""), "emailType": "text",
                         "message": self.as_text(p.get("body", "")), "options": {}})
        if t in ("slack_message", "discord_message", "teams_message"):
            key = "content" if t == "discord_message" else "text"
            body = self.expr({key: raw.get(key, "")})
            return node("n8n-nodes-base.httpRequest", 4.2,
                        {"method": "POST", "url": p.get("webhook_url", ""), "sendBody": True,
                         "specifyBody": "keypair",
                         "bodyParameters": {"parameters": [{"name": key, "value": self.as_text(body[key])}]}, "options": {}})
        if t == "http_request":
            params = {"method": raw.get("method", "GET"), "url": p.get("url", ""), "options": {}}
            if raw.get("body"):
                params.update({"sendBody": True, "specifyBody": "json",
                               "jsonBody": p["body"] if isinstance(p["body"], str) else json.dumps(p["body"])})
            return node("n8n-nodes-base.httpRequest", 4.2, params)
        if t == "sms_message":
            return node("n8n-nodes-base.twilio", 1,
                        {"from": "={{ $env.TWILIO_FROM_NUMBER }}", "to": p.get("to", ""), "message": self.as_text(p.get("body", "")),
                         "options": {}})
        if t == "notify":
            return node("n8n-nodes-base.set", 3.4, self._set_params({"message": raw.get("message", "")}))
        if t == "sheets_append":
            cols = [v.strip() for v in str(raw.get("values", "")).split(",")]
            # n8n appends by header name, so name each column after the field it holds
            # ({{trigger.name}} -> "Name"); the sheet's first row must use these headings.
            mapping = {}
            for i, c in enumerate(cols):
                ref = re.fullmatch(r"\{\{\s*[\w.]+\.(\w+)\s*\}\}", c)
                header = ref.group(1).replace("_", " ").title() if ref else f"Column {i + 1}"
                while header in mapping:
                    header += " 2"
                mapping[header] = c
            sheet = str(raw.get("range", "Sheet1!A1")).split("!")[0] or "Sheet1"
            return node("n8n-nodes-base.googleSheets", 4.5,
                        {"operation": "append",
                         "documentId": {"__rl": True, "value": p.get("spreadsheet_id", ""), "mode": "id"},
                         "sheetName": {"__rl": True, "value": sheet, "mode": "name"},
                         "columns": {"mappingMode": "defineBelow", "value": self.expr(mapping),
                                     "matchingColumns": [], "schema": [
                                         {"id": k, "displayName": k, "required": False, "defaultMatch": False,
                                          "display": True, "type": "string", "canBeUsedToMatch": True} for k in mapping]},
                         "options": {}})
        if t == "sheets_read":
            sheet = str(raw.get("range", "Sheet1!A1:Z100")).split("!")[0] or "Sheet1"
            return node("n8n-nodes-base.googleSheets", 4.5,
                        {"documentId": {"__rl": True, "value": p.get("spreadsheet_id", ""), "mode": "id"},
                         "sheetName": {"__rl": True, "value": sheet, "mode": "name"}, "options": {}})
        if t == "drive_save":
            content_refs = refs_in(raw.get("content", ""))
            binary = any("attachment" in ".".join(path) or path[-1:] == ["first_content"] for _, path in content_refs)
            out = []
            if not binary:
                # make a file from text first, then upload it
                conv = {"name": self.unique_name(f"{name} (to file)"), "type": "n8n-nodes-base.convertToFile",
                        "typeVersion": 1.1,
                        "parameters": {"operation": "toText", "sourceProperty": "content",
                                       "options": {"fileName": p.get("file_name", "file.txt")}}}
                prep = {"name": self.unique_name(f"{name} (content)"), "type": "n8n-nodes-base.set", "typeVersion": 3.4,
                        "parameters": self._set_params({"content": raw.get("content", "")})}
                out += [prep, conv]
            upload = {"name": name, "type": "n8n-nodes-base.googleDrive", "typeVersion": 3,
                      "parameters": {"name": p.get("file_name", ""),
                                     "driveId": {"__rl": True, "value": "My Drive", "mode": "list"},
                                     "folderId": {"__rl": True, "value": raw.get("folder_id") or "root", "mode": "id"},
                                     "inputDataFieldName": "attachment_0" if binary else "data",
                                     "options": {}}}
            if binary:
                self.warn(nid, "W_N8N_BINARY", "Uploads the Gmail trigger's first binary attachment (attachment_0).")
                return [upload]
            return self._chain(out + [upload])
        if t == "drive_list":
            return node("n8n-nodes-base.googleDrive", 3,
                        {"resource": "fileFolder", "queryString": p.get("query", ""), "filter": {}, "options": {}})
        if t == "condition":
            return node("n8n-nodes-base.if", 2, self._if_params(raw))
        if t == "transform":
            fields = raw.get("fields") if isinstance(raw.get("fields"), dict) else {}
            return node("n8n-nodes-base.set", 3.4, self._set_params(fields, raw.get("case") or ""))
        if t == "extract_attachments":
            types = [x.strip().lower() for x in str(raw.get("file_types", "")).split(",") if x.strip()]
            js = ("const types = " + json.dumps(types) + ";\n"
                  "const out = [];\n"
                  "for (const item of $input.all()) {\n"
                  "  for (const [key, bin] of Object.entries(item.binary || {})) {\n"
                  "    const name = (bin.fileName || key).toLowerCase();\n"
                  "    if (!types.length || types.some(t => name.endsWith('.' + t))) {\n"
                  "      out.push({ json: { first_name: bin.fileName, key }, binary: { attachment_0: bin } });\n"
                  "    }\n  }\n}\nreturn out;")
            return node("n8n-nodes-base.code", 2, {"jsCode": js})
        if t == "delay":
            return node("n8n-nodes-base.wait", 1.1, {"amount": raw.get("seconds", 60), "unit": "seconds"},
                        webhookId=_uid("wait", nid))
        if t == "llm_summarize":
            chain = {"name": name, "type": "@n8n/n8n-nodes-langchain.chainLlm", "typeVersion": 1.5,
                     "parameters": {"promptType": "define",
                                    "text": self.expr(f"{raw.get('instruction', 'Summarize.')}\n\n{raw.get('text', '')}")}}
            model = {"name": self.unique_name(f"{name} (Ollama model)"), "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
                     "typeVersion": 1, "parameters": {"model": raw.get("model", "qwen3:4b"), "options": {}}}
            self.connections[model["name"]] = {"ai_languageModel": [[{"node": name, "type": "ai_languageModel", "index": 0}]]}
            self.warn(nid, "W_N8N_OUTPUT", "The LLM chain outputs its result in the 'text' field.", "info")
            return [chain, model]
        if t == "ai_agent":
            agent = {"name": name, "type": "@n8n/n8n-nodes-langchain.agent", "typeVersion": 1.7,
                     "parameters": {"promptType": "define", "text": p.get("input", ""),
                                    "options": {"systemMessage": raw.get("instruction", "")}}}
            subs = [{"name": self.unique_name(f"{name} (Ollama model)"), "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
                     "typeVersion": 1, "parameters": {"model": raw.get("model", "qwen3:4b-instruct"), "options": {}}}]
            self.connections[subs[0]["name"]] = {"ai_languageModel": [[{"node": name, "type": "ai_languageModel", "index": 0}]]}
            for tool in [x.strip() for x in str(raw.get("tools", "")).split(",") if x.strip()]:
                spec = {"calculator": ("@n8n/n8n-nodes-langchain.toolCalculator", 1, {}),
                        "web": ("@n8n/n8n-nodes-langchain.toolHttpRequest", 1.1,
                                {"toolDescription": "Fetch a web page or API. Give the full URL.", "url": "={{ $fromAI('url') }}"}),
                        "http": ("@n8n/n8n-nodes-langchain.toolHttpRequest", 1.1,
                                 {"toolDescription": "Call the API", "url": raw.get("url") or "={{ $fromAI('url') }}"}),
                        "arxiv": None, "datetime": None}.get(tool)
                if spec is None:
                    self.warn(nid, "W_N8N_APPROX", f"Agent tool '{tool}' has no built-in n8n tool node; the agent runs without it.")
                    continue
                sub = {"name": self.unique_name(f"{name} ({tool})"), "type": spec[0], "typeVersion": spec[1], "parameters": spec[2]}
                self.connections[sub["name"]] = {"ai_tool": [[{"node": name, "type": "ai_tool", "index": 0}]]}
                subs.append(sub)
            return [agent, *subs]
        if t == "create_task":
            return node("n8n-nodes-base.jira", 1,
                        {"project": {"__rl": True, "value": p.get("project", ""), "mode": "id"},
                         "issueType": {"__rl": True, "value": "Task", "mode": "name"},
                         "summary": p.get("title", ""), "additionalFields": {"description": p.get("description", "")}})
        if t == "calendar_create":
            return node("n8n-nodes-base.googleCalendar", 1.3,
                        {"calendar": {"__rl": True, "value": "primary", "mode": "id"},
                         "start": p.get("start", ""),
                         "end": f"={{{{ DateTime.fromISO('{raw.get('start', '')}').plus({{minutes: {int(raw.get('duration_minutes') or 30)}}}).toISO() }}}}",
                         "additionalFields": {"summary": p.get("title", ""),
                                              "attendees": [a.strip() for a in str(raw.get("attendees", "")).split(",") if a.strip()]}})
        if t == "log_message":
            msg = self.expr(raw.get("message", ""))
            js = "console.log(" + json.dumps(msg.lstrip("=") if isinstance(msg, str) else msg) + ");\nreturn $input.all();"
            if isinstance(msg, str) and msg.startswith("="):
                js = ("const msg = `" + msg[1:].replace("`", "\\`").replace("{{", "${").replace("}}", "}") +
                      "`;\nconsole.log(msg);\nreturn $input.all();")
            return node("n8n-nodes-base.code", 2, {"jsCode": js})
        self.warn(nid, "E_N8N_UNMAPPED", f"No n8n mapping for '{t}'", "error")
        return []

    def _chain(self, specs: list[dict]) -> list[dict]:
        """Wire an internal sub-chain (e.g. Set -> Convert to File -> Drive upload)."""
        for a, b in zip(specs, specs[1:]):
            self.connections.setdefault(a["name"], {}).setdefault("main", [[]])[0].append(
                {"node": b["name"], "type": "main", "index": 0})
        return specs

    def _set_params(self, fields: dict, case: str = "") -> dict:
        assigns = []
        for k, v in fields.items():
            val = self.expr(v)
            if case in ("upper", "lower"):
                method = "toUpperCase" if case == "upper" else "toLowerCase"
                if isinstance(val, str) and val.startswith("="):
                    # "=text {{ expr }} more" -> "={{ `text ${expr} more`.toUpperCase() }}"
                    body = val[1:].replace("`", "\\`")
                    js = re.sub(r"\{\{\s*(.*?)\s*\}\}", lambda m: "${" + m.group(1) + "}", body)
                    val = "={{ `" + js + "`." + method + "() }}"
                else:
                    val = getattr(str(val), case)()
            assigns.append({"id": _uid("set", k, str(v)), "name": k, "value": val, "type": "string"})
        return {"assignments": {"assignments": assigns}, "options": {}}

    def _if_params(self, raw: dict) -> dict:
        op = raw.get("operator", "contains")
        num = op in ("greater_than", "less_than")
        ops = {"contains": "contains", "not_contains": "notContains", "equals": "equals", "not_equals": "notEquals",
               "starts_with": "startsWith", "ends_with": "endsWith", "is_empty": "empty", "is_not_empty": "notEmpty",
               "greater_than": "gt", "less_than": "lt"}
        operator = {"type": "number" if num else "string", "operation": ops.get(op, "contains")}
        if op in ("is_empty", "is_not_empty"):
            operator["singleValue"] = True
        right = raw.get("value", "")
        if num:
            try:
                right = float(right)
                right = int(right) if right.is_integer() else right
            except (TypeError, ValueError):
                pass
        left = self.expr(raw.get("field", ""))
        if num and isinstance(left, str) and left.startswith("="):
            left = left.replace("{{ ", "{{ Number(").replace(" }}", ") }}")
        return {
            "conditions": {
                "options": {"caseSensitive": False, "leftValue": "", "typeValidation": "loose"},
                "conditions": [{"id": _uid("if", str(raw)), "leftValue": left, "rightValue": self.expr(right),
                                "operator": operator}],
                "combinator": "and",
            },
            "options": {},
        }


def generate(plan: dict) -> dict:
    b = N8nBuilder(copy.deepcopy(plan))
    return {"workflow": b.build(), "warnings": b.warnings}
