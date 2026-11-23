"""Gradio dashboard: request -> plan, Flowise / n8n / Python outputs, validation, simulation."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import gradio as gr

from . import llm, pipeline
from .simulator import format_trace

EXAMPLES = [
    "when I get an email with an invoice attached, save it to Drive and ping me on Slack",
    "when someone submits the contact form, email them a thank-you email and add a row to the spreadsheet",
    "every monday at 9am read the sales sheet, summarize it and email me the summary",
    "when a webhook receives an order, if the amount is over 100 post to slack otherwise add a row to the spreadsheet",
    "every day at 8am call https://api.example.com/status and post it to discord",
]

POST_IMPORT_INTRO = ("**This is a starting canvas, not a one-click automation.** After importing into Flowise "
                     "(Agentflows → Import, or *Push to Flowise* below): connect credentials, pick the model, "
                     "check tool arguments, save.\n\n")


def _files(result: dict) -> list[str]:
    d = Path(tempfile.mkdtemp(prefix="flowforge-"))
    out = []
    if result["ok"]:
        for name, content in (("flowise-flow.json", json.dumps(result["flowise"]["export"], indent=2)),
                              ("n8n-workflow.json", json.dumps(result["n8n"]["workflow"], indent=2)),
                              ("automation.py", result["python"]["code"])):
            p = d / name
            p.write_text(content)
            out.append(str(p))
    p = d / "plan.json"
    p.write_text(json.dumps(result["plan"], indent=2))
    out.append(str(p))
    return out


def _attempts_md(result: dict) -> str:
    lines = []
    if result.get("planner_note"):
        lines.append(f"> {result['planner_note']}\n")
    for a in result["attempts"]:
        if a["source"] == "fault-injection":
            lines.append("**Fault injected (demo):** " + "; ".join(a["actions"]))
            continue
        status = "✅ valid" if a["ok"] else f"❌ {len(a['errors'])} error(s)"
        lines.append(f"**Attempt {a['attempt']}** · planner: `{a['source']}` · {status}")
        lines += [f"- {e}" for e in a["errors"]]
        lines += [f"- 🔧 {r}" for r in a.get("revision", [])]
    return "\n".join(lines)


def generate(request: str, mode: str, inject: bool):
    if not request.strip():
        raise gr.Error("Type an automation request first.")
    r = pipeline.run(request, mode=mode, inject_fault=inject)
    state = r
    status = (f"### {'✅ Verified' if r['ok'] else '❌ Could not produce a valid automation'} in "
              f"{len([a for a in r['attempts'] if a['source'] != 'fault-injection'])} attempt(s) · {r['seconds']}s")
    if not r["ok"]:
        return (state, status, _attempts_md(r), r["plan_summary"], r["plan"], "", "", "", r["report_text"], "",
                POST_IMPORT_INTRO, _files(r))
    post = POST_IMPORT_INTRO + "\n".join(f"- {s}" for s in r["flowise"]["post_import"])
    return (state, status, _attempts_md(r), r["plan_summary"], r["plan"],
            json.dumps(r["flowise"]["export"], indent=2), json.dumps(r["n8n"]["workflow"], indent=2),
            r["python"]["code"], r["report_text"], format_trace(r["simulation"]), post, _files(r))


def push(state: dict | None, url: str, key: str):
    if not state or not state.get("ok"):
        return "Generate a valid automation first."
    import os
    from . import flowise_client
    os.environ["FLOWISE_URL"] = url.strip() or "http://localhost:3000"
    if key.strip():
        os.environ["FLOWISE_API_KEY"] = key.strip()
    try:
        res = flowise_client.create_flow(state["flowise"]["export"])
    except Exception as exc:
        return f"❌ {exc}"
    return f"✅ Created flow `{res['id']}` — [open it on the canvas]({res['url']})"


def build_ui() -> gr.Blocks:
    ok, info = llm.available()
    llm_line = f"LLM planner: **{info}**" if ok else f"LLM planner unavailable — {info}. Using the template planner."
    with gr.Blocks(title="FlowForge") as demo:
        state = gr.State()
        gr.Markdown("# FlowForge\nDescribe an automation in plain English. FlowForge plans it, renders it as a "
                    "**Flowise flow**, an **n8n workflow** and a **Python script**, validates all three, repairs its "
                    "own mistakes, and dry-runs it against mock services.\n\n" + llm_line)
        with gr.Row():
            req = gr.Textbox(label="Automation request", lines=2, scale=4, value=EXAMPLES[0])
            with gr.Column(scale=1):
                mode = gr.Radio(["auto", "llm", "template"], value="auto", label="Planner")
                inject = gr.Checkbox(label="Demo: inject a fault (shows self-correction)")
                go = gr.Button("Generate", variant="primary")
        gr.Examples(EXAMPLES, inputs=req)
        status = gr.Markdown()
        with gr.Tabs():
            with gr.Tab("Plan"):
                attempts = gr.Markdown()
                summary = gr.Textbox(label="Steps", lines=6)
                plan_json = gr.JSON(label="Structured plan")
            with gr.Tab("Flowise"):
                post = gr.Markdown()
                fw = gr.Code(language="json", label="Flowise export (Agentflows → Import)")
                with gr.Row():
                    fw_url = gr.Textbox(label="Flowise URL", value="http://localhost:3000")
                    fw_key = gr.Textbox(label="Flowise API key", type="password")
                    fw_btn = gr.Button("Push to Flowise")
                fw_out = gr.Markdown()
            with gr.Tab("n8n"):
                nn = gr.Code(language="json", label="n8n workflow (Workflows → Import from File)")
            with gr.Tab("Python"):
                py = gr.Code(language="python", label="Standalone script (dry-run by default)")
            with gr.Tab("Validation"):
                val = gr.Textbox(label="Validator report", lines=12)
            with gr.Tab("Simulation"):
                sim = gr.Textbox(label="Dry run against mock services", lines=12)
            with gr.Tab("Downloads"):
                files = gr.File(label="Files", file_count="multiple")
        go.click(generate, [req, mode, inject],
                 [state, status, attempts, summary, plan_json, fw, nn, py, val, sim, post, files])
        fw_btn.click(push, [state, fw_url, fw_key], fw_out)
    return demo


if __name__ == "__main__":
    build_ui().launch()
