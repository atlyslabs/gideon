"""
Ramp OAuth — standard OAuth 2.0 authorization code flow.

Confidential client (client_id + client_secret).
Access tokens expire in 10 days. Refresh tokens available.

Flow:
1. Redirect user to api.ramp.com/v1/authorize
2. User (Admin/Business Owner) grants access
3. Ramp redirects to our callback with auth code
4. We exchange code for access_token + refresh_token at api.ramp.com/developer/v1/token
"""

import httpx
import structlog

from config import RAMP_CLIENT_ID, RAMP_CLIENT_SECRET, RAMP_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

RAMP_AUTHORIZE_URL = "https://app.ramp.com/v1/authorize"
RAMP_TOKEN_URL = "https://api.ramp.com/developer/v1/token"


class RampOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "ramp"

    def get_auth_url(self, state: str) -> str:
        from urllib.parse import urlencode
        params = {
            "response_type": "code",
            "client_id": RAMP_CLIENT_ID,
            "redirect_uri": RAMP_REDIRECT_URI,
            "scope": "transactions:read bills:read reimbursements:read users:read business:read cards:read departments:read",
            "state": state,
        }
        return f"{RAMP_AUTHORIZE_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str, **kwargs) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                RAMP_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": RAMP_REDIRECT_URI,
                },
                auth=(RAMP_CLIENT_ID, RAMP_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Ramp token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"Ramp token exchange failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("Ramp OAuth: no access_token in response")

        logger.info("Ramp OAuth success", has_refresh=bool(refresh_token))

        from datetime import datetime, timezone, timedelta
        # Ramp tokens expire in 10 days (864000 seconds)
        expires_in = data.get("expires_in", 864000)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=data.get("scope", "").split() if data.get("scope") else None,
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Ramp access token."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                RAMP_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                auth=(RAMP_CLIENT_ID, RAMP_CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Ramp token refresh failed", status=response.status_code, body=response.text)
            raise Exception(f"Ramp token refresh failed: {response.status_code}")

        data = response.json()
        from datetime import datetime, timezone, timedelta
        expires_in = data.get("expires_in", 864000)
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )
