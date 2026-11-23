"""Plan -> Flowise AgentFlow v2 export file.

Output is the ExportData shape that Flowise's Import (UI) and
``POST /api/v1/export-import/import`` accept::

    {"AgentFlowV2": [{"id", "name", "flowData": "<stringified {nodes, edges}>", "type": "AGENTFLOW"}]}

Node objects are built the same way the Flowise canvas builds them
(``packages/ui/src/utils/genericHelper.js#initNode``) from the node class
definitions in ``data/flowise_node_specs.json``. That file is dumped from a
built Flowise checkout by ``tools/dump_flowise_specs.js``, so names, versions,
input parameter ids and output anchors match the real platform exactly.

Variable syntax (confirmed in ``packages/server/src/utils/buildAgentflow.ts``):
``{{ question }}``, ``{{ $form.x }}``, ``{{ $webhook.body.x }}``, ``{{ $vars.X }}``,
``{{ nodeId }}`` (node output content) and ``{{ nodeId.output.path }}``.
"""
from __future__ import annotations

import copy
import json
import uuid
from functools import lru_cache
from pathlib import Path

from .. import catalog
from ..plan import node_map, refs_in, substitute, topo_order, trigger_of

SPECS_PATH = Path(__file__).resolve().parent.parent / "data" / "flowise_node_specs.json"

WHITELIST_TYPES = {
    "asyncOptions", "asyncMultiOptions", "options", "multiOptions", "array", "datagrid", "string",
    "number", "boolean", "password", "json", "code", "date", "file", "folder", "tabs",
    "conditionFunction", "timePicker", "weekDaysPicker", "monthDaysPicker", "datePicker",
}
MODEL_PLACEHOLDER_NOTE = "pick a chat model in the canvas (ChatOllama works locally)"
OLLAMA_MODEL = "chatOllama"

X_STEP, Y_ROW = 260, 120


@lru_cache(maxsize=1)
def specs() -> dict:
    return json.loads(SPECS_PATH.read_text())["nodes"]


def _default_inputs(params: list[dict]) -> dict:
    # initializeDefaultNodeData: input.default || ''
    return {p["name"]: (p.get("default") if p.get("default") not in (None, False, 0, "") else "") for p in params}


def init_node(name: str, node_id: str, position: dict) -> dict:
    """Python port of Flowise's initNode() for agentflow nodes."""
    spec = copy.deepcopy(specs()[name])
    inputs = spec.get("inputs", []) or []
    input_params, input_anchors = [], []
    for p in inputs:
        p = {**p, "id": f"{node_id}-input-{p['name']}-{p['type']}"}
        (input_params if p["type"] in WHITELIST_TYPES else input_anchors).append(p)
    if spec.get("credential"):
        cred = spec["credential"]
        input_params.insert(0, {**cred, "id": f"{node_id}-input-{cred['name']}-{cred['type']}"})

    if spec.get("hideOutput"):
        output_anchors = []
    elif spec.get("outputs"):
        output_anchors = [{"id": f"{node_id}-output-{i}", "label": spec["label"], "name": spec["name"]}
                          for i, _ in enumerate(spec["outputs"])]
    else:
        output_anchors = [{"id": f"{node_id}-output-{spec['name']}", "label": spec["label"], "name": spec["name"]}]

    data = {
        "id": node_id,
        "label": spec["label"],
        "version": spec["version"],
        "name": spec["name"],
        "type": spec["type"],
        "color": spec.get("color"),
        "baseClasses": spec.get("baseClasses", []),
        "category": spec.get("category"),
        "description": spec.get("description", ""),
        "inputParams": input_params,
        "inputAnchors": input_anchors,
        "inputs": _default_inputs(inputs),
        "outputAnchors": output_anchors,
        "outputs": {spec["name"]: ""} if spec.get("outputs") else {},
        "selected": False,
    }
    if spec.get("hideInput"):
        data["hideInput"] = True
    if spec.get("hideOutput"):
        data["hideOutput"] = True
    node_type = "stickyNote" if name == "stickyNoteAgentflow" else "agentFlow"
    return {"id": node_id, "type": node_type, "position": position, "data": data,
            "width": 200, "height": 70, "selected": False, "dragging": False,
            "positionAbsolute": dict(position)}


