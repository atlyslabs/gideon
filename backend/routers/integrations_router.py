"""
Integrations Router — connect/disconnect OAuth providers (Google, Slack, Notion).
"""

import asyncio
import os
from services.slack_client import get_slack_client
import time
import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from config.integration_registry import INTEGRATION_REGISTRY
from config.integration_cards import INTEGRATION_CARDS, OAUTH_PROVIDERS as CARD_OAUTH_PROVIDERS
from db import get_db
from db.models import Integration, Skill
from middleware.auth import get_current_user
from models.schemas import IntegrationResponse, IntegrationAuthURL
from services.auth_service import AuthUser
from services.user_service import get_or_create_user
from services.encryption_service import encrypt, decrypt
from services.oauth.base import OAuthProvider, OAuthTokens
from services.oauth.google_oauth import GoogleOAuthProvider
from services.oauth.slack_oauth import SlackOAuthProvider
from services.oauth.notion_oauth import NotionOAuthProvider
from services.oauth.granola_oauth import GranolaOAuthProvider
from services.oauth.ramp_oauth import RampOAuthProvider
from services.oauth.posthog_oauth import PostHogOAuthProvider
from services.oauth.figma_oauth import FigmaOAuthProvider
from services.oauth.semrush_oauth import SemrushOAuthProvider
from services.oauth.linear_oauth import LinearOAuthProvider
from services.oauth.github_oauth import GitHubOAuthProvider
from services.oauth.meta_ads_oauth import MetaAdsOAuthProvider

from pydantic import BaseModel

from db.models import User
from db.redis import get_redis

logger = structlog.get_logger()

router = APIRouter()

# Strong refs for fire-and-forget background tasks — asyncio holds only weak refs.
_background_tasks: set[asyncio.Task] = set()

# Provider registry
PROVIDERS: dict[str, OAuthProvider] = {
    "google": GoogleOAuthProvider(),
    "slack": SlackOAuthProvider(),
    "notion": NotionOAuthProvider(),
    "granola": GranolaOAuthProvider(),
    "ramp": RampOAuthProvider(),
    "posthog": PostHogOAuthProvider(),
    "figma": FigmaOAuthProvider(),
    "semrush": SemrushOAuthProvider(),
    "linear": LinearOAuthProvider(),
    "github": GitHubOAuthProvider(),
    "meta_ads": MetaAdsOAuthProvider(),
}

OAUTH_STATE_TTL = 600  # 10 minutes


async def _store_oauth_state(state: str, user_id: str):
    """Store state → user_id in Redis with TTL."""
    r = get_redis()
    await r.set(f"oauth_state:{state}", user_id, ex=OAUTH_STATE_TTL)


async def _pop_oauth_state(state: str) -> str | None:
    """Retrieve and delete state → user_id from Redis."""
    r = get_redis()
    key = f"oauth_state:{state}"
    user_id = await r.get(key)
    if user_id:
        await r.delete(key)
    return user_id


class IntegrationManifestEntry(BaseModel):
    key: str
    label: str
    provider: str
    internal: bool


@router.get("/manifest", response_model=list[IntegrationManifestEntry])
async def get_integration_manifest(
    auth_user: AuthUser = Depends(get_current_user),
):
    """Static integration registry: label, provider, and whether the
    integration uses shared/internal credentials. The frontend uses this to
    decide which integrations need a per-user OAuth/form connection vs.
    being always-available. Auth-gated to match the rest of this router —
    the data is non-sensitive, but every other route here requires auth and
    the FE always has a bearer token when it asks for this."""
    del auth_user  # only here to require an authenticated session
    return [
        IntegrationManifestEntry(key=key, label=meta["label"], provider=meta["provider"], internal=meta["internal"])
        for key, meta in INTEGRATION_REGISTRY.items()
    ]


