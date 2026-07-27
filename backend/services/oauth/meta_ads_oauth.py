"""
Meta Ads OAuth — Facebook/Meta Graph API.

We originally targeted the Meta Ads MCP server at https://mcp.facebook.com/ads
(announced 2026-04-29). Probing showed the MCP server is gated to a partner
allowlist (Anthropic / OpenAI / Cursor): self-registered apps get
`401 "This resource is restricted to certain users"` even with valid Graph API
tokens. So we talk to the Graph API directly — same data, no allowlist gate.

Auth flow (standard Meta OAuth code grant):
1. Redirect user to https://www.facebook.com/v25.0/dialog/oauth with our
   pre-registered META_ADS_CLIENT_ID and PKCE (S256).
2. User picks a Meta Business + ad accounts to authorize.
3. Meta redirects back with auth code.
4. Exchange code at https://graph.facebook.com/v25.0/oauth/access_token.
5. Use the resulting user access token for Graph API calls
   (see tools/meta_ads_tool.py).

Long-lived tokens: Meta returns a short-lived (~1h) token from the code grant.
We immediately exchange it for a long-lived (~60d) token via the
`fb_exchange_token` grant — Meta only issues long-lived tokens this way.

Endpoints are hardcoded based on Meta's published OAuth Authorization Server
Metadata at https://mcp.facebook.com/.well-known/oauth-authorization-server/ads
(captured 2026-05-04). If Meta bumps the API version, update via env vars.
"""

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx
import structlog

from config import META_ADS_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

META_AUTHORIZE_URL = os.environ.get(
    "META_ADS_AUTHORIZE_URL",
    "https://www.facebook.com/v25.0/dialog/oauth",
)
META_TOKEN_URL = os.environ.get(
    "META_ADS_TOKEN_URL",
    "https://graph.facebook.com/v25.0/oauth/access_token",
)
META_SCOPES = (
    "ads_read,ads_management,catalog_management,business_management,"
    "pages_show_list,pages_read_engagement"
)

META_ADS_CLIENT_ID = os.environ.get("META_ADS_CLIENT_ID", "")
# Optional. Meta's metadata says public clients are supported
# (`token_endpoint_auth_methods_supported: ["none"]`), so PKCE alone is enough.
# But Meta's developer console defaults apps to confidential mode unless you
# flip "Client OAuth Login" → "Public client" — set this only if you keep the
# default (confidential).
META_ADS_CLIENT_SECRET = os.environ.get("META_ADS_CLIENT_SECRET", "")


def _generate_pkce() -> tuple[str, str]:
    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    digest = hashlib.sha256(code_verifier.encode()).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return code_verifier, code_challenge


async def store_pkce_verifier(state: str, code_verifier: str):
    from db.redis import get_redis
    r = get_redis()
    await r.set(f"oauth_pkce_meta_ads:{state}", code_verifier, ex=600)


async def get_pkce_verifier(state: str) -> str | None:
    from db.redis import get_redis
    r = get_redis()
    key = f"oauth_pkce_meta_ads:{state}"
    verifier = await r.get(key)
    if verifier:
        await r.delete(key)
    return verifier


async def _exchange_long_lived(short_token: str) -> tuple[str, int]:
    """Trade a short-lived token (~1h) for a long-lived one (~60d).
    Meta's `fb_exchange_token` grant. Requires the client_secret — short-lived
    tokens always do, even for public clients. Returns (token, expires_in_seconds).
    Falls back to the short-lived token if no client_secret is configured."""
    if not META_ADS_CLIENT_SECRET:
        logger.warning(
            "Meta Ads: no META_ADS_CLIENT_SECRET; using short-lived token "
            "(~1h). Set the secret to upgrade to ~60d tokens."
        )
        return short_token, 3600

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            META_TOKEN_URL,
            params={
                "grant_type": "fb_exchange_token",
                "client_id": META_ADS_CLIENT_ID,
                "client_secret": META_ADS_CLIENT_SECRET,
                "fb_exchange_token": short_token,
            },
        )
    if resp.status_code != 200:
        logger.warning("Meta Ads long-lived token exchange failed; keeping short-lived",
                       status=resp.status_code, body=resp.text[:300])
        return short_token, 3600
    data = resp.json()
    return data.get("access_token", short_token), int(data.get("expires_in", 5183944))  # ~60d default


class MetaAdsOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "meta_ads"

    def get_auth_url(self, state: str) -> str:
        if not META_ADS_CLIENT_ID:
            raise Exception(
                "Meta Ads OAuth not configured: set META_ADS_CLIENT_ID env var "
                "(your Meta developer app's App ID)."
            )
        code_verifier, code_challenge = _generate_pkce()
        # PKCE verifier persisted async via store_pending_pkce(); the auth URL
        # itself is fully resolvable synchronously since Meta's endpoints are
        # static.
        self._pending_verifier = (state, code_verifier)
        params = {
            "response_type": "code",
            "client_id": META_ADS_CLIENT_ID,
            "redirect_uri": META_ADS_REDIRECT_URI,
            "scope": META_SCOPES,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
        return f"{META_AUTHORIZE_URL}?{urlencode(params)}"

    async def store_pending_pkce(self):
        if hasattr(self, "_pending_verifier"):
            state, verifier = self._pending_verifier
            await store_pkce_verifier(state, verifier)
            del self._pending_verifier

    def get_resolved_auth_url(self) -> str:
        # Compatibility shim for the integrations router which calls this for
        # MCP-style providers. Our auth URL is already complete; just return ""
        # so the router falls back to the URL returned by get_auth_url().
        return ""

    async def handle_callback(self, code: str, state: str | None = None) -> OAuthTokens:
        if not META_ADS_CLIENT_ID:
            raise Exception("Meta Ads OAuth not configured: set META_ADS_CLIENT_ID")

        # PKCE verifier MUST be present — the auth flow stores it in Redis
        # under the same `state` we receive back from Meta. If it's missing,
        # the request is either replayed, stale (>10 min Redis TTL), or
        # forged. Reject rather than fall through to a degraded code-grant
        # exchange that defeats the point of PKCE. RFC 7636 §4.6 says a
        # client MUST send code_verifier when the auth request used
        # code_challenge, which we always do (see get_auth_url).
        code_verifier = await get_pkce_verifier(state) if state else None
        if not code_verifier:
            logger.error("Meta Ads OAuth callback missing PKCE verifier; rejecting", state=state)
            raise Exception(
                "Meta Ads OAuth: PKCE verifier missing or expired. "
                "Please retry the connect flow."
            )

        params = {
            "client_id": META_ADS_CLIENT_ID,
            "redirect_uri": META_ADS_REDIRECT_URI,
            "code": code,
            "code_verifier": code_verifier,
        }
        if META_ADS_CLIENT_SECRET:
            params["client_secret"] = META_ADS_CLIENT_SECRET

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(META_TOKEN_URL, params=params)

        if response.status_code != 200:
            logger.error("Meta Ads token exchange failed",
                         status=response.status_code, body=response.text[:400])
            raise Exception(f"Meta Ads token exchange failed: {response.status_code}: {response.text[:300]}")

        data = response.json()
        short_token = data.get("access_token", "")
        if not short_token:
            raise Exception("Meta Ads OAuth: no access_token in response")

        # Upgrade to long-lived (~60d) immediately. Meta short-lived tokens
        # last ~1h which is much too short for a daily scheduler workflow.
        long_token, expires_in = await _exchange_long_lived(short_token)

        logger.info("Meta Ads OAuth success", expires_in=expires_in,
                    upgraded=long_token != short_token)
        return OAuthTokens(
            access_token=long_token,
            # Meta has no separate refresh_token — to renew, you call
            # `fb_exchange_token` with the *current* access_token. The shared
            # token_refresh helper expects `integration.refresh_token` to be the
            # renewable artifact, so we mirror the access token there. Slight
            # conceptual hack but means we don't need a Meta-specific branch in
            # services/oauth/token_refresh.py.
            refresh_token=long_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "meta_graph_api"},
        )

    async def refresh_access_token(self, refresh_token: str) -> OAuthTokens:
        # `refresh_token` here is the current long-lived access_token (we mirror
        # them at OAuth callback time — see handle_callback above). Re-exchange
        # via fb_exchange_token to get a fresh ~60d token.
        long_token, expires_in = await _exchange_long_lived(refresh_token)
        return OAuthTokens(
            access_token=long_token,
            refresh_token=long_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            extra_data={"via": "meta_graph_api", "renewed": True},
        )
