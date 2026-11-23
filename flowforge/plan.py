"""The structured plan shared by every generator.

A plan is plain JSON so the LLM can write it and humans can read it::

    {
      "name": "Invoice email to Drive and Slack",
      "request": "<original sentence>",
      "nodes": [
        {"id": "trigger", "type": "email_received", "params": {"query": "has:attachment"}},
        {"id": "save", "type": "drive_save", "params": {"file_name": "{{trigger.subject}}.pdf", ...}},
        ...
      ],
      "edges": [{"from": "trigger", "to": "save"}, {"from": "check", "to": "x", "branch": "true"}]
    }

Data mappings are written inline as references: ``{{node_id.field}}`` (dotted
paths allowed, e.g. ``{{trigger.body.email}}``) and ``{{env.NAME}}`` for
configuration and secrets, which are never stored in the plan itself.
"""
from __future__ import annotations

import copy
import re
from collections import deque
from typing import Any, Iterator

from . import catalog

REF_RE = re.compile(r"\{\{\s*([A-Za-z_][\w]*)((?:\.[\w-]+)*)\s*\}\}")
ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


def iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from iter_strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from iter_strings(v)


def refs_in(value: Any) -> list[tuple[str, list[str]]]:
    """All ``(root, [path...])`` references inside a param value."""
    found = []
    for s in iter_strings(value):
        for m in REF_RE.finditer(s):
            path = [p for p in m.group(2).split(".") if p]
            found.append((m.group(1), path))
    return found


def substitute(value: Any, fn) -> Any:
    """Rewrite every reference in ``value`` with ``fn(root, path) -> str``."""
    if isinstance(value, str):
        return REF_RE.sub(lambda m: fn(m.group(1), [p for p in m.group(2).split(".") if p]), value)
    if isinstance(value, dict):
        return {k: substitute(v, fn) for k, v in value.items()}
    if isinstance(value, list):
        return [substitute(v, fn) for v in value]
    return value


def node_map(plan: dict) -> dict[str, dict]:
    return {n["id"]: n for n in plan.get("nodes", []) if isinstance(n, dict) and "id" in n}


def trigger_of(plan: dict) -> dict | None:
    for n in plan.get("nodes", []):
        nt = catalog.get(n.get("type", ""))
        if nt and nt.kind == "trigger":
            return n
    return None


def successors(plan: dict, node_id: str) -> list[dict]:
    return [e for e in plan.get("edges", []) if e.get("from") == node_id]


def predecessors(plan: dict, node_id: str) -> list[dict]:
    return [e for e in plan.get("edges", []) if e.get("to") == node_id]


def topo_order(plan: dict) -> list[str]:
    """Kahn topological order. Nodes on a cycle are omitted (validator reports them)."""
    ids = [n["id"] for n in plan.get("nodes", [])]
    indeg = {i: 0 for i in ids}
    for e in plan.get("edges", []):
        if e.get("to") in indeg and e.get("from") in indeg:
            indeg[e["to"]] += 1
    q = deque(i for i in ids if indeg[i] == 0)
    order = []
    while q:
        i = q.popleft()
        order.append(i)
        for e in successors(plan, i):
            t = e.get("to")
            if t in indeg:
                indeg[t] -= 1
                if indeg[t] == 0:
                    q.append(t)
    return order


def ancestors(plan: dict, node_id: str) -> set[str]:
    seen: set[str] = set()
    stack = [e["from"] for e in predecessors(plan, node_id)]
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        stack.extend(e["from"] for e in predecessors(plan, n))
    return seen


def find_cycle(plan: dict) -> list[str] | None:
    nodes = node_map(plan)
    color = {i: 0 for i in nodes}  # 0 new, 1 on stack, 2 done
    path: list[str] = []

    def dfs(u: str) -> list[str] | None:
        color[u] = 1
        path.append(u)
        for e in successors(plan, u):
            v = e.get("to")
            if v not in color:
                continue
            if color[v] == 1:
                return path[path.index(v):] + [v]
            if color[v] == 0:
                c = dfs(v)
                if c:
                    return c
        path.pop()
        color[u] = 2
        return None

    for i in nodes:
        if color[i] == 0:
            c = dfs(i)
            if c:
                return c
    return None


def slug(text: str, fallback: str = "step") -> str:
    s = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    if not s or not s[0].isalpha():
        s = f"{fallback}_{s}" if s else fallback
    return s[:40]


def normalize(plan: dict) -> dict:
    """Make an LLM- or hand-written plan structurally regular, without fixing semantics.

    - fills ``params`` with catalog defaults for missing optional/required params
    - makes ids lowercase slugs and unique
    - if no edges are given, chains nodes linearly in listed order
    """
    plan = copy.deepcopy(plan) if isinstance(plan, dict) else {}
    plan.setdefault("name", "Untitled automation")
    nodes = [n for n in plan.get("nodes", []) if isinstance(n, dict)]
    rename: dict[str, str] = {}
    used: set[str] = set()
    for i, n in enumerate(nodes):
        old = str(n.get("id") or f"step_{i}")
        new = old if ID_RE.match(old) else slug(old)
        base, k = new, 2
        while new in used:
            new, k = f"{base}_{k}", k + 1
        used.add(new)
        rename[old] = new
        n["id"] = new
        n["type"] = str(n.get("type", "")).strip()
        params = n.get("params") if isinstance(n.get("params"), dict) else {}
        nt = catalog.get(n["type"])
        if nt:
            for p in nt.params:
                if p.name not in params and p.default is not None:
                    params[p.name] = copy.deepcopy(p.default)
        n["params"] = params
    plan["nodes"] = nodes

    edges = plan.get("edges")
    if not edges:
        edges = [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:])]
    clean = []
    for e in edges:
        if not isinstance(e, dict):
            continue
        f, t = rename.get(str(e.get("from")), e.get("from")), rename.get(str(e.get("to")), e.get("to"))
        edge = {"from": f, "to": t}
        br = e.get("branch")
        if br is not None:
            edge["branch"] = str(br).lower()
        if edge not in clean:
            clean.append(edge)
    plan["edges"] = clean

    # rewrite references to renamed ids
    if any(k != v for k, v in rename.items()):
        def fix(root, path):
            r = rename.get(root, root)
            return "{{" + ".".join([r, *path]) + "}}"
        for n in nodes:
            n["params"] = substitute(n["params"], fix)
    return plan


def summarize(plan: dict) -> str:
    """One line per node, for the UI and the CLI."""
    nodes = node_map(plan)
    lines = []
    for nid in topo_order(plan) or list(nodes):
        n = nodes[nid]
        nt = catalog.get(n["type"])
        label = n.get("label") or (nt.label if nt else n["type"])
        incoming = predecessors(plan, nid)
        via = ""
        if incoming:
            via = " <- " + ", ".join(e["from"] + (f"[{e['branch']}]" if e.get("branch") else "") for e in incoming)
        lines.append(f"{nid}: {label} ({n['type']}){via}")
    return "\n".join(lines)