class IntegrationCardResponse(BaseModel):
    """Server-owned card metadata for the integrations page. The FE merges
    this with its local icon map (icons are React components, can't ride
    JSON) keyed by `id`. Field names use snake_case here and camelCase via
    serialization aliases so the FE side stays idiomatic."""

    id: str
    oauth_provider: str
    name: str
    description: str
    hero_color: str
    data_safe_title: str
    data_safe_description: str
    capabilities: list[str]
    overview_title: str
    overview_text: str
    feature_title: str
    feature_text: str
    coming_soon: bool = False
    dark_icon: bool = False
    api_key_connect: bool = False
    internal: bool = False
    connect_hint: Optional[str] = None

    model_config = {
        "alias_generator": lambda name: "".join(
            word.capitalize() if i else word for i, word in enumerate(name.split("_"))
        ),
        "populate_by_name": True,
    }


class IntegrationCardsResponse(BaseModel):
    cards: list[IntegrationCardResponse]
    oauth_providers: list[str]

    model_config = {
        "alias_generator": lambda name: "".join(
            word.capitalize() if i else word for i, word in enumerate(name.split("_"))
        ),
        "populate_by_name": True,
    }


@router.get("/cards", response_model=IntegrationCardsResponse, response_model_by_alias=True)
async def get_integration_cards(
    auth_user: AuthUser = Depends(get_current_user),
):
    """Rich card metadata for the integrations page (label, copy,
    capabilities, hero color, comingSoon, etc.). Previously lived in
    `gideon-ui/.../providerConfig.ts`; moving it server-side lets us flip
    `comingSoon` or rewrite copy without a gideon-ui redeploy. Icons stay
    FE-only since they're React components."""
    del auth_user  # auth-gated for parity; payload is non-sensitive
    cards = [IntegrationCardResponse(**card) for card in INTEGRATION_CARDS]
    return IntegrationCardsResponse(cards=cards, oauth_providers=sorted(CARD_OAUTH_PROVIDERS))


