"""Start the real FastAPI server and exercise every required endpoint over HTTP.

    .venv/bin/python tools/api_smoke.py

Writes docs/evidence/api-smoke.md. Uses the template planner so the run is deterministic.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("SMOKE_PORT", "8011"))
BASE = f"http://127.0.0.1:{PORT}"
REQUEST = "when someone submits the contact form, email them a thank-you email and add a row to the spreadsheet"


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(BASE + path, method=method, headers={"Content-Type": "application/json"},
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()[:300]


def main() -> int:
    srv = subprocess.Popen([sys.executable, "-m", "flowforge", "serve", "--port", str(PORT)], cwd=ROOT,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:
                if call("GET", "/health")[0] == 200:
                    break
            except OSError:
                time.sleep(0.5)
        rows, ok_all = [], True

        def row(name, status, ok, detail):
            nonlocal ok_all
            ok_all &= ok
            rows.append(f"| `{name}` | {status} | {'PASS' if ok else 'FAIL'} | {detail} |")

        s, plan = call("POST", "/plan", {"request": REQUEST, "mode": "template"})
        p = plan.get("plan", plan) if isinstance(plan, dict) else {}
        row("POST /plan", s, s == 200 and bool(p.get("nodes")),
            f"{len(p.get('nodes', []))} nodes: " + ", ".join(n["type"] for n in p.get("nodes", [])))
        s, fw = call("POST", "/generate/flowise", {"plan": p})
        row("POST /generate/flowise", s, s == 200 and "AgentFlowV2" in json.dumps(fw)[:20000],
            f"keys: {list(fw)[:5] if isinstance(fw, dict) else fw}")
        s, nn = call("POST", "/generate/n8n", {"plan": p})
        wf = nn.get("workflow", {}) if isinstance(nn, dict) else {}
        row("POST /generate/n8n", s, s == 200 and bool(wf.get("nodes")) and "connections" in wf,
            f"{len(wf.get('nodes', []))} n8n nodes: " + ", ".join(n["type"] for n in wf.get("nodes", [])))
        s, py = call("POST", "/generate/python", {"plan": p})
        code = py.get("code", "") if isinstance(py, dict) else ""
        row("POST /generate/python", s, s == 200 and "def main()" in code, f"{len(code.splitlines())} lines, compiles: "
            f"{bool(compile(code, 'automation.py', 'exec'))}")
        s, val = call("POST", "/validate", {"plan": p})
        row("POST /validate", s, s == 200 and isinstance(val, dict) and val.get("ok") is True,
            f"ok={val.get('ok')}, errors={val.get('errors')}, warnings={val.get('warnings')}")
        bad = json.loads(json.dumps(p))
        bad["nodes"][1]["params"]["to"] = "x@example.com"
        bad["nodes"][1]["params"]["api_key"] = "sk-" + "a" * 40
        s, val2 = call("POST", "/validate", {"plan": bad})
        codes = sorted({i["code"] for i in val2.get("issues", []) if i["level"] == "error"}) if isinstance(val2, dict) else []
        row("POST /validate (embedded secret)", s, s == 200 and val2.get("ok") is False and any("SECRET" in c or "CREDENTIAL" in c for c in codes),
            f"ok={val2.get('ok')}, error codes={codes}")
        s, sim = call("POST", "/simulate", {"plan": p})
        trace = sim.get("trace", []) if isinstance(sim, dict) else []
        row("POST /simulate", s, s == 200 and sim.get("ok") is True and len(trace) == len(p["nodes"]),
            " → ".join(f"{t['node']}:{t['status']}" for t in trace))
        s, ui = call("GET", "/ui/")
        row("GET /ui/ (Gradio dashboard)", s, s == 200 and "gradio" in str(ui).lower(), f"{len(str(ui))} bytes of HTML")
    finally:
        srv.terminate()
        srv.wait(10)
    md = ["# API smoke test (real server over HTTP)", "", f"Run {time.strftime('%Y-%m-%d %H:%M')} · "
          f"`python -m flowforge serve --port {PORT}` · request: “{REQUEST}”", "",
          "| Endpoint | HTTP | Result | Detail |", "|---|---|---|---|", *rows, "",
          f"**{'All endpoints passed' if ok_all else 'Some endpoints FAILED'}.**"]
    out = ROOT / "docs/evidence/api-smoke.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
