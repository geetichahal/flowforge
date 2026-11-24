"""Command line: python -m flowforge <command> ...

  generate "<request>" [--out DIR] [--mode auto|llm|template] [--inject-fault]
  simulate PLAN.json
  validate PLAN.json
  push-flowise FLOWISE_EXPORT.json         (needs FLOWISE_URL / FLOWISE_API_KEY)
  eval [--mode template|llm|auto]
  serve [--port 8000]                        (FastAPI + Gradio UI at /ui)
  catalog
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import catalog, pipeline
from .plan import normalize, slug
from .simulator import format_trace, simulate
from .validator import validate_plan


def write_outputs(result: dict, out: Path) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "plan.json": json.dumps(result["plan"], indent=2),
        "validation.txt": result["report_text"] + "\n",
    }
    if result["ok"]:
        files["flowise-flow.json"] = json.dumps(result["flowise"]["export"], indent=2)
        files["n8n-workflow.json"] = json.dumps(result["n8n"]["workflow"], indent=2)
        files["automation.py"] = result["python"]["code"]
        files["simulation.txt"] = format_trace(result["simulation"]) + "\n"
        files["flowise-post-import.txt"] = "\n".join(f"- {s}" for s in result["flowise"]["post_import"]) + "\n"
    paths = []
    for name, content in files.items():
        p = out / name
        p.write_text(content)
        paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="flowforge", description="Natural language -> Flowise / n8n / Python automations")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("request")
    g.add_argument("--out", type=Path)
    g.add_argument("--mode", default="auto", choices=["auto", "llm", "template"])
    g.add_argument("--inject-fault", action="store_true", help="corrupt the first plan to demonstrate self-correction")
    s = sub.add_parser("simulate")
    s.add_argument("plan", type=Path)
    v = sub.add_parser("validate")
    v.add_argument("plan", type=Path)
    pf = sub.add_parser("push-flowise")
    pf.add_argument("export", type=Path)
    e = sub.add_parser("eval")
    e.add_argument("--mode", default="template", choices=["auto", "llm", "template"])
    e.add_argument("--out", type=Path, default=Path("eval"))
    e.add_argument("--requests", type=Path, default=None, help="request set (default eval/requests.json)")
    sv = sub.add_parser("serve")
    sv.add_argument("--port", type=int, default=8000)
    sv.add_argument("--host", default="127.0.0.1")
    sub.add_parser("catalog")
    a = ap.parse_args(argv)

    if a.cmd == "generate":
        r = pipeline.run(a.request, mode=a.mode, inject_fault=a.inject_fault)
        if r["planner_note"]:
            print(f"note: {r['planner_note']}")
        for att in r["attempts"]:
            if att["source"] == "fault-injection":
                print("fault injected: " + "; ".join(att["actions"]))
                continue
            status = "valid" if att["ok"] else f"{len(att['errors'])} error(s)"
            print(f"attempt {att['attempt']} [{att['source']}]: {status}")
            for err in att["errors"]:
                print(f"    - {err}")
            for act in att.get("revision", []):
                print(f"    fix: {act}")
        if r.get("removed_steps"):
            print("WARNING: the plan is valid, but self-correction removed requested steps: " + ", ".join(r["removed_steps"]))
        print("\nPLAN\n" + r["plan_summary"])
        print("\nVALIDATION\n" + r["report_text"])
        if r["ok"]:
            print("\nSIMULATION\n" + format_trace(r["simulation"]))
        out = a.out or Path("output") / slug(r["plan"].get("name", "automation"))
        paths = write_outputs(r, out)
        print("\nWrote:\n" + "\n".join(f"  {p}" for p in paths))
        return 0 if r["ok"] else 1
    if a.cmd == "simulate":
        plan = normalize(json.loads(a.plan.read_text()))
        rep = validate_plan(plan)
        if not rep.ok:
            print(rep.text())
            return 1
        res = simulate(plan)
        print(format_trace(res))
        print(json.dumps(res["world"], indent=2, default=str))
        return 0 if res["ok"] else 1
    if a.cmd == "validate":
        plan = normalize(json.loads(a.plan.read_text()))
        r = pipeline._check(plan)[0]
        print(r.text())
        return 0 if r.ok else 1
    if a.cmd == "push-flowise":
        from . import flowise_client
        res = flowise_client.create_flow(json.loads(a.export.read_text()))
        print(f"Created flow {res['id']}\nOpen: {res['url']}")
        return 0
    if a.cmd == "eval":
        from .evaluate import run_eval
        return run_eval(a.mode, a.out, a.requests)
    if a.cmd == "serve":
        import uvicorn
        from .api import create_app
        print(f"FlowForge: http://{a.host}:{a.port}/ui   (API docs: /docs)")
        uvicorn.run(create_app(with_ui=True), host=a.host, port=a.port)
        return 0
    if a.cmd == "catalog":
        print(catalog.catalog_for_prompt())
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