@router.get("", response_model=list[IntegrationResponse])
async def list_integrations(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all integrations for the current user (active and errored)."""
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user.id,
        )
    )
    integrations = result.scalars().all()
    return [
        IntegrationResponse(
            id=i.id,
            provider=i.provider,
            is_active=i.is_active,
            error_count=i.error_count or 0,
            last_error_at=i.last_error_at,
            last_error=i.last_error,
            scopes=i.scopes,
            extra_data=i.extra_data,
            connected_at=i.connected_at,
        )
        for i in integrations
    ]


@router.get("/{provider}/auth-url", response_model=IntegrationAuthURL)
async def get_auth_url(
    provider: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get the OAuth authorization URL for a provider."""
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    user = await get_or_create_user(session, auth_user)
    state = str(uuid.uuid4())
    # Store user_id in Redis so callback can identify the user (redirects don't carry auth headers)
    await _store_oauth_state(state, str(user.id))
    provider_instance = PROVIDERS[provider]
    auth_url = provider_instance.get_auth_url(state)
    # Notion/Granola MCP needs PKCE verifier stored in Redis
    if hasattr(provider_instance, "store_pending_pkce"):
        await provider_instance.store_pending_pkce()
    # Granola builds the real URL in store_pending_pkce (async client registration)
    if hasattr(provider_instance, "get_resolved_auth_url"):
        resolved = provider_instance.get_resolved_auth_url()
        if resolved:
            auth_url = resolved
    return IntegrationAuthURL(auth_url=auth_url)


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    background_tasks: BackgroundTasks,
    code: str = Query(...),
    state: Optional[str] = Query(None),
    session: AsyncSession = Depends(get_db),
):
    """Handle OAuth callback — exchange code for tokens and store.

    This endpoint does NOT require auth headers because it's called via
    browser redirect from external OAuth providers. User is identified
    via the state parameter stored during auth-url generation.
    """
    if provider not in PROVIDERS:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider}")

    # Look up user from Redis state store
    user_id = await _pop_oauth_state(state) if state else None
    if not user_id:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state. Please try connecting again.")

    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=400, detail="User not found for this OAuth session.")

    try:
        # Notion/Granola MCP needs state for PKCE verification
        if provider in ("notion", "granola", "posthog", "figma", "semrush", "meta_ads"):
            tokens: OAuthTokens = await PROVIDERS[provider].handle_callback(code, state=state)
        else:
            tokens: OAuthTokens = await PROVIDERS[provider].handle_callback(code)
    except Exception as e:
        logger.error("OAuth callback failed", provider=provider, error=str(e))
        from services.events_service import track_error
        track_error(str(user.id), "oauth_callback_failed", provider=provider, user_email=user.email, detail=str(e))
        raise HTTPException(status_code=400, detail=f"OAuth failed: {str(e)}")

    # Upsert: delete existing integration for this provider, then create new
    await session.execute(
        delete(Integration).where(
            Integration.user_id == user.id,
            Integration.provider == provider,
        )
    )

    integration = Integration(
        user_id=user.id,
        provider=provider,
        access_token=encrypt(tokens.access_token),
        refresh_token=encrypt(tokens.refresh_token) if tokens.refresh_token else None,
        bot_token=encrypt(tokens.bot_token) if tokens.bot_token else None,
        token_expiry=tokens.token_expiry,
        scopes=tokens.scopes,
        extra_data=tokens.extra_data,
        is_active=True,
    )
    session.add(integration)
    await session.flush()
    await session.refresh(integration)

    logger.info("Integration connected", provider=provider, user_id=str(user.id))
    from services.events_service import track_integration_connected
    track_integration_connected(str(user.id), provider)

    # Slack: auto-join every public channel so @Gideon mentions fire `app_mention`.
    # Without this, Slack only delivers mentions in channels the bot is already in,
    # which means users would have to /invite Gideon manually everywhere.
    if provider == "slack" and tokens.bot_token:
        from services.slack_auto_join import auto_join_public_channels
        task = asyncio.create_task(auto_join_public_channels(tokens.bot_token))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    # Auto-create default skill for this provider if it doesn't exist yet
    PROVIDER_SKILLS = {
        "google": {
            "name": "Email Tone",
            "description": "Learns your email communication style, tone, and writing patterns.",
            "prompt": (
                "Learn my email communication style, tone, and writing patterns. "
                "Analyze how I write emails including greeting patterns, sign-offs, "
                "sentence structure, vocabulary, formality level, and how my style "
                "varies by recipient or context."
            ),
        },
        "slack": {
            "name": "Slack Tone",
            "description": "Learns your Slack messaging style, tone, and communication patterns.",
            "prompt": (
                "Learn my Slack communication style, tone, and messaging patterns. "
                "Analyze how I write Slack messages including message length, emoji usage, "
                "thread vs channel behavior, formality shifts by channel or DM, formatting "
                "habits, and how my style varies by context."
            ),
        },
    }

    if provider in PROVIDER_SKILLS:
        skill_def = PROVIDER_SKILLS[provider]
        existing = await session.execute(
            select(Skill).where(
                Skill.user_id == user.id,
                Skill.name == skill_def["name"],
                Skill.is_default == True,
            )
        )
        if not existing.scalar_one_or_none():
            skill = Skill(
                user_id=user.id,
                name=skill_def["name"],
                description=skill_def["description"],
                prompt=skill_def["prompt"],
                integrations_used=[provider],
                is_default=True,
            )
            session.add(skill)
            await session.flush()
            logger.info("Default skill auto-created", skill=skill_def["name"], user_id=str(user.id))

    # Workflow generation handled by smart onboarding (POST /me/smart-onboarding)
    # after all 3 required integrations (Google, Slack, Notion) are connected

    # Return HTML that closes the OAuth popup window
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content="""
    <html><body>
    <script>
        window.close();
    </script>
    <p>Connected successfully! You can close this window.</p>
    </body></html>
    """)


@router.delete("/{integration_id}")
async def disconnect_integration(
    integration_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Disconnect (deactivate) an integration."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Integration).where(
            Integration.id == integration_id,
            Integration.user_id == user.id,
        )
    )
    integration = result.scalar_one_or_none()

    if not integration:
        raise HTTPException(status_code=404, detail="Integration not found")

    integration.is_active = False
    await session.flush()

    logger.info("Integration disconnected", provider=integration.provider, user_id=str(user.id))
    from services.events_service import track_integration_disconnected
    track_integration_disconnected(str(user.id), integration.provider)
    return {"status": "disconnected"}


