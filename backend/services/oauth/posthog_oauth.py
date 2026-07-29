"""
PostHog OAuth -- via PostHog MCP (same pattern as Granola MCP).

Uses Dynamic Client Registration (RFC 7591) with PostHog's OAuth server.
Public client (no client_secret), secured with PKCE (S256).

Flow:
1. Register client dynamically at oauth.posthog.com/oauth/register/ (one-time, cached)
2. Redirect user to oauth.posthog.com/oauth/authorize/ with PKCE
3. User grants access on PostHog's consent screen
4. PostHog redirects to our callback with auth code
5. We exchange code + code_verifier for access_token at oauth.posthog.com/oauth/token/
"""

import hashlib
import base64
import secrets

import httpx
import structlog

from config import POSTHOG_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

POSTHOG_AUTHORIZE_URL = "https://oauth.posthog.com/oauth/authorize/"
POSTHOG_TOKEN_URL = "https://oauth.posthog.com/oauth/token/"
POSTHOG_REGISTER_URL = "https://oauth.posthog.com/oauth/register/"

POSTHOG_SCOPES = (
    "user:read query:read insight:read insight:write feature_flag:read feature_flag:write "
    "experiment:read error_tracking:read dashboard:read project:read "
    "organization:read event_definition:read survey:read"
)

# Cached after first dynamic registration
_registered_client_id: str | None = None


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def _get_or_register_client() -> str:
    """Dynamically register a client with PostHog's OAuth server (RFC 7591)."""
    global _registered_client_id
    if _registered_client_id:
        return _registered_client_id

    from db.redis import get_redis
    r = get_redis()
    cached = await r.get("posthog_mcp_client_id")
    if cached:
        _registered_client_id = cached
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            POSTHOG_REGISTER_URL,
            json={
                "client_name": "Gideon AI Assistant",
                "redirect_uris": [POSTHOG_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": POSTHOG_SCOPES,
            },
        )

    if response.status_code not in (200, 201):
        logger.error("PostHog client registration failed", status=response.status_code, body=response.text)
        raise Exception(f"PostHog client registration failed: {response.status_code}")

    data = response.json()
    client_id = data.get("client_id")
    if not client_id:
        raise Exception("PostHog client registration: no client_id in response")

    _registered_client_id = client_id
    await r.set("posthog_mcp_client_id", client_id)
    logger.info("PostHog MCP client registered", client_id=client_id)
    return client_id


async def store_pkce_verifier(state: str, code_verifier: str):
    from db.redis import get_redis
    r = get_redis()
    await r.set(f"oauth_pkce_posthog:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    from db.redis import get_redis
    r = get_redis()
    key = f"oauth_pkce_posthog:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


class PostHogOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "posthog"

    def get_auth_url(self, state: str) -> str:
        code_verifier, code_challenge = _generate_pkce()
        self._pending_verifier = (state, code_verifier)
        self._pending_state = state
        self._pending_code_challenge = code_challenge
        return ""  # Real URL built in store_pending_pkce (async client registration)

    async def store_pending_pkce(self):
        if hasattr(self, "_pending_verifier"):
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            del self._pending_verifier

        if hasattr(self, "_pending_state"):
            client_id = await _get_or_register_client()
            from urllib.parse import urlencode
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": POSTHOG_REDIRECT_URI,
                "code_challenge": self._pending_code_challenge,
                "code_challenge_method": "S256",
                "state": self._pending_state,
                "scope": POSTHOG_SCOPES,
            }
            self._resolved_auth_url = f"{POSTHOG_AUTHORIZE_URL}?{urlencode(params)}"
            del self._pending_state
            del self._pending_code_challenge

    def get_resolved_auth_url(self) -> str:
        return getattr(self, "_resolved_auth_url", "")

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        code_verifier = await get_pkce_verifier(state) if state else None
        if not code_verifier:
            logger.warning("No PKCE code_verifier found for PostHog callback", state=state)

        client_id = await _get_or_register_client()

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": POSTHOG_REDIRECT_URI,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                POSTHOG_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("PostHog token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"PostHog token exchange failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("PostHog OAuth: no access_token in response")

        logger.info("PostHog OAuth success", has_refresh=bool(refresh_token))

        from datetime import datetime, timezone, timedelta
        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "posthog_mcp", "client_id": client_id},
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        client_id = await _get_or_register_client()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                POSTHOG_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("PostHog token refresh failed", status=response.status_code, body=response.text)
            raise Exception(f"PostHog token refresh failed: {response.status_code}")

        data = response.json()
        from datetime import datetime, timezone, timedelta
        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "posthog_mcp", "client_id": client_id},
        )
