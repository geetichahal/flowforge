"""Static checks for the plan and for each generated output.

Every check produces an Issue with a stable ``code`` so the self-correction
loop (and the planner's repair step) can act on it mechanically.
"""
from __future__ import annotations

import ast
import json
import re
from dataclasses import asdict, dataclass, field

from . import catalog
from .plan import ID_RE, ancestors, find_cycle, iter_strings, node_map, refs_in, trigger_of


@dataclass
class Issue:
    level: str  # error | warning | info
    code: str
    message: str
    node: str | None = None
    target: str = "plan"


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=list)

    def add(self, level, code, message, node=None, target="plan"):
        self.issues.append(Issue(level, code, message, node, target))

    def extend(self, other: "Report"):
        self.issues.extend(other.issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.level == "warning"]

    @property
    def ok(self) -> bool:
        return not self.errors

    def for_target(self, target: str) -> "Report":
        return Report([i for i in self.issues if i.target == target])

    def to_dict(self) -> dict:
        return {"ok": self.ok, "errors": len(self.errors), "warnings": len(self.warnings),
                "issues": [asdict(i) for i in self.issues]}

    def text(self) -> str:
        if not self.issues:
            return "No issues."
        return "\n".join(f"[{i.level.upper()}] {i.target}/{i.code}" + (f" ({i.node})" if i.node else "") + f": {i.message}"
                         for i in self.issues)


