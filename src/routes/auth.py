"""Authentication and OAuth2 routes with JWT session cookies."""

import secrets

import structlog
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse

from src.auth.google_oauth2 import GoogleOAuth2Service, validate_id_token
from src.auth.session import create_jwt, get_current_user
from src.config import AppConfig

logger = structlog.get_logger()

# In-memory state store (sufficient for single-instance; use Firestore for multi-instance)
_oauth_states: dict[str, str] = {}


def create_auth_router(
    config: AppConfig,
    oauth_service: GoogleOAuth2Service,
) -> APIRouter:
    """Create auth router with OAuth2 login/callback/logout.

    Args:
        config: Application configuration
        oauth_service: Google OAuth2 service instance
    """
    auth_router = APIRouter(prefix="/auth", tags=["auth"])

    @auth_router.get("/login")
    async def login():
        auth_url, state = oauth_service.generate_authorization_url()
        _oauth_states[state] = "app_login"
        return RedirectResponse(url=auth_url, status_code=302)

    @auth_router.get("/callback")
    async def callback(code: str, state: str):
        # Validate state
        stored_purpose = _oauth_states.pop(state, None)
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
        response = RedirectResponse(url="/auth/login", status_code=302)
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
