"""Firestore-backed OAuth state store for multi-instance support."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from google.cloud import firestore

from src.db.firestore_client import FirestoreClient

logger = structlog.get_logger()

COLLECTION = "oauth_states"
TTL_MINUTES = 10


class OAuthStateStore:
    """Stores OAuth state parameters in Firestore with TTL.

    Falls back to in-memory storage when no db_client is provided (local dev/tests).
    """

    def __init__(self, db_client: FirestoreClient | None) -> None:
        self._db: firestore.Client | None = db_client.db if db_client else None
        self._memory: dict[str, str] = {}

    async def store_state(self, state: str, purpose: str) -> None:
        """Store an OAuth state with TTL.

        Args:
            state: The OAuth state token.
            purpose: Purpose identifier (e.g. 'app_login', 'google_connect').
        """
        if self._db is None:
            self._memory[state] = purpose
            return
        now = datetime.now(UTC)
        self._db.collection(COLLECTION).document(state).set(
            {
                "purpose": purpose,
                "created_at": now,
                "expire_at": now + timedelta(minutes=TTL_MINUTES),
            }
        )

    async def pop_state(self, state: str) -> str | None:
        """Read and delete an OAuth state, returning purpose or None.

        Args:
            state: The OAuth state token to look up.

        Returns:
            The purpose string if found, None otherwise.
        """
        if self._db is None:
            return self._memory.pop(state, None)
        doc_ref = self._db.collection(COLLECTION).document(state)
        doc = doc_ref.get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        doc_ref.delete()
        if data is None:
            return None
        return data.get("purpose")
