"""Tests for FirestoreClient."""

from datetime import UTC, datetime
from importlib import import_module
from unittest.mock import MagicMock, patch

import pytest

from src.models.firestore_models import UserProfile


@pytest.fixture
def mock_firestore_db():
    return MagicMock()


@pytest.fixture
def firestore_client(mock_firestore_db):
    with patch("src.db.firestore_client.firestore.Client", return_value=mock_firestore_db):
        client = import_module("src.db.firestore_client").FirestoreClient(project_id="test-project")
        return client


class TestUserProfile:
    def test_save_user_profile(self, firestore_client):
        result = firestore_client.save_user_profile("user1", "test@example.com", "Test User")
        assert result is True

    def test_save_user_profile_failure(self, firestore_client):
        firestore_client.db.collection.return_value.document.return_value.set.side_effect = Exception("err")
        result = firestore_client.save_user_profile("user1", "test@example.com", "Test User")
        assert result is False

    def test_get_user_profile_exists(self, firestore_client):
        mock_doc = MagicMock()
        mock_doc.exists = True
        mock_doc.to_dict.return_value = {
            "email": "test@example.com",
            "name": "Test User",
            "status": "active",
            "created_at": datetime.now(UTC),
            "last_active": datetime.now(UTC),
        }
        firestore_client.db.collection.return_value.document.return_value.get.return_value = mock_doc

        result = firestore_client.get_user_profile("user1")
        assert result is not None
        assert isinstance(result, UserProfile)
        assert result.email == "test@example.com"

    def test_get_user_profile_not_found(self, firestore_client):
        mock_doc = MagicMock()
        mock_doc.exists = False
        firestore_client.db.collection.return_value.document.return_value.get.return_value = mock_doc

        result = firestore_client.get_user_profile("user1")
        assert result is None

    def test_get_user_profile_exception(self, firestore_client):
        firestore_client.db.collection.return_value.document.return_value.get.side_effect = Exception("err")
        result = firestore_client.get_user_profile("user1")
        assert result is None

    def test_update_user_status(self, firestore_client):
        result = firestore_client.update_user_status("user1", "inactive")
        assert result is True

    def test_update_user_status_failure(self, firestore_client):
        firestore_client.db.collection.return_value.document.return_value.update.side_effect = Exception("err")
        result = firestore_client.update_user_status("user1", "inactive")
        assert result is False


class TestGetActiveUsers:
    def test_returns_user_ids(self, firestore_client):
        mock_doc1 = MagicMock()
        mock_doc1.id = "user1"
        mock_doc2 = MagicMock()
        mock_doc2.id = "user2"
        firestore_client.db.collection.return_value.where.return_value.stream.return_value = [mock_doc1, mock_doc2]

        result = firestore_client.get_active_users()
        assert result == ["user1", "user2"]

    def test_returns_empty_on_error(self, firestore_client):
        firestore_client.db.collection.return_value.where.return_value.stream.side_effect = Exception("err")
        result = firestore_client.get_active_users()
        assert result == []


class TestLogSync:
    def test_log_sync_success(self, firestore_client):
        result = firestore_client.log_sync(user_id="user1", status="success", weight_kg=85.5, body_fat_pct=22.1)
        assert result is True

    def test_log_sync_exception(self, firestore_client):
        firestore_client.db.collection.return_value.document.return_value.collection.return_value.add.side_effect = (
            Exception("err")
        )
        result = firestore_client.log_sync(user_id="user1", status="success")
        assert result is False


class TestGetRecentSyncs:
    def test_returns_sync_logs(self, firestore_client):
        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {"status": "success", "weight_kg": 85.0}
        chain = firestore_client.db.collection.return_value.document.return_value.collection.return_value
        chain.order_by.return_value.limit.return_value.stream.return_value = [mock_doc]

        result = firestore_client.get_recent_syncs("user1")
        assert len(result) == 1
        assert result[0]["status"] == "success"

    def test_returns_empty_on_error(self, firestore_client):
        chain = firestore_client.db.collection.return_value.document.return_value.collection.return_value
        chain.order_by.return_value.limit.return_value.stream.side_effect = Exception("err")
        result = firestore_client.get_recent_syncs("user1")
        assert result == []


class TestGetLastSyncTimestamp:
    def test_returns_timestamp(self, firestore_client):
        ts = datetime.now(UTC)
        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {"timestamp": ts}
        chain = firestore_client.db.collection.return_value.document.return_value.collection.return_value
        chain.where.return_value.order_by.return_value.limit.return_value.stream.return_value = [mock_doc]

        result = firestore_client.get_last_sync_timestamp("user1")
        assert result == ts

    def test_returns_none_when_no_syncs(self, firestore_client):
        chain = firestore_client.db.collection.return_value.document.return_value.collection.return_value
        chain.where.return_value.order_by.return_value.limit.return_value.stream.return_value = []

        result = firestore_client.get_last_sync_timestamp("user1")
        assert result is None

    def test_returns_none_on_error(self, firestore_client):
        chain = firestore_client.db.collection.return_value.document.return_value.collection.return_value
        chain.where.return_value.order_by.return_value.limit.return_value.stream.side_effect = Exception("err")
        result = firestore_client.get_last_sync_timestamp("user1")
        assert result is None


class TestLogPollRun:
    def test_log_poll_run_success(self, firestore_client):
        result = firestore_client.log_poll_run(
            synced_count=3, skipped_count=1, error_count=0, total_users=4, duration_seconds=12.5
        )
        assert result is True

    def test_log_poll_run_failure(self, firestore_client):
        firestore_client.db.collection.return_value.add.side_effect = Exception("err")
        result = firestore_client.log_poll_run(
            synced_count=0, skipped_count=0, error_count=0, total_users=0, duration_seconds=0.0
        )
        assert result is False


class TestGetRecentPollLogs:
    def test_returns_poll_logs(self, firestore_client):
        mock_doc = MagicMock()
        mock_doc.to_dict.return_value = {"synced_count": 3, "error_count": 0}
        firestore_client.db.collection.return_value.order_by.return_value.limit.return_value.stream.return_value = [
            mock_doc
        ]

        result = firestore_client.get_recent_poll_logs()
        assert len(result) == 1
        assert result[0]["synced_count"] == 3

    def test_returns_empty_on_error(self, firestore_client):
        firestore_client.db.collection.return_value.order_by.return_value.limit.return_value.stream.side_effect = (
            Exception("err")
        )
        result = firestore_client.get_recent_poll_logs()
        assert result == []
