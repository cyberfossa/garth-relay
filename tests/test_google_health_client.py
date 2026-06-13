"""Tests for Google Health API client."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.google_health_client import (
    BodyFatDataPoint,
    GoogleHealthAPIClient,
    GoogleScopeRevokedError,
    GoogleTokenExpiredError,
    WeightDataPoint,
)


@pytest.fixture
def health_client():
    return GoogleHealthAPIClient(max_retries=2, retry_delay=0)


class TestParseTimestamp:
    def test_parses_iso_with_z(self):
        result = GoogleHealthAPIClient._parse_timestamp("2024-01-15T10:30:00Z")
        assert result == datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC)

    def test_parses_iso_with_offset(self):
        result = GoogleHealthAPIClient._parse_timestamp("2024-01-15T10:30:00+01:00")
        assert result.hour == 10

    def test_raises_on_none(self):
        with pytest.raises(ValueError, match="Missing"):
            GoogleHealthAPIClient._parse_timestamp(None)

    def test_raises_on_empty(self):
        with pytest.raises(ValueError, match="Missing"):
            GoogleHealthAPIClient._parse_timestamp("")


class TestParseUtcOffsetSeconds:
    def test_parses_positive_offset(self):
        assert GoogleHealthAPIClient._parse_utc_offset_seconds("3600s") == 3600

    def test_parses_zero(self):
        assert GoogleHealthAPIClient._parse_utc_offset_seconds("0s") == 0

    def test_parses_negative(self):
        assert GoogleHealthAPIClient._parse_utc_offset_seconds("-7200s") == -7200


class TestParseCivilTimeHours:
    def test_parses_hours(self):
        sample_time = {"civilTime": {"time": {"hours": 14}}}
        assert GoogleHealthAPIClient._parse_civil_time_hours(sample_time) == 14

    def test_defaults_to_zero(self):
        assert GoogleHealthAPIClient._parse_civil_time_hours({}) == 0

    def test_missing_time_key(self):
        assert GoogleHealthAPIClient._parse_civil_time_hours({"civilTime": {}}) == 0


class TestParseWeightDataPoint:
    def test_parses_valid_data_point(self, health_client):
        data_point = {
            "weight": {
                "weightGrams": 85500,
                "sampleTime": {
                    "physicalTime": "2024-01-15T10:00:00Z",
                    "utcOffset": "3600s",
                    "civilTime": {"time": {"hours": 11}},
                },
            }
        }
        result = health_client._parse_weight_data_point(data_point)
        assert isinstance(result, WeightDataPoint)
        assert result.weight_kg == 85.5
        assert result.utc_offset_seconds == 3600
        assert result.civil_time_hours == 11


class TestParseBodyFatDataPoint:
    def test_parses_valid_data_point(self, health_client):
        data_point = {
            "bodyFat": {
                "percentage": 22.3,
                "sampleTime": {
                    "physicalTime": "2024-01-15T10:00:00Z",
                    "utcOffset": "3600s",
                    "civilTime": {"time": {"hours": 11}},
                },
            }
        }
        result = health_client._parse_body_fat_data_point(data_point)
        assert isinstance(result, BodyFatDataPoint)
        assert result.percentage == 22.3
        assert result.utc_offset_seconds == 3600


class TestFetchWeight:
    @pytest.mark.asyncio
    async def test_fetch_weight_returns_parsed_points(self, health_client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "dataPoints": [
                {
                    "weight": {
                        "weightGrams": 85500,
                        "sampleTime": {
                            "physicalTime": "2024-01-15T10:00:00Z",
                            "utcOffset": "0s",
                        },
                    }
                }
            ]
        }
        mock_response.status_code = 200

        with patch.object(health_client, "_request_with_retry", new_callable=AsyncMock, return_value=mock_response):
            result = await health_client.fetch_weight("access-tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0].weight_kg == 85.5


class TestFetchBodyFat:
    @pytest.mark.asyncio
    async def test_fetch_body_fat_returns_parsed_points(self, health_client):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "dataPoints": [
                {
                    "bodyFat": {
                        "percentage": 22.3,
                        "sampleTime": {
                            "physicalTime": "2024-01-15T10:00:00Z",
                            "utcOffset": "0s",
                        },
                    }
                }
            ]
        }
        mock_response.status_code = 200

        with patch.object(health_client, "_request_with_retry", new_callable=AsyncMock, return_value=mock_response):
            result = await health_client.fetch_body_fat("access-tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0].percentage == 22.3


class TestFetchLatestMeasurements:
    @pytest.mark.asyncio
    async def test_returns_none_when_no_weight(self, health_client):
        with patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=[]):
            result = await health_client.fetch_latest_measurements("tok", "2024-01-01T00:00:00Z")
            assert result is None

    @pytest.mark.asyncio
    async def test_returns_latest_weight_with_body_fat(self, health_client):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
            WeightDataPoint(
                weight_kg=86.0,
                timestamp=ts - timedelta(days=1),
                utc_offset_seconds=0,
                civil_time_hours=10,
            ),
        ]
        body_fat_points = [
            BodyFatDataPoint(percentage=22.5, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=body_fat_points),
        ):
            result = await health_client.fetch_latest_measurements("tok", "2024-01-01T00:00:00Z")
            assert result is not None
            assert result.weight_kg == 85.0
            assert result.body_fat_percentage == 22.5

    @pytest.mark.asyncio
    async def test_returns_weight_without_body_fat(self, health_client):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=[]),
        ):
            result = await health_client.fetch_latest_measurements("tok", "2024-01-01T00:00:00Z")
            assert result is not None
            assert result.weight_kg == 85.0
            assert result.body_fat_percentage is None


class TestFetchAllMeasurements:
    @pytest.mark.asyncio
    async def test_returns_paired_measurements(self, health_client):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]
        body_fat_points = [
            BodyFatDataPoint(percentage=22.5, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=body_fat_points),
        ):
            result = await health_client.fetch_all_measurements("tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0]["weight_kg"] == 85.0
            assert result[0]["body_fat_pct"] == 22.5

    @pytest.mark.asyncio
    async def test_filters_by_until_timestamp(self, health_client):
        ts1 = datetime(2024, 1, 10, 10, 0, 0, tzinfo=UTC)
        ts2 = datetime(2024, 1, 20, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts1, utc_offset_seconds=0, civil_time_hours=10),
            WeightDataPoint(weight_kg=86.0, timestamp=ts2, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=[]),
        ):
            result = await health_client.fetch_all_measurements(
                "tok", "2024-01-01T00:00:00Z", until_timestamp="2024-01-15T00:00:00Z"
            )
            assert len(result) == 1
            assert result[0]["weight_kg"] == 85.0

    @pytest.mark.asyncio
    async def test_body_fat_too_far_from_weight(self, health_client):
        ts_weight = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        ts_fat = datetime(2024, 1, 18, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts_weight, utc_offset_seconds=0, civil_time_hours=10),
        ]
        body_fat_points = [
            BodyFatDataPoint(percentage=22.5, timestamp=ts_fat, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=body_fat_points),
        ):
            result = await health_client.fetch_all_measurements("tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0]["body_fat_pct"] is None

    @pytest.mark.asyncio
    async def test_deduplicates_by_timestamp(self, health_client):
        ts = datetime(2024, 1, 15, 10, 0, 0, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
            WeightDataPoint(weight_kg=84.9, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]
        body_fat_points = [
            BodyFatDataPoint(percentage=22.5, timestamp=ts, utc_offset_seconds=0, civil_time_hours=10),
        ]

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=body_fat_points),
        ):
            result = await health_client.fetch_all_measurements("tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0]["weight_kg"] in (85.0, 84.9)
            assert result[0]["body_fat_pct"] == 22.5

    @pytest.mark.asyncio
    async def test_deduplicates_subsecond_timestamps(self, health_client):
        ts1 = datetime(2024, 1, 15, 10, 0, 0, 123456, tzinfo=UTC)
        ts2 = datetime(2024, 1, 15, 10, 0, 0, 789012, tzinfo=UTC)
        weight_points = [
            WeightDataPoint(weight_kg=85.0, timestamp=ts1, utc_offset_seconds=0, civil_time_hours=10),
            WeightDataPoint(weight_kg=84.9, timestamp=ts2, utc_offset_seconds=0, civil_time_hours=10),
        ]
        body_fat_points = []

        with (
            patch.object(health_client, "fetch_weight", new_callable=AsyncMock, return_value=weight_points),
            patch.object(health_client, "fetch_body_fat", new_callable=AsyncMock, return_value=body_fat_points),
        ):
            result = await health_client.fetch_all_measurements("tok", "2024-01-01T00:00:00Z")
            assert len(result) == 1
            assert result[0]["weight_kg"] in (85.0, 84.9)


class TestRequestWithRetry:
    @pytest.mark.asyncio
    async def test_401_raises_token_expired(self, health_client):
        mock_response = MagicMock()
        mock_response.status_code = 401

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        health_client.http_client_factory = lambda: mock_client

        with pytest.raises(GoogleTokenExpiredError):
            await health_client._request_with_retry("/path", {"Authorization": "Bearer tok"}, {"filter": "x"})

    @pytest.mark.asyncio
    async def test_403_raises_scope_revoked(self, health_client):
        mock_response = MagicMock()
        mock_response.status_code = 403

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        health_client.http_client_factory = lambda: mock_client

        with pytest.raises(GoogleScopeRevokedError):
            await health_client._request_with_retry("/path", {"Authorization": "Bearer tok"}, {"filter": "x"})

    @pytest.mark.asyncio
    async def test_success_returns_response(self, health_client):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        health_client.http_client_factory = lambda: mock_client

        result = await health_client._request_with_retry("/path", {"Authorization": "Bearer tok"}, {"filter": "x"})
        assert result is mock_response


class TestFetchDataPoints:
    @pytest.mark.asyncio
    async def test_pagination(self, health_client):
        page1_response = MagicMock()
        page1_response.json.return_value = {
            "dataPoints": [{"id": 1}],
            "nextPageToken": "page2",
        }
        page1_response.status_code = 200

        page2_response = MagicMock()
        page2_response.json.return_value = {
            "dataPoints": [{"id": 2}],
        }
        page2_response.status_code = 200

        with patch.object(
            health_client,
            "_request_with_retry",
            new_callable=AsyncMock,
            side_effect=[page1_response, page2_response],
        ):
            result = await health_client._fetch_data_points("tok", "weight", "filter")
            assert len(result) == 2
