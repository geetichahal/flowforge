"""Build a static copy of the website that runs entirely in the browser (Pyodide).

    python tools/build_site.py [OUT_DIR]      (default: site/)

Host OUT_DIR anywhere that serves static files (GitHub Pages, Netlify, `python -m http.server`).
The rule-based planner runs in the browser; the LLM planner and Push to Flowise need the server.
"""
import html
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE = ["__init__.py", "catalog.py", "plan.py", "planner.py", "llm.py", "validator.py", "simulator.py",
          "pipeline.py", "generators/__init__.py", "generators/flowise.py", "generators/n8n.py",
          "generators/python_gen.py", "data/flowise_node_specs.json"]

out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "site"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(ROOT / "web", out)
for f in ENGINE:
    dest = out / "engine" / "flowforge" / f
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "flowforge" / f, dest)
(out / ".nojekyll").write_text("")  # GitHub Pages: serve files as-is

# Point the static site at a FlowForge server, if one is configured (e.g. the Hugging Face Space).
api_url = os.environ.get("FLOWFORGE_API_URL", "").strip()
if api_url:
    index = out / "index.html"
    index.write_text(index.read_text().replace('<meta name="flowforge-api" content="">',
                                               f'<meta name="flowforge-api" content="{html.escape(api_url, quote=True)}">'))
    print(f"Website will use the server at {api_url} (falls back to the in-browser engine)")
print(f"Static site written to {out} ({sum(1 for _ in out.rglob('*') if _.is_file())} files)")
