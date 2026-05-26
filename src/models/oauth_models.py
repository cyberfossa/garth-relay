"""OAuth-related models for garth-relay."""

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class OAuthProvider(StrEnum):
    """OAuth2 provider types."""

    GOOGLE = "google"
    GARMIN = "garmin"


class OAuthToken(BaseModel):
    """OAuth2 token stored in Firestore."""

    user_id: str = Field(..., description="Unique user ID")
    provider: OAuthProvider = Field(..., description="OAuth provider")
    access_token: str = Field(..., description="Access token (encrypted at rest)")
    refresh_token: str | None = Field(None, description="Refresh token (encrypted at rest)")
    expires_at: datetime = Field(..., description="Token expiry timestamp")
    scope: str | None = Field(None, description="OAuth scope")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Config:
        use_enum_values = True
