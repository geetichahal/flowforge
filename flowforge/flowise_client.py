"""Push a generated flow into a running Flowise instance (explicit, opt-in).

Configuration: FLOWISE_URL (default http://localhost:3000) and FLOWISE_API_KEY
(created in Flowise under "API Keys"). Nothing is sent unless the user asks.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def base_url() -> str:
    return os.environ.get("FLOWISE_URL", "http://localhost:3000").rstrip("/")


def _request(method: str, path: str, body: dict | None = None, timeout: float = 30,
             base: str | None = None, api_key: str | None = None) -> dict:
    headers = {"Content-Type": "application/json"}
    key = api_key or os.environ.get("FLOWISE_API_KEY")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request((base or base_url()).rstrip("/") + path, method=method, headers=headers,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            return json.loads(raw)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:300]
        raise RuntimeError(f"Flowise {method} {path} -> HTTP {exc.code}: {detail}") from None


def create_flow(export: dict, base: str | None = None, api_key: str | None = None) -> dict:
    """POST /api/v1/chatflows with the single flow in an ExportData file. Returns {id, url}."""
    flow = export["AgentFlowV2"][0]
    created = _request("POST", "/api/v1/chatflows",
                       {"name": flow["name"], "flowData": flow["flowData"], "type": "AGENTFLOW"},
                       base=base, api_key=api_key)
    fid = created.get("id")
    return {"id": fid, "url": f"{(base or base_url()).rstrip('/')}/v2/agentcanvas/{fid}", "response": {k: created.get(k) for k in ("id", "name", "type")}}


def get_flow(flow_id: str) -> dict:
    return _request("GET", f"/api/v1/chatflows/{flow_id}")


def predict(flow_id: str, question: str) -> dict:
    return _request("POST", f"/api/v1/prediction/{flow_id}", {"question": question, "streaming": False}, timeout=120)
