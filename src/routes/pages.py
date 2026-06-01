"""Pages router for garth-relay SSR views."""

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.templating import Jinja2Templates

from src.auth.session import get_current_user
from src.config import AppConfig
from src.db import FirestoreClient

logger = structlog.get_logger()


def create_pages_router(
    templates: Jinja2Templates,
    db_client: FirestoreClient | None,
    config: AppConfig,
) -> APIRouter:
    """Create pages router with template rendering.

    Args:
        templates: Jinja2Templates instance.
        db_client: Firestore client (may be None in dev).
        config: Application configuration.

    Returns:
        Configured APIRouter with page routes.
    """
    pages_router = APIRouter(tags=["pages"])

    @pages_router.get("/")
    async def root(request: Request):
        try:
            await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
            return RedirectResponse(url="/dashboard", status_code=302)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

    @pages_router.get("/login")
    async def login(request: Request):
        try:
            await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
            return RedirectResponse(url="/dashboard", status_code=302)
        except HTTPException:
            return templates.TemplateResponse(
                request,
                "login.html",
                {"request": request, "error_message": None},
            )

    @pages_router.get("/dashboard")
    async def dashboard(request: Request):
        try:
            user_id = await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

        user_profile = None
        google_connected = False
        garmin_connected = False
        recent_syncs = []

        if db_client:
            user_profile = db_client.get_user_profile(user_id)
            google_token = db_client.get_oauth_token(user_id, "google")
            garmin_connected = db_client.has_garmin_session(user_id)
            google_connected = google_token is not None
            recent_syncs = db_client.get_recent_syncs(user_id, limit=10)

        return templates.TemplateResponse(
            request,
            "dashboard.html",
            {
                "request": request,
                "error_message": None,
                "user_profile": user_profile,
                "google_connected": google_connected,
                "garmin_connected": garmin_connected,
                "recent_syncs": recent_syncs,
            },
        )

    @pages_router.get("/connect-google")
    async def connect_google(request: Request):
        try:
            await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return RedirectResponse(url="/login", status_code=302)

        return templates.TemplateResponse(
            request,
            "connect-google.html",
            {"request": request, "error_message": None},
        )

    return pages_router
