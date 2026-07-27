"""
Figma OAuth -- confidential client with PKCE (S256).

Uses a pre-registered Figma app (client_id + client_secret).
Auth: Basic auth header for token exchange.
Tokens expire in 90 days.

Flow:
1. Redirect user to figma.com/oauth with PKCE
2. User grants access on Figma's consent screen
3. Figma redirects to our callback with auth code (expires in 30s)
4. We exchange code + code_verifier for access_token at api.figma.com/v1/oauth/token
"""

import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

from config import FIGMA_CLIENT_ID, FIGMA_CLIENT_SECRET, FIGMA_REDIRECT_URI
from db.redis import get_redis
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

FIGMA_AUTHORIZE_URL = "https://www.figma.com/oauth"
FIGMA_TOKEN_URL = "https://api.figma.com/v1/oauth/token"
FIGMA_REFRESH_URL = "https://api.figma.com/v1/oauth/refresh"

FIGMA_SCOPES = ",".join([
    "current_user:read",
    "file_content:read",
    "file_metadata:read",
    "file_comments:read",
    "file_versions:read",
    "file_dev_resources:read",
    "library_assets:read",
    "library_content:read",
    "team_library_content:read",
    "projects:read",
])


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


def _basic_auth_header() -> str:
    credentials = f"{FIGMA_CLIENT_ID}:{FIGMA_CLIENT_SECRET}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return f"Basic {encoded}"


async def store_pkce_verifier(state: str, code_verifier: str):
    r = get_redis()
    await r.set(f"oauth_pkce_figma:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    r = get_redis()
    key = f"oauth_pkce_figma:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


class FigmaOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "figma"

    def get_auth_url(self, state: str) -> str:
        code_verifier, code_challenge = _generate_pkce()
        self._pending_verifier = (state, code_verifier)
        params = {
            "client_id": FIGMA_CLIENT_ID,
            "redirect_uri": FIGMA_REDIRECT_URI,
            "scope": FIGMA_SCOPES,
            "state": state,
            "response_type": "code",
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{FIGMA_AUTHORIZE_URL}?{urlencode(params)}"

    async def store_pending_pkce(self):
        if hasattr(self, "_pending_verifier"):
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            del self._pending_verifier

    def get_resolved_auth_url(self) -> str:
        return ""

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        code_verifier = await get_pkce_verifier(state) if state else None
        if not code_verifier:
            logger.warning("No PKCE code_verifier found for Figma callback", state=state)

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": FIGMA_REDIRECT_URI,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                FIGMA_TOKEN_URL,
                data=payload,
                headers={
                    "Authorization": _basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        if response.status_code != 200:
            logger.error("Figma token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"Figma token exchange failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("Figma OAuth: no access_token in response")

        logger.info("Figma OAuth success", has_refresh=bool(refresh_token))

        expires_in = data.get("expires_in", 7776000)  # 90 days default
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "figma_rest_api"},
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                FIGMA_REFRESH_URL,
                data={"refresh_token": refresh_token},
                headers={
                    "Authorization": _basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )

        if response.status_code != 200:
            logger.error("Figma token refresh failed", status=response.status_code, body=response.text)
            raise Exception(f"Figma token refresh failed: {response.status_code}")

        data = response.json()
        expires_in = data.get("expires_in", 7776000)
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "figma_rest_api"},
        )
