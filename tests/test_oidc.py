"""Tests for OIDC token validation."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException

from src.auth.oidc import verify_oidc_token


class TestVerifyOidcToken:
    @pytest.mark.asyncio
    async def test_missing_auth_header_raises_401(self):
        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            await verify_oidc_token(request, audience="test-audience")
        assert exc_info.value.status_code == 401
        assert "Missing OIDC token" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_non_bearer_header_raises_401(self):
        request = MagicMock()
        request.headers = {"Authorization": "Basic abc123"}
        with pytest.raises(HTTPException) as exc_info:
            await verify_oidc_token(request, audience="test-audience")
        assert exc_info.value.status_code == 401

    @pytest.mark.asyncio
    async def test_valid_token_returns_email(self):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer valid-token"}

        with patch("src.auth.oidc.id_token.verify_oauth2_token") as mock_verify:
            mock_verify.return_value = {
                "iss": "https://accounts.google.com",
                "email": "user@example.com",
                "sub": "12345",
            }
            result = await verify_oidc_token(request, audience="test-audience")
            assert result == "user@example.com"

    @pytest.mark.asyncio
    async def test_invalid_issuer_raises_401(self):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer valid-token"}

        with patch("src.auth.oidc.id_token.verify_oauth2_token") as mock_verify:
            mock_verify.return_value = {
                "iss": "https://evil.com",
                "email": "user@example.com",
            }
            with pytest.raises(HTTPException) as exc_info:
                await verify_oidc_token(request, audience="test-audience")
            assert exc_info.value.status_code == 401
            assert "Invalid token issuer" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_invalid_token_raises_401(self):
        request = MagicMock()
        request.headers = {"Authorization": "Bearer bad-token"}

        with patch("src.auth.oidc.id_token.verify_oauth2_token") as mock_verify:
            mock_verify.side_effect = ValueError("Invalid token")
            with pytest.raises(HTTPException) as exc_info:
                await verify_oidc_token(request, audience="test-audience")
            assert exc_info.value.status_code == 401
            assert "Invalid OIDC token" in exc_info.value.detail