# ----------------------------------------------------------------- secrets
SECRET_PATTERNS = [
    ("Slack webhook", re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]{8,}")),
    ("Discord webhook", re.compile(r"discord(?:app)?\.com/api/webhooks/\d{6,}/[\w-]{20,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("OpenAI-style key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("Google OAuth token", re.compile(r"\bya29\.[0-9A-Za-z_-]{20,}")),
    ("Private key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    ("Bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]{24,}")),
    ("Password assignment", re.compile(r"(?i)\b(password|passwd|api[_-]?key|secret)\s*[=:]\s*['\"]?[^\s'\"{<]{6,}")),
]


def scan_secrets(text: str) -> list[str]:
    return [name for name, rx in SECRET_PATTERNS if rx.search(text)]


# ----------------------------------------------------------------- plan
def validate_plan(plan: dict) -> Report:
    r = Report()
    if not isinstance(plan, dict):
        r.add("error", "E_PLAN_SHAPE", "Plan must be a JSON object")
        return r
    nodes_list = plan.get("nodes")
    if not isinstance(nodes_list, list) or not nodes_list:
        r.add("error", "E_NO_NODES", "Plan has no nodes")
        return r

    seen: set[str] = set()
    for n in nodes_list:
        nid = n.get("id")
        if not isinstance(nid, str) or not ID_RE.match(nid):
            r.add("error", "E_BAD_ID", f"Invalid node id {nid!r} (use lowercase letters, digits, _)", nid)
        if nid in seen:
            r.add("error", "E_DUPLICATE_ID", f"Duplicate node id {nid!r}", nid)
        seen.add(nid)
        if catalog.get(n.get("type", "")) is None:
            r.add("error", "E_UNKNOWN_TYPE", f"Unknown node type {n.get('type')!r}; must be one of the catalog", nid)

    nodes = node_map(plan)
    triggers = [n for n in nodes_list if (nt := catalog.get(n.get("type", ""))) and nt.kind == "trigger"]
    if not triggers:
        r.add("error", "E_NO_TRIGGER", "Plan needs exactly one trigger node")
    elif len(triggers) > 1:
        r.add("error", "E_MULTIPLE_TRIGGERS", "Plan has more than one trigger: " + ", ".join(t["id"] for t in triggers))

    edges = plan.get("edges", [])
    for e in edges:
        for end in ("from", "to"):
            if e.get(end) not in nodes:
                r.add("error", "E_DANGLING_EDGE", f"Edge {e.get('from')}->{e.get('to')}: '{e.get(end)}' does not exist",
                      e.get("from"))
        src = nodes.get(e.get("from"))
        if src and src.get("type") == "condition":
            if e.get("branch") not in ("true", "false"):
                r.add("error", "E_BRANCH_LABEL", f"Edge from condition '{src['id']}' must have branch 'true' or 'false'", src["id"])
        elif e.get("branch") is not None:
            r.add("warning", "W_STRAY_BRANCH", f"Edge {e.get('from')}->{e.get('to')} has a branch but its source is not a condition",
                  e.get("from"))
        if triggers and e.get("to") == triggers[0]["id"]:
            r.add("error", "E_TRIGGER_INCOMING", "The trigger cannot have incoming edges", triggers[0]["id"])
        if e.get("from") == e.get("to"):
            r.add("error", "E_SELF_LOOP", f"Node '{e.get('from')}' connects to itself", e.get("from"))

    cycle = find_cycle(plan)
    if cycle:
        r.add("error", "E_CYCLE", "Cycle detected: " + " -> ".join(cycle), cycle[0])

    if triggers and len(nodes_list) == 1:
        r.add("error", "E_NO_ACTIONS", "Plan has a trigger but no actions")

    # reachability from trigger
    if triggers and not cycle:
        reach, stack = set(), [triggers[0]["id"]]
        while stack:
            u = stack.pop()
            if u in reach:
                continue
            reach.add(u)
            stack.extend(e["to"] for e in edges if e.get("from") == u and e.get("to") in nodes)
        for nid in nodes:
            if nid not in reach:
                r.add("error", "E_UNREACHABLE", f"Node '{nid}' is not reachable from the trigger", nid)

    for nid, n in nodes.items():
        nt = catalog.get(n.get("type", ""))
        if nt is None:
            continue
        params = n.get("params") or {}
        for p in nt.params:
            v = params.get(p.name)
            if p.required and (v is None or (isinstance(v, str) and not v.strip()) or v == {}):
                r.add("error", "E_MISSING_PARAM", f"'{nid}' ({nt.type}) is missing required parameter '{p.name}'", nid)
                continue
            if v is None:
                continue
            if p.kind == "enum" and isinstance(v, str) and not refs_in(v) and v not in p.options:
                r.add("error", "E_BAD_ENUM", f"'{nid}'.{p.name}={v!r} must be one of {list(p.options)}", nid)
            if p.kind == "number" and not refs_in(v):
                try:
                    float(v)
                except (TypeError, ValueError):
                    r.add("error", "E_BAD_NUMBER", f"'{nid}'.{p.name}={v!r} must be a number", nid)
            if p.kind == "mapping" and not isinstance(v, dict):
                r.add("error", "E_BAD_MAPPING", f"'{nid}'.{p.name} must be an object of field -> template", nid)
            if p.kind == "secret" and isinstance(v, str) and not re.fullmatch(r"\s*\{\{\s*env\.[A-Z][A-Z0-9_]*\s*\}\}\s*", v):
                r.add("error", "E_LITERAL_SECRET",
                      f"'{nid}'.{p.name} must be an environment reference like {{{{env.NAME}}}}, not a literal value", nid)
        unknown = set(params) - {p.name for p in nt.params}
        if unknown:
            r.add("warning", "W_UNKNOWN_PARAM", f"'{nid}' has parameters the catalog does not define: {sorted(unknown)}", nid)
        if nt.type == "condition" and not any(e.get("from") == nid for e in edges):
            r.add("warning", "W_CONDITION_NO_BRANCH", f"Condition '{nid}' has no outgoing branches", nid)

        # data mappings
        anc = ancestors(plan, nid) if not cycle else set(nodes)
        for root, path in refs_in(params):
            if root == "env":
                if not path or not re.fullmatch(r"[A-Z][A-Z0-9_]*", path[0]):
                    r.add("error", "E_BAD_ENV_REF", f"'{nid}' has malformed env reference env.{'.'.join(path)}", nid)
                continue
            if root not in nodes:
                r.add("error", "E_BAD_REF", f"'{nid}' references unknown step '{root}'", nid)
            elif root not in anc:
                r.add("error", "E_REF_NOT_UPSTREAM", f"'{nid}' references '{root}', which does not run before it", nid)
            elif path:
                outs = catalog.outputs_for(nodes[root])
                if path[0] not in outs:
                    r.add("error", "E_BAD_REF_FIELD",
                          f"'{nid}' references {root}.{path[0]}, but '{root}' outputs only {sorted(outs)}", nid)

        for s in iter_strings(params):
            hits = scan_secrets(s)
            if hits:
                r.add("error", "E_EMBEDDED_CREDENTIAL", f"'{nid}' contains what looks like a {hits[0]}; use {{{{env.NAME}}}}", nid)
    return r


# ----------------------------------------------------------------- flowise
def validate_flowise(export: dict, plan: dict | None = None) -> Report:
    from .generators.flowise import specs
    r = Report()
    T = "flowise"
    flows = export.get("AgentFlowV2") if isinstance(export, dict) else None
    if not isinstance(flows, list) or len(flows) != 1:
        r.add("error", "E_FW_SHAPE", "Export must contain exactly one entry under 'AgentFlowV2'", target=T)
        return r
    f = flows[0]
    for k in ("id", "name", "flowData", "type"):
        if k not in f:
            r.add("error", "E_FW_SHAPE", f"Flow entry missing '{k}'", target=T)
    if f.get("type") != "AGENTFLOW":
        r.add("error", "E_FW_SHAPE", "Flow type must be 'AGENTFLOW'", target=T)
    if "workspaceId" in f:
        r.add("error", "E_FW_WORKSPACE", "Never put a workspaceId in the import file", target=T)
    if not isinstance(f.get("flowData"), str):
        r.add("error", "E_FW_FLOWDATA", "flowData must be a JSON string, not an object", target=T)
        return r
    try:
        flow = json.loads(f["flowData"])
    except ValueError as exc:
        r.add("error", "E_FW_FLOWDATA", f"flowData is not valid JSON: {exc}", target=T)
        return r
    nodes, edges = flow.get("nodes", []), flow.get("edges", [])
    by_id = {n.get("id"): n for n in nodes}
    known = specs()
    starts = [n for n in nodes if n.get("data", {}).get("name") == "startAgentflow"]
    if len(starts) != 1:
        r.add("error", "E_FW_START", f"Flow must have exactly one Start node (found {len(starts)})", target=T)
    for n in nodes:
        d = n.get("data", {})
        name = d.get("name")
        if name not in known:
            r.add("error", "E_FW_UNKNOWN_NODE", f"Node '{n.get('id')}' uses unknown Flowise node '{name}'", n.get("id"), T)
            continue
        if d.get("version") != known[name]["version"]:
            r.add("warning", "W_FW_VERSION", f"{n['id']}: version {d.get('version')} != installed {known[name]['version']}", n["id"], T)
        if n.get("id") != d.get("id") or not re.fullmatch(rf"{name}_\d+", str(n.get("id"))):
            r.add("error", "E_FW_NODE_ID", f"Node id '{n.get('id')}' must be '<name>_<n>' and match data.id", n.get("id"), T)
        for p in d.get("inputParams", []):
            if p.get("id") != f"{n['id']}-input-{p.get('name')}-{p.get('type')}":
                r.add("error", "E_FW_PARAM_ID", f"{n['id']}: inputParam id {p.get('id')!r} malformed", n["id"], T)
        if name == "toolAgentflow":
            tool = d.get("inputs", {}).get("toolAgentflowSelectedTool")
            if tool not in known:
                r.add("error", "E_FW_UNKNOWN_TOOL", f"{n['id']}: tool '{tool}' is not a known Flowise tool", n["id"], T)
            else:
                cfg = d["inputs"].get("toolAgentflowSelectedToolConfig") or {}
                if "FLOWISE_CREDENTIAL_ID" in cfg:
                    r.add("error", "E_FW_CREDENTIAL", f"{n['id']}: credential ids must not be in the file", n["id"], T)
                for k, v in cfg.items():
                    tool_inputs = {i["name"]: i for i in known[tool].get("inputs", [])}
                    if k in tool_inputs and tool_inputs[k]["type"] == "multiOptions":
                        allowed = {o["name"] for o in tool_inputs[k].get("options", [])}
                        bad = set(json.loads(v)) - allowed if isinstance(v, str) and v.startswith("[") else set()
                        if bad:
                            r.add("error", "E_FW_TOOL_ACTION", f"{n['id']}: unknown {tool} action(s) {sorted(bad)}", n["id"], T)
        if name == "startAgentflow":
            opts = {o["name"] for o in known[name]["inputs"][0]["options"]}
            if d.get("inputs", {}).get("startInputType") not in opts:
                r.add("error", "E_FW_START", f"startInputType must be one of {sorted(opts)}", n["id"], T)
    for e in edges:
        s, t = by_id.get(e.get("source")), by_id.get(e.get("target"))
        if not s or not t:
            r.add("error", "E_FW_EDGE", f"Edge {e.get('id')} references a missing node", target=T)
            continue
        anchors = {a["id"] for a in s["data"].get("outputAnchors", [])}
        if e.get("sourceHandle") not in anchors:
            r.add("error", "E_FW_EDGE", f"Edge sourceHandle {e.get('sourceHandle')!r} is not an output of {s['id']}", s["id"], T)
        if e.get("targetHandle") != t["id"]:
            r.add("error", "E_FW_EDGE", f"Agentflow targetHandle must equal the target id ({t['id']})", t["id"], T)
        if e.get("id") != f"{e['source']}-{e['sourceHandle']}-{e['target']}-{e['targetHandle']}":
            r.add("error", "E_FW_EDGE", f"Edge id {e.get('id')!r} does not follow the convention", s["id"], T)
        if e.get("type") != "agentFlow":
            r.add("error", "E_FW_EDGE", "Edge type must be 'agentFlow'", s["id"], T)
        if t["data"].get("name") == "startAgentflow":
            r.add("error", "E_FW_EDGE", "Start node cannot have incoming edges", t["id"], T)
    # reachability (sticky notes are annotations and may stand alone)
    if starts:
        reach, stack = set(), [starts[0]["id"]]
        while stack:
            u = stack.pop()
            if u in reach:
                continue
            reach.add(u)
            stack.extend(e["target"] for e in edges if e.get("source") == u)
        for n in nodes:
            if n["id"] not in reach and n["data"].get("name") != "stickyNoteAgentflow":
                r.add("error", "E_FW_UNREACHABLE", f"Node {n['id']} is not connected to Start", n["id"], T)
    # cycles
    adj: dict[str, list[str]] = {}
    for e in edges:
        adj.setdefault(e["source"], []).append(e["target"])
    state: dict[str, int] = {}

    def has_cycle(u):
        state[u] = 1
        for v in adj.get(u, []):
            if state.get(v) == 1 or (state.get(v) is None and has_cycle(v)):
                return True
        state[u] = 2
        return False
    if any(state.get(n) is None and has_cycle(n) for n in list(by_id)):
        r.add("error", "E_FW_CYCLE", "Flow graph contains a cycle", target=T)
    hits = scan_secrets(f["flowData"])
    if hits:
        r.add("error", "E_EMBEDDED_CREDENTIAL", f"Flow contains what looks like a {hits[0]}", target=T)
    return r


# ----------------------------------------------------------------- n8n
TRIGGER_TYPES_N8N = {"n8n-nodes-base.manualTrigger", "n8n-nodes-base.webhook", "n8n-nodes-base.scheduleTrigger",
                     "n8n-nodes-base.gmailTrigger", "n8n-nodes-base.formTrigger"}


def validate_n8n(wf: dict) -> Report:
    r = Report()
    T = "n8n"
    if not isinstance(wf, dict) or not isinstance(wf.get("nodes"), list) or not isinstance(wf.get("connections"), dict):
        r.add("error", "E_N8N_SHAPE", "Workflow must have 'nodes' (list) and 'connections' (object)", target=T)
        return r
    names = [n.get("name") for n in wf["nodes"]]
    if len(set(names)) != len(names):
        r.add("error", "E_N8N_DUP_NAME", "Node names must be unique", target=T)
    ids = [n.get("id") for n in wf["nodes"]]
    if len(set(ids)) != len(ids):
        r.add("error", "E_N8N_DUP_ID", "Node ids must be unique", target=T)
    for n in wf["nodes"]:
        for k, typ in (("id", str), ("name", str), ("type", str), ("typeVersion", (int, float)),
                       ("position", list), ("parameters", dict)):
            if not isinstance(n.get(k), typ):
                r.add("error", "E_N8N_NODE", f"Node {n.get('name')!r} field '{k}' missing or wrong type", n.get("name"), T)
        if isinstance(n.get("position"), list) and len(n["position"]) != 2:
            r.add("error", "E_N8N_NODE", f"Node {n.get('name')!r} position must be [x, y]", n.get("name"), T)
        if "credentials" in n:
            r.add("error", "E_N8N_CREDENTIAL", f"Node {n.get('name')!r} must not embed credentials", n.get("name"), T)
    if not any(n.get("type") in TRIGGER_TYPES_N8N for n in wf["nodes"]):
        r.add("error", "E_N8N_TRIGGER", "Workflow has no trigger node", target=T)
    name_set = set(names)
    for src, outs in wf["connections"].items():
        if src not in name_set:
            r.add("error", "E_N8N_CONNECTION", f"Connection source {src!r} is not a node", src, T)
        for kind, branches in outs.items():
            for branch in branches:
                for c in branch:
                    if c.get("node") not in name_set:
                        r.add("error", "E_N8N_CONNECTION", f"{src} -> {c.get('node')!r}: target does not exist", src, T)
                    if c.get("type") != kind:
                        r.add("error", "E_N8N_CONNECTION", f"{src} -> {c.get('node')}: type {c.get('type')!r} != {kind!r}", src, T)
    # every non-trigger, non-sub-node must have an incoming main connection
    incoming = {c["node"] for outs in wf["connections"].values() for branches in outs.values() for b in branches for c in b}
    for n in wf["nodes"]:
        if n.get("type") in TRIGGER_TYPES_N8N or n.get("type", "").startswith("@n8n/n8n-nodes-langchain.lm"):
            continue
        if n.get("name") not in incoming:
            r.add("error", "E_N8N_UNREACHABLE", f"Node {n.get('name')!r} has no incoming connection", n.get("name"), T)
    adj = {s: [c["node"] for b in o.get("main", []) for c in b] for s, o in wf["connections"].items()}
    state: dict[str, int] = {}

    def cyc(u):
        state[u] = 1
        for v in adj.get(u, []):
            if state.get(v) == 1 or (state.get(v) is None and cyc(v)):
                return True
        state[u] = 2
        return False
    if any(state.get(n) is None and cyc(n) for n in names):
        r.add("error", "E_N8N_CYCLE", "Workflow contains a cycle", target=T)
    hits = scan_secrets(json.dumps(wf))
    if hits:
        r.add("error", "E_EMBEDDED_CREDENTIAL", f"Workflow contains what looks like a {hits[0]}", target=T)
    return r


# ----------------------------------------------------------------- python
def validate_python(code: str, plan: dict | None = None) -> Report:
    r = Report()
    T = "python"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        r.add("error", "E_PY_SYNTAX", f"Line {exc.lineno}: {exc.msg}", target=T)
        return r
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    if "main" not in funcs:
        r.add("error", "E_PY_STRUCTURE", "Script has no main()", target=T)
    if plan:
        for nid in node_map(plan):
            if f"step_{nid}" not in funcs:
                r.add("error", "E_PY_STRUCTURE", f"No function for step '{nid}'", nid, T)
    allowed = {"argparse", "base64", "json", "os", "re", "sys", "time", "email", "requests", "http", "urllib", "datetime"}
    for n in ast.walk(tree):
        mods = []
        if isinstance(n, ast.Import):
            mods = [a.name.split(".")[0] for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods = [n.module.split(".")[0]]
        for m in mods:
            if m not in allowed:
                r.add("error", "E_PY_DEPENDENCY", f"Import of '{m}' (only stdlib + requests allowed)", target=T)
    hits = scan_secrets(code)
    if hits:
        r.add("error", "E_EMBEDDED_CREDENTIAL", f"Script contains what looks like a {hits[0]}", target=T)
    return r


def validate_all(plan: dict, outputs: dict) -> Report:
    r = validate_plan(plan)
    if "flowise" in outputs:
        r.extend(validate_flowise(outputs["flowise"]["export"], plan))
        for w in outputs["flowise"].get("warnings", []):
            r.add(w["level"], w["code"], w["message"], w.get("node"), "flowise")
    if "n8n" in outputs:
        r.extend(validate_n8n(outputs["n8n"]["workflow"]))
        for w in outputs["n8n"].get("warnings", []):
            r.add(w["level"], w["code"], w["message"], w.get("node"), "n8n")
    if "python" in outputs:
        r.extend(validate_python(outputs["python"]["code"], plan))
    return r
