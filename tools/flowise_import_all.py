"""Import every evaluation request's Flowise export into a running Flowise and read it back.

    .venv/bin/python tools/flowise_import_all.py

For each request in eval/requests.json, requests-heldout.json and requests-heldout2.json (template planner):
generate -> POST /api/v1/chatflows -> GET it back -> compare node ids/types and edge count -> DELETE.
Writes docs/evidence/flowise-import-all.md. This checks that Flowise accepts and stores the
flows unchanged; only tools/flowise_demo.py executes one in Flowise's runtime.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from flowforge import flowise_client, pipeline  # noqa: E402


def main() -> int:
    cases = [c for f in ("requests.json", "requests-heldout.json", "requests-heldout2.json")
             for c in json.loads((ROOT / "eval" / f).read_text())]
    version = flowise_client._request("GET", "/api/v1/version").get("version")
    rows, passed = [], 0
    for c in cases:
        r = pipeline.run(c["request"], mode="template")
        if not r["ok"]:
            rows.append(f"| {c['id']} | – | – | not generated (plan invalid) |")
            continue
        flow = json.loads(r["flowise"]["export"]["AgentFlowV2"][0]["flowData"])
        try:
            fid = flowise_client.create_flow(r["flowise"]["export"])["id"]
            back = json.loads(flowise_client.get_flow(fid)["flowData"])
            flowise_client._request("DELETE", f"/api/v1/chatflows/{fid}")
        except Exception as exc:
            rows.append(f"| {c['id']} | {len(flow['nodes'])} | – | FAIL: {str(exc)[:120]} |")
            continue
        same = [(n["id"], n["data"]["name"]) for n in back["nodes"]] == [(n["id"], n["data"]["name"]) for n in flow["nodes"]] \
            and len(back["edges"]) == len(flow["edges"])
        passed += same
        types = ", ".join(sorted({n["data"]["name"].removesuffix("Agentflow") for n in flow["nodes"]}))
        rows.append(f"| {c['id']} | {len(flow['nodes'])} | {len(flow['edges'])} | "
                    f"{'PASS' if same else 'FAIL: changed on read-back'} ({types}) |")
    md = ["# Flowise import check (all evaluation requests)", "",
          f"Run {time.strftime('%Y-%m-%d %H:%M')} against Flowise {version} at {flowise_client.base_url()}.", "",
          f"**{passed}/{len(cases)}** flows were accepted by `POST /api/v1/chatflows` and read back with identical "
          "node ids, node types and edge count (each flow was deleted afterwards).", "",
          "This proves Flowise accepts and stores the generated JSON. It does not prove each flow runs: flows "
          "using Gmail/Drive/Sheets tools or a chat model need credentials/a model attached in Flowise first. "
          "Runtime execution is shown in `flowise-runtime-demo.md`.", "",
          "| ID | Nodes | Edges | Result (Flowise node types) |", "|---|---|---|---|", *rows]
    out = ROOT / "docs/evidence/flowise-import-all.md"
    out.write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0 if passed == len(cases) else 1


if __name__ == "__main__":
    sys.exit(main())
