"""Garmin Connect client wrapping garth-ng."""

import asyncio
from datetime import UTC, date, datetime
from typing import Any, cast

import garth
import structlog
from garth.auth_tokens import OAuth2Token
from garth.data.weight import WeightData
from garth.exc import GarthHTTPError
from garth.http import Client as GarthHttpClient
from garth.sso.state import MFAChallenge, MFAState
from google.cloud import firestore
from structlog.typing import FilteringBoundLogger

from src.crypto import TokenEncryptor
from src.db.firestore_token_storage import FirestoreTokenStorage

logger = cast(FilteringBoundLogger, structlog.get_logger())


class GarminSessionExpiredError(Exception):
    pass


class GarminRateLimitError(Exception):
    pass


class GarminClient:
    def __init__(self, client: GarthHttpClient | None = None, storage: FirestoreTokenStorage | None = None) -> None:
        if client is None:
            client = garth.http.Client()
        self._client: GarthHttpClient = client
        self._storage: FirestoreTokenStorage | None = storage

    @classmethod
    def create_for_user(cls, user_id: str, db: firestore.Client, encryptor: TokenEncryptor) -> "GarminClient":
        storage = FirestoreTokenStorage(user_id, db, encryptor)
        client = garth.http.Client()
        client.configure(storage=storage)
        return cls(client, storage)

    async def login(self, email: str, password: str) -> MFAChallenge | None:
        try:
            result: OAuth2Token | MFAState = await asyncio.to_thread(
                self._client.login, email, password, return_on_mfa=True
            )
            if isinstance(result, MFAState):
                return MFAChallenge(mfa_state=result, cookies=dict(self._client.session.cookies.items()))
            return None
        except GarthHTTPError as exc:
            self._raise_mapped_http_error(exc)
            raise

    async def complete_mfa(self, mfa_state_json: str, mfa_code: str) -> None:
        try:
            challenge = MFAChallenge.from_json(mfa_state_json)
            _ = await asyncio.to_thread(self._client.resume_mfa, challenge, mfa_code)
        except GarthHTTPError as exc:
            self._raise_mapped_http_error(exc)
            raise

    async def upload_body_composition(
        self,
        weight_kg: float,
        body_fat_pct: float | None,
        timestamp: datetime,
    ) -> None:
        try:
            logger.info(
                "Uploading body composition to Garmin",
                weight_kg=weight_kg,
                body_fat_pct=body_fat_pct,
                timestamp=timestamp.isoformat(),
            )
            await asyncio.to_thread(
                WeightData.create_body_composition,
                weight_kg,
                percent_fat=body_fat_pct,
                timestamp=timestamp,
                client=self._client,
            )
            logger.info("Body composition uploaded to Garmin successfully")
        except GarthHTTPError as exc:
            logger.warning(
                "Garmin upload failed",
                weight_kg=weight_kg,
                timestamp=timestamp.isoformat(),
                exc_info=True,
            )
            self._raise_mapped_http_error(exc)
            raise

    async def fetch_existing_weights(
        self,
        end_date: date,
        days: int,
    ) -> list[dict[str, object]]:
        try:
            logger.debug(
                "Fetching existing weights from Garmin",
                end_date=end_date.isoformat(),
                days=days,
            )
            weights = await asyncio.to_thread(WeightData.list, end=end_date, days=days, client=self._client)
            result: list[dict[str, object]] = [
                {
                    "timestamp_utc": weight.datetime_utc,
                    "weight_kg": weight.weight / 1000,
                }
                for weight in weights
            ]
            logger.debug("Fetched %d weight records from Garmin", len(result))
            return result
        except GarthHTTPError as exc:
            logger.warning(
                "Garmin weight fetch failed",
                end_date=end_date.isoformat(),
                days=days,
                exc_info=True,
            )
            self._raise_mapped_http_error(exc)
            raise

    async def upload_blood_pressure(
        self,
        systolic: int,
        diastolic: int,
        pulse: int,
        timestamp: datetime,
        notes: str | None = None,
    ) -> None:
        try:
            # Garmin expects measurementTimestampLocal (YYYY-MM-DDTHH:MM:SS.000) and measurementTimestampGMT (YYYY-MM-DDTHH:MM:SS.000)
            # Normalize to UTC
            if timestamp.tzinfo is None:
                utc_dt = timestamp.replace(tzinfo=UTC)
                local_dt = timestamp
            else:
                utc_dt = timestamp.astimezone(UTC)
                local_dt = timestamp

            payload = {
                "measurementTimestampLocal": local_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
                "measurementTimestampGMT": utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000"),
                "systolic": systolic,
                "diastolic": diastolic,
                "pulse": pulse,
                "sourceType": "MANUAL",
                "notes": notes or "",
            }

            for name, val, lo, hi in (
                ("systolic", systolic, 70, 260),
                ("diastolic", diastolic, 40, 150),
                ("pulse", pulse, 20, 250),
            ):
                if not isinstance(val, int) or not (lo <= val <= hi):
                    raise ValueError(f"{name} must be an int in [{lo}, {hi}]")

            logger.info("Uploading blood pressure to Garmin", payload=payload)
            await asyncio.to_thread(
                self._client.connectapi,
                "/bloodpressure-service/bloodpressure",
                method="POST",
                json=payload,
            )
        except GarthHTTPError as exc:
            logger.warning(
                "Garmin blood pressure upload failed",
                systolic=systolic,
                diastolic=diastolic,
                pulse=pulse,
                timestamp=timestamp.isoformat(),
                exc_info=True,
            )
            self._raise_mapped_http_error(exc)
            raise

    async def fetch_existing_blood_pressures(
        self,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        try:
            logger.debug(
                "Fetching existing blood pressures from Garmin",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
            )
            path = f"/bloodpressure-service/bloodpressure/range/{start_date.isoformat()}/{end_date.isoformat()}"
            response = await asyncio.to_thread(
                self._client.connectapi,
                path,
                method="GET",
            )
            result: list[dict[str, Any]] = []
            if isinstance(response, dict) and "measurementSummaries" in response:
                for summary in response["measurementSummaries"]:
                    for m in summary.get("measurements", []):
                        result.append(m)
            logger.debug("Fetched %d blood pressure records from Garmin", len(result))
            return result
        except GarthHTTPError as exc:
            logger.warning(
                "Garmin blood pressure fetch failed",
                start_date=start_date.isoformat(),
                end_date=end_date.isoformat(),
                exc_info=True,
            )
            self._raise_mapped_http_error(exc)
            raise

    def _raise_mapped_http_error(self, exc: GarthHTTPError) -> None:
        status_code = self._extract_status_code(exc)
        if status_code == 401:
            raise GarminSessionExpiredError("Garmin session expired") from exc
        if status_code == 429:
            raise GarminRateLimitError("Garmin rate limit exceeded") from exc

    def _extract_status_code(self, exc: GarthHTTPError) -> int | None:
        response = getattr(exc.error, "response", None)
        return getattr(response, "status_code", None)
