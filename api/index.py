"""Vercel/FastAPI entry point, also runnable with uvicorn api.index:app."""

from pathlib import Path
import sys

# Hosting installs only the minimal requirements, not the desktop project.
_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "src"
if str(_SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(_SOURCE_ROOT))

from trade_area_local.web_api import create_app

app = create_app()
