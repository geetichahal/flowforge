"""Assemble the Hugging Face Space folder (Docker SDK) from this repository.

    python tools/build_space.py [OUT_DIR]      (default: build/space)
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "build" / "space"
if out.exists():
    shutil.rmtree(out)
shutil.copytree(ROOT / "space", out)
ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "ui.py")  # the Gradio UI is not used on the Space
shutil.copytree(ROOT / "flowforge", out / "flowforge", ignore=ignore)
shutil.copytree(ROOT / "web", out / "web")
print(f"Space folder written to {out} ({sum(1 for p in out.rglob('*') if p.is_file())} files)")
