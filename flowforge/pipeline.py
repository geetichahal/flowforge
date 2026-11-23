"""End-to-end pipeline: plan -> validate -> generate x3 -> validate -> (revise)* -> simulate."""
from __future__ import annotations

import os
import time

from . import planner
from .generators import flowise, n8n, python_gen
from .plan import normalize, summarize
from .simulator import simulate
from .validator import Report, validate_all, validate_plan

MAX_ATTEMPTS = 3
LLM_BUDGET_S = float(os.environ.get("FLOWFORGE_LLM_BUDGET", "60"))


def generate_all(plan: dict) -> dict:
    return {"flowise": flowise.generate(plan), "n8n": n8n.generate(plan), "python": python_gen.generate(plan)}


def _check(plan: dict) -> tuple[Report, dict]:
    """Validate the plan; only if it is sound, generate and validate the three outputs."""
    report = validate_plan(plan)
    if not report.ok:
        return report, {}
    try:
        outputs = generate_all(plan)
    except Exception as exc:  # a generator bug must surface, not crash the loop
        report.add("error", "E_GENERATOR", f"{exc.__class__.__name__}: {exc}", target="generator")
        return report, {}
    return validate_all(plan, outputs), outputs


def run(request: str, mode: str = "auto", max_attempts: int = MAX_ATTEMPTS,
        inject_fault: bool = False, plan_override: dict | None = None) -> dict:
    t0 = time.time()
    if plan_override is not None:
        plan, source, note = plan_override, "user", ""
    else:
        plan, source, note = planner.plan(request, mode)
    plan = normalize(plan)
    plan.setdefault("request", request)
    attempts = []
    if inject_fault:
        plan, faults = planner.inject_fault(plan)
        attempts.append({"attempt": 0, "source": "fault-injection", "actions": faults})

    report, outputs = Report(), {}
    for attempt in range(1, max_attempts + 1):
        report, outputs = _check(plan)
        attempts.append({"attempt": attempt, "source": source, "ok": report.ok,
                         "errors": [f"{i.code}: {i.message}" for i in report.errors]})
        if report.ok:
            break
        if attempt == max_attempts:
            break
        # Keep a request under ~100 s (proxies such as Cloudflare tunnels time out): once the
        # LLM budget is spent, further revisions use the instant rule-based repairer.
        if source.startswith("llm") and time.time() - t0 > LLM_BUDGET_S:
            source = "repair"
        plan, source, actions = planner.revise(plan, report, request, source)
        plan = normalize(plan)
        attempts[-1]["revision"] = actions

    sim = simulate(plan) if report.ok else None
    return {
        "ok": report.ok,
        "request": request,
        "planner_note": note,
        "plan": plan,
        "plan_summary": summarize(plan),
        "attempts": attempts,
        "report": report.to_dict(),
        "report_text": report.text(),
        "flowise": outputs.get("flowise"),
        "n8n": outputs.get("n8n"),
        "python": outputs.get("python"),
        "simulation": sim,
        "seconds": round(time.time() - t0, 2),
    }
