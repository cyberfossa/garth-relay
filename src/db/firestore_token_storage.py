"""Firestore-backed TokenStorage for garth-ng OAuth2 sessions."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import cast

from cryptography.exceptions import InvalidTag
from garth.auth_tokens import OAuth2Token
from garth.utils import asdict
from google.cloud import firestore

from src.crypto import TokenEncryptor

logger = logging.getLogger(__name__)

ENCRYPTED_FIELDS: frozenset[str] = frozenset({"access_token", "refresh_token", "mfa_token"})


class FirestoreTokenStorage:
    """Implements garth's TokenStorage protocol using Firestore + encryption."""

    def __init__(self, user_id: str, db: firestore.Client, encryptor: TokenEncryptor) -> None:
        self._user_id: str = user_id
        self._db: firestore.Client = db
        self._encryptor: TokenEncryptor = encryptor

    def _doc_ref(self) -> firestore.DocumentReference:
        return self._db.collection("users").document(self._user_id).collection("oauth_tokens").document("garmin")

    def save(self, token: OAuth2Token) -> None:
        """Serialize, encrypt, and persist token to Firestore."""
        try:
            token_fields = cast(dict[str, object], asdict(token))
            token_data: dict[str, object] = {}
            for key, value in token_fields.items():
                if key in ENCRYPTED_FIELDS:
                    token_data[key] = self._encryptor.encrypt(json.dumps(value), aad=self._user_id)
                else:
                    token_data[key] = json.dumps(value)
            token_data["updated_at"] = datetime.now(tz=UTC)

            _ = self._doc_ref().set(token_data, merge=True)
            logger.info("Saved Garmin token for user %s", self._user_id)
        except Exception:
            logger.error("Failed to save Garmin token for user %s", self._user_id)
            raise

    def load(self) -> OAuth2Token | None:
        """Load and decrypt token from Firestore. Returns None on missing/invalid data."""
        try:
            doc_ref: firestore.DocumentReference = self._doc_ref()
            doc = doc_ref.get()

            if not doc.exists:
                return None

            data = cast(dict[str, object] | None, doc.to_dict())
            if not data:
                return None

            access_token = cast(
                str,
                json.loads(
                    self._encryptor.decrypt(cast(str, data["access_token"]), aad=self._user_id)
                    if "access_token" in ENCRYPTED_FIELDS
                    else cast(str, data["access_token"])
                ),
            )
            refresh_token = cast(
                str,
                json.loads(
                    self._encryptor.decrypt(cast(str, data["refresh_token"]), aad=self._user_id)
                    if "refresh_token" in ENCRYPTED_FIELDS
                    else cast(str, data["refresh_token"])
                ),
            )
            expires_in = cast(int, json.loads(cast(str, data["expires_in"])))
            token_type = cast(str, json.loads(cast(str, data["token_type"])))
            expires_at = cast(float | None, json.loads(cast(str, data["expires_at"])))
            refresh_token_expires_in = cast(int | None, json.loads(cast(str, data["refresh_token_expires_in"])))
            refresh_token_expires_at = cast(float | None, json.loads(cast(str, data["refresh_token_expires_at"])))
            scope = cast(str | None, json.loads(cast(str, data["scope"])))
            jti = cast(str | None, json.loads(cast(str, data["jti"])))
            mfa_token = cast(
                str | None,
                json.loads(
                    self._encryptor.decrypt(cast(str, data["mfa_token"]), aad=self._user_id)
                    if "mfa_token" in ENCRYPTED_FIELDS
                    else cast(str, data["mfa_token"])
                ),
            )
            mfa_expiration_timestamp = cast(
                str | None,
                json.loads(cast(str, data["mfa_expiration_timestamp"])),
            )
            mfa_expiration_timestamp_millis = cast(
                int | None, json.loads(cast(str, data["mfa_expiration_timestamp_millis"]))
            )
            client_id = cast(str | None, json.loads(cast(str, data["client_id"])))

            return OAuth2Token(
                access_token=access_token,
                refresh_token=refresh_token,
                expires_in=expires_in,
                token_type=token_type,
                expires_at=expires_at,
                refresh_token_expires_in=refresh_token_expires_in,
                refresh_token_expires_at=refresh_token_expires_at,
                scope=scope,
                jti=jti,
                mfa_token=mfa_token,
                mfa_expiration_timestamp=mfa_expiration_timestamp,
                mfa_expiration_timestamp_millis=mfa_expiration_timestamp_millis,
                client_id=client_id,
            )
        except (KeyError, json.JSONDecodeError, ValueError, TypeError, InvalidTag):
            logger.warning(
                "Failed to load Garmin token for user %s (missing or legacy format)",
                self._user_id,
            )
            return None

    def delete(self) -> None:
        """Remove stored token from Firestore."""
        _ = self._doc_ref().delete()
        logger.info("Deleted Garmin token for user %s", self._user_id)
