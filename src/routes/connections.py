"""Connection management routes (Google OAuth + Garmin)."""

from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast

import httpx
import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from garth.exc import GarthHTTPError, MFARequiredError
from starlette.templating import Jinja2Templates

from src.crypto import TokenEncryptor
from src.db.firestore_client import FirestoreClient
from src.routes.connections_helpers import is_htmx, require_user
from src.services.garmin_client import GarminClient, GarminRateLimitError, GarminSessionExpiredError

logger = structlog.get_logger()

_google_oauth_states: dict[str, str] = {}


class _GarminAuthDB(Protocol):
    def save_mfa_state(self, user_id: str, encrypted_state: str, expires_at: datetime | None = None) -> bool: ...

    def get_mfa_state(self, user_id: str) -> dict[str, object] | None: ...

    def delete_mfa_state(self, user_id: str) -> bool: ...

    def delete_garmin_session(self, user_id: str) -> bool: ...


def create_connections_router(  # noqa: C901, PLR0915
    templates: Jinja2Templates,
    db_client: FirestoreClient | None,
    encryptor: TokenEncryptor,
    jwt_secret: str,
    jwt_algorithm: str,
    google_client_id: str,
    google_client_secret: str,
    google_redirect_uri: str,
    app_base_url: str,
) -> APIRouter:
    """Create connections router with Google OAuth routes.

    Args:
        db_client: Firestore client instance (or None).
        jwt_secret: JWT secret key for session validation.
        jwt_algorithm: JWT algorithm.
        google_client_id: Google OAuth client ID.
        google_client_secret: Google OAuth client secret.
        google_redirect_uri: Google OAuth redirect URI.
        app_base_url: Application base URL.

    Returns:
        Configured APIRouter.
    """
    router = APIRouter(prefix="/connections", tags=["connections"])
    google_connect_redirect_uri = (
        f"{app_base_url.rstrip('/')}/connections/google/callback" if app_base_url else google_redirect_uri
    )

    @router.get("/google/connect")
    async def google_connect(request: Request):
        await require_user(request, jwt_secret, jwt_algorithm)
        return templates.TemplateResponse(request, "connect-google.html", {"error_message": None})

    @router.get("/google/auth")
    async def google_auth():
        state = secrets.token_urlsafe(32)
        _google_oauth_states[state] = "google_connect"

        scopes = "https://www.googleapis.com/auth/googlehealth.health_metrics_and_measurements.readonly"
        auth_url = (
            "https://accounts.google.com/o/oauth2/v2/auth"
            f"?client_id={google_client_id}"
            f"&redirect_uri={google_connect_redirect_uri}"
            f"&response_type=code"
            f"&scope={scopes}"
            f"&state={state}"
            f"&access_type=offline"
            f"&prompt=consent"
        )
        return RedirectResponse(url=auth_url, status_code=302)

    @router.get("/google/callback")
    async def google_callback(state: str, request: Request, code: str | None = None, error: str | None = None):
        if error or not code:
            _google_oauth_states.pop(state, None)
            return RedirectResponse(url="/login", status_code=302)

        stored_purpose = _google_oauth_states.pop(state, None)
        if stored_purpose != "google_connect":
            raise HTTPException(status_code=403, detail="Invalid or expired state parameter")

        token_payload = {
            "client_id": google_client_id,
            "client_secret": google_client_secret,
            "redirect_uri": google_connect_redirect_uri,
            "code": code,
            "grant_type": "authorization_code",
        }

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post("https://oauth2.googleapis.com/token", data=token_payload)

        if response.status_code != 200:
            raise HTTPException(status_code=400, detail="Token exchange failed")

        token_data = response.json()
        access_token = token_data.get("access_token")
        refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in")
        if not isinstance(access_token, str) or not access_token or not isinstance(expires_in, int):
            raise HTTPException(status_code=400, detail="Token exchange failed")

        user_id = await require_user(request, jwt_secret, jwt_algorithm)
        expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)

        if db_client is None:
            logger.warning("No db_client configured, skipping Google OAuth token save", user_id=user_id)
        else:
            _ = db_client.save_oauth_token(user_id, "google", access_token, refresh_token, expires_at)

        return RedirectResponse(url="/dashboard", status_code=302)

    @router.post("/google/disconnect")
    async def google_disconnect(request: Request):
        user_id = await require_user(request, jwt_secret, jwt_algorithm)

        if db_client is None:
            logger.warning("No db_client configured, skipping token deletion", user_id=user_id)
        else:
            db_client.delete_oauth_token(user_id, "google")

        logger.info("Google disconnected", user_id=user_id)

        if is_htmx(request):
            return Response(headers={"HX-Redirect": "/dashboard"})
        return RedirectResponse(url="/dashboard", status_code=302)

    # ── Garmin routes ──────────────────────────────────────────────────────

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

    @router.get("/garmin/connect")
    async def garmin_connect(request: Request):
        try:
            _ = await require_user(request, jwt_secret, jwt_algorithm)
        except HTTPException as exc:
            if is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)
        return templates.TemplateResponse(request, "garmin-connect.html", {"request": request, "error_message": None})

    @router.post("/garmin/auth")
    async def garmin_auth(request: Request):  # noqa: PLR0911
        if db_client is None:
            return Response(status_code=503, content="Database not configured")

        try:
            user_id = await require_user(request, jwt_secret, jwt_algorithm)
        except HTTPException as exc:
            if is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        typed_db: _GarminAuthDB = cast(_GarminAuthDB, db_client)

        form = await request.form()
        email = str(form.get("email", "")).strip()
        password = str(form.get("password", "")).strip()
        await form.close()

        if not email or not password:
            return HTMLResponse('<p class="error">Email and password are required.</p>', status_code=400)

        try:
            client = GarminClient.create_for_user(user_id, db_client.db, encryptor)
            await client.login(email, password)
            if is_htmx(request):
                return Response(headers={"HX-Redirect": "/dashboard"})
            return RedirectResponse(url="/dashboard", status_code=302)
        except (
            GarthHTTPError,
            GarminSessionExpiredError,
            GarminRateLimitError,
            MFARequiredError,
            ValueError,
            RuntimeError,
        ):
            logger.info("Direct Garmin login failed for user %s, trying MFA path", user_id)

        try:
            client = GarminClient()
            mfa_state_json, status = await client.login_with_mfa(email, password)
            if status != "mfa_required":
                return HTMLResponse('<p class="error">Unexpected Garmin authentication state.</p>', status_code=500)

            normalized_mfa_state_json = json.dumps(json.loads(mfa_state_json))
            _ = typed_db.save_mfa_state(user_id, normalized_mfa_state_json)
            return templates.TemplateResponse(request, "garmin-mfa.html", {"request": request})
        except (GarthHTTPError, GarminSessionExpiredError, GarminRateLimitError, ValueError, RuntimeError) as exc:
            logger.warning("Garmin authentication failed for user %s: %s", user_id, exc)
            return HTMLResponse(
                '<p class="error">Invalid Garmin credentials or MFA challenge failed.</p>', status_code=401
            )

    @router.post("/garmin/mfa")
    async def garmin_mfa(request: Request):  # noqa: PLR0911
        if db_client is None:
            return Response(status_code=503, content="Database not configured")

        try:
            user_id = await require_user(request, jwt_secret, jwt_algorithm)
        except HTTPException as exc:
            if is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        typed_db: _GarminAuthDB = cast(_GarminAuthDB, db_client)

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
            if is_htmx(request):
                return Response(headers={"HX-Redirect": "/dashboard"})
            return RedirectResponse(url="/dashboard", status_code=302)
        except (GarthHTTPError, GarminSessionExpiredError, GarminRateLimitError, ValueError, RuntimeError) as exc:
            logger.warning("Garmin MFA verification failed for user %s: %s", user_id, exc)
            return HTMLResponse('<p class="error">Invalid MFA code. Please try again.</p>', status_code=401)

    @router.post("/garmin/disconnect")
    async def garmin_disconnect(request: Request):
        if db_client is None:
            return Response(status_code=503, content="Database not configured")

        try:
            user_id = await require_user(request, jwt_secret, jwt_algorithm)
        except HTTPException as exc:
            if is_htmx(request) and _is_hx_redirect(exc):
                return Response(headers={"HX-Redirect": "/login"})
            return RedirectResponse(url="/login", status_code=302)

        typed_db: _GarminAuthDB = cast(_GarminAuthDB, db_client)

        typed_db.delete_garmin_session(user_id)
        typed_db.delete_mfa_state(user_id)
        logger.info("Garmin disconnected", user_id=user_id)

        if is_htmx(request):
            return Response(headers={"HX-Redirect": "/dashboard"})
        return RedirectResponse(url="/dashboard", status_code=302)

    return router
