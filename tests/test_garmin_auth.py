from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from garth.sso.state import MFAChallenge, MFAState

from src.auth.session import create_jwt
from src.config import get_config
from src.routes import garmin_auth as garmin_auth_module
from src.routes.garmin_auth import create_garmin_auth_router
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
    client.login_with_mfa = AsyncMock()
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

    with patch.object(garmin_auth_module, "GarminClient", mock_cls):
        router = create_garmin_auth_router(templates, mock_db, cfg, mock_encryptor)
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
        response = client.get("/garmin/connect")
        assert response.status_code == 302

    def test_renders_form(self, client, auth_token):
        _auth(client, auth_token)
        response = client.get("/garmin/connect")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestGarminAuth:
    def test_login_success_redirects(self, client, auth_token, mock_garmin_client):
        _auth(client, auth_token)
        response = client.post(
            "/garmin/auth",
            data={"email": "garmin@example.com", "password": "secret123"},
        )
        assert response.status_code == 302
        assert response.headers["location"] == "/dashboard"
        mock_garmin_client.login.assert_awaited_once_with("garmin@example.com", "secret123")

    def test_mfa_required_returns_mfa_form(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mock_garmin_client.login.side_effect = Exception("login failed")
        mfa_json = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        ).to_json()
        mock_garmin_client.login_with_mfa.return_value = (mfa_json, "mfa_required")

        response = client.post(
            "/garmin/auth",
            data={"email": "garmin@example.com", "password": "secret123"},
        )
        assert response.status_code == 200
        mock_db.save_mfa_state.assert_called_once()

    def test_wrong_credentials(self, client, auth_token, mock_garmin_client):
        _auth(client, auth_token)
        mock_garmin_client.login.side_effect = Exception("Wrong")
        mock_garmin_client.login_with_mfa.side_effect = Exception("Wrong")

        response = client.post(
            "/garmin/auth",
            data={"email": "wrong@example.com", "password": "wrong"},
        )
        assert response.status_code == 401

    def test_empty_credentials_returns_400(self, client, auth_token):
        _auth(client, auth_token)
        response = client.post("/garmin/auth", data={"email": "", "password": ""})
        assert response.status_code == 400

    def test_requires_auth(self, client):
        response = client.post("/garmin/auth", data={"email": "t@t.com", "password": "p"})
        assert response.status_code == 302


class TestGarminMFA:
    def test_mfa_success(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mfa_json = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        ).to_json()
        mock_db.get_mfa_state.return_value = {"encrypted_state": mfa_json}

        response = client.post("/garmin/mfa", data={"mfa_code": "123456"})
        assert response.status_code == 302
        assert response.headers["location"] == "/dashboard"
        mock_garmin_client.complete_mfa.assert_awaited_once()
        mock_db.delete_mfa_state.assert_called_once_with("test-user-123")

    def test_mfa_invalid_code(self, client, auth_token, mock_garmin_client, mock_db):
        _auth(client, auth_token)
        mock_garmin_client.complete_mfa.side_effect = Exception("Invalid MFA")
        mfa_json = MFAChallenge(
            MFAState(strategy_name="sms", domain="garmin.com", state={}), {"cookie": "value"}
        ).to_json()
        mock_db.get_mfa_state.return_value = {"encrypted_state": mfa_json}

        response = client.post("/garmin/mfa", data={"mfa_code": "000000"})
        assert response.status_code == 401

    def test_mfa_expired_state(self, client, auth_token, mock_db):
        _auth(client, auth_token)
        mock_db.get_mfa_state.return_value = None

        response = client.post("/garmin/mfa", data={"mfa_code": "123456"})
        assert response.status_code == 400
        assert "expired" in response.text.lower()

    def test_mfa_empty_code_returns_400(self, client, auth_token):
        _auth(client, auth_token)
        response = client.post("/garmin/mfa", data={"mfa_code": ""})
        assert response.status_code == 400


class TestGarminDisconnect:
    def test_disconnect_success(self, client, auth_token, mock_db):
        _auth(client, auth_token)
        mock_db.delete_garmin_session.return_value = True

        response = client.post("/garmin/disconnect")
        assert response.status_code == 200
        assert response.headers.get("HX-Trigger") == "connectionsChanged"
        mock_db.delete_garmin_session.assert_called_once()

    def test_disconnect_requires_auth(self, client):
        response = client.post("/garmin/disconnect")
        assert response.status_code == 302
