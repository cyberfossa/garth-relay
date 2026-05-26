"""Shared test fixtures."""

import os
from unittest.mock import MagicMock, Mock

import pytest

os.environ.setdefault("APP_GCP_PROJECT_ID", "test-project")


@pytest.fixture
def fake_firestore_client():
    client = MagicMock()
    client.get_user_profile = Mock(return_value=None)
    client.save_user_profile = Mock(return_value=True)
    client.update_user_status = Mock(return_value=True)
    client.get_active_users = Mock(return_value=[])
    client.log_sync = Mock(return_value=True)
    client.log_poll_run = Mock(return_value=True)
    client.get_recent_syncs = Mock(return_value=[])
    client.get_last_sync_timestamp = Mock(return_value=None)
    client.get_recent_poll_logs = Mock(return_value=[])
    return client
