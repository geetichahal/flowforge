"""Dry-run a plan against mock connectors and produce a step-by-step trace.

Nothing here touches the network. Each mock service records what it was asked
to do, so after a run you can inspect e.g. the mock outbox, the rows appended
to the mock spreadsheet, or the files saved to mock storage.
"""
from __future__ import annotations

import copy
import json
import time
from dataclasses import dataclass, field
from typing import Any

from . import catalog
from .plan import REF_RE, node_map, topo_order, trigger_of


# --------------------------------------------------------------------- mocks
@dataclass
class MockEmail:
    inbox: list[dict] = field(default_factory=lambda: [copy.deepcopy(catalog.CATALOG["email_received"].sample_output)])
    outbox: list[dict] = field(default_factory=list)

    def newest(self, query: str) -> dict | None:
        q = (query or "").lower()
        for m in self.inbox:
            if "has:attachment" in q and not m.get("has_attachments"):
                continue
            words = [w.split(":", 1)[1] for w in q.split() if w.startswith("subject:")]
            if any(w.strip('"') not in m["subject"].lower() for w in words):
                continue
            return m
        return None

    def send(self, to, subject, body) -> dict:
        msg = {"message_id": f"sent-{len(self.outbox) + 1}", "to": to, "subject": subject, "body": body}
        self.outbox.append(msg)
        return {"message_id": msg["message_id"], "status": "sent"}


@dataclass
class MockMessaging:
    posts: list[dict] = field(default_factory=list)

    def post(self, channel: str, text: str, **extra) -> dict:
        self.posts.append({"channel": channel, "text": text, **extra})
        return {"ok": True}


@dataclass
class MockSpreadsheet:
    sheets: dict[str, list[list[str]]] = field(default_factory=lambda: {
        "Sheet1": [["Task", "Owner"], ["Ship v2", "Ada"], ["Fix bug", "Alan"]]})

    def append(self, spreadsheet_id: str, rng: str, row: list) -> dict:
        sheet = (rng or "Sheet1").split("!")[0] or "Sheet1"
        rows = self.sheets.setdefault(sheet, [])
        rows.append([str(c) for c in row])
        n = len(rows)
        return {"updated_range": f"{sheet}!A{n}:{chr(64 + max(1, len(row)))}{n}", "row": row}

    def read(self, spreadsheet_id: str, rng: str) -> dict:
        sheet = (rng or "Sheet1").split("!")[0] or "Sheet1"
        rows = self.sheets.get(sheet, [])
        return {"rows": rows, "row_count": len(rows)}


@dataclass
class MockStorage:
    files: dict[str, dict] = field(default_factory=lambda: {"drv-1": {"name": "Q3 report.pdf", "size": 1200}})

    def save(self, name: str, content: Any, folder: str = "") -> dict:
        fid = f"drv-{len(self.files) + 1}"
        size = len(content) if isinstance(content, (str, bytes)) else len(json.dumps(content, default=str))
        self.files[fid] = {"name": name, "folder": folder or "root", "size": size}
        return {"file_id": fid, "link": f"https://drive.mock/file/{fid}"}

    def list(self, query: str) -> dict:
        q = (query or "").lower()
        hits = [{"id": k, "name": v["name"]} for k, v in self.files.items()
                if not q or any(tok.strip("'\"") in v["name"].lower() for tok in q.split() if tok not in ("name", "contains"))]
        return {"files": hits, "count": len(hits)}


@dataclass
class MockHTTP:
    calls: list[dict] = field(default_factory=list)

    def request(self, method: str, url: str, body: Any = None) -> dict:
        self.calls.append({"method": method, "url": url, "body": body})
        return {"status": 200, "body": {"ok": True, "echo": body, "data": [1, 2, 3]}}


@dataclass
class MockWorld:
    email: MockEmail = field(default_factory=MockEmail)
    messaging: MockMessaging = field(default_factory=MockMessaging)
    spreadsheet: MockSpreadsheet = field(default_factory=MockSpreadsheet)
    storage: MockStorage = field(default_factory=MockStorage)
    http: MockHTTP = field(default_factory=MockHTTP)
    tasks: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def snapshot(self) -> dict:
        return {
            "email_outbox": self.email.outbox,
            "messages_posted": self.messaging.posts,
            "spreadsheet": self.spreadsheet.sheets,
            "storage_files": self.storage.files,
            "http_calls": self.http.calls,
            "tasks": self.tasks,
            "calendar_events": self.events,
            "log": self.log,
        }


