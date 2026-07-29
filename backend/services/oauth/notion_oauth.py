"""
Notion OAuth — via Notion MCP (Model Context Protocol).

Uses Dynamic Client Registration (RFC 7591) with Notion's MCP server.
No Notion developer app needed — registered via https://mcp.notion.com/register.
Public client (no client_secret), secured with PKCE (S256).

Flow:
1. Redirect user to mcp.notion.com/authorize with PKCE
2. User grants access on Notion's consent screen
3. Notion redirects to our callback with auth code
4. We exchange code + code_verifier for access_token at mcp.notion.com/token
5. Access token used for Notion API calls (expires in 1 hour, refresh with refresh_token)
"""

import hashlib
import base64
import secrets
import json

from datetime import datetime, timezone, timedelta

import httpx
import structlog

from config import NOTION_MCP_CLIENT_ID, NOTION_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

NOTION_MCP_AUTHORIZE_URL = "https://mcp.notion.com/authorize"
NOTION_MCP_TOKEN_URL = "https://mcp.notion.com/token"


def _token_expiry_from(data: dict) -> datetime:
    """Compute access-token expiry from Notion's actual ``expires_in``.

    We used to hardcode now+1h. That over-assumed a short lifetime and forced a
    refresh every hour — and because Notion MCP uses SINGLE-USE rotating refresh
    tokens, every needless refresh rotates the RT and widens the race that can
    strand a now-dead RT (the "please reconnect" spiral). Honoring the real
    ``expires_in`` cuts refresh frequency to what Notion actually requires.
    Falls back to 1h only if Notion omits the field.
    """
    raw = data.get("expires_in")
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 3600
    if seconds <= 0:
        seconds = 3600
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def store_pkce_verifier(state: str, code_verifier: str):
    """Store PKCE verifier in Redis alongside the OAuth state."""
    from db.redis import get_redis
    r = get_redis()
    await r.set(f"oauth_pkce:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    """Retrieve and delete PKCE verifier from Redis."""
    from db.redis import get_redis
    r = get_redis()
    key = f"oauth_pkce:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


class NotionOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "notion"

    def get_auth_url(self, state: str) -> str:
        code_verifier, code_challenge = _generate_pkce()

        # Store verifier — will be called from async context via router
        # The router will call store_pkce_verifier() after this returns
        self._pending_verifier = (state, code_verifier)

        from urllib.parse import urlencode
        params = {
            "response_type": "code",
            "client_id": NOTION_MCP_CLIENT_ID,
            "redirect_uri": NOTION_REDIRECT_URI,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{NOTION_MCP_AUTHORIZE_URL}?{urlencode(params)}"

    async def store_pending_pkce(self):
        """Call after get_auth_url() to persist the PKCE verifier to Redis."""
        if hasattr(self, "_pending_verifier"):
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            del self._pending_verifier

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        # Retrieve PKCE verifier from Redis
        code_verifier = await get_pkce_verifier(state) if state else None

        if not code_verifier:
            logger.warning("No PKCE code_verifier found for Notion callback", state=state)

        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": NOTION_MCP_CLIENT_ID,
            "redirect_uri": NOTION_REDIRECT_URI,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                NOTION_MCP_TOKEN_URL,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error(
                "Notion MCP token exchange failed",
                status=response.status_code,
                body=response.text,
            )
            raise Exception(f"Notion MCP token exchange failed: {response.status_code} - {response.text}")

        data = response.json()
        access_token = data.get("access_token", "")
        refresh_token = data.get("refresh_token")

        if not access_token:
            raise Exception("Notion MCP OAuth: no access_token in response")

        logger.info("Notion MCP OAuth success", has_refresh=bool(refresh_token))

        return OAuthTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=_token_expiry_from(data),
            extra_data={
                "via": "notion_mcp",
                "client_id": NOTION_MCP_CLIENT_ID,
            },
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        """Refresh an expired Notion MCP access token."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                NOTION_MCP_TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": NOTION_MCP_CLIENT_ID,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

        if response.status_code != 200:
            logger.error("Notion MCP token refresh failed", status=response.status_code, body=response.text)
            # Include the response body so the refresh helper's _is_invalid_grant
            # can tell a spent/revoked RT (invalid_grant -> mark needs-reconnect)
            # from a transient 5xx/timeout (-> retry; the RT is still valid).
            raise Exception(
                f"Notion MCP token refresh failed: {response.status_code} {response.text[:300]}"
            )

        data = response.json()
        return OAuthTokens(
            access_token=data["access_token"],
            refresh_token=data.get("refresh_token", refresh_token),
            token_expiry=_token_expiry_from(data),
            extra_data={
                "via": "notion_mcp",
                "client_id": NOTION_MCP_CLIENT_ID,
            },
        )
