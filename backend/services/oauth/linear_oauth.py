"""
Linear OAuth2 — authorization code flow with refresh tokens.
Access tokens expire in 24 hours. Refresh tokens are used to obtain new ones.
API is GraphQL at https://api.linear.app/graphql.
"""

from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

from config import LINEAR_CLIENT_ID, LINEAR_CLIENT_SECRET, LINEAR_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

LINEAR_AUTH_URL = "https://linear.app/oauth/authorize"
LINEAR_TOKEN_URL = "https://api.linear.app/oauth/token"
LINEAR_SCOPES = ["read", "write"]


class LinearOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "linear"

    def get_auth_url(self, state: str) -> str:
        params = {
            "client_id": LINEAR_CLIENT_ID,
            "redirect_uri": LINEAR_REDIRECT_URI,
            "response_type": "code",
            "scope": ",".join(LINEAR_SCOPES),
            "state": state,
            "prompt": "consent",
        }
        return f"{LINEAR_AUTH_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                LINEAR_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": LINEAR_CLIENT_ID,
                    "client_secret": LINEAR_CLIENT_SECRET,
                    "code": code,
                    "grant_type": "authorization_code",
                    "redirect_uri": LINEAR_REDIRECT_URI,
                },
            )

        if response.status_code != 200:
            logger.error("Linear token exchange failed", status=response.status_code, request_id=response.headers.get("x-request-id"))
            raise Exception(f"Linear token exchange failed: {response.status_code}")

        data = response.json()
        expires_in = data.get("expires_in", 86400)

        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=data.get("scope", "").split(",") if data.get("scope") else LINEAR_SCOPES,
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Linear access token."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                LINEAR_TOKEN_URL,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                data={
                    "client_id": LINEAR_CLIENT_ID,
                    "client_secret": LINEAR_CLIENT_SECRET,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
            )

        if response.status_code != 200:
            logger.error("Linear token refresh failed", status=response.status_code, request_id=response.headers.get("x-request-id"))
            raise Exception(f"Linear token refresh failed: {response.status_code}")

        data = response.json()
        expires_in = data.get("expires_in", 86400)

        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token"),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=data.get("scope", "").split(",") if data.get("scope") else LINEAR_SCOPES,
        )
