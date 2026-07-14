"""Frontend static route registration."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import FileResponse, RedirectResponse

_FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
_APP_SHELL_CACHE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Expires": "0",
    "Pragma": "no-cache",
}
_logger = logging.getLogger(__name__)


def configure_frontend_routes(
    app: FastAPI,
    *,
    frontend_dir: Path = _FRONTEND_DIR,
) -> None:
    index_file = frontend_dir / "index.html"
    if not index_file.exists():
        _logger.warning(
            "Frontend files not found at %s; /app routes will not be served.",
            frontend_dir,
        )
        return
    app.mount(
        "/app/static",
        StaticFiles(directory=frontend_dir),
        name="frontend-static",
    )

    @app.get("/", include_in_schema=False)
    def root_redirect():
        return RedirectResponse(url="/app/")

    @app.get("/app", include_in_schema=False)
    def app_redirect(request: Request):
        query = request.url.query
        target = f"/app/?{query}" if query else "/app/"
        return RedirectResponse(url=target)

    sw_file = frontend_dir / "sw.js"

    @app.get("/app/sw.js", include_in_schema=False)
    def service_worker():
        # Served at /app/sw.js so its registration scope can default to /app/
        # without setting a Service-Worker-Allowed header.
        return FileResponse(
            sw_file,
            media_type="application/javascript",
            headers=_APP_SHELL_CACHE_HEADERS,
        )

    @app.post("/app/share-target", include_in_schema=False)
    def share_target_fallback():
        # The active service worker intercepts this POST and parks the file
        # in the share-target inbox before redirecting. This server-side
        # handler exists only as a graceful fallback for the brief window
        # between install and first activation, or for browsers without
        # service worker support. The shared payload is lost in that path;
        # we still redirect the user into the app so the failure is visible.
        return RedirectResponse(url="/app/capture", status_code=303)

    @app.get("/app/", include_in_schema=False)
    @app.get("/app/{_path:path}", include_in_schema=False)
    def frontend_index(_path: str = ""):
        return FileResponse(index_file, headers=_APP_SHELL_CACHE_HEADERS)
