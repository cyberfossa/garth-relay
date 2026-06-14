from __future__ import annotations

import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytz

from src.services.sync_orchestrator import SyncOrchestrator, SyncResult
from src.services.garmin_client import GarminRateLimitError, GarminSessionExpiredError
from src.services.omron_client import BPMeasurement, DeviceCategory, OmronDevice


@pytest.fixture
def encryptor():
    enc = MagicMock()
    enc.encrypt.return_value = b"encrypted"
    enc.decrypt.return_value = "decrypted-token"
    return enc


@pytest.fixture
def db_client():
    client = MagicMock()
    client.db = MagicMock()
    return client


@pytest.fixture
def orchestrator(db_client, encryptor):
    google_client = MagicMock()
    return SyncOrchestrator(google_client=google_client, db_client=db_client, encryptor=encryptor)


def _bp_measurement(
    systolic: int = 120,
    diastolic: int = 80,
    pulse: int = 70,
    timestamp_ms: int = 1712224800000,
    tz_name: str = "Europe/Prague",
) -> BPMeasurement:
    # 1712224800000 ms is 2024-04-04 10:00:00 UTC
    return BPMeasurement(
        systolic=systolic,
        diastolic=diastolic,
        pulse=pulse,
        measurementDate=timestamp_ms,
        irregularHB=False,
        movementDetect=False,
        timeZone=pytz.timezone(tz_name),
    )


class TestAuthenticateOmron:
    @pytest.mark.asyncio
    async def test_authenticate_success(self, orchestrator, db_client, encryptor):
        tokens = {
            "email": "user@example.com",
            "refresh_token": "refresh-1",
            "region": "EMEA",
            "user_slot": 1,
        }
        mock_client = MagicMock()
        mock_client.refresh_oauth2.return_value = ("new-access", "new-refresh", datetime.datetime.now(datetime.UTC))

        with patch("src.services.sync_orchestrator.OmronClient", return_value=mock_client):
            client, error = await orchestrator._authenticate_omron("u1", tokens)

        assert error is None
        assert client is mock_client
        db_client.save_omron_tokens.assert_called_once()

    @pytest.mark.asyncio
    async def test_authenticate_session_expired(self, orchestrator, db_client):
        tokens = {
            "email": "user@example.com",
            "refresh_token": "refresh-1",
            "region": "EMEA",
            "user_slot": 1,
        }
        mock_client = MagicMock()
        mock_client.refresh_oauth2.return_value = None

        with patch("src.services.sync_orchestrator.OmronClient", return_value=mock_client):
            client, error = await orchestrator._authenticate_omron("u1", tokens)

        assert client is None
        assert error is not None
        assert error.status == "error"
        assert error.message == "omron_session_expired"
        db_client.update_user_status.assert_called_once_with("u1", "needs_reauth")


class TestFetchOmronBPMeasurements:
    @pytest.mark.asyncio
    async def test_fetch_success_with_devices(self, orchestrator):
        mock_client = MagicMock()
        mock_device = OmronDevice(name="BPM 1", macaddr="mac-1", category=DeviceCategory.BPM, user=1)
        mock_client.get_registered_devices.return_value = [mock_device]
        
        m1 = _bp_measurement()
        mock_client.get_measurements.return_value = [m1]

        bps, error = await orchestrator._fetch_omron_bp_measurements("u1", mock_client, 1, 1000)

        assert error is None
        assert len(bps) == 1
        assert bps[0] == m1
        mock_client.get_measurements.assert_called_once_with(mock_device, 1000)

    @pytest.mark.asyncio
    async def test_fetch_fallback_virtual_bpm(self, orchestrator):
        mock_client = MagicMock()
        mock_client.get_registered_devices.return_value = []
        mock_client.servers = ["https://vlt-mobile-api.prd.eu.ohiomron.eu/prd"]
        
        m1 = _bp_measurement()
        mock_client.get_measurements.return_value = [m1]

        bps, error = await orchestrator._fetch_omron_bp_measurements("u1", mock_client, 1, 1000)

        assert error is None
        assert len(bps) == 1
        assert bps[0] == m1
        assert mock_client.get_measurements.call_count == 1


class TestDeduplicateBPMeasurements:
    def test_dedup_tolerance_matching(self, orchestrator):
        # 2024-04-04 10:00:00 UTC
        omron_bps = [_bp_measurement(timestamp_ms=1712224800000)]
        existing_bps = [{"measurementTimestampGMT": "2024-04-04T10:02:00.000Z"}]

        new_bps = orchestrator._deduplicate_bp_measurements(omron_bps, existing_bps)
        assert len(new_bps) == 0  # Deduplicated because 2 mins < 5 mins tolerance

    def test_dedup_no_match(self, orchestrator):
        omron_bps = [_bp_measurement(timestamp_ms=1712224800000)]
        existing_bps = [{"measurementTimestampGMT": "2024-04-04T10:10:00.000Z"}]

        new_bps = orchestrator._deduplicate_bp_measurements(omron_bps, existing_bps)
        assert len(new_bps) == 1  # Kept because 10 mins > 5 mins tolerance


class TestUploadBPsToGarmin:
    @pytest.mark.asyncio
    async def test_upload_success(self, orchestrator):
        mock_garmin = MagicMock()
        mock_garmin.upload_blood_pressure = AsyncMock()

        m1 = _bp_measurement()
        with patch("src.services.sync_orchestrator.GarminClient.create_for_user", return_value=mock_garmin):
            uploaded, error = await orchestrator._upload_bps_to_garmin("u1", [m1])

        assert error is None
        assert uploaded == 1
        mock_garmin.upload_blood_pressure.assert_called_once()

    @pytest.mark.asyncio
    async def test_upload_rate_limited(self, orchestrator, db_client):
        mock_garmin = MagicMock()
        mock_garmin.upload_blood_pressure = AsyncMock(side_effect=GarminRateLimitError("Rate limit"))

        m1 = _bp_measurement()
        with patch("src.services.sync_orchestrator.GarminClient.create_for_user", return_value=mock_garmin):
            uploaded, error = await orchestrator._upload_bps_to_garmin("u1", [m1])

        assert uploaded == 0
        assert error is not None
        assert error.status == "skipped"
        assert error.message == "garmin_rate_limited"


class TestSyncOmronUser:
    @pytest.mark.asyncio
    async def test_sync_omron_success(self, orchestrator, db_client):
        # Setup tokens and session
        tokens = {
            "email": "user@example.com",
            "refresh_token": "refresh-1",
            "region": "EMEA",
            "user_slot": 1,
        }
        db_client.get_omron_tokens.return_value = tokens
        db_client.has_garmin_session.return_value = True

        # Mock helpers
        orchestrator._authenticate_omron = AsyncMock(return_value=(MagicMock(), None))
        m1 = _bp_measurement()
        orchestrator._fetch_omron_bp_measurements = AsyncMock(return_value=([m1], None))
        orchestrator._fetch_existing_garmin_bps = AsyncMock(return_value=([], None))
        orchestrator._upload_bps_to_garmin = AsyncMock(return_value=(1, None))

        result = await orchestrator.sync_omron_user("u1")

        assert result.status == "success"
        assert result.uploaded == 1
        assert result.skipped == 0
        assert result.total == 1
        db_client.log_sync.assert_called_once()
