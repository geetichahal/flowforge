"""Thin FastAPI service over the engine: /plan, /generate/*, /validate, /simulate, /run."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, llm, pipeline, planner
from .generators import flowise, n8n, python_gen
from .plan import normalize
from .simulator import simulate
from .validator import validate_all, validate_plan

PKG_DIR = Path(__file__).resolve().parent
WEB_DIR = PKG_DIR.parent / "web"


class PlanRequest(BaseModel):
    request: str = Field(..., examples=["when I get an email with an invoice attached, save it to Drive and ping me on Slack"])
    mode: str = Field("auto", pattern="^(auto|llm|template)$")


class PlanBody(BaseModel):
    plan: dict[str, Any]


class RunRequest(PlanRequest):
    inject_fault: bool = False


class SimulateBody(PlanBody):
    trigger_payload: dict[str, Any] | None = None


class FlowisePush(BaseModel):
    export: dict[str, Any]
    confirm: bool = Field(False, description="Must be true: this writes to your Flowise instance")


class FlowisePushTo(FlowisePush):
    url: str = "http://localhost:3000"
    api_key: str = ""


def _valid_plan(body: PlanBody) -> dict:
    plan = normalize(body.plan)
    rep = validate_plan(plan)
    if not rep.ok:
        raise HTTPException(422, rep.to_dict())
    return plan


def create_app(with_ui: bool = False) -> FastAPI:
    app = FastAPI(title="FlowForge", version="1.0.0",
                  description="Natural-language automations rendered as Flowise flows, n8n workflows and Python scripts.")

    @app.get("/health")
    def health():
        ok, info = llm.available()
        return {"ok": True, "llm": {"available": ok, "detail": info}, "catalog_nodes": len(catalog.CATALOG)}

    @app.get("/catalog")
    def get_catalog():
        return [{"type": n.type, "kind": n.kind, "label": n.label, "description": n.description,
                 "params": [p.__dict__ for p in n.params], "outputs": n.outputs,
                 "flowise": n.flowise, "n8n": n.n8n} for n in catalog.NODES]

    @app.post("/plan")
    def make_plan(body: PlanRequest):
        plan, source, note = planner.plan(body.request, body.mode)
        plan = normalize(plan)
        return {"plan": plan, "source": source, "note": note, "validation": validate_plan(plan).to_dict()}

    @app.post("/generate/flowise")
    def gen_flowise(body: PlanBody):
        return flowise.generate(_valid_plan(body))

    @app.post("/generate/n8n")
    def gen_n8n(body: PlanBody):
        return n8n.generate(_valid_plan(body))

    @app.post("/generate/python")
    def gen_python(body: PlanBody):
        return python_gen.generate(_valid_plan(body))

    @app.post("/validate")
    def validate(body: PlanBody):
        plan = normalize(body.plan)
        rep = validate_plan(plan)
        if rep.ok:
            rep = validate_all(plan, pipeline.generate_all(plan))
        return rep.to_dict()

    @app.post("/simulate")
    def sim(body: SimulateBody):
        return simulate(_valid_plan(body), body.trigger_payload)

    @app.post("/run")
    def run(body: RunRequest):
        return pipeline.run(body.request, body.mode, inject_fault=body.inject_fault)

    @app.post("/flowise/push")
    def push(body: FlowisePush):
        if not body.confirm:
            raise HTTPException(400, "Set confirm=true to create this flow in Flowise")
        from . import flowise_client
        try:
            return flowise_client.create_flow(body.export)
        except Exception as exc:
            raise HTTPException(502, str(exc))

    @app.post("/flowise/push-to")
    def push_to(body: FlowisePushTo):
        """Like /flowise/push, but with the Flowise URL and API key given per request (used by the website)."""
        if not body.confirm:
            raise HTTPException(400, "Set confirm=true to create this flow in Flowise")
        from . import flowise_client
        try:
            return flowise_client.create_flow(body.export, base=body.url, api_key=body.api_key or None)
        except Exception as exc:
            raise HTTPException(502, str(exc))

    if with_ui:
        import gradio as gr
        from .ui import build_ui
        app = gr.mount_gradio_app(app, build_ui(), path="/ui")

    # The website at "/" and the engine source it can also run in the browser (Pyodide).
    app.mount("/engine/flowforge", StaticFiles(directory=PKG_DIR), name="engine")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app(with_ui=False)
