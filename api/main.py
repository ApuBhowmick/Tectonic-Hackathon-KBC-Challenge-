"""FastAPI app: JSON API plus one static page (web/index.html). No debug mode, generic error messages."""
from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse

from api.routes import admin, auth, customer, public
from api.security import RateLimiter
from api.services import ApiError
from api.settings import Settings, SettingsError, load_settings
from engine.config import load_config
from engine.db import REPO_ROOT

log = logging.getLogger("api")
PAGE = REPO_ROOT / "web" / "index.html"
CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    app = FastAPI(title="KBC Life Moments", debug=False, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.config = load_config()
    app.state.limiter = RateLimiter(settings.login_max_failures, settings.login_window_seconds)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > settings.max_body_bytes:
            return JSONResponse({"detail": "Request too large"}, status_code=413)
        resp = await call_next(request)
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = CSP
        if request.url.path != "/":
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    @app.exception_handler(RequestValidationError)
    async def _invalid(_: Request, __: RequestValidationError):
        return JSONResponse({"detail": "Invalid request"}, status_code=422)   # never echo the input back

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.error("unhandled error: %s", type(exc).__name__)                  # no request data, no PII
        return JSONResponse({"detail": "Internal error"}, status_code=500)

    app.include_router(auth)
    app.include_router(customer)
    app.include_router(admin)
    app.include_router(public)

    @app.get("/", include_in_schema=False)
    def page():
        return FileResponse(PAGE, media_type="text/html")

    return app


def _unconfigured_app(message: str) -> FastAPI:
    app = FastAPI(debug=False, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def _503(path: str):
        return JSONResponse({"detail": "Server is not configured"}, status_code=503)

    log.error("API not configured: %s", message)
    print(f"ERROR: API not configured: {message}")
    return app


try:
    app = create_app()
except SettingsError as _exc:
    app = _unconfigured_app(str(_exc))
