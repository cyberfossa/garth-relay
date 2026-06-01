from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt

from src.auth.session import create_jwt, decode_jwt, set_session_cookie, clear_session_cookie


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
