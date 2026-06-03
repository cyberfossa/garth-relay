"""Authentication and OAuth2 routes with JWT session cookies."""

from datetime import UTC, datetime, timedelta

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from src.auth.google_oauth2 import GoogleOAuth2Service, validate_id_token
from src.auth.session import create_jwt, get_current_user
from src.config import AppConfig
from src.db.firestore_client import FirestoreClient
from src.services.oauth_state_store import OAuthStateStore

logger = structlog.get_logger()


def create_auth_router(  # noqa: C901
    config: AppConfig,
    oauth_service: GoogleOAuth2Service,
    db_client: FirestoreClient | None = None,
) -> APIRouter:
    """Create auth router with OAuth2 login/callback/logout.

    Args:
        config: Application configuration
        oauth_service: Google OAuth2 service instance
    """
    auth_router = APIRouter(prefix="/auth", tags=["auth"])
    state_store = OAuthStateStore(db_client)

    @auth_router.get("/login")
    async def login():
        auth_url, state = oauth_service.generate_authorization_url()
        await state_store.store_state(state, "app_login")
        return RedirectResponse(url=auth_url, status_code=302)

    @auth_router.get("/callback")
    async def callback(state: str, code: str | None = None, error: str | None = None):
        if error or not code:
            await state_store.pop_state(state)
            return RedirectResponse(url="/login", status_code=302)

        # Validate state
        stored_purpose = await state_store.pop_state(state)
        if stored_purpose != "app_login":
            raise HTTPException(status_code=403, detail="Invalid or expired state parameter")

        # Exchange code for tokens
        token_response = await oauth_service.exchange_code_for_token(code)
        if not token_response:
            raise HTTPException(status_code=400, detail="Token exchange failed")

        # Validate ID token
        if not token_response.id_token:
            raise HTTPException(status_code=400, detail="No id_token in response")

        user_info = validate_id_token(token_response.id_token, config.google_client_id)
        if not user_info:
            raise HTTPException(status_code=400, detail="Invalid id_token")

        user_id = user_info["sub"]
        email = user_info["email"]
        name = user_info["name"]

        # Create JWT session
        jwt_token = create_jwt(
            user_id=user_id,
            email=email,
            name=name,
            secret=config.jwt_secret_key,
            algorithm=config.jwt_algorithm,
        )

        # Persist user profile and Google OAuth tokens for background polling
        if db_client:
            db_client.save_user_profile(user_id, email, name)
            expires_at = datetime.now(UTC) + timedelta(seconds=token_response.expires_in)
            db_client.save_oauth_token(
                user_id,
                "google",
                token_response.access_token,
                token_response.refresh_token,
                expires_at,
            )

        response = RedirectResponse(url="/dashboard", status_code=302)
        response.set_cookie(
            key="session",
            value=jwt_token,
            httponly=True,
            secure=not config.debug,
            samesite="lax",
            max_age=86400,
            path="/",
        )
        return response

    @auth_router.post("/logout")
    async def logout():
        response = RedirectResponse(url="/login", status_code=302)
        response.delete_cookie(key="session", path="/")
        return response

    @auth_router.get("/status")
    async def auth_status(request: Request):
        try:
            user_id = await get_current_user(request, config.jwt_secret_key, config.jwt_algorithm)
        except HTTPException:
            return {"authenticated": False}
        return {"authenticated": True, "user_id": user_id}

    return auth_router
