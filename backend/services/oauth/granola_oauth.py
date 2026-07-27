"""
Granola OAuth -- via Granola MCP (same pattern as Notion MCP).

Uses Dynamic Client Registration (RFC 7591) with Granola's MCP auth server.
Public client (no client_secret), secured with PKCE (S256).

Flow:
1. Register client dynamically at mcp-auth.granola.ai/oauth2/register (one-time, cached)
2. Redirect user to mcp-auth.granola.ai/oauth2/authorize with PKCE
3. User grants access on Granola's consent screen
4. Granola redirects to our callback with auth code
5. We exchange code + code_verifier for access_token at mcp-auth.granola.ai/oauth2/token
6. Access token used for Granola MCP calls (refresh with refresh_token)
"""

import hashlib
import base64
import secrets

import httpx
import structlog

from config import GRANOLA_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

GRANOLA_MCP_AUTHORIZE_URL = "https://mcp-auth.granola.ai/oauth2/authorize"
GRANOLA_MCP_TOKEN_URL = "https://mcp-auth.granola.ai/oauth2/token"
GRANOLA_MCP_REGISTER_URL = "https://mcp-auth.granola.ai/oauth2/register"

# Cached after first dynamic registration
_registered_client_id: str | None = None


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def _get_or_register_client() -> str:
    """Dynamically register a client with Granola's MCP auth server (RFC 7591).
    Returns the client_id. Cached in-memory after first call."""
    global _registered_client_id
    if _registered_client_id:
        return _registered_client_id

    # Also check Redis for persistence across restarts
    from db.redis import get_redis
    r = get_redis()
    cached = await r.get("granola_mcp_client_id")
    if cached:
        _registered_client_id = cached
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            GRANOLA_MCP_REGISTER_URL,
            json={
                "client_name": "Gideon AI Assistant",
                "redirect_uris": [GRANOLA_REDIRECT_URI],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )

    if response.status_code not in (200, 201):
        logger.error("Granola dynamic client registration failed", status=response.status_code, body=response.text)
        raise Exception(f"Granola client registration failed: {response.status_code}")

    data = response.json()
    client_id = data.get("client_id")
    if not client_id:
        raise Exception("Granola client registration: no client_id in response")

    _registered_client_id = client_id
    # Cache in Redis (no expiry -- client registrations are permanent)
    await r.set("granola_mcp_client_id", client_id)
    logger.info("Granola MCP client registered", client_id=client_id)
    return client_id


async def store_pkce_verifier(state: str, code_verifier: str):
    """Store PKCE verifier in Redis alongside the OAuth state."""
    from db.redis import get_redis
    r = get_redis()
    await r.set(f"oauth_pkce_granola:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    """Retrieve and delete PKCE verifier from Redis."""
    from db.redis import get_redis
    r = get_redis()
    key = f"oauth_pkce_granola:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


class GranolaOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "granola"

    def get_auth_url(self, state: str) -> str:
        code_verifier, code_challenge = _generate_pkce()

        # Store verifier -- will be persisted to Redis by store_pending_pkce()
        self._pending_verifier = (state, code_verifier)

        # Client ID will be set async in store_pending_pkce
        self._pending_state = state
        self._pending_code_challenge = code_challenge
        return ""  # Placeholder -- real URL built in store_pending_pkce

    async def store_pending_pkce(self):
        """Call after get_auth_url() to persist PKCE verifier and build real auth URL."""
        if hasattr(self, "_pending_verifier"):
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            del self._pending_verifier

        # Build the actual auth URL with dynamically registered client_id
        if hasattr(self, "_pending_state"):
            client_id = await _get_or_register_client()
            from urllib.parse import urlencode
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": GRANOLA_REDIRECT_URI,
                "code_challenge": self._pending_code_challenge,
                "code_challenge_method": "S256",
                "state": self._pending_state,
            }
            self._resolved_auth_url = f"{GRANOLA_MCP_AUTHORIZE_URL}?{urlencode(params)}"
            del self._pending_state
            del self._pending_code_challenge

    def get_resolved_auth_url(self) -> str:
        """Get the fully-resolved auth URL after store_pending_pkce()."""
        return getattr(self, "_resolved_auth_url", "")

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        code_verifier = await get_pkce_verifier(state) if state else None

        if not code_verifier:
            logger.warning("No PKCE code_verifier found for Granola callback", state=state)

        client_id = await _get_or_register_client()

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": GRANOLA_REDIRECT_URI,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                GRANOLA_MCP_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Granola MCP token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"Granola MCP token exchange failed: {response.status_code}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("Granola MCP OAuth: no access_token in response")

        logger.info("Granola MCP OAuth success", has_refresh=bool(refresh_token))

        from datetime import datetime, timezone, timedelta
        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "granola_mcp", "client_id": client_id},
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Granola MCP access token."""
        client_id = await _get_or_register_client()

        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                GRANOLA_MCP_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Granola MCP token refresh failed", status=response.status_code, body=response.text)
            raise Exception(f"Granola MCP token refresh failed: {response.status_code}")

        data = response.json()
        from datetime import datetime, timezone, timedelta
        expires_in = data.get("expires_in", 3600)
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "granola_mcp", "client_id": client_id},
        )
