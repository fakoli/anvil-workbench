"""ASGI entrypoint kept separate so importing API helpers never opens a database.

The app is composed through :func:`workbench.deployment.create_live_app`, the one
place a live deployment opts injectable supervision surfaces in via explicit,
env-driven operator decision (``WORKBENCH_LIVE_SURFACES``).  With that switch
empty this is byte-for-byte the hermetic ``create_app()`` default -- every
injectable surface stays ``None`` and fails closed with 503.
"""
from pathlib import Path

from fastapi.staticfiles import StaticFiles

from .deployment import create_live_app

app = create_live_app()
_static = Path("/app/static")
if _static.is_dir():
    # The published single-image hub serves the browser shell and API from one
    # private endpoint. Specific /api and /healthz routes remain higher priority.
    app.mount("/", StaticFiles(directory=_static, html=True), name="workbench-ui")
