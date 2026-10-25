"""Application FastAPI du Dashboard Nexbot."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from ..core.config import BOT_NAME, VERSION, get_settings
from ..core.migrations import run_migrations
from . import diagnostics
from .routes import api, pages
from .web import BASE_DIR, templates

log = logging.getLogger("nexbot.dashboard")

CSP = ("default-src 'self'; img-src 'self' https://cdn.discordapp.com https: data:; "
       "style-src 'self' https://fonts.googleapis.com 'unsafe-inline'; font-src https://fonts.gstatic.com; "
       "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
       "form-action 'self' https://discord.com https://checkout.stripe.com")


def create_app() -> FastAPI:
    state = {"migrated": False}

    def ensure_db() -> list[str]:
        """Prépare la base. Ne lève jamais d'exception : renvoie la liste des problèmes à afficher."""
        problems = diagnostics.config_problems()
        if problems:
            return problems
        if not state["migrated"]:
            try:
                run_migrations()  # idempotent : sûr à chaque démarrage à froid (Vercel)
                state["migrated"] = True
            except Exception as exc:
                log.exception("Connexion à la base de données impossible")
                return ["Connexion à la base de données impossible. Vérifiez DATABASE_URL "
                        "(adresse Neon complète, copiée sans espace). Détail : " + diagnostics.describe_db_error(exc)]
        return []

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ensure_db()
        yield

    app = FastAPI(title=f"{BOT_NAME} Dashboard", version=VERSION, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static"), check_dir=False), name="static")
    app.include_router(pages.router)
    app.include_router(api.router)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        # certains hébergeurs serverless n'envoient pas l'événement lifespan
        problems = [] if request.url.path.startswith("/static/") else ensure_db()
        if problems:
            if request.url.path.startswith("/api/"):
                return JSONResponse({"message": problems[0], "errors": []}, status_code=503)
            return HTMLResponse(diagnostics.render_page(problems), status_code=503)
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        if get_settings().is_production:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-store")
        return response

    def _wants_html(request: Request) -> bool:
        return not request.url.path.startswith("/api/") and "text/html" in request.headers.get("accept", "")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if _wants_html(request):
            if exc.status_code == 401:
                return RedirectResponse("/login", status_code=302)
            message = exc.detail["message"] if isinstance(exc.detail, dict) else str(exc.detail)
            return templates.TemplateResponse(request, "error.html",
                                              {"request": request, "user": None, "s": get_settings(),
                                               "code": exc.status_code, "message": message},
                                              status_code=exc.status_code)
        detail = exc.detail if isinstance(exc.detail, dict) else {"message": str(exc.detail), "errors": []}
        return JSONResponse(detail, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse({"message": "Requête invalide", "errors": [
            {"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()]},
            status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        log.exception("Erreur non gérée sur %s", request.url.path)
        if _wants_html(request):
            return HTMLResponse("<h1>Erreur interne</h1><p>L'erreur a été enregistrée.</p>", status_code=500)
        return JSONResponse({"message": "Erreur interne, réessayez.", "errors": []}, status_code=500)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True, "name": BOT_NAME, "version": VERSION}

    return app


app = create_app()
