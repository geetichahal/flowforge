"""Minimal Ollama client (standard library only)."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

DEFAULT_MODEL = "qwen3:4b"


def host() -> str:
    h = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    return h if h.startswith("http") else f"http://{h}"


def model() -> str:
    return os.environ.get("FLOWFORGE_MODEL", DEFAULT_MODEL)


def agent_model() -> str:
    """Model used inside generated agents and LLM steps: a non-thinking model answers fast and clean."""
    return os.environ.get("FLOWFORGE_AGENT_MODEL", "qwen3:4b-instruct")


def available(timeout: float | None = None) -> tuple[bool, str]:
    """(True, model) if Ollama answers and has the configured model pulled."""
    if timeout is None:  # a busy 8 GB machine can take a few seconds to answer
        timeout = float(os.environ.get("FLOWFORGE_OLLAMA_TIMEOUT", "5"))
    try:
        with urllib.request.urlopen(f"{host()}/api/tags", timeout=timeout) as r:
            tags = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, f"Ollama not reachable at {host()} ({exc.__class__.__name__})"
    names = {m.get("name", "") for m in tags.get("models", [])}
    want = model()
    if want in names or f"{want}:latest" in names:
        return True, want
    return False, f"Ollama is running but model '{want}' is not pulled (ollama pull {want})"


def chat_json(system: str, user: str, timeout: float = 300) -> dict:
    """Ask the model for a single JSON object and parse it."""
    body = {
        "model": model(),
        "stream": False,
        "format": "json",
        "think": False,
        "options": {"temperature": 0},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    req = urllib.request.Request(f"{host()}/api/chat", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    text = data.get("message", {}).get("content", "")
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    try:
        return json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise ValueError(f"model did not return JSON: {text[:200]!r}")
        return json.loads(m.group(0))
