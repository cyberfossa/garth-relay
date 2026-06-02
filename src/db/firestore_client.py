"""Firestore database client with subcollection schema for multi-user isolation."""

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import structlog
from google.cloud import firestore

from src.models.firestore_models import UserProfile
from src.models.oauth_models import OAuthToken

logger = structlog.get_logger()


class FirestoreClient:
    def __init__(self, project_id: str):
        self.db = firestore.Client(project=project_id)
        self.project_id = project_id

    def _user_ref(self, user_id: str):
        return self.db.collection("users").document(user_id)

    def save_user_profile(self, user_id: str, email: str, name: str) -> bool:
        try:
            profile_data = {
                "email": email,
                "name": name,
                "status": "active",
                "created_at": datetime.now(UTC),
                "last_active": datetime.now(UTC),
            }
            self._user_ref(user_id).set(profile_data, merge=True)
            logger.info("Saved user profile", user_id=user_id)
            return True
        except Exception:
            logger.exception("Failed to save user profile", user_id=user_id)
            return False

    def get_user_profile(self, user_id: str) -> UserProfile | None:
        try:
            doc = cast(firestore.DocumentSnapshot, self._user_ref(user_id).get())
            if not doc.exists:
                logger.warning("User profile not found", user_id=user_id)
                return None
            return UserProfile(**cast(dict[str, Any], doc.to_dict() or {}))
        except Exception:
            logger.exception("Failed to retrieve user profile", user_id=user_id)
            return None

    def update_user_status(self, user_id: str, status: str) -> bool:
        try:
            self._user_ref(user_id).update({"status": status, "last_active": datetime.now(UTC)})
            logger.info("Updated user status", user_id=user_id, status=status)
            return True
        except Exception:
            logger.exception("Failed to update user status", user_id=user_id)
            return False

    def get_active_users(self) -> list[str]:
        try:
            docs = self.db.collection("users").where("status", "==", "active").stream()
            return [doc.id for doc in docs]
        except Exception:
            logger.exception("Failed to retrieve active users")
            return []

    def log_sync(
        self,
        user_id: str,
        status: str,
        weight_kg: float | None = None,
        body_fat_pct: float | None = None,
        error_message: str | None = None,
    ) -> bool:
        try:
            timestamp = datetime.now(UTC)
            log_data = {
                "status": status,
                "weight_kg": weight_kg,
                "body_fat_pct": body_fat_pct,
                "error_message": error_message,
                "timestamp": timestamp,
                "expire_at": timestamp + timedelta(days=90),
            }
            self._user_ref(user_id).collection("sync_logs").add(log_data)
            logger.info("Logged sync", user_id=user_id, status=status)
            return True
        except Exception:
            logger.exception("Failed to log sync", user_id=user_id)
            return False

    def get_recent_syncs(self, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
        try:
            docs = (
                self._user_ref(user_id)
                .collection("sync_logs")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            return [cast(dict[str, Any], doc.to_dict() or {}) for doc in docs]
        except Exception:
            logger.exception("Failed to retrieve sync logs", user_id=user_id)
            return []

    def get_last_sync_timestamp(self, user_id: str) -> datetime | None:
        try:
            docs = (
                self._user_ref(user_id)
                .collection("sync_logs")
                .where("status", "==", "success")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(1)
                .stream()
            )
            for doc in docs:
                return doc.to_dict().get("timestamp")
            return None
        except Exception:
            logger.exception("Failed to get last sync timestamp", user_id=user_id)
            return None

    def log_poll_run(
        self, synced_count: int, skipped_count: int, error_count: int, total_users: int, duration_seconds: float
    ) -> bool:
        try:
            timestamp = datetime.now(UTC)
            poll_data = {
                "synced_count": synced_count,
                "skipped_count": skipped_count,
                "error_count": error_count,
                "total_users": total_users,
                "duration_seconds": duration_seconds,
                "timestamp": timestamp,
                "expire_at": timestamp + timedelta(days=30),
            }
            self.db.collection("poll_logs").add(poll_data)
            logger.info("Logged poll run", synced=synced_count, skipped=skipped_count, errors=error_count)
            return True
        except Exception:
            logger.exception("Failed to log poll run")
            return False

    def save_mfa_state(self, user_id: str, encrypted_state: str, expires_at: datetime | None = None) -> bool:
        try:
            data: dict[str, Any] = {
                "encrypted_state": encrypted_state,
                "created_at": datetime.now(UTC),
            }
            if expires_at:
                data["expires_at"] = expires_at
            self._user_ref(user_id).collection("mfa_state").document("current").set(data)
            logger.info("Saved MFA state", user_id=user_id)
            return True
        except Exception:
            logger.exception("Failed to save MFA state", user_id=user_id)
            return False

    def get_mfa_state(self, user_id: str) -> dict[str, Any] | None:
        try:
            doc = cast(
                firestore.DocumentSnapshot, self._user_ref(user_id).collection("mfa_state").document("current").get()
            )
            if not doc.exists:
                return None
            return cast(dict[str, Any], doc.to_dict() or {})
        except Exception:
            logger.exception("Failed to get MFA state", user_id=user_id)
            return None

    def delete_mfa_state(self, user_id: str) -> bool:
        try:
            self._user_ref(user_id).collection("mfa_state").document("current").delete()
            logger.info("Deleted MFA state", user_id=user_id)
            return True
        except Exception:
            logger.exception("Failed to delete MFA state", user_id=user_id)
            return False

    def delete_garmin_session(self, user_id: str) -> bool:
        try:
            self._user_ref(user_id).collection("oauth_tokens").document("garmin").delete()
            logger.info("Deleted Garmin session", user_id=user_id)
            return True
        except Exception:
            logger.exception("Failed to delete Garmin session", user_id=user_id)
            return False

    def has_garmin_session(self, user_id: str) -> bool:
        try:
            doc = cast(
                firestore.DocumentSnapshot, self._user_ref(user_id).collection("oauth_tokens").document("garmin").get()
            )
            return doc.exists
        except Exception:
            logger.exception("Failed to check Garmin session", user_id=user_id)
            return False

    def get_oauth_token(self, user_id: str, provider: str) -> OAuthToken | None:
        try:
            doc = cast(
                firestore.DocumentSnapshot,
                self._user_ref(user_id).collection("oauth_tokens").document(provider).get(),
            )
            if not doc.exists:
                return None
            data = cast(dict[str, Any], doc.to_dict())
            return OAuthToken(
                user_id=user_id,
                provider=provider,
                access_token=data.get("access_token", ""),
                refresh_token=data.get("refresh_token"),
                expires_at=data.get("expires_at", datetime.now(UTC)),
                created_at=data.get("created_at", datetime.now(UTC)),
                updated_at=data.get("updated_at", datetime.now(UTC)),
            )
        except Exception:
            logger.exception("Failed to get OAuth token", user_id=user_id, provider=provider)
            return None

    def save_oauth_token(
        self,
        user_id: str,
        provider: str,
        access_token: str,
        refresh_token: str | None,
        expires_at: datetime,
    ) -> bool:
        try:
            self._user_ref(user_id).collection("oauth_tokens").document(provider).set(
                {
                    "access_token": access_token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at,
                    "updated_at": datetime.now(UTC),
                },
                merge=True,
            )
            return True
        except Exception:
            logger.exception("Failed to save OAuth token", user_id=user_id, provider=provider)
            return False

    def delete_oauth_token(self, user_id: str, provider: str) -> bool:
        try:
            self._user_ref(user_id).collection("oauth_tokens").document(provider).delete()
            logger.info("Deleted OAuth token", user_id=user_id, provider=provider)
            return True
        except Exception:
            logger.exception("Failed to delete OAuth token", user_id=user_id, provider=provider)
            return False

    def get_recent_poll_logs(self, limit: int = 10) -> list[dict[str, Any]]:
        try:
            docs = (
                self.db.collection("poll_logs")
                .order_by("timestamp", direction=firestore.Query.DESCENDING)
                .limit(limit)
                .stream()
            )
            return [cast(dict[str, Any], doc.to_dict() or {}) for doc in docs]
        except Exception:
            logger.exception("Failed to retrieve recent poll logs")
            return []
