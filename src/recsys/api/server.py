"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response

from recsys import __version__
from recsys.api.routes import router
from recsys.config import get_settings
from recsys.db import init_db
from recsys.log import get_logger, setup_logging

log = get_logger(__name__)

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


class _NoCacheStatic(StaticFiles):
    """Serve the UI without caching.

    The window is a webview with no devtools reload; a cached app.js after an
    edit is a confusing failure mode for zero performance gain locally.
    """

    def file_response(self, *args, **kwargs) -> Response:
        resp = super().file_response(*args, **kwargs)
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return resp


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    version = init_db()
    log.info("db ready at %s (schema v%d)", settings.db_path, version)
    yield


def create_app() -> FastAPI:
    setup_logging()
    app = FastAPI(
        title="recsys",
        version=__version__,
        description="Cross-domain hybrid recommender -- local catalog API",
        lifespan=lifespan,
    )
    app.include_router(router)
    # Registered last so /api/* wins; html=True serves index.html at "/".
    app.mount("/", _NoCacheStatic(directory=UI_DIR, html=True), name="ui")
    return app


app = create_app()