# --------------------------------------------------------------------- engine
def _lookup(ctx: dict, root: str, path: list[str]) -> Any:
    if root == "env":
        return f"<env:{path[0] if path else '?'}>"
    v: Any = ctx.get(root, {})
    for k in path:
        if isinstance(v, dict):
            v = v.get(k, "")
        elif isinstance(v, list) and k.isdigit() and int(k) < len(v):
            v = v[int(k)]
        else:
            return ""
    return v


def render(value: Any, ctx: dict) -> Any:
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    if not isinstance(value, str):
        return value
    whole = REF_RE.fullmatch(value.strip())
    if whole:
        return _lookup(ctx, whole.group(1), [p for p in whole.group(2).split(".") if p])

    def one(m):
        v = _lookup(ctx, m.group(1), [p for p in m.group(2).split(".") if p])
        return v if isinstance(v, str) else json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
    return REF_RE.sub(one, value)


def check(left: Any, op: str, right: Any) -> bool:
    l, r = ("" if left is None else str(left)), ("" if right is None else str(right))

    def num(x):
        try:
            return float(str(x).replace(",", "").replace("$", ""))
        except ValueError:
            return None
    if op == "contains":
        return r.lower() in l.lower()
    if op == "not_contains":
        return r.lower() not in l.lower()
    if op == "equals":
        return l.strip().lower() == r.strip().lower()
    if op == "not_equals":
        return l.strip().lower() != r.strip().lower()
    if op == "starts_with":
        return l.lower().startswith(r.lower())
    if op == "ends_with":
        return l.lower().endswith(r.lower())
    if op in ("greater_than", "less_than"):
        a, b = num(l), num(r)
        return a is not None and b is not None and (a > b if op == "greater_than" else a < b)
    if op == "is_empty":
        return not l.strip()
    if op == "is_not_empty":
        return bool(l.strip())
    raise ValueError(f"unknown operator {op}")


def _short(v: Any, n: int = 160) -> Any:
    if isinstance(v, str) and len(v) > n:
        return v[:n] + "…"
    if isinstance(v, dict):
        return {k: _short(x, n) for k, x in v.items()}
    if isinstance(v, list):
        return [_short(x, n) for x in v[:10]]
    return v


