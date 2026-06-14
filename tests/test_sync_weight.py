"""Tests for bulk sync routes."""

# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportPrivateLocalImportUsage=false, reportCallIssue=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.routes.sync_weight as sync_weight_module
from src.auth.session import create_jwt
from src.config import get_config
from src.middleware.csrf import CSRFMiddleware
from src.models.oauth_models import OAuthProvider, OAuthToken
from src.routes.sync_weight import create_sync_weight_router
from src.services.garmin_client import GarminSessionExpiredError
from src.services.sync_orchestrator import SyncResult
from src.templates_config import create_templates


def _oauth_token(access_token: str = "google-access", refresh_token: str = "google-refresh") -> OAuthToken:
    now = datetime.now(UTC)
    return OAuthToken(
        user_id="test-user-123",
        provider=OAuthProvider.GOOGLE,
        access_token=access_token,
        refresh_token=refresh_token,
        scope=None,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )


def _measurement(
    weight_kg: float = 80.5,
    body_fat_pct: float | None = 20.1,
    ts: datetime | None = None,
) -> dict[str, object]:
    return {
        "weight_kg": weight_kg,
        "body_fat_pct": body_fat_pct,
        "timestamp": ts or datetime(2026, 4, 10, 8, 0, tzinfo=UTC),
    }


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_user_profile = Mock(
        return_value={
            "name": "Test User",
            "email": "test@example.com",
            "sync_enabled": True,
            "omron_sync_enabled": False,
        }
    )
    db.get_oauth_token = Mock(return_value=_oauth_token())
    db.has_garmin_session = Mock(return_value=True)
    db.get_recent_syncs = Mock(return_value=[])
    db.save_oauth_token = Mock(return_value=True)
    db.log_sync = Mock(return_value=True)
    db.update_user_status = Mock(return_value=True)
    db.encryptor = MagicMock()
    return db


@pytest.fixture
def mock_google_client():
    client = AsyncMock()
    client.fetch_all_measurements = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_garmin_client():
    client = AsyncMock()
    client.fetch_existing_weights = AsyncMock(return_value=[])
    return client


@pytest.fixture
def mock_sync_orchestrator():
    orch = AsyncMock()
    orch.upload_measurement = AsyncMock(
        return_value=SyncResult(status="success", user_id="test-user-123", message="uploaded", uploaded=1, total=1)
    )
    return orch


@pytest.fixture
def mock_oauth_service():
    service = AsyncMock()
    service.refresh_access_token = AsyncMock(return_value=None)
    return service


@pytest.fixture
def sync_weight_app(mock_db, mock_google_client, mock_garmin_client, mock_sync_orchestrator, mock_oauth_service):
    cfg = get_config()
    templates = create_templates()
    app = FastAPI()
    app.add_middleware(CSRFMiddleware)
    with patch.object(
        sync_weight_module.GarminClient,
        "create_for_user",
        return_value=mock_garmin_client,
    ):
        router = create_sync_weight_router(
            templates, mock_db, mock_google_client, mock_garmin_client, mock_sync_orchestrator, mock_oauth_service, cfg
        )
        app.include_router(router)
        yield app


@pytest.fixture
def client(sync_weight_app):
    return TestClient(sync_weight_app, follow_redirects=False)


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


def _get_csrf_token(client: TestClient, auth_token: str) -> str:
    client.cookies.set("session", auth_token)
    response = client.get("/sync/weight")
    return response.cookies.get("csrf_token") or ""


class TestSyncWeightPage:
    def test_requires_auth(self, client):
        response = client.get("/sync/weight")
        assert response.status_code == 302
        assert "/login" in response.headers["location"]

    def test_renders_authenticated(self, client, auth_token):
        client.cookies.set("session", auth_token)
        response = client.get("/sync/weight")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]


class TestSyncWeightMeasurements:
    def test_returns_table(self, client, auth_token, mock_google_client, mock_garmin_client):
        measurements = [_measurement(), _measurement(weight_kg=75.0, body_fat_pct=18.5)]
        mock_google_client.fetch_all_measurements.return_value = measurements
        mock_garmin_client.fetch_existing_weights.return_value = []

        client.cookies.set("session", auth_token)
        response = client.get("/sync/weight/measurements")
        assert response.status_code == 200
        assert "<table" in response.text

    def test_empty_state(self, client, auth_token, mock_google_client):
        mock_google_client.fetch_all_measurements.return_value = []

        client.cookies.set("session", auth_token)
        response = client.get("/sync/weight/measurements")
        assert response.status_code == 200
        assert "No measurements found" in response.text

    def test_no_google_token(self, client, auth_token, mock_db):
        mock_db.get_oauth_token.return_value = None

        client.cookies.set("session", auth_token)
        response = client.get("/sync/weight/measurements")
        assert response.status_code == 200
        assert "Google Health not connected" in response.text


class TestSyncWeightRecord:
    def test_sync_record_success(self, client, auth_token, mock_sync_orchestrator):
        csrf_token = _get_csrf_token(client, auth_token)

        response = client.post(
            "/sync/weight/sync-record",
            data={
                "csrf_token": csrf_token,
                "timestamp": "2026-04-10T08:00:00+00:00",
                "weight_kg": "80.5",
                "body_fat_pct": "20.1",
                "row_index": "0",
            },
            cookies={"session": auth_token, "csrf_token": csrf_token},
        )
        assert response.status_code == 200
        assert "✓ Synced" in response.text
        mock_sync_orchestrator.upload_measurement.assert_awaited_once()

    def test_requires_auth(self, client):
        response = client.post(
            "/sync/weight/sync-record",
            data={
                "timestamp": "2026-04-10T08:00:00+00:00",
                "weight_kg": "80.5",
                "body_fat_pct": "20.1",
                "row_index": "0",
            },
        )
        assert response.status_code in {302, 403}

    def test_garmin_expired(self, client, auth_token, mock_sync_orchestrator):
        mock_sync_orchestrator.upload_measurement.side_effect = GarminSessionExpiredError("expired")

        csrf_token = _get_csrf_token(client, auth_token)

        response = client.post(
            "/sync/weight/sync-record",
            data={
                "csrf_token": csrf_token,
                "timestamp": "2026-04-10T08:00:00+00:00",
                "weight_kg": "80.5",
                "body_fat_pct": "20.1",
                "row_index": "0",
            },
            cookies={"session": auth_token, "csrf_token": csrf_token},
        )
        assert response.status_code == 200
        assert "expired" in response.text.lower()