def html(text: str) -> str:
    """Flowise rich-text inputs store paragraphs as <p>..</p>."""
    text = str(text)
    return "".join(f"<p>{line}</p>" for line in text.split("\n")) if text else ""


class FlowiseBuilder:
    def __init__(self, plan: dict):
        self.plan = plan
        self.nodes: list[dict] = []
        self.edges: list[dict] = []
        self.counters: dict[str, int] = {}
        self.warnings: list[dict] = []  # {level, code, node, message}
        self.post_import: list[str] = []
        self.ref_map: dict[str, callable] = {}  # plan node id -> fn(path)->flowise ref text
        self.entry: dict[str, str] = {}  # plan id -> first flowise node id
        self.exit: dict[str, list[tuple[str, str]]] = {}  # plan id -> [(flowise id, sourceHandle)] per branch
        self.col = 0

    # -- helpers ------------------------------------------------------------
    def new_id(self, name: str) -> str:
        n = self.counters.get(name, 0)
        self.counters[name] = n + 1
        return f"{name}_{n}"

    def add(self, name: str, label: str | None = None, row: int = 0) -> dict:
        nid = self.new_id(name)
        node = init_node(name, nid, {"x": 80 + self.col * X_STEP, "y": 100 + row * Y_ROW})
        if label:
            node["data"]["label"] = label
        self.nodes.append(node)
        self.col += 1
        return node

    def connect(self, src: dict, dst: dict, handle: str | None = None, label: str | None = None) -> None:
        sh = handle or src["data"]["outputAnchors"][0]["id"]
        edge = {
            "source": src["id"], "sourceHandle": sh, "target": dst["id"], "targetHandle": dst["id"],
            "data": {"sourceColor": src["data"]["color"], "targetColor": dst["data"]["color"], "isHumanInput": False},
            "type": "agentFlow", "id": f"{src['id']}-{sh}-{dst['id']}-{dst['id']}",
        }
        if label is not None:
            edge["data"]["edgeLabel"] = label
        self.edges.append(edge)

    def warn(self, node: str, code: str, message: str, level: str = "warning") -> None:
        self.warnings.append({"level": level, "code": code, "node": node, "message": message, "target": "flowise"})

    def render(self, value):
        """Rewrite plan references into Flowise variable syntax."""
        def fn(root, path):
            if root == "env":
                return "{{ $vars." + (path[0] if path else "UNSET") + " }}"
            r = self.ref_map.get(root)
            if r is None:
                return "{{ " + ".".join([root, *path]) + " }}"
            return r(path)
        return substitute(value, fn)

    def model_config(self, prefix: str, model: str = "qwen3:4b") -> dict:
        return {"modelName": model, "baseUrl": "http://localhost:11434", "temperature": 0.2,
                "streaming": True, f"{prefix}Model": OLLAMA_MODEL}

    # -- build --------------------------------------------------------------
    def build(self) -> dict:
        plan = self.plan
        nodes = node_map(plan)
        trig = trigger_of(plan)
        start = self.add("startAgentflow", "Start")
        self._configure_start(start, trig)
        if trig:
            self.entry[trig["id"]] = start["id"]
            self.exit[trig["id"]] = [(start["id"], start["data"]["outputAnchors"][0]["id"])]
            extra = self._trigger_followup(trig, start)
            if extra:
                self.exit[trig["id"]] = [(extra["id"], extra["data"]["outputAnchors"][0]["id"])]

        row_of: dict[str, int] = {}
        for nid in topo_order(plan):
            n = nodes[nid]
            if trig and nid == trig["id"]:
                continue
            nt = catalog.get(n["type"])
            if nt is None:
                continue
            incoming = [e for e in plan["edges"] if e["to"] == nid]
            row = 0
            for e in incoming:
                if e.get("branch") == "false":
                    row = max(row, row_of.get(e["from"], 0) + 1)
                else:
                    row = max(row, row_of.get(e["from"], 0))
            row_of[nid] = row
            first, last = self._emit(n, nt, row)
            self.entry[nid] = first["id"]
            if n["type"] == "condition":
                self.exit[nid] = [(first["id"], f"{first['id']}-output-0"), (first["id"], f"{first['id']}-output-1")]
            elif last is not None and last["data"]["outputAnchors"]:
                self.exit[nid] = [(last["id"], last["data"]["outputAnchors"][0]["id"])]
            else:
                self.exit[nid] = []

        # wire plan edges
        by_id = {n["id"]: n for n in self.nodes}
        for e in plan["edges"]:
            if e["from"] not in self.exit or e["to"] not in self.entry:
                continue
            exits = self.exit[e["from"]]
            if not exits:
                self.warn(e["from"], "W_FLOWISE_TERMINAL",
                          f"'{e['from']}' maps to a terminal Flowise node; edge to '{e['to']}' dropped", "error")
                continue
            if nodes[e["from"]]["type"] == "condition":
                idx = 1 if e.get("branch") == "false" else 0
                src_id, handle = exits[idx]
                self.connect(by_id[src_id], by_id[self.entry[e["to"]]], handle, str(idx))
            else:
                src_id, handle = exits[0]
                self.connect(by_id[src_id], by_id[self.entry[e["to"]]], handle)

        flow = {"nodes": self.nodes, "edges": self.edges, "viewport": {"x": 0, "y": 0, "zoom": 0.8}}
        export = {
            "AgentFlowV2": [{
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "flowforge:" + json.dumps(plan, sort_keys=True))),
                "name": plan.get("name", "FlowForge automation")[:100],
                "flowData": json.dumps(flow),
                "type": "AGENTFLOW",
            }]
        }
        return export

    def _configure_start(self, start: dict, trig: dict | None) -> None:
        inp = start["data"]["inputs"]
        t = trig["type"] if trig else "manual"
        p = trig.get("params", {}) if trig else {}
        tid = trig["id"] if trig else "trigger"
        if t == "form_submitted":
            inp["startInputType"] = "formInput"
            inp["formTitle"] = p.get("title", "Form")
            inp["formDescription"] = ""
            fields = [f.strip() for f in str(p.get("fields", "")).split(",") if f.strip()]
            inp["formInputTypes"] = [{"type": "string", "label": f.replace("_", " ").title(), "name": f, "addOptions": ""}
                                     for f in fields]
            self.ref_map[tid] = lambda path: ("{{ current_date_time }}" if path[:1] == ["submitted_at"]
                                              else "{{ $form." + ".".join(path) + " }}")
        elif t == "webhook":
            inp["startInputType"] = "webhookTrigger"
            inp["webhookMethod"] = p.get("method", "POST")
            inp["webhookContentType"] = "application/json"
            inp["webhookInputMode"] = "payload"
            inp["webhookResponseMode"] = "sync"
            self.ref_map[tid] = lambda path: "{{ $webhook." + ".".join(path or ["body"]) + " }}"
            self.post_import.append("Webhook URL after saving: <FLOWISE_URL>/api/v1/webhook/<flowId> "
                                    f"({p.get('method', 'POST')}). Enable webhook auth on the Start node for production.")
        elif t in ("schedule", "email_received"):
            cron = p.get("cron", "*/5 * * * *") if t == "schedule" else "*/5 * * * *"
            inp["startInputType"] = "scheduleInput"
            inp["scheduleType"] = "cronExpression"
            inp["scheduleCronExpression"] = cron
            inp["scheduleTimezone"] = p.get("timezone", "UTC") if t == "schedule" else "UTC"
            inp["scheduleInputMode"] = "text"
            inp["scheduleDefaultInput"] = "Scheduled run"
            self.ref_map[tid] = lambda path: "{{ current_date_time }}"
            if t == "email_received":
                self.warn(tid, "W_FLOWISE_POLLING",
                          "Flowise has no email-arrival event: the flow runs every 5 minutes and an Agent with "
                          "the Gmail tool fetches the newest matching email.")
            self.post_import.append(f"Enable the schedule on the Start node (cron '{cron}').")
        else:
            inp["startInputType"] = "chatInput"
            self.ref_map[tid] = lambda path: "{{ question }}"

    def _trigger_followup(self, trig: dict, start: dict) -> dict | None:
        """email_received needs a fetching step after Start (Flowise polls instead of listening)."""
        if trig["type"] != "email_received":
            return None
        query = trig["params"].get("query", "is:unread")
        agent = self.add("agentAgentflow", "Fetch Email")
        inp = agent["data"]["inputs"]
        inp["agentModel"] = OLLAMA_MODEL
        inp["agentModelConfig"] = self.model_config("agent")
        inp["agentMessages"] = [{"role": "system", "content": html(
            "You fetch emails. Use the Gmail tool: list messages with the query "
            f"'{query}' (maxResults 1), then get that message. Return its fields exactly; "
            "never invent an email. If none match, return empty strings and has_attachments=false.")}]
        inp["agentTools"] = [{
            "agentSelectedTool": "gmail",
            "agentSelectedToolRequiresHumanInput": "",
            "agentSelectedToolConfig": {"gmailType": "messages",
                                        "messageActions": json.dumps(["listMessages", "getMessage"]),
                                        "agentSelectedTool": "gmail"},
        }]
        inp["agentEnableMemory"] = False
        inp["agentUserMessage"] = html(f"Fetch the newest email matching: {query}")
        inp["agentStructuredOutput"] = [
            {"key": k, "type": "boolean" if k == "has_attachments" else "string", "enumValues": "",
             "jsonSchema": "", "description": d}
            for k, d in [("message_id", "Gmail message id"), ("from", "Sender"), ("subject", "Subject"),
                         ("body", "Plain-text body"), ("has_attachments", "Has attachments"),
                         ("attachments", "JSON list of attachment names")]
        ]
        self.connect(start, agent)
        aid = agent["id"]
        self.ref_map[trig["id"]] = lambda path: "{{ " + aid + ".output." + ".".join(path or ["body"]) + " }}"
        self.post_import.append(f"{aid} ('Fetch Email'): connect a Gmail credential to the Gmail tool and pick the model.")
        return agent

    # -- per node -----------------------------------------------------------
    def _emit(self, n: dict, nt, row: int) -> tuple[dict, dict | None]:
        t, p, nid = n["type"], self.render(n["params"]), n["id"]
        label = n.get("label") or nt.label

        def ref_content(node):
            fid = node["id"]
            self.ref_map[nid] = lambda path: "{{ " + fid + " }}"

        if t == "send_email":
            node = self._tool(label, "gmail", {"gmailType": "messages", "messageActions": json.dumps(["sendMessage"])},
                              {"to": p.get("to", ""), "subject": p.get("subject", ""), "body": p.get("body", "")}, row)
            ref_content(node)
            return node, node
        if t == "drive_save":
            args = {"name": p.get("file_name", ""), "content": p.get("content", ""), "mimeType": p.get("mime_type", "")}
            if p.get("folder_id"):
                args["parents"] = p["folder_id"]
            node = self._tool(label, "googleDriveTool", {"driveType": "file", "fileActions": json.dumps(["createFile"])}, args, row)
            if any(r == "trigger" or "attachment" in "".join(pp) or "first_content" in pp for r, pp in refs_in(n["params"].get("content", ""))):
                self.warn(nid, "W_FLOWISE_BINARY",
                          "Flowise's Drive tool uploads text content; binary attachments are passed as text. "
                          "Check the uploaded file in Drive before relying on it.")
            ref_content(node)
            return node, node
        if t == "drive_list":
            node = self._tool(label, "googleDriveTool", {"driveType": "file", "fileActions": json.dumps(["listFiles"])},
                              {"query": p.get("query", "")}, row)
            ref_content(node)
            return node, node
        if t == "sheets_append":
            values = json.dumps([[v.strip() for v in str(p.get("values", "")).split(",")]])
            node = self._tool(label, "googleSheetsTool", {"sheetsType": "values", "valuesActions": json.dumps(["appendValues"])},
                              {"spreadsheetId": p.get("spreadsheet_id", ""), "range": p.get("range", "Sheet1!A1"),
                               "values": values}, row)
            ref_content(node)
            return node, node
        if t == "sheets_read":
            node = self._tool(label, "googleSheetsTool", {"sheetsType": "values", "valuesActions": json.dumps(["getValues"])},
                              {"spreadsheetId": p.get("spreadsheet_id", ""), "range": p.get("range", "Sheet1!A1:Z100")}, row)
            ref_content(node)
            return node, node
        if t in ("slack_message", "discord_message", "teams_message", "http_request"):
            node = self.add("httpAgentflow", label, row)
            inp = node["data"]["inputs"]
            if t == "http_request":
                inp["method"] = p.get("method", "GET")
                inp["url"] = p.get("url", "")
                body = p.get("body", "")
                if body:
                    inp["bodyType"] = "json"
                    inp["body"] = body if isinstance(body, str) else json.dumps(body)
            else:
                key = "content" if t == "discord_message" else "text"
                inp["method"] = "POST"
                inp["url"] = p.get("webhook_url", "")
                inp["headers"] = [{"key": "Content-Type", "value": "application/json"}]
                inp["bodyType"] = "json"
                inp["body"] = json.dumps({key: p.get(key, "")})
                self.post_import.append(f"{node['id']} ({label}): create the Flowise variable used in its URL "
                                        f"({p.get('webhook_url', '')}) under Variables.")
            inp["responseType"] = "json"
            fid = node["id"]
            self.ref_map[nid] = lambda path: ("{{ " + fid + ".output.http.status }}" if path[:1] == ["status"]
                                              else "{{ " + fid + ".output.http.data }}" if path[:1] == ["body"]
                                              else "{{ " + fid + " }}")
            return node, node
        if t == "condition":
            node = self.add("conditionAgentflow", label, row)
            node["data"]["inputs"]["conditions"] = [self._condition(n["params"], p)]
            self.ref_map[nid] = lambda path: "true"
            return node, node
        if t in ("transform", "extract_attachments"):
            node = self.add("customFunctionAgentflow", label, row)
            inp = node["data"]["inputs"]
            if t == "transform":
                fields = p.get("fields") if isinstance(p.get("fields"), dict) else {}
                inp["customFunctionInputVariables"] = [{"variableName": k, "variableValue": html(v)} for k, v in fields.items()]
                inp["customFunctionJavascriptFunction"] = (
                    "return JSON.stringify({" + ", ".join(f"{json.dumps(k)}: ${k}" for k in fields) + "});")
            else:
                inp["customFunctionInputVariables"] = [{"variableName": "source", "variableValue": html(p.get("source", ""))},
                                                       {"variableName": "types", "variableValue": p.get("file_types", "")}]
                inp["customFunctionJavascriptFunction"] = (
                    "let items = $source;\n"
                    "try { if (typeof items === 'string') items = JSON.parse(items) } catch (e) { items = [] }\n"
                    "if (!Array.isArray(items)) items = [];\n"
                    "const types = String($types || '').split(',').map(s => s.trim().toLowerCase()).filter(Boolean);\n"
                    "const keep = items.filter(a => { const nm = typeof a === 'string' ? a : (a.name || '');\n"
                    "  return !types.length || types.some(t => nm.toLowerCase().endsWith('.' + t)) });\n"
                    "return JSON.stringify({ files: keep, count: keep.length });")
                self.warn(nid, "W_FLOWISE_APPROX",
                          "Attachment extraction in Flowise works on attachment metadata returned by the Gmail tool; "
                          "Gmail's tool has no attachment-download action.")
            ref_content(node)
            return node, node
        if t == "llm_summarize":
            node = self.add("llmAgentflow", label, row)
            inp = node["data"]["inputs"]
            inp["llmModel"] = OLLAMA_MODEL
            inp["llmModelConfig"] = self.model_config("llm", n["params"].get("model", "qwen3:4b"))
            inp["llmMessages"] = [{"role": "system", "content": html(p.get("instruction", "Summarize."))}]
            inp["llmEnableMemory"] = False
            inp["llmUserMessage"] = html(p.get("text", ""))
            ref_content(node)
            return node, node
        if t == "notify":
            node = self.add("directReplyAgentflow", label, row)
            node["data"]["inputs"]["directReplyMessage"] = html(p.get("message", ""))
            ref_content(node)
            return node, None
        if t == "create_task":
            node = self._tool(label, "jiraTool", {}, {"summary": p.get("title", ""), "description": p.get("description", ""),
                                                      "project": p.get("project", "")}, row)
            self.warn(nid, "W_FLOWISE_APPROX", "Jira tool: pick the 'create issue' action and check argument names in the canvas.")
            ref_content(node)
            return node, node
        if t == "calendar_create":
            node = self._tool(label, "googleCalendarTool", {}, {"summary": p.get("title", ""), "start": p.get("start", ""),
                                                                "attendees": p.get("attendees", "")}, row)
            self.warn(nid, "W_FLOWISE_APPROX", "Google Calendar tool: pick the 'create event' action and check arguments in the canvas.")
            ref_content(node)
            return node, node
        if t == "sms_message":
            node = self._tool(label, "awsSNS", {}, {"message": p.get("body", ""), "phoneNumber": p.get("to", "")}, row)
            self.warn(nid, "W_FLOWISE_APPROX", "SMS uses the AWS SNS tool in Flowise (Twilio in n8n/Python); check its arguments in the canvas.")
            ref_content(node)
            return node, node
        # mapping 'none': keep the graph connected and make the gap visible on the canvas
        note = self.add("stickyNoteAgentflow", f"Unsupported: {label}", row)
        note["data"]["inputs"]["note"] = (f"FlowForge step '{nid}' ({t}) has no Flowise equivalent and was skipped here. "
                                          "The n8n workflow and Python script still perform it.")
        self.warn(nid, "W_FLOWISE_UNSUPPORTED", f"'{t}' has no Flowise node; step skipped in the Flowise flow "
                                                "(n8n and Python outputs still include it).")
        self._passthrough(nid)
        return note, None

    def _passthrough(self, nid: str) -> None:
        """An unsupported step is bypassed: its predecessors connect straight to its successors."""
        plan = self.plan
        ins = [e for e in plan["edges"] if e["to"] == nid]
        outs = [e for e in plan["edges"] if e["from"] == nid]
        plan["edges"] = [e for e in plan["edges"] if nid not in (e["from"], e["to"])]
        for a in ins:
            for b in outs:
                edge = {"from": a["from"], "to": b["to"]}
                if a.get("branch"):
                    edge["branch"] = a["branch"]
                if edge not in plan["edges"]:
                    plan["edges"].append(edge)

    def _tool(self, label: str, tool: str, config: dict, args: dict, row: int) -> dict:
        node = self.add("toolAgentflow", label, row)
        inp = node["data"]["inputs"]
        inp["toolAgentflowSelectedTool"] = tool
        inp["toolInputArgs"] = [{"inputArgName": k, "inputArgValue": html(v) if isinstance(v, str) else v}
                                for k, v in args.items()]
        inp["toolUpdateState"] = ""
        inp["toolAgentflowSelectedToolConfig"] = {**config, "toolAgentflowSelectedTool": tool}
        self.post_import.append(f"{node['id']} ({label}): connect the credential for the '{tool}' tool.")
        return node

    @staticmethod
    def _condition(raw: dict, rendered: dict) -> dict:
        op = raw.get("operator", "contains")
        v1, v2 = html(rendered.get("field", "")), html(str(rendered.get("value", "")))
        numeric = {"greater_than": "larger", "less_than": "smaller"}
        string_ops = {"contains": "contains", "not_contains": "notContains", "equals": "equal",
                      "not_equals": "notEqual", "starts_with": "startsWith", "ends_with": "endsWith",
                      "is_empty": "isEmpty", "is_not_empty": "notEmpty"}
        if op in numeric:
            return {"type": "number", "value1": v1, "operation": numeric[op], "value2": v2}
        return {"type": "string", "value1": v1, "operation": string_ops.get(op, "contains"), "value2": v2}


def generate(plan: dict) -> dict:
    """Return {"export": <ExportData>, "warnings": [...], "post_import": [...]}."""
    b = FlowiseBuilder(copy.deepcopy(plan))
    export = b.build()
    return {"export": export, "warnings": b.warnings, "post_import": list(dict.fromkeys(b.post_import))}


def flow_data(export: dict) -> dict:
    return json.loads(export["AgentFlowV2"][0]["flowData"])


