"""Tests for polling routes."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.auth.session import create_jwt
from src.config import get_config
from src.routes.polling import create_polling_router
from src.services.sync_orchestrator import PollSummary, SyncResult


@pytest.fixture
def mock_sync_orchestrator():
    orch = MagicMock()
    orch.sync_user = AsyncMock(
        return_value=SyncResult(
            status="success", user_id="u1", message="sync_completed", uploaded=2, skipped=1, total=3
        )
    )
    orch.sync_all_users = AsyncMock(
        return_value=PollSummary(synced=2, skipped=1, errors=0, total=3, duration_seconds=5.1)
    )
    return orch


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_recent_syncs = MagicMock(return_value=[])
    return db


@pytest.fixture
def polling_app(mock_sync_orchestrator, mock_db):
    cfg = get_config()
    app = FastAPI()
    router = create_polling_router(
        db_client=mock_db,
        sync_orchestrator=mock_sync_orchestrator,
        config=cfg,
    )
    app.include_router(router)
    return app


@pytest.fixture
def client(polling_app):
    return TestClient(polling_app, follow_redirects=False)


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


class TestPoll:
    def test_poll_returns_200(self, client, mock_sync_orchestrator):
        response = client.post("/polling/poll", json={})
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["synced"] == 2
        mock_sync_orchestrator.sync_all_users.assert_awaited_once()

    def test_poll_error_returns_500(self, client, mock_sync_orchestrator):
        mock_sync_orchestrator.sync_all_users.side_effect = RuntimeError("boom")
        response = client.post("/polling/poll", json={})
        assert response.status_code == 500
        assert response.json()["status"] == "error"


class TestSyncNow:
    def test_requires_auth(self, client):
        response = client.post("/polling/sync-now")
        assert response.status_code in {401, 302}

    def test_success_returns_html_notice(self, client, auth_token, mock_sync_orchestrator):
        client.cookies.set("session", auth_token)
        response = client.post("/polling/sync-now")

        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "notice" in response.text
        assert "success" in response.text
        mock_sync_orchestrator.sync_user.assert_awaited_once_with("test-user-123")

    def test_error_result_shows_error_notice(self, client, auth_token, mock_sync_orchestrator):
        client.cookies.set("session", auth_token)
        mock_sync_orchestrator.sync_user.return_value = SyncResult(
            status="error", user_id="u1", message="garmin_session_expired"
        )

        response = client.post("/polling/sync-now")

        assert response.status_code == 200
        assert "error" in response.text

    def test_unexpected_error_returns_error_notice(self, client, auth_token, mock_sync_orchestrator):
        client.cookies.set("session", auth_token)
        mock_sync_orchestrator.sync_user.side_effect = RuntimeError("boom")

        response = client.post("/polling/sync-now")

        assert response.status_code == 200
        assert "error" in response.text


class TestSyncLogs:
    def test_requires_auth(self, client):
        response = client.get("/polling/sync-logs")
        assert response.status_code in {401, 302}

    def test_empty_logs(self, client, auth_token, mock_db):
        client.cookies.set("session", auth_token)
        mock_db.get_recent_syncs.return_value = []

        response = client.get("/polling/sync-logs")

        assert response.status_code == 200
        assert "No sync history" in response.text


class TestSyncAll:
    def test_requires_auth(self, client):
        response = client.post("/polling/sync-all")
        assert response.status_code in {401, 302}

    def test_returns_summary_html(self, client, auth_token, mock_sync_orchestrator):
        client.cookies.set("session", auth_token)

        response = client.post("/polling/sync-all")

        assert response.status_code == 200
        assert "Poll complete" in response.text
        assert "2 synced" in response.text
        mock_sync_orchestrator.sync_all_users.assert_awaited_once()

    def test_failure_returns_error_html(self, client, auth_token, mock_sync_orchestrator):
        client.cookies.set("session", auth_token)
        mock_sync_orchestrator.sync_all_users.side_effect = RuntimeError("boom")

        response = client.post("/polling/sync-all")

        assert response.status_code == 200
        assert "error" in response.text
