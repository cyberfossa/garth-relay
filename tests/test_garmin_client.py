from __future__ import annotations

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest
from garth.exc import GarthHTTPError
from garth.sso.state import MFAChallenge, MFAState

from src.services.garmin_client import (
    GarminClient,
    GarminRateLimitError,
    GarminSessionExpiredError,
)


@pytest.fixture
def mock_garth_client():
    return MagicMock()


@pytest.fixture
def garmin_client(mock_garth_client):
    return GarminClient(client=mock_garth_client)


def _make_garth_http_error(status_code: int) -> GarthHTTPError:
    mock_response = MagicMock()
    mock_response.status_code = status_code
    mock_error = MagicMock()
    mock_error.response = mock_response
    exc = GarthHTTPError.__new__(GarthHTTPError)
    exc.error = mock_error
    return exc


class TestGarminClientCreation:
    def test_init_creates_default_client(self):
        mock_client = MagicMock()
        with patch("src.services.garmin_client.garth.http.Client", return_value=mock_client):
            client = GarminClient()
        assert client._client is mock_client

    def test_create_for_user_configures_storage(self):
        mock_client = MagicMock()
        mock_storage = MagicMock()
        db = MagicMock()
        encryptor = MagicMock()

        with (
            patch("src.services.garmin_client.garth.http.Client", return_value=mock_client),
            patch("src.services.garmin_client.FirestoreTokenStorage", return_value=mock_storage),
        ):
            client = GarminClient.create_for_user("user-1", db, encryptor)

        mock_client.configure.assert_called_once_with(storage=mock_storage)
        assert client._client is mock_client
        assert client._storage is mock_storage


class TestLogin:
    async def test_login_success(self, garmin_client, mock_garth_client):
        mock_garth_client.login = MagicMock(return_value=MagicMock(name="OAuth2Token"))

        result = await garmin_client.login("user@example.com", "password")

        assert result is None
        mock_garth_client.login.assert_called_once_with(
            "user@example.com", "password", return_on_mfa=True
        )

    async def test_login_401_raises_session_expired(self, garmin_client, mock_garth_client):
        mock_garth_client.login = MagicMock(side_effect=_make_garth_http_error(401))
        with pytest.raises(GarminSessionExpiredError):
            await garmin_client.login("user@example.com", "password")

    async def test_login_429_raises_rate_limit(self, garmin_client, mock_garth_client):
        mock_garth_client.login = MagicMock(side_effect=_make_garth_http_error(429))
        with pytest.raises(GarminRateLimitError):
            await garmin_client.login("user@example.com", "password")


class TestLoginMFA:
    async def test_returns_mfa_challenge(self, garmin_client, mock_garth_client):
        mock_mfa_state = MFAState(strategy_name="sms", domain="garmin.com", state={"key": "val"})
        mock_challenge = MFAChallenge(mock_mfa_state, {"GARMIN-SSO": "cookie-value"})
        mock_garth_client.login = MagicMock(return_value=mock_mfa_state)
        garmin_client._client.session.cookies = MagicMock(
            items=MagicMock(return_value={"GARMIN-SSO": "cookie-value"}.items())
        )

        result = await garmin_client.login("user@example.com", "password")

        assert result == mock_challenge
        mock_garth_client.login.assert_called_once_with(
            "user@example.com", "password", return_on_mfa=True
        )

    async def test_http_error_raises(self, garmin_client, mock_garth_client):
        mock_garth_client.login = MagicMock(side_effect=_make_garth_http_error(429))
        garmin_client._client.session.cookies = MagicMock(items=MagicMock(return_value={}.items()))

        with pytest.raises(GarminRateLimitError):
            await garmin_client.login("user@example.com", "password")


class TestCompleteMFA:
    async def test_success(self, garmin_client, mock_garth_client):
        mfa_state = MFAState(strategy_name="sms", domain="garmin.com", state={"key": "val"})
        mfa_payload = MFAChallenge(mfa_state, {"GARMIN-SSO": "cookie-value"}).to_json()
        mock_garth_client.resume_mfa = MagicMock(return_value=MagicMock())

        await garmin_client.complete_mfa(mfa_payload, "123456")
        mock_garth_client.resume_mfa.assert_called_once()

    async def test_http_error_raises(self, garmin_client, mock_garth_client):
        mfa_state = MFAState(strategy_name="sms", domain="garmin.com", state={})
        mfa_payload = MFAChallenge(mfa_state, {"GARMIN-SSO": "cookie-value"}).to_json()
        mock_garth_client.resume_mfa = MagicMock(side_effect=_make_garth_http_error(401))

        with pytest.raises(GarminSessionExpiredError):
            await garmin_client.complete_mfa(mfa_payload, "123456")


class TestUploadBodyComposition:
    async def test_upload_success(self, garmin_client, mock_garth_client):
        with patch("src.services.garmin_client.WeightData") as mock_wd:
            mock_wd.create_body_composition = MagicMock(return_value=None)
            await garmin_client.upload_body_composition(
                weight_kg=85.0,
                body_fat_pct=22.5,
                timestamp=datetime(2024, 1, 15, 10, 0, 0),
            )
            mock_wd.create_body_composition.assert_called_once_with(
                85.0,
                percent_fat=22.5,
                timestamp=datetime(2024, 1, 15, 10, 0, 0),
                client=mock_garth_client,
            )

    async def test_upload_http_error(self, garmin_client, mock_garth_client):
        with patch("src.services.garmin_client.WeightData") as mock_wd:
            mock_wd.create_body_composition = MagicMock(side_effect=_make_garth_http_error(429))
            with pytest.raises(GarminRateLimitError):
                await garmin_client.upload_body_composition(
                    weight_kg=85.0, body_fat_pct=22.5, timestamp=datetime(2024, 1, 15, 10, 0, 0)
                )


class TestFetchExistingWeights:
    async def test_fetch_success(self, garmin_client, mock_garth_client):
        mock_weight = MagicMock()
        mock_weight.datetime_utc = datetime(2024, 1, 15, 10, 0, 0)
        mock_weight.weight = 85000

        with patch("src.services.garmin_client.WeightData") as mock_wd:
            mock_wd.list = MagicMock(return_value=[mock_weight])
            result = await garmin_client.fetch_existing_weights(end_date=date(2024, 1, 15), days=7)
            assert len(result) == 1
            assert result[0]["weight_kg"] == 85.0

    async def test_fetch_http_error(self, garmin_client, mock_garth_client):
        with patch("src.services.garmin_client.WeightData") as mock_wd:
            mock_wd.list = MagicMock(side_effect=_make_garth_http_error(401))
            with pytest.raises(GarminSessionExpiredError):
                await garmin_client.fetch_existing_weights(end_date=date(2024, 1, 15), days=7)


class TestRaiseMappedHttpError:
    def test_401_raises_session_expired(self, garmin_client):
        exc = _make_garth_http_error(401)
        with pytest.raises(GarminSessionExpiredError):
            garmin_client._raise_mapped_http_error(exc)

    def test_429_raises_rate_limit(self, garmin_client):
        exc = _make_garth_http_error(429)
        with pytest.raises(GarminRateLimitError):
            garmin_client._raise_mapped_http_error(exc)

    def test_500_does_not_raise(self, garmin_client):
        exc = _make_garth_http_error(500)
        garmin_client._raise_mapped_http_error(exc)
