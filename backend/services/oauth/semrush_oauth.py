"""
Semrush OAuth -- via Semrush MCP (same pattern as PostHog/Figma MCP).

Uses Dynamic Client Registration (RFC 7591) with Semrush's OAuth server.
Public client (no client_secret), secured with PKCE (S256).

Flow:
1. Register client dynamically at api.semrush.com (one-time, cached)
2. Redirect user to api.semrush.com authorize endpoint with PKCE
3. User grants access on Semrush's consent screen
4. Semrush redirects to our callback with auth code
5. We exchange code + code_verifier for access_token
"""

import asyncio
import hashlib
import base64
import secrets
from datetime import datetime, timezone, timedelta
from urllib.parse import urlencode

import httpx
import structlog

from config import SEMRUSH_REDIRECT_URI
from db.redis import get_redis
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

SEMRUSH_AUTHORIZE_URL = "https://api.semrush.com/apis/v4/auth/v0/oauth2/auth"
SEMRUSH_TOKEN_URL = "https://api.semrush.com/apis/v4-raw/auth/v1/oauth2/access_token"
SEMRUSH_REGISTER_URL = "https://api.semrush.com/apis/v4-raw/auth/v1/oauth2/register"

SEMRUSH_SCOPES = "mcp.access offline_access"

_registered_client_id: str | None = None
_registration_lock = asyncio.Lock()


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def _get_or_register_client() -> str:
    """Dynamically register a client with Semrush's OAuth server (RFC 7591)."""
    global _registered_client_id
    async with _registration_lock:
        if _registered_client_id:
            return _registered_client_id

        r = get_redis()
        cached = await r.get("semrush_mcp_client_id")
        if cached:
            _registered_client_id = cached
            return cached

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                SEMRUSH_REGISTER_URL,
                json={
                    "client_name": "Gideon AI Assistant",
                    "redirect_uris": [SEMRUSH_REDIRECT_URI],
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                    "token_endpoint_auth_method": "none",
                    "scope": SEMRUSH_SCOPES,
                },
            )

        if response.status_code not in (200, 201):
            logger.error("Semrush client registration failed", status=response.status_code, body=response.text[:500])
            raise Exception(f"Semrush client registration failed: {response.status_code}")

        data = response.json()
        client_id = data.get("client_id")
        if not client_id:
            raise Exception("Semrush client registration: no client_id in response")

        _registered_client_id = client_id
        await r.set("semrush_mcp_client_id", client_id)
        logger.info("Semrush MCP client registered", client_id=client_id)
        return client_id


async def store_pkce_verifier(state: str, code_verifier: str):
    r = get_redis()
    await r.set(f"oauth_pkce_semrush:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    r = get_redis()
    key = f"oauth_pkce_semrush:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


class SemrushOAuthProvider(OAuthProvider):
    def __init__(self):
        self._pending_verifier: tuple[str, str] | None = None
        self._pending_state: str | None = None
        self._pending_code_challenge: str | None = None
        self._resolved_auth_url: str | None = None

    @property
    def provider_name(self) -> str:
        return "semrush"

    def get_auth_url(self, state: str) -> str:
        code_verifier, code_challenge = _generate_pkce()
        self._pending_verifier = (state, code_verifier)
        self._pending_state = state
        self._pending_code_challenge = code_challenge
        return ""  # Real URL built in store_pending_pkce (async client registration)

    async def store_pending_pkce(self):
        if self._pending_verifier is not None:
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            self._pending_verifier = None

        if self._pending_state is not None:
            client_id = await _get_or_register_client()
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": SEMRUSH_REDIRECT_URI,
                "code_challenge": self._pending_code_challenge,
                "code_challenge_method": "S256",
                "state": self._pending_state,
                "scope": SEMRUSH_SCOPES,
            }
            self._resolved_auth_url = f"{SEMRUSH_AUTHORIZE_URL}?{urlencode(params)}"
            self._pending_state = None
            self._pending_code_challenge = None

    def get_resolved_auth_url(self) -> str:
        return self._resolved_auth_url or ""

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        code_verifier = await get_pkce_verifier(state) if state else None
        if not code_verifier:
            logger.error("No PKCE code_verifier found for Semrush callback", state=state)
            raise Exception("Semrush OAuth failed: missing PKCE verifier. Please try connecting again.")

        client_id = await _get_or_register_client()

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": SEMRUSH_REDIRECT_URI,
            "code_verifier": code_verifier,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                SEMRUSH_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Semrush token exchange failed", status=response.status_code, body=response.text[:500])
            raise Exception(f"Semrush token exchange failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("Semrush OAuth: no access_token in response")

        logger.info("Semrush OAuth success", has_refresh=bool(refresh_token))

        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "semrush_mcp", "client_id": client_id},
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        client_id = await _get_or_register_client()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                SEMRUSH_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Semrush token refresh failed", status=response.status_code, body=response.text[:500])
            raise Exception(f"Semrush token refresh failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        new_refresh = data.get("refresh_token")

        if not access_token:
            raise Exception("Semrush refresh: no access_token in response")

        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=new_refresh or refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
        )
