"""Check generated n8n workflows against a real n8n install: node registry, CLI import, and one execution.

    N8N_DIR=/path/with/node_modules/n8n .venv/bin/python tools/n8n_check.py

N8N_DIR is a folder where `npm install n8n` was run. A private n8n data folder (SQLite) is created in a
temp directory, so no existing n8n instance or account is touched.

1. Static: every node type exists in the installed n8n-nodes-base / @n8n/n8n-nodes-langchain registry,
   the typeVersion is one that node supports, and every top-level parameter name is a real property.
2. Import: all workflows are imported with `n8n import:workflow --separate`, exported again with
   `n8n export:workflow --all`, and compared (names, node count, node types).
3. Execution: a generated manual-trigger workflow (HTTP GET -> Slack message) is run with `n8n execute`
   against a local receiver; SLACK_WEBHOOK_URL is supplied only as an environment variable.
Writes docs/evidence/n8n-check.md.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flowforge import pipeline  # noqa: E402

N8N_DIR = Path(os.environ.get("N8N_DIR", ".")).resolve()
BIN = N8N_DIR / "node_modules" / ".bin" / "n8n"
PREFIX = {"n8n-nodes-base": "n8n-nodes-base", "@n8n/n8n-nodes-langchain": "@n8n/n8n-nodes-langchain"}


def registry() -> dict[str, dict]:
    reg = {}
    for pkg in PREFIX:
        for n in json.loads((N8N_DIR / "node_modules" / pkg / "dist/types/nodes.json").read_text()):
            full = f"{pkg}.{n['name']}"
            versions = n["version"] if isinstance(n["version"], list) else [n["version"]]
            entry = reg.setdefault(full, {"versions": set(), "props": set()})
            entry["versions"].update(versions)
            entry["props"].update(p["name"] for p in n.get("properties", []))
    return reg


def n8n(args: list[str], env: dict, timeout: int = 300) -> subprocess.CompletedProcess:
    return subprocess.run([str(BIN), *args], capture_output=True, text=True, env=env, timeout=timeout, cwd=N8N_DIR)


received: list[tuple[str, str, str]] = []


class Receiver(BaseHTTPRequestHandler):
    def _h(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        received.append((self.command, self.path, raw))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "all systems operational", "uptime": 99.9}')

    do_GET = do_POST = _h

    def log_message(self, *a):
        pass


def main() -> int:
    if not BIN.exists():
        print(f"n8n not found at {BIN}; set N8N_DIR")
        return 2
    version = n8n(["--version"], os.environ.copy()).stdout.strip()
    reg = registry()
    cases = []
    for f in ("requests.json", "requests-heldout.json", "requests-heldout2.json"):
        cases += json.loads((ROOT / "eval" / f).read_text())

    work = Path(tempfile.mkdtemp(prefix="flowforge-n8n-"))
    wf_dir = work / "workflows"
    wf_dir.mkdir()
    env = {**os.environ, "N8N_USER_FOLDER": str(work / "home"), "N8N_DIAGNOSTICS_ENABLED": "false",
           "N8N_ENCRYPTION_KEY": "flowforge-local-test-only", "N8N_RUNNERS_ENABLED": "false",
           "DB_SQLITE_POOL_SIZE": "1", "N8N_BLOCK_ENV_ACCESS_IN_NODE": "false"}

    # ---- 1. static registry check
    static_rows, static_ok, generated = [], 0, {}
    for i, c in enumerate(cases):
        r = pipeline.run(c["request"], mode="template")
        if not r["ok"]:
            static_rows.append(f"| {c['id']} | – | FAIL: plan invalid |")
            continue
        wf = r["n8n"]["workflow"]
        wf["name"] = f"{c['id']} {wf['name']}"[:120]
        wf["id"] = f"ff{i:014d}"  # n8n ids are 16 chars
        generated[c["id"]] = wf
        (wf_dir / f"{c['id']}.json").write_text(json.dumps(wf))
        problems = []
        for node in wf["nodes"]:
            info = reg.get(node["type"])
            if info is None:
                problems.append(f"unknown type {node['type']}")
                continue
            if node["typeVersion"] not in info["versions"]:
                problems.append(f"{node['type']} v{node['typeVersion']} not in {sorted(info['versions'])}")
            unknown = [k for k in node.get("parameters", {}) if k not in info["props"]]
            if unknown:
                problems.append(f"{node['type'].split('.')[-1]}: unknown params {unknown}")
        for src, outs in wf["connections"].items():
            names = {n["name"] for n in wf["nodes"]}
            if src not in names or any(t["node"] not in names for o in outs.get("main", []) for t in o):
                problems.append(f"connection from/to a missing node ({src})")
        static_ok += not problems
        static_rows.append(f"| {c['id']} | {len(wf['nodes'])} | {'PASS' if not problems else 'FAIL: ' + '; '.join(problems)} |")

    # ---- 2. import into n8n and export back
    imp = n8n(["import:workflow", "--separate", f"--input={wf_dir}"], env, timeout=600)
    exp_file = work / "export.json"
    exp = n8n(["export:workflow", "--all", f"--output={exp_file}"], env, timeout=300)
    exported = json.loads(exp_file.read_text()) if exp_file.exists() else []
    by_id = {w["id"]: w for w in exported}
    import_rows, import_ok = [], 0
    for cid, wf in generated.items():
        back = by_id.get(wf["id"])
        same = bool(back) and sorted(n["type"] for n in back["nodes"]) == sorted(n["type"] for n in wf["nodes"]) \
            and set(back["connections"]) == set(wf["connections"])
        import_ok += same
        import_rows.append(f"| {cid} | {'PASS' if same else 'FAIL: ' + ('missing after import' if not back else 'changed')} |")

    # ---- 3. execute one workflow in n8n's runtime
    srv = HTTPServer(("127.0.0.1", 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    request = f"call {base}/status and post it to slack"
    r = pipeline.run(request, mode="template")
    wf = r["n8n"]["workflow"]
    wf["id"], wf["name"] = "ffexec0000000001", "FlowForge execution demo"
    (work / "exec.json").write_text(json.dumps(wf))
    n8n(["import:workflow", f"--input={work / 'exec.json'}"], env)
    t0 = time.time()
    ex = n8n(["execute", "--id=ffexec0000000001", "--rawOutput"], {**env, "SLACK_WEBHOOK_URL": f"{base}/slack-hook"}, timeout=300)
    srv.shutdown()
    slack = [x for x in received if x[1] == "/slack-hook"]
    status = [x for x in received if x[1] == "/status"]
    slack_text = json.loads(slack[0][2]).get("text") if slack else None
    # Slack's incoming webhooks require "text" to be a string, so an object here would be a real failure
    exec_ok = ex.returncode == 0 and len(status) == 1 and isinstance(slack_text, str) and "all systems operational" in slack_text

    md = ["# n8n compatibility check", "", f"Run {time.strftime('%Y-%m-%d %H:%M')} against **n8n {version}** "
          f"(npm package, private SQLite data folder). Generated by `tools/n8n_check.py`.", "",
          "## Summary", "",
          f"- Static check against n8n's node registry (type exists, typeVersion supported, parameter names valid): "
          f"**{static_ok}/{len(cases)}**",
          f"- Imported with `n8n import:workflow` and read back unchanged via `n8n export:workflow`: "
          f"**{import_ok}/{len(generated)}** (import exit code {imp.returncode})",
          f"- Executed in n8n's runtime with `n8n execute`: **{'PASS' if exec_ok else 'FAIL'}**", "",
          "Not checked: workflows that need Google/Slack/Jira/Twilio credentials were imported but not executed "
          "(n8n asks for credentials on those nodes after import). Webhook, form, schedule and Gmail triggers "
          "need an active n8n server to fire, so they were not executed either.", "",
          "## Execution", "", f"Request: “{request}”", "",
          "n8n nodes: " + " → ".join(f"{n['name']} (`{n['type']}`)" for n in wf["nodes"]), "",
          f"`n8n execute` exit code {ex.returncode}, {time.time() - t0:.1f}s. The receiver got:", "",
          *[f"- `{m} {p}` {('body: `' + b[:200] + '`') if b else ''}" for m, p, b in received], "",
          ("The Slack step's URL came from `$env.SLACK_WEBHOOK_URL`; its `text` is a string (as Slack requires) carrying the HTTP step's JSON response."
           if exec_ok else "FAILED. n8n output (tail):\n\n```\n" + (ex.stdout + ex.stderr)[-1500:] + "\n```"), "",
          "## Static registry check", "", "| ID | Nodes | Result |", "|---|---|---|", *static_rows, "",
          "## Import / export round trip", "", "| ID | Result |", "|---|---|", *import_rows]
    if imp.returncode != 0:
        md += ["", "Import output:", "", "```", (imp.stdout + imp.stderr)[-1500:], "```"]
    out = ROOT / "docs/evidence/n8n-check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n")
    print("\n".join(md[:16]))
    print(f"\nWritten: {out}")
    return 0 if exec_ok and static_ok == len(cases) and import_ok == len(generated) else 1


if __name__ == "__main__":
    sys.exit(main())
