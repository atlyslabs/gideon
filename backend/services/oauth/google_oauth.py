"""
Google OAuth2 — single consent screen for Gmail, Calendar, Drive, Docs, Sheets, Slides.
Supports refresh token rotation.
"""

from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import httpx
import structlog

from config import GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

# All scopes requested in a single OAuth consent
GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.readonly",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/presentations.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/directory.readonly",
]

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "google"

    def get_auth_url(self, state: str) -> str:
        params = {
            "client_id": GOOGLE_CLIENT_ID,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "response_type": "code",
            "scope": " ".join(GOOGLE_SCOPES),
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        }
        return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": GOOGLE_REDIRECT_URI,
                },
            )

        if response.status_code != 200:
            logger.error("Google token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"Google token exchange failed: {response.status_code}")

        data = response.json()
        expires_in = data.get("expires_in", 3600)

        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=data.get("scope", "").split(" "),
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": GOOGLE_CLIENT_ID,
                    "client_secret": GOOGLE_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )

        if response.status_code != 200:
            logger.error("Google token refresh failed", status=response.status_code, body=response.text)
            raise Exception(f"Google token refresh failed: {response.status_code}")

        data = response.json()
        expires_in = data.get("expires_in", 3600)

        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=data.get("scope", "").split(" "),
        )