def execute(node: dict, p: dict, world: MockWorld) -> tuple[dict, str]:
    """Run one node against the mock world. Returns (outputs, human note)."""
    t = node["type"]
    nt = catalog.CATALOG[t]
    if t == "manual":
        return {"input": p.get("input", "")}, "run started manually"
    if t == "webhook":
        return copy.deepcopy(nt.sample_output), f"mock {p.get('method', 'POST')} /{p.get('path', '')} received"
    if t == "schedule":
        return {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, f"cron '{p.get('cron')}' fired"
    if t == "form_submitted":
        fields = [f.strip() for f in str(p.get("fields", "")).split(",") if f.strip()]
        sample = nt.sample_output
        out = {f: sample.get(f, f"sample {f}") for f in fields} | {"submitted_at": sample["submitted_at"]}
        return out, f"form '{p.get('title')}' submitted"
    if t == "email_received":
        m = world.email.newest(p.get("query", ""))
        if m is None:
            raise RuntimeError(f"no mock email matches query {p.get('query')!r}")
        return copy.deepcopy(m), f"email '{m['subject']}' from {m['from']}"
    if t == "send_email":
        return world.email.send(p["to"], p["subject"], p["body"]), f"mock email sent to {p['to']}"
    if t in ("slack_message", "discord_message", "teams_message"):
        text = p.get("text", p.get("content", ""))
        svc = t.split("_")[0]
        return world.messaging.post(svc, text, channel_name=p.get("channel", "")), f"posted to mock {svc}"
    if t == "sms_message":
        world.messaging.post("sms", p["body"], to=p["to"])
        return {"sid": f"SM-mock-{len(world.messaging.posts)}"}, f"mock SMS to {p['to']}"
    if t == "notify":
        world.messaging.post("reply", p["message"])
        return {"message": p["message"]}, "reply returned to the caller"
    if t == "sheets_append":
        vals = p["values"]
        row = ([v.strip() for v in vals.split(",")] if isinstance(vals, str)
               else list(vals) if isinstance(vals, (list, tuple)) else [vals])
        return world.spreadsheet.append(p["spreadsheet_id"], p.get("range", "Sheet1!A1"), row), f"row appended ({len(row)} cells)"
    if t == "sheets_read":
        out = world.spreadsheet.read(p["spreadsheet_id"], p.get("range", ""))
        return out, f"read {out['row_count']} rows"
    if t == "drive_save":
        out = world.storage.save(p["file_name"], p["content"], p.get("folder_id", ""))
        return out, f"saved '{p['file_name']}' to mock Drive"
    if t == "drive_list":
        out = world.storage.list(p.get("query", ""))
        return out, f"{out['count']} file(s) found"
    if t == "extract_attachments":
        items = p["source"] if isinstance(p["source"], list) else []
        types = [x.strip().lower() for x in str(p.get("file_types", "")).split(",") if x.strip()]
        keep = [a for a in items if not types or any(str(a.get("name", "")).lower().endswith("." + x) for x in types)]
        first = keep[0] if keep else {}
        return ({"files": keep, "count": len(keep), "first_name": first.get("name", ""),
                 "first_content": first.get("content", "")}, f"{len(keep)} attachment(s) kept")
    if t == "http_request":
        out = world.http.request(p.get("method", "GET"), p["url"], p.get("body") or None)
        return out, f"mock {p.get('method', 'GET')} {p['url']} -> 200"
    if t == "condition":
        res = check(p.get("field"), p.get("operator", "contains"), p.get("value", ""))
        return {"result": res}, f"{str(p.get('field'))[:50]!r} {p.get('operator')} {p.get('value')!r} -> {res}"
    if t == "transform":
        return dict(p.get("fields") or {}), "fields built"
    if t == "delay":
        return {"waited": p.get("seconds")}, f"would wait {p.get('seconds')}s (skipped in simulation)"
    if t == "llm_summarize":
        words = str(p.get("text", "")).split()
        summary = " ".join(words[:25]) + ("…" if len(words) > 25 else "")
        return {"summary": f"[mock {p.get('model')}] {summary}"}, "mock LLM summary"
    if t == "ai_agent":
        tools = [x.strip() for x in str(p.get("tools", "")).split(",") if x.strip()]
        answer = f"[mock {p.get('model')}] answer to: {str(p.get('input', ''))[:120]}"
        return {"answer": answer}, f"mock agent answered (tools available: {', '.join(tools) or 'none'})"
    if t == "create_task":
        key = f"{p['project']}-{100 + len(world.tasks)}"
        world.tasks.append({"key": key, "title": p["title"]})
        return {"task_id": key, "url": f"https://jira.mock/browse/{key}"}, f"task {key} created"
    if t == "calendar_create":
        eid = f"evt-{len(world.events) + 1}"
        world.events.append({"id": eid, "title": p["title"], "start": p["start"]})
        return {"event_id": eid, "link": f"https://calendar.mock/{eid}"}, f"event '{p['title']}' created"
    if t == "log_message":
        world.log.append(str(p["message"]))
        return {"logged": True}, "logged"
    raise RuntimeError(f"no mock for node type {t}")


def simulate(plan: dict, trigger_payload: dict | None = None, world: MockWorld | None = None) -> dict:
    """Run the plan; return {"ok", "trace": [...], "world": snapshot, "error"}."""
    world = world or MockWorld()
    nodes = node_map(plan)
    trig = trigger_of(plan)
    ctx: dict[str, Any] = {}
    trace: list[dict] = []
    ok, error = True, None
    for i, nid in enumerate(topo_order(plan), 1):
        n = nodes[nid]
        incoming = [e for e in plan["edges"] if e["to"] == nid]
        if incoming:
            live = [e for e in incoming if e["from"] in ctx and (
                e.get("branch") is None or bool(ctx[e["from"]].get("result")) == (e["branch"] == "true"))]
            if not live:
                trace.append({"step": i, "node": nid, "type": n["type"], "status": "skipped",
                              "inputs": {}, "outputs": {}, "note": "branch not taken"})
                continue
        params = render(n.get("params", {}), ctx)
        try:
            if trig and nid == trig["id"] and trigger_payload is not None:
                out, note = trigger_payload, "trigger payload supplied by user"
            else:
                out, note = execute(n, params, world)
            ctx[nid] = out
            trace.append({"step": i, "node": nid, "type": n["type"], "status": "ok",
                          "inputs": _short(params), "outputs": _short(out), "note": note})
        except Exception as exc:  # report, stop the run
            ok, error = False, f"{nid}: {exc}"
            trace.append({"step": i, "node": nid, "type": n["type"], "status": "error",
                          "inputs": _short(params), "outputs": {}, "note": str(exc)})
            break
    return {"ok": ok, "trace": trace, "world": world.snapshot(), "error": error}


def format_trace(result: dict) -> str:
    lines = []
    for s in result["trace"]:
        mark = {"ok": "✓", "skipped": "·", "error": "✗"}[s["status"]]
        lines.append(f"{mark} {s['step']:>2}. {s['node']} ({s['type']}): {s['note']}")
    lines.append("RESULT: " + ("completed" if result["ok"] else f"FAILED — {result['error']}"))
    return "\n".join(lines)
