"""
Slack OAuth V2 — Bot token flow.
Tokens don't expire. Returns bot_token + access_token.
"""

from urllib.parse import urlencode

import httpx
import structlog
from config import SLACK_CLIENT_ID, SLACK_CLIENT_SECRET, SLACK_REDIRECT_URI

from .base import OAuthProvider, OAuthTokens

logger = structlog.get_logger()

# Bot scopes
SLACK_BOT_SCOPES = [
    "app_mentions:read",
    "chat:write",
    "channels:read",
    "channels:history",
    "channels:join",  # Auto-join all public channels on connect so @Gideon mentions fire
    "groups:read",
    "groups:history",
    "im:read",
    "im:history",
    "mpim:read",
    "mpim:write",
    "mpim:history",
    "reactions:write",
    "users:read",
    "users:read.email",
    "search:read.public",
]

# User scopes — messages sent as the user, not the bot. Existing users must
# reconnect Slack to pick up new scopes (reactions:write / pins:write /
# files:write added 2026-05 for the message-management tools).
SLACK_USER_SCOPES = [
    "chat:write",
    "channels:read",
    "channels:history",
    "groups:read",
    "groups:history",
    "im:read",
    "im:write",
    "im:history",
    "mpim:read",
    "mpim:write",
    "mpim:history",
    "users:read",
    "search:read",
    "canvases:read",
    "canvases:write",
    "reactions:write",
    "pins:write",
    "files:write",
]

SLACK_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"
SLACK_TOKEN_URL = "https://slack.com/api/oauth.v2.access"


class SlackOAuthProvider(OAuthProvider):
    @property
    def provider_name(self) -> str:
        return "slack"

    def get_auth_url(self, state: str) -> str:
        params = {
            "client_id": SLACK_CLIENT_ID,
            "scope": ",".join(SLACK_BOT_SCOPES),
            "user_scope": ",".join(SLACK_USER_SCOPES),
            "redirect_uri": SLACK_REDIRECT_URI,
            "state": state,
        }
        return f"{SLACK_AUTHORIZE_URL}?{urlencode(params)}"

    async def handle_callback(self, code: str) -> OAuthTokens:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                SLACK_TOKEN_URL,
                data={
                    "client_id": SLACK_CLIENT_ID,
                    "client_secret": SLACK_CLIENT_SECRET,
                    "code": code,
                    "redirect_uri": SLACK_REDIRECT_URI,
                },
            )

        if response.status_code != 200:
            logger.error("Slack token exchange failed", status=response.status_code)
            raise Exception(f"Slack token exchange failed: HTTP {response.status_code}")

        data = response.json()
        if not data.get("ok"):
            error_code = data.get("error", "unknown")
            logger.error("Slack OAuth error", error_code=error_code)
            raise Exception(f"Slack OAuth error: {error_code}")

        # Slack V2 returns both:
        # - data["access_token"] = bot token (xoxb-...)
        # - data["authed_user"]["access_token"] = user token (xoxp-...)
        bot_token = data.get("access_token", "")
        user_token = data.get("authed_user", {}).get("access_token", "")

        # Merge bot scopes + user scopes
        bot_scopes = set(s for s in data.get("scope", "").split(",") if s)
        user_scopes = set(
            s for s in data.get("authed_user", {}).get("scope", "").split(",") if s
        )
        all_scopes = sorted(bot_scopes | user_scopes)

        return OAuthTokens(
            access_token=user_token or bot_token,  # prefer user token
            bot_token=bot_token,
            scopes=all_scopes,
            extra_data={
                "team_id": data.get("team", {}).get("id"),
                "team_name": data.get("team", {}).get("name"),
                "bot_user_id": data.get("bot_user_id"),
                "app_id": data.get("app_id"),
                "user_id": data.get("authed_user", {}).get("id"),
            },
        )

    # Slack tokens don't expire — no refresh needed
