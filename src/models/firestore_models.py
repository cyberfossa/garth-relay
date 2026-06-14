"""Pydantic models for Firestore user documents."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class UserProfile(BaseModel):
    """User profile stored in Firestore."""

    email: str = Field(..., description="User email address")
    name: str = Field(..., description="User display name")
    status: str = Field(default="active", description="Account status (active/inactive/suspended)")
    google_health_user_id: str | None = Field(default=None, description="Google Health user ID")
    sync_enabled: bool = Field(default=True, description="Whether automatic sync is enabled for the user")
    omron_sync_enabled: bool = Field(default=False, description="Whether automatic Omron sync is enabled for the user")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active: datetime = Field(default_factory=lambda: datetime.now(UTC))
