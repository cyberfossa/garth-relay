"""Tests for Google OAuth2 service."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.auth.google_oauth2 import GoogleOAuth2Config, GoogleOAuth2Service, GoogleTokenResponse, validate_id_token


@pytest.fixture
def oauth_config():
    return GoogleOAuth2Config(
        client_id="test-client-id",
        client_secret="test-client-secret",
        redirect_uri="http://localhost:8080/auth/callback",
    )


@pytest.fixture
def oauth_service(oauth_config):
    return GoogleOAuth2Service(oauth_config)


class TestGenerateAuthorizationUrl:
    def test_returns_url_and_state(self, oauth_service):
        url, state = oauth_service.generate_authorization_url()
        assert "accounts.google.com" in url
        assert "client_id=test-client-id" in url
        assert "state=" in url
        assert len(state) > 20

    def test_includes_offline_access(self, oauth_service):
        url, _ = oauth_service.generate_authorization_url()
        assert "access_type=offline" in url
        assert "prompt=consent" in url

    def test_includes_openid_scope(self, oauth_service):
        url, _ = oauth_service.generate_authorization_url()
        assert "openid" in url
        assert "email" in url


class TestExchangeCodeForToken:
    @pytest.mark.asyncio
    async def test_success_returns_token_response(self, oauth_service):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "access-tok",
            "refresh_token": "refresh-tok",
            "expires_in": 3600,
            "token_type": "Bearer",
            "id_token": "id-tok",
        }

        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await oauth_service.exchange_code_for_token("auth-code")
            assert result is not None
            assert isinstance(result, GoogleTokenResponse)
            assert result.access_token == "access-tok"
            assert result.refresh_token == "refresh-tok"

    @pytest.mark.asyncio
    async def test_failure_returns_none(self, oauth_service):
        mock_response = MagicMock()
        mock_response.status_code = 400
        mock_response.text = "Bad Request"

        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await oauth_service.exchange_code_for_token("bad-code")
            assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, oauth_service):
        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client_class.side_effect = Exception("connection error")
            result = await oauth_service.exchange_code_for_token("code")
            assert result is None


class TestRefreshAccessToken:
    @pytest.mark.asyncio
    async def test_success_returns_token(self, oauth_service):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "access_token": "new-access",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await oauth_service.refresh_access_token("refresh-tok")
            assert result is not None
            assert result.access_token == "new-access"

    @pytest.mark.asyncio
    async def test_failure_returns_none(self, oauth_service):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            result = await oauth_service.refresh_access_token("bad-refresh")
            assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self, oauth_service):
        with patch("src.auth.google_oauth2.httpx.AsyncClient") as mock_client_class:
            mock_client_class.side_effect = Exception("network error")
            result = await oauth_service.refresh_access_token("tok")
            assert result is None


class TestValidateIdToken:
    def test_valid_token(self):
        with patch("src.auth.google_oauth2.jwt.decode") as mock_decode:
            mock_decode.return_value = {
                "sub": "google-user-123",
                "email": "test@example.com",
                "name": "Test User",
            }
            result = validate_id_token("fake-id-token", "test-client-id")
            assert result is not None
            assert result["sub"] == "google-user-123"
            assert result["email"] == "test@example.com"
            assert result["name"] == "Test User"

    def test_missing_email_returns_empty(self):
        with patch("src.auth.google_oauth2.jwt.decode") as mock_decode:
            mock_decode.return_value = {"sub": "user-123"}
            result = validate_id_token("token", "client-id")
            assert result["email"] == ""
            assert result["name"] == ""

    def test_invalid_token_returns_none(self):
        with patch("src.auth.google_oauth2.jwt.decode") as mock_decode:
            mock_decode.side_effect = Exception("invalid token")
            result = validate_id_token("bad-token", "client-id")
            assert result is None
