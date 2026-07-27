"""
GitHub OAuth2 — classic OAuth app authorization code flow.
Access tokens don't expire. No refresh needed.
API is REST at https://api.github.com.
Note: Token exchange requires Accept: application/json header.
"""

from urllib.parse import urlencode

import httpx
import structlog

from config import GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET, GITHUB_REDIRECT_URI
from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_SCOPES = ["repo", "read:user", "user:email", "read:org"]


class GitHubOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "github"

    def get_auth_url(self, state: str) -> str:
        params = {
            "client_id": GITHUB_CLIENT_ID,
            "redirect_uri": GITHUB_REDIRECT_URI,
            "scope": " ".join(GITHUB_SCOPES),
            "state": state,
        }
        return f"{GITHUB_AUTH_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                GITHUB_TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "client_secret": GITHUB_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": GITHUB_REDIRECT_URI,
                },
            )

        if response.status_code != 200:
            logger.error("GitHub token exchange failed", status=response.status_code, body=response.text)
            raise Exception(f"GitHub token exchange failed: {response.status_code}")

        data = response.json()

        if "error" in data:
            logger.error("GitHub OAuth error", error=data.get("error"), description=data.get("error_description"))
            raise Exception(f"GitHub OAuth error: {data.get('error_description', data.get('error'))}")

        return OAuthTokens(
            access_token=data["access_token"],
            scopes=data.get("scope", "").split(",") if data.get("scope") else GITHUB_SCOPES,
        )

    # GitHub classic OAuth tokens don't expire — no refresh needed
