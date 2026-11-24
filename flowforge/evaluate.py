"""Evaluation over the fixed request catalog (eval/requests.json).

Per request, binary outcomes:
  intent      plan has the expected trigger and every expected node type
  plan        plan passes validation (after at most 3 attempts)
  flowise     Flowise export passes the Flowise validator
  n8n         n8n workflow passes the n8n validator
  py_parse    Python output parses
  py_dryrun   Python output runs to completion in dry-run mode (subprocess)
  simulate    simulation against mock connectors completes
  repair      same request with an injected fault is repaired within 3 attempts, keeping the same steps and dry-run behaviour
Nothing is reported that was not measured here.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import pipeline
from .validator import validate_flowise, validate_n8n, validate_python

COLUMNS = ["intent", "plan", "flowise", "n8n", "py_parse", "py_dryrun", "simulate", "repair"]
REQUESTS = Path(__file__).resolve().parent.parent / "eval" / "requests.json"


def _dry_run(code: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "automation.py"
        p.write_text(code)
        env = {k: v for k, v in os.environ.items() if not k.startswith("FLOWFORGE_")}
        env["FLOWFORGE_DRY_RUN"] = "1"
        r = subprocess.run([sys.executable, "-I", str(p)], capture_output=True, text=True, timeout=60, env=env, cwd=d)
        return r.returncode == 0, (r.stderr or r.stdout)[-300:]


def evaluate_one(case: dict, mode: str) -> dict:
    r = pipeline.run(case["request"], mode=mode)
    plan = r["plan"]
    types = [n["type"] for n in plan["nodes"]]
    row = {"id": case["id"], "pattern": case["pattern"], "request": case["request"], "attempts":
           len([a for a in r["attempts"] if a["source"] != "fault-injection"]), "notes": [],
           "planner": r["attempts"][0]["source"] if r["attempts"] else "?"}
    if r.get("planner_note"):
        row["notes"].append(r["planner_note"][:160])
    missing = [t for t in case["expect_nodes"] if t not in types]
    row["intent"] = types[:1] == [case["expect_trigger"]] and not missing
    if not row["intent"]:
        row["notes"].append(f"got {types}; missing {missing}" if missing else f"trigger {types[:1]}")
    row["plan"] = r["ok"]
    if r["ok"]:
        row["flowise"] = validate_flowise(r["flowise"]["export"]).ok
        row["n8n"] = validate_n8n(r["n8n"]["workflow"]).ok
        row["py_parse"] = validate_python(r["python"]["code"], plan).ok
        row["py_dryrun"], out = _dry_run(r["python"]["code"])
        if not row["py_dryrun"]:
            row["notes"].append("dry-run: " + out.strip().splitlines()[-1] if out.strip() else "dry-run failed")
        row["simulate"] = bool(r["simulation"] and r["simulation"]["ok"])
        if not row["simulate"]:
            row["notes"].append("simulation: " + str((r["simulation"] or {}).get("error"))[:160])
        fw_unsupported = [w["node"] for w in r["flowise"]["warnings"] if w["code"] == "W_FLOWISE_UNSUPPORTED"]
        if fw_unsupported:
            row["notes"].append(f"Flowise skips {fw_unsupported} (no equivalent node)")
    else:
        for k in ("flowise", "n8n", "py_parse", "py_dryrun", "simulate"):
            row[k] = False
        row["notes"].append(r["report_text"].splitlines()[0])
    rf = pipeline.run(case["request"], mode=mode, inject_fault=True)
    # repaired = valid AND still the same automation (same steps), not just "valid"
    same = sorted(n["type"] for n in rf["plan"]["nodes"]) == sorted(types)
    behaves = bool(rf["ok"] and r["ok"]) and [(t["node"], t["status"]) for t in rf["simulation"]["trace"]] == \
        [(t["node"], t["status"]) for t in r["simulation"]["trace"]]
    row["repair"] = rf["ok"] and same and behaves
    if rf["ok"] and not same:
        row["notes"].append("repair produced a valid plan but changed its steps")
    elif rf["ok"] and not behaves:
        row["notes"].append("repair produced a valid plan that behaves differently in the dry run")
    return row


def run_eval(mode: str = "template", out_dir: Path = Path("eval"), requests: Path | None = None) -> int:
    requests = requests or REQUESTS
    cases = json.loads(requests.read_text())
    # results-<mode>.* for the main set, results-<set>-<mode>.* for any other set
    tag = mode if requests.resolve() == REQUESTS else f"{requests.stem.removeprefix('requests-')}-{mode}"
    t0 = time.time()
    rows = []
    for c in cases:
        try:
            rows.append(evaluate_one(c, mode))
        except Exception as exc:  # record the failure and keep going; a crash is a failed case, not a lost run
            rows.append({"id": c["id"], "pattern": c["pattern"], "request": c["request"], "attempts": 0,
                         "planner": "error", "notes": [f"crashed: {type(exc).__name__}: {str(exc)[:160]}"],
                         **{k: False for k in COLUMNS}})
        print(f"  {c['id']}: " + " ".join(k for k in COLUMNS if rows[-1][k]) + (f"  [{rows[-1]['planner']}]"), flush=True)
    totals = {c: sum(1 for r in rows if r[c]) for c in COLUMNS}
    n = len(rows)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"results-{tag}.json").write_text(json.dumps({"mode": mode, "n": n, "totals": totals, "rows": rows}, indent=2))

    md = [f"# FlowForge evaluation ({mode} planner, `{requests.name}`)", "",
          f"{n} requests · run {time.strftime('%Y-%m-%d %H:%M')} · {time.time() - t0:.1f}s", "",
          "First-attempt planner per request: " + ", ".join(
              f"{k} ×{v}" for k, v in sorted(__import__("collections").Counter(r["planner"] for r in rows).items())), "",
          "| Metric | Passed | Rate |", "|---|---|---|"]
    md += [f"| {c} | {totals[c]}/{n} | {100 * totals[c] / n:.0f}% |" for c in COLUMNS]
    md += ["", "| ID | Pattern | " + " | ".join(COLUMNS) + " | Attempts | Notes |",
           "|---|---|" + "---|" * len(COLUMNS) + "---|---|"]
    for r in rows:
        md.append(f"| {r['id']} | {r['pattern']} | " + " | ".join("✅" if r[c] else "❌" for c in COLUMNS)
                  + f" | {r['attempts']} | {'; '.join(r['notes'])} |")
    md += ["", "Metric definitions are in `flowforge/evaluate.py`. `repair` = the same request with an injected "
           "fault (missing required param, dangling edge, literal Slack secret, unknown node type) was repaired "
           "within 3 attempts AND the repaired plan has the same steps and the same dry-run behaviour as the unfaulted one."]
    (out_dir / f"results-{tag}.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    return 0
