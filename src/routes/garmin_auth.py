"""Garmin authentication routes."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Protocol, cast

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.templating import Jinja2Templates

from src.auth.session import get_current_user
from src.config import AppConfig
from src.crypto import TokenEncryptor
from src.db.firestore_client import FirestoreClient
from src.services.garmin_client import GarminClient

logger = structlog.get_logger()


class _GarminAuthDB(Protocol):
    def save_mfa_state(self, user_id: str, encrypted_state: str, expires_at: datetime | None = None) -> bool: ...

    def get_mfa_state(self, user_id: str) -> dict[str, object] | None: ...

    def delete_mfa_state(self, user_id: str) -> bool: ...

    def delete_garmin_session(self, user_id: str) -> bool: ...


def create_garmin_auth_router(
    templates: Jinja2Templates,
    db_client: FirestoreClient,
    config: AppConfig,
    encryptor: TokenEncryptor,
) -> APIRouter:
    garmin_router = APIRouter(prefix="/garmin", tags=["garmin-auth"])
    typed_db: _GarminAuthDB = cast(_GarminAuthDB, db_client)

    def _is_htmx(request: Request) -> bool:
        return request.headers.get("HX-Request") == "true"

    async def _require_user(request: Request) -> str:
        try:
            return await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            if _is_htmx(request):
                raise HTTPException(status_code=200, headers={"HX-Redirect": "/login"}) from None
            raise HTTPException(status_code=302, headers={"Location": "/login"}) from None

    def _is_hx_redirect(exc: HTTPException) -> bool:
        return "HX-Redirect" in (exc.headers or {})

    def _extract_mfa_state_json(stored_state: object) -> str | None:
        if not isinstance(stored_state, dict):
            return None
        typed_state = cast(dict[str, object], stored_state)
        raw_state_obj = typed_state.get("encrypted_state")
        if not isinstance(raw_state_obj, str) or not raw_state_obj:
            return None
        try:
            json.loads(raw_state_obj)
        except json.JSONDecodeError:
            return None
        return raw_state_obj

    @garmin_router.get("/connect")
    async def garmin_connect(request: Request):
        try:
            _ = await _require_user(request)
        except HTTPException as exc:
            if _is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)
        return templates.TemplateResponse(request, "garmin-connect.html", {"request": request, "error_message": None})

    @garmin_router.post("/auth")
    async def garmin_auth(request: Request):  # noqa: PLR0911
        try:
            user_id = await _require_user(request)
        except HTTPException as exc:
            if _is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", "")).strip()
        await form.close()

        if not email or not password:
            return HTMLResponse('<p class="error">Email and password are required.</p>', status_code=400)

        try:
            client = GarminClient.create_for_user(user_id, db_client.db, encryptor)
            await client.login(email, password)
            if _is_htmx(request):
                return Response(headers={"HX-Redirect": "/dashboard"})
            return RedirectResponse(url="/dashboard", status_code=302)
        except Exception:
            logger.info("Direct Garmin login failed for user %s, trying MFA path", user_id)

        try:
            client = GarminClient()
            mfa_state_json, status = await client.login_with_mfa(email, password)
            if status != "mfa_required":
                return HTMLResponse('<p class="error">Unexpected Garmin authentication state.</p>', status_code=500)

            normalized_mfa_state_json = json.dumps(json.loads(mfa_state_json))
            _ = typed_db.save_mfa_state(user_id, normalized_mfa_state_json)
            return templates.TemplateResponse(request, "garmin-mfa.html", {"request": request})
        except Exception as exc:
            logger.warning("Garmin authentication failed for user %s: %s", user_id, exc)
            return HTMLResponse(
                '<p class="error">Invalid Garmin credentials or MFA challenge failed.</p>', status_code=401
            )

    @garmin_router.post("/mfa")
    async def garmin_mfa(request: Request):  # noqa: PLR0911
        try:
            user_id = await _require_user(request)
        except HTTPException as exc:
            if _is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        form = await request.form()
        mfa_code = str(form.get("mfa_code", "")).strip()
        await form.close()

        if not mfa_code:
            return HTMLResponse('<p class="error">MFA code is required.</p>', status_code=400)

        stored_mfa_state = cast(object, typed_db.get_mfa_state(user_id))
        mfa_state_json = _extract_mfa_state_json(stored_mfa_state)
        if mfa_state_json is None:
            _ = typed_db.delete_mfa_state(user_id)
            return HTMLResponse('<p class="error">MFA session expired. Please start again.</p>', status_code=400)

        try:
            client = GarminClient.create_for_user(user_id, db_client.db, encryptor)
            await client.complete_mfa(mfa_state_json, mfa_code)
            _ = typed_db.delete_mfa_state(user_id)
            if _is_htmx(request):
                return Response(headers={"HX-Redirect": "/dashboard"})
            return RedirectResponse(url="/dashboard", status_code=302)
        except Exception as exc:
            logger.warning("Garmin MFA verification failed for user %s: %s", user_id, exc)
            return HTMLResponse('<p class="error">Invalid MFA code. Please try again.</p>', status_code=401)

    @garmin_router.post("/disconnect")
    async def garmin_disconnect(request: Request):
        try:
            user_id = await _require_user(request)
        except HTTPException as exc:
            if _is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        disconnected = typed_db.delete_garmin_session(user_id)
        _ = typed_db.delete_mfa_state(user_id)
        return templates.TemplateResponse(
            request,
            "partials/garmin-auth-result.html",
            {
                "request": request,
                "success": bool(disconnected),
                "message": "Garmin disconnected." if disconnected else "Failed to disconnect Garmin.",
            },
            headers={"HX-Trigger": "connectionsChanged"},
        )

    _ = (garmin_connect, garmin_auth, garmin_mfa, garmin_disconnect)
    return garmin_router
