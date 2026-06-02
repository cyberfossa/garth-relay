from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.session import create_jwt
from src.config import get_config
from src.routes.connections import create_connections_router
from src.routes.pages import create_pages_router
from src.templates_config import create_templates


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_user_profile.return_value = {"name": "Test User", "email": "test@example.com"}
    db.get_oauth_token.return_value = {"access_token": "google_token"}
    db.has_garmin_session.return_value = True
    db.get_recent_syncs.return_value = []
    return db


@pytest.fixture
def mock_encryptor():
    return MagicMock()


@pytest.fixture
def pages_app(mock_db, mock_encryptor):
    cfg = get_config()
    templates = create_templates()
    app = FastAPI()
    router = create_pages_router(templates, mock_db, cfg)
    app.include_router(router)
    connections_router = create_connections_router(
        templates,
        mock_db,
        mock_encryptor,
        cfg.jwt_secret_key,
        cfg.jwt_algorithm,
        cfg.google_client_id,
        cfg.google_client_secret,
        cfg.google_oauth_redirect_uri,
        "",
    )
    app.include_router(connections_router)
    return app


@pytest.fixture
def client(pages_app):
    return TestClient(pages_app, follow_redirects=False)


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


class TestLoginPage:
    def test_renders_login(self, client):
        response = client.get("/login")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]

    def test_redirects_if_authenticated(self, client, auth_token):
        client.cookies.set("session", auth_token)
        response = client.get("/login")
        assert response.status_code == 302
        assert "/dashboard" in response.headers["location"]


class TestDashboard:
    def test_requires_auth(self, client):
        response = client.get("/dashboard")
        assert response.status_code == 302
        assert "/login" in response.headers["location"]

    def test_renders_with_auth(self, client, auth_token):
        client.cookies.set("session", auth_token)
        response = client.get("/dashboard")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestRootRedirect:
    def test_unauthenticated_redirects_to_login(self, client):
        response = client.get("/")
        assert response.status_code == 302
        assert "/login" in response.headers["location"]

    def test_authenticated_redirects_to_dashboard(self, client, auth_token):
        client.cookies.set("session", auth_token)
        response = client.get("/")
        assert response.status_code == 302
        assert "/dashboard" in response.headers["location"]


class TestConnectGoogle:
    def test_requires_auth(self, client):
        response = client.get("/connections/google/connect")
        assert response.status_code == 302

    def test_renders_with_auth(self, client, auth_token):
        client.cookies.set("session", auth_token)
        response = client.get("/connections/google/connect")
        assert response.status_code == 200
