"""Serve the built React/Cytoscape frontend from the FastAPI app (post-v1 one-click packaging).

In dev, Vite serves the UI on :5173 and proxies ``/api`` to the backend on :8000. For the packaged
one-click launcher there is a single origin: FastAPI serves the built SPA (``frontend/dist``) at
``/`` and the API at ``/api`` — so the frontend's relative ``fetch("/api/graph")`` works with no
proxy and no CORS. Mounting is a no-op when the build is absent (e.g. CI without ``npm run build``),
so importing the app never requires a frontend build.

Note (SEC-17): "single origin, no CORS" removes the app's *own* need to relax CORS; it is NOT a
cross-site defense. A hostile page can still issue cross-site requests to the loopback API — the actual
request-provenance defense (loopback ``Host`` allowlist + Origin/Referer check on state-changing methods)
lives in ``middleware.RequestProvenanceMiddleware`` (Batch 2 / SEC-02/SEC-04), not here.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException

from .app_paths import resource_path

# The built SPA — a bundled READ-ONLY resource (P7): _MEIPASS/frontend/dist when frozen, else
# repo-root/frontend/dist in source. Routed through resource_path so the frozen app finds it.
FRONTEND_DIST = resource_path("frontend/dist")

# Path prefixes that must NOT be swallowed by the SPA fallback (they are API/infra, not app routes).
_RESERVED = ("api", "health", "docs", "redoc", "openapi.json")


def frontend_dist() -> Path | None:
    """The built SPA directory, or ``None`` if it hasn't been built yet."""
    return FRONTEND_DIST if (FRONTEND_DIST / "index.html").exists() else None


def mount_frontend(app, dist: Path | None = None) -> bool:
    """Mount the built SPA on ``app``. Returns ``True`` if mounted, ``False`` if no build exists.

    ``/assets/*`` is served by Starlette ``StaticFiles`` (traversal-safe, with caching). Every other
    non-reserved GET returns ``index.html`` (the SPA shell does its own client-side routing). Defined
    last, so the explicit API routes always win.
    """
    dist = dist or frontend_dist()
    if dist is None:
        return False
    index = dist / "index.html"
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

    @app.get("/", include_in_schema=False)
    def _spa_root() -> FileResponse:
        return FileResponse(index)

    @app.get("/{full_path:path}", include_in_schema=False)
    def _spa_fallback(full_path: str) -> FileResponse:
        # API/infra paths must 404 here rather than masquerade as the SPA shell.
        if full_path.split("/", 1)[0] in _RESERVED:
            raise HTTPException(status_code=404, detail="not found")
        return FileResponse(index)  # client-side route -> serve the shell

    return True
