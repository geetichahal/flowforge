"""Thin FastAPI service over the engine: /plan, /generate/*, /validate, /simulate, /run."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import ipaddress
import os
import socket
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import catalog, llm, pipeline, planner
from .generators import flowise, n8n, python_gen
from .plan import normalize
from .simulator import simulate
from .validator import validate_all, validate_plan

# FLOWFORGE_PUBLIC=1 on a public deployment: outbound pushes may only go to public hosts.
PUBLIC = os.environ.get("FLOWFORGE_PUBLIC") == "1"
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "FLOWFORGE_CORS_ORIGINS", "https://geetichahal.github.io,http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]

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


class FlowiseDeploy(BaseModel):
    plan: dict[str, Any]
    url: str = ""
    api_key: str = ""
    variables: dict[str, str] = {}
    test_input: str | None = None
    confirm: bool = Field(False, description="Must be true: this creates a flow in your Flowise and runs it once")


class FlowiseChat(BaseModel):
    flow_id: str
    message: str
    chat_id: str | None = None
    url: str = ""
    api_key: str = ""


def flowise_target(url: str, api_key: str, request: Request) -> tuple[str, str | None]:
    """Where to deploy. Local requests may fall back to FLOWISE_URL / FLOWISE_API_KEY set on this machine;
    requests from the internet must bring their own and pass the network guard."""
    public = is_public_request(request)
    from . import flowise_client
    base = (url or "").strip() or ("" if public else flowise_client.base_url())
    if not base:
        raise HTTPException(400, "Enter your Flowise URL")
    # the saved key (env or ~/.flowforge/config.json) is only ever used for requests from this machine
    key = (api_key or "").strip() or (None if public else flowise_client.configured_key())
    check_push_target(base, public)
    return base, key


def _valid_plan(body: PlanBody) -> dict:
    plan = normalize(body.plan)
    rep = validate_plan(plan)
    if not rep.ok:
        raise HTTPException(422, rep.to_dict())
    return plan


def is_public_request(request: Request | None) -> bool:
    """Public server, or a request that reached a local server through a Cloudflare tunnel
    (cloudflared always adds Cf-Connecting-Ip; requests from the machine itself never carry it)."""
    return PUBLIC or (request is not None and "cf-connecting-ip" in request.headers)


def check_push_target(url: str, public: bool = True) -> None:
    """On a public server, refuse to send requests to private, loopback or link-local addresses (SSRF guard)."""
    if not public:
        return
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise HTTPException(400, "Flowise URL must be http(s)://host")
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(u.hostname, u.port or (443 if u.scheme == "https" else 80))}
    except socket.gaierror:
        raise HTTPException(400, f"Cannot resolve {u.hostname}")
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise HTTPException(400, "This public server can only reach a Flowise with a public address. "
                                     "For a Flowise on your own machine, run FlowForge locally (python -m flowforge serve).")


def create_app(with_ui: bool = False) -> FastAPI:
    app = FastAPI(title="FlowForge", version="1.0.0",
                  description="Natural-language automations rendered as Flowise flows, n8n workflows and Python scripts.")
    app.add_middleware(CORSMiddleware, allow_origins=CORS_ORIGINS, allow_methods=["GET", "POST"],
                       allow_headers=["Content-Type"])

    @app.get("/health")
    def health(request: Request):
        ok, info = llm.available()
        return {"ok": True, "llm": {"available": ok, "detail": info}, "catalog_nodes": len(catalog.CATALOG),
                "public": is_public_request(request)}

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
    def push(body: FlowisePush, request: Request):
        if is_public_request(request):
            raise HTTPException(403, "Disabled on the public server; use /flowise/push-to with your Flowise URL")
        if not body.confirm:
            raise HTTPException(400, "Set confirm=true to create this flow in Flowise")
        from . import flowise_client
        try:
            return flowise_client.create_flow(body.export)
        except Exception as exc:
            raise HTTPException(502, str(exc))

    @app.post("/flowise/push-to")
    def push_to(body: FlowisePushTo, request: Request):
        """Like /flowise/push, but with the Flowise URL and API key given per request (used by the website)."""
        if not body.confirm:
            raise HTTPException(400, "Set confirm=true to create this flow in Flowise")
        check_push_target(body.url, is_public_request(request))
        from . import flowise_client
        try:
            return flowise_client.create_flow(body.export, base=body.url, api_key=body.api_key or None)
        except Exception as exc:
            raise HTTPException(502, str(exc))

    @app.post("/flowise/deploy")
    def flowise_deploy(body: FlowiseDeploy, request: Request):
        """Create the flow in Flowise, connect variables and saved credentials, and run it once."""
        if not body.confirm:
            raise HTTPException(400, "Set confirm=true to create and run this flow in your Flowise")
        plan = _valid_plan(PlanBody(plan=body.plan))
        base, key = flowise_target(body.url, body.api_key, request)
        from . import deploy as deploy_mod
        try:
            return deploy_mod.deploy(plan, flowise.generate(plan)["export"], base=base, api_key=key,
                                     variables=body.variables, test_input=body.test_input)
        except Exception as exc:
            raise HTTPException(502, str(exc)[:500])

    @app.post("/flowise/chat")
    def flowise_chat(body: FlowiseChat, request: Request):
        """Send one message to a deployed chat agent; reuse chat_id to continue the conversation."""
        base, key = flowise_target(body.url, body.api_key, request)
        from . import flowise_client
        import time as _t
        t0 = _t.time()
        try:
            out = flowise_client.predict(body.flow_id, body.message, chat_id=body.chat_id, base=base, api_key=key)
        except Exception as exc:
            raise HTTPException(502, str(exc)[:500])
        return {"answer": (out.get("text") or "").strip(), "chat_id": out.get("chatId") or body.chat_id,
                "seconds": round(_t.time() - t0, 1)}

    if with_ui:
        import gradio as gr
        from .ui import build_ui
        app = gr.mount_gradio_app(app, build_ui(), path="/ui")

    # The website at "/" and the engine source it can also run in the browser (Pyodide).
    app.mount("/engine/flowforge", StaticFiles(directory=PKG_DIR), name="engine")
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app(with_ui=False)
