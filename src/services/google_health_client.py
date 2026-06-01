"""Google Health API client for fetching weight and body composition data."""

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import NotRequired, TypedDict, cast

import httpx
import structlog

logger = structlog.get_logger()

JsonValue = object
JsonDict = dict[str, JsonValue]

GOOGLE_HEALTH_SCOPE = "https://www.googleapis.com/auth/fitness.body.read"


class Measurement(TypedDict):
    weight_kg: NotRequired[float | None]
    body_fat_pct: NotRequired[float | None]
    timestamp: datetime


class GoogleHealthError(Exception):
    """Base exception for Google Health API errors."""


class GoogleTokenExpiredError(GoogleHealthError):
    """Raised when the Google access token is expired (HTTP 401)."""


class GoogleScopeRevokedError(GoogleHealthError):
    """Raised when the required Google scope has been revoked (HTTP 403)."""


@dataclass
class GoogleHealthData:
    """Latest health measurement result."""

    weight_kg: float
    body_fat_percentage: float | None
    timestamp: datetime


@dataclass
class WeightDataPoint:
    weight_kg: float
    timestamp: datetime
    utc_offset_seconds: int
    civil_time_hours: int


@dataclass
class BodyFatDataPoint:
    percentage: float
    timestamp: datetime
    utc_offset_seconds: int
    civil_time_hours: int


