"""End-to-end Flowise runtime demonstration (objective 3 / 9).

    .venv/bin/python tools/flowise_demo.py [--receiver-host 127.0.0.1] [--keep]

1. starts a local HTTP receiver that records every request it gets;
2. turns a natural-language request into a plan with FlowForge and generates the Flowise AgentFlow v2 export;
3. validates the export with FlowForge's Flowise validator;
4. imports it into the running Flowise (FLOWISE_URL / FLOWISE_API_KEY, or ~/.flowforge/config.json);
5. triggers the flow twice through Flowise's own webhook endpoint (amount 250, amount 40);
6. checks each node's runtime status reported by Flowise and what the receiver actually got;
7. writes docs/evidence/flowise-runtime-demo.md with the raw results.

Nothing here touches an external account: the only HTTP target is the local receiver.
Flowise's HTTP node refuses loopback/private targets unless the Flowise server runs with
HTTP_SECURITY_CHECK=false; if it refuses, the script says so and exits non-zero.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flowforge import flowise_client, pipeline  # noqa: E402
from flowforge.validator import validate_flowise  # noqa: E402

received: list[tuple[str, dict]] = []


class Receiver(BaseHTTPRequestHandler):
    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
        try:
            body = json.loads(raw)
        except ValueError:
            body = {"raw": raw}
        received.append((self.path, body))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok": true}')

    def log_message(self, *a):
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--receiver-host", default="127.0.0.1")
    ap.add_argument("--keep", action="store_true", help="leave the flow in Flowise (default: keep anyway; flag kept for clarity)")
    a = ap.parse_args()

    srv = HTTPServer(("0.0.0.0", 0), Receiver)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://{a.receiver_host}:{srv.server_port}"
    request = (f"when a webhook receives an order, if the amount is over 100 post it to {base}/big-orders "
               f"otherwise post it to {base}/small-orders")
    log = [f"# Flowise runtime demonstration", "", f"Run: {time.strftime('%Y-%m-%d %H:%M:%S')}",
           f"Flowise: {flowise_client.base_url()} (version {flowise_client._request('GET', '/api/v1/version').get('version')})",
           "", "## Request", "", f"> {request}", ""]

    r = pipeline.run(request, mode="template")
    if not r["ok"]:
        print(r["report_text"])
        return 1
    export = r["flowise"]["export"]
    rep = validate_flowise(export)
    nodes = json.loads(export["AgentFlowV2"][0]["flowData"])["nodes"]
    log += ["## Generated flow", "", f"FlowForge Flowise validator: {'pass' if rep.ok else 'FAIL'} "
            f"({len(rep.errors)} errors, {len(rep.warnings)} warnings)", "",
            "| Node id | Flowise node | Label |", "|---|---|---|"]
    log += [f"| {n['id']} | {n['data']['name']} v{n['data'].get('version')} | {n['data']['label']} |" for n in nodes]

    created = flowise_client.create_flow(export)
    fid = created["id"]
    back = flowise_client.get_flow(fid)
    log += ["", "## Import", "", f"`POST /api/v1/chatflows` -> id `{fid}`, type `{back.get('type')}`; "
            f"read back with {len(json.loads(back['flowData'])['nodes'])} nodes.", f"Canvas: {created['url']}", "",
            "## Execution in Flowise (via `POST /api/v1/webhook/<id>`)", ""]

    ok = True
    for amount, expect_path in ((250, "/big-orders"), (40, "/small-orders")):
        before = len(received)
        payload = {"order_id": f"A-{amount}", "amount": amount}
        try:
            out = flowise_client.trigger_webhook(fid, payload)
        except Exception as exc:
            log += [f"- amount {amount}: Flowise returned an error: `{str(exc)[:300]}`"]
            ok = False
            continue
        time.sleep(0.5)
        got = received[before:]
        statuses = [(e.get("nodeLabel"), e.get("status")) for e in out.get("agentFlowExecutedData") or []]
        errs = [e for e in out.get("agentFlowExecutedData") or [] if e.get("status") != "FINISHED"]
        this_ok = bool(statuses) and not errs and [p for p, _ in got] == [expect_path] and got[0][1] == payload
        ok &= this_ok
        log += [f"### amount = {amount} -> expected `{expect_path}`: {'PASS' if this_ok else 'FAIL'}", "",
                "Node statuses reported by Flowise: " + ", ".join(f"{l} = {s}" for l, s in statuses), "",
                f"Receiver got: `{json.dumps(got)}`", ""]
        if errs:
            log += ["Errors: `" + json.dumps(errs)[:600] + "`", ""]
    srv.shutdown()
    log += [f"## Result: {'PASS' if ok else 'FAIL'}", ""]
    out_path = ROOT / "docs" / "evidence" / "flowise-runtime-demo.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(log) + "\n")
    (out_path.parent / "flowise-runtime-demo-flow.json").write_text(json.dumps(export, indent=2))
    print("\n".join(log))
    print(f"\nWritten: {out_path}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
