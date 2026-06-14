from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, cast

import httpx
import structlog

from src.config import get_config
from src.crypto import TokenEncryptor
from src.db.firestore_client import FirestoreClient
from src.models.oauth_models import OAuthToken
from src.services.garmin_client import GarminClient, GarminRateLimitError, GarminSessionExpiredError
from src.services.google_health_client import GoogleHealthAPIClient, GoogleScopeRevokedError, GoogleTokenExpiredError
from src.services.omron_client import BPMeasurement, DeviceCategory, OmronClient, OmronDevice

logger = structlog.get_logger()

SYNC_WINDOW_DAYS = 7
DEDUP_TOLERANCE_MINUTES = 5

# Czech localization strings for sync notes
# Note: The application's user-facing logs and dashboard are Czech by design.
OMRON_SYNC_PREFIX = "Sync z Omron Connect."
IRREGULAR_PULSE_YES = "Nepravidelný puls: Ano"
IRREGULAR_PULSE_NO = "Nepravidelný puls: Ne"
MOVEMENT_DETECT_YES = "Pohyb při měření: Ano"
MOVEMENT_DETECT_NO = "Pohyb při měření: Ne"


@dataclass
class SyncResult:
    status: str
    user_id: str
    message: str = ""
    uploaded: int = 0
    skipped: int = 0
    total: int = 0


@dataclass
class PollSummary:
    synced: int
    skipped: int
    errors: int
    total: int
    duration_seconds: float


