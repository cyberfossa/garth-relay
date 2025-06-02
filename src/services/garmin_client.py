"""Garmin Connect client wrapping garth-ng."""

import asyncio
from datetime import date, datetime
from typing import cast

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

    def _raise_mapped_http_error(self, exc: GarthHTTPError) -> None:
        status_code = self._extract_status_code(exc)
        if status_code == 401:
            raise GarminSessionExpiredError("Garmin session expired") from exc
        if status_code == 429:
            raise GarminRateLimitError("Garmin rate limit exceeded") from exc

    def _extract_status_code(self, exc: GarthHTTPError) -> int | None:
        response = getattr(exc.error, "response", None)
        return getattr(response, "status_code", None)
