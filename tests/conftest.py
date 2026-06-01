"""Shared test fixtures."""

import os
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from src.auth.session import create_jwt
from src.config import get_config

os.environ.setdefault("APP_GCP_PROJECT_ID", "test-project")
os.environ.setdefault("APP_ENCRYPTION_KEY", "WEA3X4NzorSc5Rvv19JtZdg4LTka6ScNFQD1_RTys8k=")
os.environ.setdefault("APP_JWT_SECRET_KEY", "test-jwt-secret-key-for-testing")
os.environ.setdefault("APP_CSRF_SECRET", "test-csrf-secret")
os.environ.setdefault("APP_GOOGLE_CLIENT_ID", "test-client-id")
os.environ.setdefault("APP_GOOGLE_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("APP_GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8080/auth/callback")


@pytest.fixture
def config():
    return get_config()


@pytest.fixture
def fake_firestore_client():
    client = MagicMock()
    client.db = MagicMock()
    client.get_user_profile = Mock(return_value=None)
    client.save_user_profile = Mock(return_value=True)
    client.update_user_status = Mock(return_value=True)
    client.get_active_users = Mock(return_value=[])
    client.log_sync = Mock(return_value=True)
    client.log_poll_run = Mock(return_value=True)
    client.get_recent_syncs = Mock(return_value=[])
    client.get_last_sync_timestamp = Mock(return_value=None)
    client.get_recent_poll_logs = Mock(return_value=[])
    client.save_mfa_state = Mock(return_value=True)
    client.get_mfa_state = Mock(return_value=None)
    client.delete_mfa_state = Mock(return_value=True)
    client.delete_garmin_session = Mock(return_value=True)
    client.has_garmin_session = Mock(return_value=False)
    return client


@pytest.fixture
def fake_garmin_client():
    client = AsyncMock()
    client.login = AsyncMock(return_value=None)
    client.login_with_mfa = AsyncMock(return_value=('{"mfa_state":{}}', "mfa_required"))
    client.complete_mfa = AsyncMock(return_value=None)
    client.upload_body_composition = AsyncMock(return_value=None)
    client.fetch_existing_weights = AsyncMock(return_value=[])
    return client


@pytest.fixture
def jwt_token(config):
    return create_jwt(
        user_id="test-user-123",
        email="test@example.com",
        name="Test User",
        secret=config.jwt_secret_key,
        algorithm=config.jwt_algorithm,
    )