class ApiKeyConnectBody(BaseModel):
    api_key: str
    company_id: str


@router.post("/recruitee/connect")
async def connect_recruitee(
    body: ApiKeyConnectBody,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Connect Recruitee with a personal API key + company ID.
    Validates the key by calling the Recruitee API before storing."""
    user = await get_or_create_user(session, auth_user)

    # Validate the key by making a test API call
    import httpx
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"https://api.recruitee.com/c/{body.company_id}/offers",
            headers={"Authorization": f"Bearer {body.api_key}", "Accept": "application/json"},
            params={"scope": "active", "view_mode": "brief"},
        )
    if resp.status_code == 401:
        raise HTTPException(status_code=400, detail="Invalid API key. Please check and try again.")
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail=f"Recruitee API returned {resp.status_code}. Please check your company ID.")

    # Store as integration — API key + company ID combined in access_token
    # Format: "{api_key}||{company_id}" — split apart in tool execution
    combined_token = f"{body.api_key}||{body.company_id}"

    await session.execute(
        delete(Integration).where(
            Integration.user_id == user.id,
            Integration.provider == "recruitee",
        )
    )

    integration = Integration(
        user_id=user.id,
        provider="recruitee",
        access_token=encrypt(combined_token),
        is_active=True,
        extra_data={"company_id": body.company_id},
    )
    session.add(integration)
    await session.flush()

    logger.info("Recruitee connected via API key", user_id=str(user.id))
    return {"status": "connected"}


class IntegrationRequestBody(BaseModel):
    message: str


# Simple in-memory rate limit: user_id -> list of request timestamps
_request_timestamps: dict[str, list[float]] = {}
_RATE_LIMIT_MAX = 5
_RATE_LIMIT_WINDOW = 86400  # 24 hours

# Where "request an integration" feedback is posted. Sent with the requesting
# user's own Slack token, so this is a channel in THEIR workspace.
FEEDBACK_SLACK_CHANNEL = os.environ.get("FEEDBACK_SLACK_CHANNEL", "#gideon-feedback")


@router.post("/request")
async def request_integration(
    body: IntegrationRequestBody,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Submit a request for a new integration. Posts to FEEDBACK_SLACK_CHANNEL via Slack if connected."""
    user = await get_or_create_user(session, auth_user)
    user_label = auth_user.email or str(user.id)

    # Rate limit: max 5 requests per user per 24h
    now = time.time()
    uid = str(user.id)
    timestamps = _request_timestamps.get(uid, [])
    timestamps = [t for t in timestamps if now - t < _RATE_LIMIT_WINDOW]
    if len(timestamps) >= _RATE_LIMIT_MAX:
        raise HTTPException(status_code=429, detail="Too many integration requests. Please try again later.")
    timestamps.append(now)
    _request_timestamps[uid] = timestamps

    # Sanitize message to prevent Slack notification spam
    safe_message = body.message.replace("@here", "here").replace("@channel", "channel").replace("@everyone", "everyone")

    # Try to send via user's Slack integration
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user.id,
            Integration.provider == "slack",
            Integration.is_active == True,
        )
    )
    slack_integration = result.scalar_one_or_none()

    if slack_integration:
        try:

            token = slack_integration.bot_token or slack_integration.access_token
            if token:
                token = decrypt(token)
                client = get_slack_client(token=token)
                await client.chat_postMessage(
                    channel=FEEDBACK_SLACK_CHANNEL,
                    text=f"*Integration Request* from {user_label}:\n> {safe_message}",
                )
                logger.info("Integration request sent to Slack", user_id=str(user.id))
        except Exception as e:
            logger.warning("Failed to send integration request to Slack, logging instead", error=str(e))
            logger.info("Integration request", user_id=str(user.id), message=safe_message)
    else:
        logger.info("Integration request (no Slack)", user_id=str(user.id), message=safe_message)

    return {"status": "ok"}
