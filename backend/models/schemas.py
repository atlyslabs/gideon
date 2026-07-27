"""Pydantic request/response schemas for Gideon API."""

from datetime import datetime
from enum import Enum
from typing import Optional
from uuid import UUID

from pydantic import BaseModel, Field


# --- User schemas ---

class UserProfile(BaseModel):
    """Response: user profile combining identity-provider data + gideon-specific fields."""
    id: UUID
    auth_provider_id: str
    email: str
    name: Optional[str] = None
    avatar_url: Optional[str] = None
    persona_text: Optional[str] = None
    timezone: str = "UTC"
    preferred_communication: str = "gmail"
    onboarding_completed: bool = False
    roles: list[str] = []
    is_survey_admin: bool = False
    created_at: datetime
    updated_at: datetime


class CommunicationChannel(str, Enum):
    gmail = "gmail"
    slack = "slack"


class UserUpdate(BaseModel):
    """Request: update gideon-specific user fields."""
    name: Optional[str] = None
    persona_text: Optional[str] = Field(None, max_length=5000)
    timezone: Optional[str] = None
    preferred_communication: Optional[CommunicationChannel] = None


# --- Settings schemas ---


class UserSettingsResponse(BaseModel):
    """Response: user settings."""
    name: Optional[str] = None
    email: str
    timezone: str = "UTC"
    city: Optional[str] = None
    role: Optional[str] = None
    department: Optional[str] = None
    linkedin_url: Optional[str] = None
    preferred_communication: str = "gmail"
    background_info: Optional[str] = None
    background_generated: bool = False
    feature_action_items: bool = True
    feature_email_drafts: bool = True
    feature_meeting_prep: bool = True
    feature_dev_console: bool = False
    onboarding_stage: Optional[str] = None


class UserSettingsUpdate(BaseModel):
    """Request: update user settings. Email is not updatable."""
    name: Optional[str] = None
    timezone: Optional[str] = None
    city: Optional[str] = None
    role: Optional[str] = None
    department: Optional[str] = None
    linkedin_url: Optional[str] = None
    background_info: Optional[str] = None
    preferred_communication: Optional[CommunicationChannel] = None
    feature_action_items: Optional[bool] = None
    feature_email_drafts: Optional[bool] = None
    feature_meeting_prep: Optional[bool] = None
    feature_dev_console: Optional[bool] = None


# --- Integration schemas ---

class IntegrationResponse(BaseModel):
    """Response: a connected integration."""
    id: UUID
    provider: str
    is_active: bool
    error_count: int = 0
    last_error_at: Optional[datetime] = None
    last_error: Optional[str] = None
    scopes: Optional[list[str]] = None
    extra_data: Optional[dict] = None
    connected_at: datetime


class IntegrationAuthURL(BaseModel):
    """Response: OAuth authorization URL."""
    auth_url: str


class OAuthCallbackRequest(BaseModel):
    """Request: OAuth callback with authorization code."""
    code: str
    state: Optional[str] = None
