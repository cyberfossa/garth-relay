from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from garth.sso.state import MFAChallenge, MFAState

from src.auth.session import create_jwt
from src.config import get_config
from src.routes import connections as connections_module
from src.routes.connections import create_connections_router
from src.templates_config import create_templates


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.db = MagicMock()
    db.save_mfa_state.return_value = True
    db.get_mfa_state.return_value = None
    db.delete_mfa_state.return_value = True
    db.delete_garmin_session.return_value = True
    return db


@pytest.fixture
def mock_encryptor():
    return MagicMock()


@pytest.fixture
def mock_garmin_client():
    client = MagicMock()
    client.login = AsyncMock(return_value=None)
    client.complete_mfa = AsyncMock(return_value=None)
    return client


@pytest.fixture
def app(mock_db, mock_encryptor, mock_garmin_client):
    cfg = get_config()
    templates = create_templates()
    test_app = FastAPI()

    mock_cls = MagicMock()
    mock_cls.create_for_user = MagicMock(return_value=mock_garmin_client)
    mock_cls.return_value = mock_garmin_client

    with patch.object(connections_module, "GarminClient", mock_cls):
        router = create_connections_router(
            templates,
            mock_db,
            mock_encryptor,
            cfg.jwt_secret_key,
            cfg.jwt_algorithm,
            cfg.google_client_id,
            cfg.google_client_secret,
            cfg.google_oauth_redirect_uri,
            "http://localhost:8080",
        )
        test_app.include_router(router)
        yield test_app


@pytest.fixture
def client(app):
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def auth_token():
    cfg = get_config()
    return create_jwt(
        user_id="test-user-123",
        email="test@example.com",
        name="Test User",
        secret=cfg.jwt_secret_key,
        algorithm=cfg.jwt_algorithm,
    )


def _auth(client, token):
    client.cookies.set("session", token)


class TestGarminConnect:
    def test_requires_auth(self, client):
        response = client.get("/connections/garmin/connect")
        assert response.status_code == 302

    def test_renders_form(self, client, auth_token):
        _auth(client, auth_token)
        response = client.get("/connections/garmin/connect")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestGarminAuth:
    def test_login_success_redirects(self, client, auth_token, mock_garmin_client):
        _auth(client, auth_token)
        response = client.post(
            "/connections/garmin/auth",
            data={"email": "garmin@example.com", "password": "secret123"},
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/dashboard"
        mock_garmin_client.login.assert_awaited_once_with("garmin@example.com", "secret123")

    def test_mfa_required_returns_mfa_form(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mfa_challenge = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        )
        mock_garmin_client.login.return_value = mfa_challenge

        response = client.post(
            "/connections/garmin/auth",
            data={"email": "garmin@example.com", "password": "secret123"},
        )
        assert response.status_code == 200
        mock_db.save_mfa_state.assert_called_once()
        mock_garmin_client.login.assert_awaited_once()

    def test_wrong_credentials(self, client, auth_token, mock_garmin_client):
        _auth(client, auth_token)
        mock_garmin_client.login.side_effect = ValueError("Wrong")

        response = client.post(
            "/connections/garmin/auth",
            data={"email": "wrong@example.com", "password": "wrong"},
        )
        assert response.status_code == 401

    def test_empty_credentials_returns_400(self, client, auth_token):
        _auth(client, auth_token)
        response = client.post("/connections/garmin/auth", data={"email": "", "password": ""})
        assert response.status_code == 400

    def test_requires_auth(self, client):
        response = client.post("/connections/garmin/auth", data={"email": "t@t.com", "password": "p"})
        assert response.status_code == 302


class TestGarminMFA:
    def test_mfa_success(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mfa_json = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        ).to_json()
        mock_db.get_mfa_state.return_value = {"encrypted_state": mfa_json}

        response = client.post("/connections/garmin/mfa", data={"mfa_code": "123456"})
        assert response.status_code == 302
        assert response.headers["location"] == "/dashboard"
        mock_garmin_client.complete_mfa.assert_awaited_once()
        mock_db.delete_mfa_state.assert_called_once_with("test-user-123")

    def test_mfa_invalid_code(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mock_garmin_client.complete_mfa.side_effect = ValueError("Invalid MFA")
        mfa_json = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        ).to_json()
        mock_db.get_mfa_state.return_value = {"encrypted_state": mfa_json}

        response = client.post("/connections/garmin/mfa", data={"mfa_code": "000000"})
        assert response.status_code == 401

    def test_mfa_expired_state(self, client, auth_token, mock_db):
        _auth(client, auth_token)
        mock_db.get_mfa_state.return_value = None

        response = client.post("/connections/garmin/mfa", data={"mfa_code": "123456"})
        assert response.status_code == 400
        assert "expired" in response.text.lower()

    def test_mfa_empty_code_returns_400(self, client, auth_token):
        _auth(client, auth_token)
        response = client.post("/connections/garmin/mfa", data={"mfa_code": ""})
        assert response.status_code == 400


class TestGarminDisconnect:
    def test_disconnect_success(self, client, auth_token, mock_db):
        _auth(client, auth_token)
        mock_db.delete_garmin_session.return_value = True

        response = client.post("/connections/garmin/disconnect")
        assert response.status_code == 302
        assert response.headers["location"] == "/dashboard"
        mock_db.delete_garmin_session.assert_called_once()

    def test_disconnect_requires_auth(self, client):
        response = client.post("/connections/garmin/disconnect")
        assert response.status_code == 302


class TestGoogleConnectFlow:
    def test_auth_uses_connections_callback_uri(self, client, auth_token):
        _auth(client, auth_token)
        response = client.get("/connections/google/auth")
        assert response.status_code == 302
        location = response.headers["location"]
        assert "/connections/google/callback" in location

    def test_callback_saves_tokens_and_redirects(self, client, auth_token, mock_db):
        _auth(client, auth_token)
        response = client.get("/connections/google/auth")
        state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]

        token_response = MagicMock()
        token_response.status_code = 200
        token_response.json.return_value = {
            "access_token": "google-access",
            "refresh_token": "google-refresh",
            "expires_in": 3600,
        }

        with patch("src.routes.connections.httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.post = AsyncMock(return_value=token_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client_class.return_value = mock_client

            callback = client.get(f"/connections/google/callback?code=auth-code&state={state}")

        assert callback.status_code == 302
        assert callback.headers["location"] == "/dashboard"
        mock_db.save_oauth_token.assert_called_once()

    def test_callback_rejects_invalid_state(self, client, auth_token):
        _auth(client, auth_token)
        response = client.get("/connections/google/callback?code=auth-code&state=bad-state")
        assert response.status_code == 403
