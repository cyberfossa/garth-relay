from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import urllib.parse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jose import jwt

from src.auth.google_oauth2 import GoogleOAuth2Config, GoogleOAuth2Service
from src.auth.session import create_jwt, decode_jwt, set_session_cookie, clear_session_cookie
from src.config import get_config
from src.routes.auth import create_auth_router


class TestJWT:
    def test_jwt_contains_correct_claims(self):
        secret = "test-jwt-secret-key-for-testing"
        token = create_jwt(
            user_id="google-user-123",
            email="test@example.com",
            name="Test User",
            secret=secret,
        )
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        assert payload["sub"] == "google-user-123"
        assert payload["email"] == "test@example.com"
        assert payload["name"] == "Test User"
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        now = datetime.now(UTC)
        assert timedelta(hours=23) < (exp - now) < timedelta(hours=25)

    def test_decode_jwt_roundtrip(self):
        secret = "test-jwt-secret"
        token = create_jwt(user_id="u1", email="e@x.com", name="N", secret=secret)
        payload = decode_jwt(token, secret)
        assert payload["sub"] == "u1"

    def test_expired_jwt_raises(self):
        secret = "test-jwt-secret"
        token = create_jwt(user_id="u1", email="e@x.com", name="N", secret=secret, expiry_hours=-1)
        with pytest.raises(Exception):
            decode_jwt(token, secret)

    def test_custom_algorithm(self):
        secret = "test-jwt-secret"
        token = create_jwt(user_id="u1", email="e@x.com", name="N", secret=secret, algorithm="HS384")
        payload = decode_jwt(token, secret, algorithm="HS384")
        assert payload["sub"] == "u1"

    def test_custom_expiry(self):
        secret = "test-jwt-secret"
        token = create_jwt(user_id="u1", email="e@x.com", name="N", secret=secret, expiry_hours=48)
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        exp = datetime.fromtimestamp(payload["exp"], tz=UTC)
        now = datetime.now(UTC)
        assert timedelta(hours=47) < (exp - now) < timedelta(hours=49)


class TestCookieUtilities:
    def test_set_session_cookie_secure_in_production(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        set_session_cookie(response, "token123", debug=False)
        response.set_cookie.assert_called_once_with(
            key="session",
            value="token123",
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=86400,
        )

    def test_set_session_cookie_insecure_in_debug(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        set_session_cookie(response, "token123", debug=True)
        response.set_cookie.assert_called_once_with(
            key="session",
            value="token123",
            httponly=True,
            secure=False,
            samesite="lax",
            max_age=86400,
        )

    def test_clear_session_cookie(self):
        from unittest.mock import MagicMock

        response = MagicMock()
        clear_session_cookie(response, debug=False)
        response.delete_cookie.assert_called_once_with(
            key="session",
            httponly=True,
            secure=True,
            samesite="lax",
        )


class TestGetCurrentUser:
    @pytest.mark.asyncio
    async def test_no_cookie_raises_redirect(self):
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from src.auth.session import get_current_user

        request = MagicMock()
        request.cookies = {}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, jwt_secret="secret")
        assert exc_info.value.status_code == 302

    @pytest.mark.asyncio
    async def test_valid_cookie_returns_user_id(self):
        from unittest.mock import MagicMock

        from src.auth.session import get_current_user

        secret = "test-secret"
        token = create_jwt(user_id="user-42", email="e@x.com", name="N", secret=secret)
        request = MagicMock()
        request.cookies = {"session": token}
        user_id = await get_current_user(request, jwt_secret=secret)
        assert user_id == "user-42"

    @pytest.mark.asyncio
    async def test_invalid_cookie_raises_redirect(self):
        from unittest.mock import MagicMock

        from fastapi import HTTPException

        from src.auth.session import get_current_user

        request = MagicMock()
        request.cookies = {"session": "invalid-token"}
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user(request, jwt_secret="secret")
        assert exc_info.value.status_code == 302


def _extract_state(response) -> str:
    location = response.headers["location"]
    parsed = urllib.parse.urlparse(location)
    params = urllib.parse.parse_qs(parsed.query)
    return params["state"][0]


@pytest.fixture
def mock_oauth_service():
    cfg = get_config()
    google_config = GoogleOAuth2Config(
        client_id=cfg.google_client_id,
        client_secret=cfg.google_client_secret,
        redirect_uri=cfg.google_oauth_redirect_uri,
    )
    return GoogleOAuth2Service(google_config)


@pytest.fixture
def auth_app(mock_oauth_service):
    cfg = get_config()
    app = FastAPI()
    router = create_auth_router(config=cfg, oauth_service=mock_oauth_service)
    app.include_router(router)
    return app


@pytest.fixture
def auth_client(auth_app):
    return TestClient(auth_app, follow_redirects=False)


class TestLoginRedirect:
    def test_redirects_to_google(self, auth_client):
        response = auth_client.get("/auth/login")
        assert response.status_code == 302
        location = response.headers["location"]
        assert "accounts.google.com" in location
        assert "openid" in location
        assert "email" in location

    def test_includes_state_parameter(self, auth_client):
        response = auth_client.get("/auth/login")
        state = _extract_state(response)
        assert len(state) > 20


class TestCallback:
    def test_creates_jwt_cookie(self, auth_client, mock_oauth_service):
        login_resp = auth_client.get("/auth/login")
        state = _extract_state(login_resp)

        mock_oauth_service.exchange_code_for_token = AsyncMock(
            return_value=MagicMock(
                access_token="google-access",
                id_token="fake-id-token",
                refresh_token="refresh-tok",
                expires_in=3600,
                token_type="Bearer",
            )
        )

        with patch("src.routes.auth.validate_id_token") as mock_validate:
            mock_validate.return_value = {
                "sub": "google-user-123",
                "email": "test@example.com",
                "name": "Test User",
            }
            response = auth_client.get(f"/auth/callback?code=auth-code-123&state={state}")

        assert response.status_code == 302
        assert "/dashboard" in response.headers["location"]
        set_cookie = response.headers.get("set-cookie", "")
        assert "session=" in set_cookie
        assert "httponly" in set_cookie.lower()

    def test_invalid_state_returns_403(self, auth_client):
        response = auth_client.get("/auth/callback?code=auth-code&state=invalid-state")
        assert response.status_code == 403

    def test_token_exchange_failure_returns_400(self, auth_client, mock_oauth_service):
        login_resp = auth_client.get("/auth/login")
        state = _extract_state(login_resp)

        mock_oauth_service.exchange_code_for_token = AsyncMock(return_value=None)
        response = auth_client.get(f"/auth/callback?code=bad-code&state={state}")
        assert response.status_code == 400


class TestLogout:
    def test_clears_session_cookie(self, auth_client):
        cfg = get_config()
        token = create_jwt(
            user_id="u1", email="e@x.com", name="N", secret=cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm
        )
        auth_client.cookies.set("session", token)
        response = auth_client.post("/auth/logout")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["location"]


class TestAuthStatus:
    def test_unauthenticated_returns_false(self, auth_client):
        response = auth_client.get("/auth/status")
        assert response.status_code == 200
        assert response.json()["authenticated"] is False

    def test_authenticated_returns_user_id(self, auth_client):
        cfg = get_config()
        token = create_jwt(
            user_id="user-42", email="e@x.com", name="N", secret=cfg.jwt_secret_key, algorithm=cfg.jwt_algorithm
        )
        auth_client.cookies.set("session", token)
        response = auth_client.get("/auth/status")
        data = response.json()
        assert data["authenticated"] is True
        assert data["user_id"] == "user-42"