class SyncOrchestrator:
    def __init__(
        self,
        google_client: GoogleHealthAPIClient,
        db_client: FirestoreClient,
        encryptor: TokenEncryptor,
    ):
        self.google: GoogleHealthAPIClient = google_client
        self.db: FirestoreClient = db_client
        self.encryptor: TokenEncryptor = encryptor

    async def upload_measurement(
        self,
        user_id: str,
        weight_kg: float,
        body_fat_pct: float | None,
        timestamp: datetime,
        source: str = "google_health",
    ) -> SyncResult:
        del source

        session = self.db.has_garmin_session(user_id)
        if not session:
            _ = self.db.log_sync(user_id, "error", weight_kg, body_fat_pct, "Missing Garmin session")
            return SyncResult(status="error", user_id=user_id, message="missing_garmin_session", total=1)

        try:
            garmin = GarminClient.create_for_user(user_id, self.db.db, self.encryptor)
            await garmin.upload_body_composition(weight_kg, body_fat_pct, timestamp)
            _ = self.db.log_sync(user_id, "success", weight_kg, body_fat_pct, None)
            return SyncResult(status="success", user_id=user_id, message="uploaded", uploaded=1, total=1)
        except GarminSessionExpiredError as exc:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            _ = self.db.log_sync(user_id, "error", weight_kg, body_fat_pct, str(exc))
            return SyncResult(status="error", user_id=user_id, message="garmin_session_expired", total=1)
        except GarminRateLimitError as exc:
            _ = self.db.log_sync(user_id, "error", weight_kg, body_fat_pct, str(exc))
            return SyncResult(status="skipped", user_id=user_id, message="garmin_rate_limited", skipped=1, total=1)

    async def sync_user(self, user_id: str) -> SyncResult:  # noqa: PLR0911
        profile = self.db.get_user_profile(user_id)
        if profile and not getattr(profile, "sync_enabled", True):
            logger.info("Automatic sync is disabled by user, skipping", user_id=user_id)
            return SyncResult(status="skipped", user_id=user_id, message="sync_disabled_by_user", total=0)

        token = self.db.get_oauth_token(user_id, "google")
        session = self.db.has_garmin_session(user_id)
        prereq_error = self._validate_sync_prerequisites(user_id, token, session)
        if prereq_error:
            return SyncResult(status="error", user_id=user_id, message=prereq_error)
        assert token is not None

        now = datetime.now(UTC)
        _ = self.db.get_last_sync_timestamp(user_id)
        since_timestamp = (now - timedelta(days=SYNC_WINDOW_DAYS)).isoformat()

        measurements, fetch_error = await self._fetch_google_measurements_window(user_id, token, since_timestamp, now)
        if fetch_error:
            return SyncResult(status="error", user_id=user_id, message=fetch_error)

        total = len(measurements)
        if total == 0:
            _ = self.db.log_sync(user_id, "success", None, None, "No Google measurements in sync window")
            return SyncResult(status="skipped", user_id=user_id, message="no_measurements", total=0)

        existing_weights, garmin_error = await self._fetch_existing_garmin_weights(
            user_id,
            now.date(),
            total,
        )
        if garmin_error:
            return SyncResult(status=garmin_error.status, user_id=user_id, message=garmin_error.message, total=total)

        uploaded, skipped, upload_error = await self._upload_new_measurements(user_id, measurements, existing_weights)
        if upload_error:
            return SyncResult(
                status="error",
                user_id=user_id,
                message=upload_error,
                uploaded=uploaded,
                skipped=skipped,
                total=total,
            )

        summary_status = "success" if uploaded > 0 else "skipped"
        summary_message = f"uploaded={uploaded} skipped={skipped} total={total}"
        _ = self.db.log_sync(user_id, summary_status, None, None, summary_message)
        return SyncResult(
            status=summary_status,
            user_id=user_id,
            message="sync_completed",
            uploaded=uploaded,
            skipped=skipped,
            total=total,
        )

    async def _fetch_google_measurements_window(
        self,
        user_id: str,
        token: OAuthToken,
        since_timestamp: str,
        now: datetime,
    ) -> tuple[list[dict[str, object]], str | None]:
        try:
            _ = await self.google.fetch_latest_measurements(token.access_token, since_timestamp)
            measurements = await self.google.fetch_all_measurements(
                token.access_token, since_timestamp, now.isoformat()
            )
            return cast(list[dict[str, object]], measurements), None
        except GoogleTokenExpiredError:
            return await self._fetch_measurements_with_refresh(user_id, token, since_timestamp, now)
        except GoogleScopeRevokedError:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return [], "google_scope_revoked"

    async def _fetch_existing_garmin_weights(
        self,
        user_id: str,
        today: date,
        total: int,
    ) -> tuple[list[dict[str, object]], SyncResult | None]:
        try:
            logger.debug(
                "Fetching existing Garmin weights for dedup",
                user_id=user_id,
                end_date=today.isoformat(),
                days=SYNC_WINDOW_DAYS,
            )
            garmin = GarminClient.create_for_user(user_id, self.db.db, self.encryptor)
            existing_weights = await garmin.fetch_existing_weights(today, SYNC_WINDOW_DAYS)
            logger.debug(
                "Garmin dedup data fetched",
                user_id=user_id,
                garmin_weight_count=len(existing_weights),
            )
            return existing_weights, None
        except GarminSessionExpiredError:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return [], SyncResult(status="error", user_id=user_id, message="garmin_session_expired", total=total)
        except GarminRateLimitError as exc:
            _ = self.db.log_sync(user_id, "error", None, None, str(exc))
            return [], SyncResult(status="skipped", user_id=user_id, message="garmin_rate_limited", total=total)

    async def _upload_new_measurements(
        self,
        user_id: str,
        measurements: list[dict[str, object]],
        existing_weights: list[dict[str, object]],
    ) -> tuple[int, int, str | None]:
        uploaded = 0
        skipped = 0
        tolerance = timedelta(minutes=DEDUP_TOLERANCE_MINUTES)

        for measurement in measurements:
            measurement_ts = cast(datetime, measurement["timestamp"])
            measurement_weight = cast(float | None, measurement.get("weight_kg"))
            measurement_body_fat = cast(float | None, measurement.get("body_fat_pct"))

            if measurement_weight is None:
                skipped += 1
                continue

            if self._has_matching_weight(existing_weights, measurement_ts, tolerance):
                logger.info("Skipping measurement from %s — already exists in Garmin", measurement_ts.isoformat())
                skipped += 1
                continue

            logger.info("Uploading %.2fkg from %s", measurement_weight, measurement_ts.isoformat())
            result = await self.upload_measurement(
                user_id=user_id,
                weight_kg=measurement_weight,
                body_fat_pct=measurement_body_fat,
                timestamp=measurement_ts,
                source="google_health",
            )
            if result.status == "success":
                uploaded += 1
                existing_weights.append({"timestamp_utc": measurement_ts, "weight_kg": measurement_weight})
                continue
            if result.message == "garmin_rate_limited":
                skipped += 1
                continue
            return uploaded, skipped, result.message

        return uploaded, skipped, None

    async def _authenticate_omron(
        self, user_id: str, omron_tokens: dict[str, Any]
    ) -> tuple[OmronClient | None, SyncResult | None]:
        try:
            client = OmronClient(region=omron_tokens["region"])
            new_tokens = await asyncio.to_thread(
                client.refresh_oauth2,
                refresh_token=omron_tokens["refresh_token"],
                email=omron_tokens["email"],
            )
            if not new_tokens:
                _ = self.db.update_user_status(user_id, "needs_reauth")
                _ = self.db.log_sync(user_id, "error", error_message="Omron Connect session expired")
                return None, SyncResult(status="error", user_id=user_id, message="omron_session_expired")

            access_token, refresh_token, expires_at = new_tokens
            _ = self.db.save_omron_tokens(
                user_id=user_id,
                email=omron_tokens["email"],
                access_token=access_token,
                refresh_token=refresh_token,
                expires_at=expires_at,
                region=omron_tokens["region"],
                user_slot=omron_tokens["user_slot"],
                encryptor=self.encryptor,
            )
            return client, None
        except Exception as exc:
            logger.exception("Omron authentication failed during sync for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=f"Omron auth failed: {exc}")
            return None, SyncResult(status="error", user_id=user_id, message="omron_auth_failed")

    async def _fetch_omron_bp_measurements(
        self, user_id: str, client: OmronClient, user_slot: int, since_timestamp_ms: int
    ) -> tuple[list[BPMeasurement] | None, SyncResult | None]:
        try:
            devices = await asyncio.to_thread(client.get_registered_devices)
            bpm_devices = []
            if devices:
                bpm_devices = [d for d in devices if d.category == DeviceCategory.BPM and d.user == user_slot]

            if not bpm_devices and client.supports_virtual_bpm():
                bpm_devices = [
                    OmronDevice(
                        name="Virtual BPM",
                        macaddr="00:11:22:33:44:55",
                        category=DeviceCategory.BPM,
                        user=user_slot,
                    )
                ]

            if not bpm_devices:
                _ = self.db.log_sync(user_id, "skipped", error_message="No Omron BPM devices registered")
                return None, SyncResult(status="skipped", user_id=user_id, message="no_bpm_devices")
        except Exception as exc:
            logger.exception("Failed to fetch Omron devices for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=f"Omron device fetch failed: {exc}")
            return None, SyncResult(status="error", user_id=user_id, message="omron_device_fetch_failed")

        try:
            all_measurements = []
            for dev in bpm_devices:
                dev_measurements = await asyncio.to_thread(client.get_measurements, dev, since_timestamp_ms)
                all_measurements.extend(dev_measurements)

            bp_measurements = [m for m in all_measurements if isinstance(m, BPMeasurement)]
            return bp_measurements, None
        except Exception as exc:
            logger.exception("Failed to fetch Omron measurements for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=f"Omron measurement fetch failed: {exc}")
            return None, SyncResult(status="error", user_id=user_id, message="omron_measurement_fetch_failed")

    async def _fetch_existing_garmin_bps(
        self, user_id: str, start_date: date, end_date: date
    ) -> tuple[list[dict[str, Any]] | None, SyncResult | None]:
        try:
            garmin = GarminClient.create_for_user(user_id, self.db.db, self.encryptor)
            existing_bps = await garmin.fetch_existing_blood_pressures(start_date, end_date)
            return existing_bps, None
        except GarminSessionExpiredError as exc:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            return None, SyncResult(status="error", user_id=user_id, message="garmin_session_expired")
        except GarminRateLimitError as exc:
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            return None, SyncResult(status="skipped", user_id=user_id, message="garmin_rate_limited")
        except Exception as exc:
            logger.exception("Failed to fetch Garmin blood pressures for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=f"Garmin fetch failed: {exc}")
            return None, SyncResult(status="error", user_id=user_id, message="garmin_fetch_failed")

    def _deduplicate_bp_measurements(
        self, omron_bps: list[BPMeasurement], existing_bps: list[dict[str, Any]]
    ) -> list[BPMeasurement]:
        new_measurements = []
        tolerance = timedelta(minutes=DEDUP_TOLERANCE_MINUTES)

        for omron_m in omron_bps:
            omron_ts = omron_m.measurementDate / 1000
            omron_utc = datetime.fromtimestamp(omron_ts, tz=UTC)

            duplicate = False
            for garmin_m in existing_bps:
                g_ts_str = garmin_m.get("measurementTimestampGMT")
                if not g_ts_str:
                    continue
                try:
                    g_dt = datetime.fromisoformat(g_ts_str.replace("Z", "+00:00"))
                    if g_dt.tzinfo is None:
                        g_dt = g_dt.replace(tzinfo=UTC)
                    if abs(g_dt - omron_utc) <= tolerance:
                        duplicate = True
                        break
                except ValueError:
                    continue

            if not duplicate:
                new_measurements.append(omron_m)
        return new_measurements

    async def _upload_bps_to_garmin(self, user_id: str, bps: list[BPMeasurement]) -> tuple[int, SyncResult | None]:
        uploaded = 0
        try:
            garmin = GarminClient.create_for_user(user_id, self.db.db, self.encryptor)
            for m in bps:
                flags = []
                if m.irregularHB:
                    flags.append(IRREGULAR_PULSE_YES)
                else:
                    flags.append(IRREGULAR_PULSE_NO)

                if m.movementDetect:
                    flags.append(MOVEMENT_DETECT_YES)
                else:
                    flags.append(MOVEMENT_DETECT_NO)

                notes = f"{OMRON_SYNC_PREFIX} {', '.join(flags)}"
                dt_local = datetime.fromtimestamp(m.measurementDate / 1000, tz=m.timeZone)

                await garmin.upload_blood_pressure(
                    systolic=m.systolic,
                    diastolic=m.diastolic,
                    pulse=m.pulse,
                    timestamp=dt_local,
                    notes=notes,
                )
                uploaded += 1
            return uploaded, None
        except GarminSessionExpiredError as exc:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            return uploaded, SyncResult(status="error", user_id=user_id, message="garmin_session_expired")
        except GarminRateLimitError as exc:
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            return uploaded, SyncResult(status="skipped", user_id=user_id, message="garmin_rate_limited")
        except Exception as exc:
            logger.exception("Failed to upload blood pressure for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=f"Garmin upload failed: {exc}")
            return uploaded, SyncResult(status="error", user_id=user_id, message="garmin_upload_failed")

    def _validate_omron_sync_prerequisites(self, user_id: str, omron_tokens: dict | None, session: bool) -> str | None:
        if not omron_tokens:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            _ = self.db.log_sync(user_id, "error", error_message="Missing Omron Connect connection")
            return "missing_omron_tokens"
        if not session:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            _ = self.db.log_sync(user_id, "error", error_message="Missing Garmin connection")
            return "missing_garmin_session"
        return None

    async def sync_omron_user(self, user_id: str) -> SyncResult:
        omron_tokens = self.db.get_omron_tokens(user_id, self.encryptor)
        session = self.db.has_garmin_session(user_id)
        prereq_error = self._validate_omron_sync_prerequisites(user_id, omron_tokens, session)
        if prereq_error:
            return SyncResult(status="error", user_id=user_id, message=prereq_error)

        assert omron_tokens is not None

        client, auth_error = await self._authenticate_omron(user_id, omron_tokens)
        if auth_error:
            return auth_error

        assert client is not None

        now = datetime.now(UTC)
        since_timestamp_ms = int((now - timedelta(days=SYNC_WINDOW_DAYS)).timestamp() * 1000)
        omron_bps, fetch_error = await self._fetch_omron_bp_measurements(
            user_id, client, omron_tokens["user_slot"], since_timestamp_ms
        )
        if fetch_error:
            return fetch_error

        assert omron_bps is not None

        start_date = (now - timedelta(days=SYNC_WINDOW_DAYS)).date()
        end_date = now.date()
        existing_bps, garmin_fetch_error = await self._fetch_existing_garmin_bps(user_id, start_date, end_date)
        if garmin_fetch_error:
            return garmin_fetch_error

        assert existing_bps is not None

        new_bps = self._deduplicate_bp_measurements(omron_bps, existing_bps)
        total_count = len(omron_bps)
        skipped_count = total_count - len(new_bps)

        uploaded_count, upload_error = await self._upload_bps_to_garmin(user_id, new_bps)
        if upload_error:
            upload_error.uploaded = uploaded_count
            upload_error.skipped = skipped_count + (len(new_bps) - uploaded_count)
            upload_error.total = total_count
            return upload_error

        summary_status = "success" if uploaded_count > 0 else "skipped"
        summary_message = f"uploaded={uploaded_count} skipped={skipped_count} total={total_count}"
        _ = self.db.log_sync(user_id, summary_status, error_message=f"Omron sync: {summary_message}")
        return SyncResult(
            status=summary_status,
            user_id=user_id,
            message="sync_completed",
            uploaded=uploaded_count,
            skipped=skipped_count,
            total=total_count,
        )

    async def _sync_single_user_all_sources(self, user_id: str) -> str:
        user_synced = False
        user_error = False

        # Google Health weight sync
        try:
            result = await self.sync_user(user_id)
            if result.status == "success":
                user_synced = True
            elif result.status == "error":
                user_error = True
        except Exception as exc:
            logger.exception("Unexpected Google Health sync error for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            user_error = True

        # Omron Connect blood pressure sync (if enabled)
        try:
            profile = self.db.get_user_profile(user_id)
            if profile and getattr(profile, "omron_sync_enabled", False):
                omron_result = await self.sync_omron_user(user_id)
                if omron_result.status == "success":
                    user_synced = True
                elif omron_result.status == "error":
                    user_error = True
        except Exception as exc:
            logger.exception("Unexpected Omron Connect sync error for user %s", user_id)
            _ = self.db.log_sync(user_id, "error", error_message=str(exc))
            user_error = True

        if user_error:
            return "error"
        if user_synced:
            return "success"
        return "skipped"

    async def sync_all_users(self) -> PollSummary:
        start_time = time.time()
        users = self.db.get_active_users()

        synced = 0
        skipped = 0
        errors = 0

        for idx, user_id in enumerate(users):
            status = await self._sync_single_user_all_sources(user_id)
            if status == "error":
                errors += 1
            elif status == "success":
                synced += 1
            else:
                skipped += 1

            if idx < len(users) - 1:
                await asyncio.sleep(2)

        duration_seconds = time.time() - start_time
        _ = self.db.log_poll_run(synced, skipped, errors, len(users), duration_seconds)
        return PollSummary(
            synced=synced,
            skipped=skipped,
            errors=errors,
            total=len(users),
            duration_seconds=duration_seconds,
        )

    async def _fetch_measurements_with_refresh(
        self,
        user_id: str,
        token: OAuthToken,
        since_timestamp: str,
        until_timestamp: datetime,
    ) -> tuple[list[dict[str, object]], str | None]:
        refreshed_access_token = await self._refresh_google_access_token(token.refresh_token)
        if not refreshed_access_token:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return [], "google_refresh_failed"

        expires_at = datetime.now(UTC) + timedelta(hours=1)
        _ = self.db.save_oauth_token(
            user_id,
            "google",
            refreshed_access_token,
            token.refresh_token,
            expires_at,
        )

        try:
            measurements = await self.google.fetch_all_measurements(
                refreshed_access_token,
                since_timestamp,
                until_timestamp.isoformat(),
            )
        except (GoogleTokenExpiredError, GoogleScopeRevokedError):
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return [], "google_auth_error"

        return cast(list[dict[str, object]], measurements), None

    async def _refresh_google_access_token(self, refresh_token: str | None) -> str | None:
        if not refresh_token:
            logger.warning("Cannot refresh Google token without refresh token")
            return None

        config = get_config()
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.google_client_id,
            "client_secret": config.google_client_secret,
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post("https://oauth2.googleapis.com/token", data=payload)
                _ = response.raise_for_status()
        except Exception:
            logger.warning("Google token refresh failed", exc_info=True)
            return None

        payload_data = cast(dict[str, object], response.json())
        return cast(str | None, payload_data.get("access_token"))

    @staticmethod
    def _has_matching_weight(
        garmin_weights: list[dict[str, object]],
        measurement_timestamp: datetime,
        tolerance: timedelta,
    ) -> bool:
        measurement_utc = SyncOrchestrator._normalize_utc(measurement_timestamp)

        for garmin_weight in garmin_weights:
            garmin_ts_raw = garmin_weight.get("timestamp_utc")
            if not isinstance(garmin_ts_raw, datetime):
                continue

            garmin_utc = SyncOrchestrator._normalize_utc(garmin_ts_raw)
            if abs(garmin_utc - measurement_utc) <= tolerance:
                return True

        return False

    @staticmethod
    def _normalize_utc(ts: datetime) -> datetime:
        if ts.tzinfo is None:
            return ts.replace(tzinfo=UTC)
        return ts.astimezone(UTC)

    def _validate_sync_prerequisites(
        self,
        user_id: str,
        token: OAuthToken | None,
        session: object | None,
    ) -> str | None:
        if not token:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return "missing_google_token"
        if not session:
            _ = self.db.update_user_status(user_id, "needs_reauth")
            return "missing_garmin_session"
        return None
