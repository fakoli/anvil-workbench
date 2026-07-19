"""ASGI entrypoint kept separate so importing API helpers never opens a database."""
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from .api import create_app

app = create_app()
_static = Path("/app/static")
if _static.is_dir():
    # The published single-image hub serves the browser shell and API from one
    # private endpoint. Specific /api and /healthz routes remain higher priority.
    app.mount("/", StaticFiles(directory=_static, html=True), name="workbench-ui")
