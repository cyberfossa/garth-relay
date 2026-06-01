# pyright: reportMissingImports=false

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.models.oauth_models import OAuthProvider, OAuthToken
from src.services.garmin_client import GarminRateLimitError, GarminSessionExpiredError
from src.services.google_health_client import GoogleScopeRevokedError, GoogleTokenExpiredError
from src.services.sync_orchestrator import PollSummary, SyncOrchestrator, SyncResult


def _oauth_token(access_token: str = "google-access", refresh_token: str | None = "google-refresh") -> OAuthToken:
    now = datetime.now(UTC)
    return OAuthToken(
        user_id="u1",
        provider=OAuthProvider.GOOGLE,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=now + timedelta(hours=1),
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def google_client() -> MagicMock:
    client = MagicMock()
    client.fetch_latest_measurements = AsyncMock()
    client.fetch_all_measurements = AsyncMock()
    return client


@pytest.fixture
def garmin_client() -> MagicMock:
    client = MagicMock()
    client.upload_body_composition = AsyncMock()
    client.fetch_existing_weights = AsyncMock(return_value=[])
    return client


@pytest.fixture
def db_client() -> MagicMock:
    client = MagicMock()
    client.db = MagicMock()
    return client


@pytest.fixture
def encryptor() -> MagicMock:
    return MagicMock()


@pytest.fixture
def orchestrator(google_client, garmin_client, db_client, encryptor, monkeypatch) -> SyncOrchestrator:
    monkeypatch.setattr(
        "src.services.sync_orchestrator.GarminClient.create_for_user",
        MagicMock(return_value=garmin_client),
    )
    return SyncOrchestrator(google_client=google_client, db_client=db_client, encryptor=encryptor)


def _weight_entries(entries: list[tuple[datetime, float]]) -> list[dict[str, object]]:
    return [{"timestamp_utc": ts, "weight_kg": weight} for ts, weight in entries]


class TestUploadMeasurement:
    async def test_success(self, orchestrator, garmin_client, db_client):
        db_client.has_garmin_session.return_value = True
        garmin_client.upload_body_composition.return_value = "new-blob"

        result = await orchestrator.upload_measurement(
            user_id="u1", weight_kg=81.2, body_fat_pct=19.4, timestamp=datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

        assert result.status == "success"
        assert result.uploaded == 1
        garmin_client.upload_body_composition.assert_awaited_once()
        garmin_client.upload_body_composition.assert_awaited_once_with(
            81.2, 19.4, datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

    async def test_without_body_fat(self, orchestrator, garmin_client, db_client):
        db_client.has_garmin_session.return_value = True
        garmin_client.upload_body_composition.return_value = "new-blob"

        result = await orchestrator.upload_measurement(
            user_id="u1", weight_kg=75.3, body_fat_pct=None, timestamp=datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

        assert result.status == "success"
        call_args = garmin_client.upload_body_composition.call_args
        assert call_args[0][0] == 75.3
        assert call_args[0][1] is None
        assert call_args[0][2] == datetime(2026, 4, 4, 10, 0, tzinfo=UTC)

    async def test_missing_garmin_session(self, orchestrator, db_client):
        db_client.has_garmin_session.return_value = False

        result = await orchestrator.upload_measurement(
            user_id="u1", weight_kg=75.3, body_fat_pct=10.0, timestamp=datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

        assert result.status == "error"
        assert result.message == "missing_garmin_session"

    async def test_garmin_session_expired(self, orchestrator, garmin_client, db_client):
        db_client.has_garmin_session.return_value = True
        garmin_client.upload_body_composition.side_effect = GarminSessionExpiredError("expired")

        result = await orchestrator.upload_measurement(
            user_id="u1", weight_kg=75.0, body_fat_pct=10.0, timestamp=datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

        assert result.status == "error"
        assert result.message == "garmin_session_expired"
        db_client.update_user_status.assert_called_once_with("u1", "needs_reauth")

    async def test_garmin_rate_limit(self, orchestrator, garmin_client, db_client):
        db_client.has_garmin_session.return_value = True
        garmin_client.upload_body_composition.side_effect = GarminRateLimitError("429")

        result = await orchestrator.upload_measurement(
            user_id="u1", weight_kg=75.0, body_fat_pct=10.0, timestamp=datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        )

        assert result.status == "skipped"
        assert result.message == "garmin_rate_limited"


class TestSyncUser:
    async def test_missing_google_token(self, orchestrator, db_client):
        db_client.get_oauth_token.return_value = None
        db_client.has_garmin_session.return_value = True

        result = await orchestrator.sync_user("u1")

        assert result.status == "error"
        assert result.message == "missing_google_token"

    async def test_missing_garmin_session(self, orchestrator, db_client):
        db_client.get_oauth_token.return_value = _oauth_token()
        db_client.has_garmin_session.return_value = False

        result = await orchestrator.sync_user("u1")

        assert result.status == "error"
        assert result.message == "missing_garmin_session"

    async def test_no_measurements_returns_skipped(self, orchestrator, google_client, db_client):
        db_client.get_oauth_token.return_value = _oauth_token()
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None
        google_client.fetch_latest_measurements.return_value = None
        google_client.fetch_all_measurements.return_value = []

        result = await orchestrator.sync_user("u1")

        assert result.status == "skipped"
        assert result.message == "no_measurements"

    async def test_google_scope_revoked(self, orchestrator, google_client, db_client):
        db_client.get_oauth_token.return_value = _oauth_token()
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None
        google_client.fetch_latest_measurements.side_effect = GoogleScopeRevokedError("revoked")

        result = await orchestrator.sync_user("u1")

        assert result.status == "error"
        assert result.message == "google_scope_revoked"
        db_client.update_user_status.assert_called_once_with("u1", "needs_reauth")

    async def test_google_token_expired_refresh_succeeds(self, orchestrator, google_client, db_client, monkeypatch):
        token = _oauth_token(access_token="expired", refresh_token="refresh-ok")
        db_client.get_oauth_token.return_value = token
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None

        measurement = {"weight_kg": 80.0, "body_fat_pct": 20.0, "timestamp": datetime(2026, 4, 4, 10, 0, tzinfo=UTC)}
        google_client.fetch_latest_measurements.side_effect = GoogleTokenExpiredError("expired")
        google_client.fetch_all_measurements.side_effect = [
            GoogleTokenExpiredError("expired"),
            [measurement],
        ]

        class FakeResponse:
            def raise_for_status(self):
                pass

            def json(self):
                return {"access_token": "fresh-access", "expires_in": 3600}

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        monkeypatch.setattr("src.services.sync_orchestrator.httpx.AsyncClient", FakeAsyncClient)

        await orchestrator.sync_user("u1")

        db_client.save_oauth_token.assert_called_once()

    async def test_google_token_refresh_failure(self, orchestrator, google_client, db_client, monkeypatch):
        token = _oauth_token(access_token="expired", refresh_token="refresh-bad")
        db_client.get_oauth_token.return_value = token
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None
        google_client.fetch_latest_measurements.side_effect = GoogleTokenExpiredError("expired")
        google_client.fetch_all_measurements.side_effect = GoogleTokenExpiredError("expired")

        class FakeResponse:
            def raise_for_status(self):
                raise RuntimeError("bad refresh")

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_):
                return False

            async def post(self, *args, **kwargs):
                return FakeResponse()

        monkeypatch.setattr("src.services.sync_orchestrator.httpx.AsyncClient", FakeAsyncClient)

        result = await orchestrator.sync_user("u1")

        assert result.status == "error"
        assert result.message == "google_refresh_failed"
        db_client.update_user_status.assert_called_once_with("u1", "needs_reauth")

    async def test_successful_sync_with_measurements(self, orchestrator, google_client, garmin_client, db_client):
        db_client.get_oauth_token.return_value = _oauth_token()
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None

        measurements = [
            {"weight_kg": 80.1, "body_fat_pct": 20.5, "timestamp": datetime(2026, 4, 4, 10, 0, tzinfo=UTC)},
        ]
        google_client.fetch_latest_measurements.return_value = None
        google_client.fetch_all_measurements.return_value = measurements
        garmin_client.upload_body_composition.return_value = "new-blob"
        garmin_client.fetch_existing_weights.return_value = []

        result = await orchestrator.sync_user("u1")

        assert result.status == "success"
        assert result.message == "sync_completed"
        assert result.uploaded == 1
        assert result.total == 1

    async def test_dedup_skips_existing_measurements(self, orchestrator, google_client, garmin_client, db_client):
        db_client.get_oauth_token.return_value = _oauth_token()
        db_client.has_garmin_session.return_value = True
        db_client.get_last_sync_timestamp.return_value = None

        ts = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        measurements = [{"weight_kg": 80.1, "body_fat_pct": 20.5, "timestamp": ts}]
        google_client.fetch_latest_measurements.return_value = None
        google_client.fetch_all_measurements.return_value = measurements
        garmin_client.fetch_existing_weights.return_value = [{"timestamp_utc": ts, "weight_kg": 80.1}]

        result = await orchestrator.sync_user("u1")

        assert result.status == "skipped"
        assert result.skipped == 1
        garmin_client.upload_body_composition.assert_not_awaited()


class TestSyncAllUsers:
    async def test_mixed_results(self, orchestrator, db_client, monkeypatch):
        db_client.get_active_users.return_value = ["u1", "u2", "u3"]
        results = [
            SyncResult(status="success", user_id="u1", message="ok"),
            SyncResult(status="skipped", user_id="u2", message="no data"),
            SyncResult(status="error", user_id="u3", message="expired"),
        ]
        monkeypatch.setattr(orchestrator, "sync_user", AsyncMock(side_effect=results))

        summary = await orchestrator.sync_all_users()

        assert summary.synced == 1
        assert summary.skipped == 1
        assert summary.errors == 1
        assert summary.total == 3

    async def test_2_second_delay_between_users(self, orchestrator, db_client, monkeypatch):
        db_client.get_active_users.return_value = ["u1", "u2", "u3"]
        monkeypatch.setattr(
            orchestrator,
            "sync_user",
            AsyncMock(return_value=SyncResult(status="success", user_id="u1", message="ok")),
        )
        sleep_mock = AsyncMock()
        monkeypatch.setattr("src.services.sync_orchestrator.asyncio.sleep", sleep_mock)

        await orchestrator.sync_all_users()

        assert sleep_mock.await_count == 2
        sleep_mock.assert_any_await(2)

    async def test_returns_poll_summary(self, orchestrator, db_client, monkeypatch):
        db_client.get_active_users.return_value = ["u1"]
        monkeypatch.setattr(
            orchestrator,
            "sync_user",
            AsyncMock(return_value=SyncResult(status="success", user_id="u1", message="ok")),
        )

        summary = await orchestrator.sync_all_users()

        assert isinstance(summary, PollSummary)
        assert summary.duration_seconds >= 0

    async def test_logs_poll_run(self, orchestrator, db_client, monkeypatch):
        db_client.get_active_users.return_value = ["u1"]
        monkeypatch.setattr(
            orchestrator,
            "sync_user",
            AsyncMock(return_value=SyncResult(status="success", user_id="u1", message="ok")),
        )

        await orchestrator.sync_all_users()

        db_client.log_poll_run.assert_called_once()

    async def test_handles_unexpected_exception(self, orchestrator, db_client, monkeypatch):
        db_client.get_active_users.return_value = ["u1"]
        monkeypatch.setattr(orchestrator, "sync_user", AsyncMock(side_effect=RuntimeError("boom")))

        summary = await orchestrator.sync_all_users()

        assert summary.errors == 1
        assert summary.total == 1


class TestHasMatchingWeight:
    def test_exact_match(self):
        ts = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        garmin_weights = _weight_entries([(ts, 80.0)])
        assert SyncOrchestrator._has_matching_weight(garmin_weights, ts, timedelta(minutes=5))

    def test_within_tolerance(self):
        ts = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        garmin_ts = datetime(2026, 4, 4, 10, 3, tzinfo=UTC)
        garmin_weights = _weight_entries([(garmin_ts, 80.0)])
        assert SyncOrchestrator._has_matching_weight(garmin_weights, ts, timedelta(minutes=5))

    def test_outside_tolerance(self):
        ts = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        garmin_ts = datetime(2026, 4, 4, 10, 10, tzinfo=UTC)
        garmin_weights = _weight_entries([(garmin_ts, 80.0)])
        assert not SyncOrchestrator._has_matching_weight(garmin_weights, ts, timedelta(minutes=5))

    def test_empty_list(self):
        ts = datetime(2026, 4, 4, 10, 0, tzinfo=UTC)
        assert not SyncOrchestrator._has_matching_weight([], ts, timedelta(minutes=5))