class GoogleHealthAPIClient:
    """Client for Google Health API v4 — weight and body composition data."""

    BASE_URL: str = "https://health.googleapis.com/v4"

    def __init__(
        self,
        max_retries: int = 3,
        retry_delay: int = 1,
        http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
    ):
        self.max_retries: int = max_retries
        self.retry_delay: int = retry_delay
        self.http_client_factory: Callable[[], httpx.AsyncClient] = http_client_factory or (
            lambda: httpx.AsyncClient(timeout=30)
        )

    async def fetch_weight(self, access_token: str, since_timestamp: str) -> list[WeightDataPoint]:
        """Fetch weight data points since given timestamp."""
        logger.debug("fetch_weight", since_timestamp=since_timestamp)
        data_points = await self._fetch_data_points(
            access_token=access_token,
            data_type_path="weight",
            filter_value=f'weight.sample_time.physical_time >= "{since_timestamp}"',
        )
        return [self._parse_weight_data_point(data_point) for data_point in data_points]

    async def fetch_body_fat(self, access_token: str, since_timestamp: str) -> list[BodyFatDataPoint]:
        """Fetch body fat data points since given timestamp."""
        logger.debug("fetch_body_fat", since_timestamp=since_timestamp)
        data_points = await self._fetch_data_points(
            access_token=access_token,
            data_type_path="body-fat",
            filter_value=f'body_fat.sample_time.physical_time >= "{since_timestamp}"',
        )
        return [self._parse_body_fat_data_point(data_point) for data_point in data_points]

    async def fetch_latest_measurements(self, access_token: str, since_timestamp: str) -> GoogleHealthData | None:
        """Fetch latest weight measurement, paired with closest body fat if available."""
        weight_points = await self.fetch_weight(access_token, since_timestamp)
        if not weight_points:
            return None

        latest_weight = max(weight_points, key=lambda point: point.timestamp)
        body_fat_points = await self.fetch_body_fat(access_token, since_timestamp)

        body_fat_percentage = None
        if body_fat_points:
            closest_body_fat = min(
                body_fat_points,
                key=lambda point: abs((point.timestamp - latest_weight.timestamp).total_seconds()),
            )
            body_fat_percentage = closest_body_fat.percentage

        return GoogleHealthData(
            weight_kg=latest_weight.weight_kg,
            body_fat_percentage=body_fat_percentage,
            timestamp=latest_weight.timestamp,
        )

    async def fetch_all_measurements(
        self,
        access_token: str,
        since_timestamp: str,
        until_timestamp: str | None = None,
    ) -> list[Measurement]:
        """Fetch all measurements in range, pairing weight with nearest body fat."""
        weight_points = await self.fetch_weight(access_token, since_timestamp)
        body_fat_points = await self.fetch_body_fat(access_token, since_timestamp)

        until_dt: datetime | None = None
        if until_timestamp is not None:
            until_dt = datetime.fromisoformat(until_timestamp.replace("Z", "+00:00"))

        if until_dt is not None:
            weight_points = [p for p in weight_points if p.timestamp <= until_dt]
            body_fat_points = [p for p in body_fat_points if p.timestamp <= until_dt]

        pairing_threshold_seconds = 86400

        measurements: list[Measurement] = []
        for weight in weight_points:
            body_fat_pct: float | None = None
            if body_fat_points:
                closest = min(
                    body_fat_points,
                    key=lambda bf: abs((bf.timestamp - weight.timestamp).total_seconds()),
                )
                if abs((closest.timestamp - weight.timestamp).total_seconds()) <= pairing_threshold_seconds:
                    body_fat_pct = closest.percentage

            measurements.append(
                Measurement(
                    weight_kg=weight.weight_kg,
                    body_fat_pct=body_fat_pct,
                    timestamp=weight.timestamp,
                )
            )

        measurements.sort(key=lambda m: m["timestamp"])
        return measurements

    async def _fetch_data_points(self, access_token: str, data_type_path: str, filter_value: str) -> list[JsonDict]:
        """Fetch paginated data points from the Health API."""
        headers = {"Authorization": f"Bearer {access_token}"}
        path = f"/users/me/dataTypes/{data_type_path}/dataPoints"
        collected_points: list[JsonDict] = []
        page_token: str | None = None

        while True:
            params: dict[str, str] = {"filter": filter_value}
            if page_token:
                params["pageToken"] = page_token

            response = await self._request_with_retry(path=path, headers=headers, params=params)
            payload = cast(JsonDict, response.json())
            page_data_points = cast(list[JsonDict], payload.get("dataPoints", []))
            collected_points.extend(page_data_points)

            next_page_token = cast(str | None, payload.get("nextPageToken"))
            if not next_page_token:
                break
            page_token = next_page_token

        return collected_points

    async def _request_with_retry(self, path: str, headers: dict[str, str], params: dict[str, str]) -> httpx.Response:
        """Make HTTP request with exponential backoff retry on 5xx."""
        async with self.http_client_factory() as client:
            for attempt in range(self.max_retries):
                response = await client.get(f"{self.BASE_URL}{path}", headers=headers, params=params)

                if response.status_code == 401:
                    raise GoogleTokenExpiredError("Google access token expired")
                if response.status_code == 403:
                    raise GoogleScopeRevokedError("Google scope revoked")

                if 500 <= response.status_code < 600:
                    if attempt == self.max_retries - 1:
                        _ = response.raise_for_status()
                    backoff_seconds = self.retry_delay * (1 << attempt)
                    logger.warning(
                        "health_api_5xx_retry",
                        status=response.status_code,
                        path=path,
                        backoff=backoff_seconds,
                        attempt=attempt + 1,
                    )
                    await asyncio.sleep(backoff_seconds)
                    continue

                _ = response.raise_for_status()
                return response

        raise RuntimeError("Unreachable retry state")

    def _parse_weight_data_point(self, data_point: JsonDict) -> WeightDataPoint:
        weight = cast(JsonDict, data_point.get("weight", {}))
        sample_time = cast(JsonDict, weight.get("sampleTime", {}))
        physical_time = cast(str | None, sample_time.get("physicalTime"))
        weight_grams = cast(int, weight.get("weightGrams", 0))
        utc_offset = cast(str, sample_time.get("utcOffset", "0s"))

        return WeightDataPoint(
            weight_kg=float(weight_grams) / 1000,
            timestamp=self._parse_timestamp(physical_time),
            utc_offset_seconds=self._parse_utc_offset_seconds(utc_offset),
            civil_time_hours=self._parse_civil_time_hours(sample_time),
        )

    def _parse_body_fat_data_point(self, data_point: JsonDict) -> BodyFatDataPoint:
        body_fat = cast(JsonDict, data_point.get("bodyFat", {}))
        sample_time = cast(JsonDict, body_fat.get("sampleTime", {}))
        physical_time = cast(str | None, sample_time.get("physicalTime"))
        percentage = cast(float | int, body_fat.get("percentage", 0.0))
        utc_offset = cast(str, sample_time.get("utcOffset", "0s"))

        return BodyFatDataPoint(
            percentage=float(percentage),
            timestamp=self._parse_timestamp(physical_time),
            utc_offset_seconds=self._parse_utc_offset_seconds(utc_offset),
            civil_time_hours=self._parse_civil_time_hours(sample_time),
        )

    @staticmethod
    def _parse_timestamp(value: str | None) -> datetime:
        if not value:
            raise ValueError("Missing sampleTime.physicalTime")
        return datetime.fromisoformat(value.replace("Z", "+00:00"))

    @staticmethod
    def _parse_utc_offset_seconds(value: str) -> int:
        return int(value.rstrip("s"))

    @staticmethod
    def _parse_civil_time_hours(sample_time: Mapping[str, JsonValue]) -> int:
        civil_time = cast(Mapping[str, JsonValue], sample_time.get("civilTime", {}))
        civil_time_time = cast(Mapping[str, JsonValue], civil_time.get("time", {}))
        return int(cast(int, civil_time_time.get("hours", 0)))
