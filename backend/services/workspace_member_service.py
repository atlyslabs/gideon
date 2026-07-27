"""
Workspace Member Service — fetches and caches workspace members from
Linear and Slack for onboarding requirements and @-mention search.

Uses any existing connected user's token to query workspace/org members,
caches results in Redis for 1 hour (3600s).

Notion is intentionally NOT covered here. The Notion MCP refresh token is
single-use, and iterating active Notion integrations to "find a working
token" historically caused mass-deactivation cascades when one stale
candidate's failure path consumed RTs for healthy users. We treat Notion
membership as "user-declared" — if a user wants Notion, they connect it
directly via OAuth.
"""

import asyncio
import json
import os
from uuid import UUID

import structlog
from slack_sdk.errors import SlackApiError
from services.slack_client import get_slack_client
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Integration
from db.redis import get_redis
from services.encryption_service import decrypt
from services.integration_health import record_integration_error
from tools.linear_tool import _linear_gql

logger = structlog.get_logger()

# Your organisation's primary email domain (bare, e.g. "example.com"). When set,
# a member's address on this domain is treated as canonical and duplicate
# personal accounts are folded into it. Unset (default) keeps every address.
PRIMARY_EMAIL_DOMAIN = os.environ.get("PRIMARY_EMAIL_DOMAIN", "").strip().lower().lstrip("@")


def _load_email_aliases() -> dict[str, str]:
    """Optional {"secondary@example.com": "primary@example.com"} JSON map, for
    people whose accounts cannot be matched by name."""
    raw = os.environ.get("EMAIL_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k).lower(): str(v) for k, v in data.items()}
    except Exception:
        logger.warning("EMAIL_ALIASES failed to parse; ignoring")
        return {}


EMAIL_ALIASES = _load_email_aliases()


def _filter_members(members: list[dict], query: str) -> list[dict]:
    """Substring match on name or email; capped at 20 results. Empty query
    returns the full deduped list unchanged."""
    if not query:
        return members
    q = query.lower()
    return [
        m for m in members
        if q in (m.get("name", "") or "").lower() or q in (m.get("email", "") or "").lower()
    ][:20]


CACHE_TTL = 3600  # 1 hour
CACHE_TTL_EMPTY = 120  # 2 min backoff when fetch returns empty
LINEAR_MEMBERS_KEY = "workspace_members:linear"
SLACK_MEMBERS_KEY = "workspace_members:slack"

# Slack error codes that mean "this token is dead — never use it again".
_SLACK_DEAD_TOKEN_ERRORS = {"invalid_auth", "token_revoked", "account_inactive", "not_authed"}


class _TokenDead(Exception):
    """Raised by fetchers when the token is permanently invalid (revoked, expired,
    workspace uninstalled). Signals the cache layer to deactivate the integration
    row and try the next candidate. Distinct from transient/network failures
    which should not deactivate."""


# ---------------------------------------------------------------------------
# Token helper
# ---------------------------------------------------------------------------

async def _resolve_token_for(session: AsyncSession, integration: Integration, provider: str) -> str | None:
    """Decrypt the right token for a single integration, refreshing if expired.
    Returns None if the row has no usable token. Doesn't deactivate — that's
    the caller's job (after a fetch fails)."""
    from datetime import datetime, timezone

    try:
        if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
            if not integration.refresh_token:
                return None
            if provider not in ("notion", "linear"):
                return decrypt(integration.access_token) if integration.access_token else None

            from services.oauth.token_refresh import get_valid_access_token
            new_token = await get_valid_access_token(provider, integration.user_id)
            if new_token:
                await session.refresh(integration)
                logger.info("Refreshed token for workspace member lookup", provider=provider)
            return new_token

        # Slack: prefer bot token (has users:read.email scope for member listing)
        if provider == "slack":
            token = integration.bot_token or integration.access_token
            if not token:
                return None
            return decrypt(token)

        if not integration.access_token:
            return None
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to get token for workspace member lookup", provider=provider, error=str(e))
        return None


async def _iter_active_candidates(session: AsyncSession, provider: str) -> list[tuple[UUID, str]]:
    """Return (integration_id, decrypted_token) pairs for every active row of
    `provider`, newest first. The cache fetcher iterates these in order so that
    a freshly-reconnected user's token is tried before stale ones, and dead
    tokens get deactivated as they're discovered."""
    result = await session.execute(
        select(Integration).where(
            Integration.provider == provider,
            Integration.is_active == True,
        ).order_by(Integration.updated_at.desc())
    )
    out: list[tuple[UUID, str]] = []
    for integ in result.scalars().all():
        token = await _resolve_token_for(session, integ, provider)
        if token:
            out.append((integ.id, token))
    return out


# Backwards-compatible alias — single-token getter still used by other callers
# in this module (avatars, required-integrations check) that don't need the
# self-healing iteration path.
async def _get_any_active_token(session: AsyncSession, provider: str) -> str | None:
    candidates = await _iter_active_candidates(session, provider)
    return candidates[0][1] if candidates else None


async def _get_user_token(session: AsyncSession, user_id: UUID, provider: str) -> str | None:
    """Get a specific user's decrypted token for a provider."""
    from datetime import datetime, timezone
    from services.encryption_service import encrypt

    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
            Integration.is_active == True,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        return None

    try:
        if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
            if not integration.refresh_token:
                return None
            if provider not in ("notion", "linear", "google"):
                return decrypt(integration.access_token)

            # Shared lock-coordinated refresh. Prevents parallel tool calls and
            # cross-service lookups from spending the same single-use refresh token.
            from services.oauth.token_refresh import get_valid_access_token
            new_token = await get_valid_access_token(provider, user_id)
            if new_token:
                await session.refresh(integration)
            return new_token

        # Slack: prefer user token over bot token
        if provider == "slack":
            token = integration.access_token or integration.bot_token
            if not token:
                return None
            return decrypt(token)

        if not integration.access_token:
            return None
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to get user token", provider=provider, error=str(e))
        return None


# Public alias for cross-module use (avoids importing private _get_user_token)
get_user_token = _get_user_token


# ---------------------------------------------------------------------------
# Member fetchers — return list[dict] with {name, email, source}
# ---------------------------------------------------------------------------

async def _fetch_linear_members(token: str) -> list[dict]:
    """Fetch all Linear organization member names + emails via GraphQL."""
    query = """
    query {
        organization {
            users {
                nodes {
                    name
                    email
                    active
                    avatarUrl
                }
            }
        }
    }
    """
    try:
        data = await _linear_gql(token, query)
        users = (data.get("organization") or {}).get("users") or {}
        nodes = users.get("nodes", [])
        return [
            {
                "name": n.get("name", ""),
                "email": n["email"].lower(),
                "source": "linear",
                "avatar_url": n.get("avatarUrl"),
            }
            for n in nodes
            if n.get("email") and n.get("active", True)
        ]
    except Exception as e:
        msg = str(e).lower()
        if "401" in msg or "authentication required" in msg or "invalid api key" in msg or "unauthorized" in msg:
            raise _TokenDead(f"linear:{e}") from e
        logger.error("Linear org users query failed", error=str(e))
        return []


async def _fetch_slack_members(token: str) -> list[dict]:
    """Fetch all Slack workspace members via Slack API."""
    try:
        client = get_slack_client(token=token)
        members: list[dict] = []
        cursor = None

        while True:
            resp = await client.users_list(limit=200, cursor=cursor or "")
            for user in resp.get("members", []):
                if user.get("deleted") or user.get("is_bot") or user.get("id") == "USLACKBOT":
                    continue
                profile = user.get("profile", {})
                email = profile.get("email", "")
                name = profile.get("real_name") or user.get("real_name") or user.get("name", "")
                if email:
                    members.append({
                        "name": name,
                        "email": email.lower(),
                        "source": "slack",
                        "slack_id": user.get("id", ""),
                        "display_name": profile.get("display_name") or user.get("name", ""),
                        "avatar_url": profile.get("image_72"),
                    })

            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        return members
    except SlackApiError as e:
        err = ""
        try:
            err = (e.response or {}).get("error", "") or ""
        except Exception:
            err = ""
        if err in _SLACK_DEAD_TOKEN_ERRORS:
            raise _TokenDead(f"slack:{err}") from e
        logger.error("Slack users list failed", error=err or str(e))
        return []
    except Exception as e:
        logger.error("Slack users list failed", error=str(e))
        return []


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

# Stampede guard: one fetch at a time per cache key
_fetch_locks: dict[str, asyncio.Lock] = {}


async def _get_cached_or_fetch(
    session: AsyncSession, provider: str, cache_key: str, fetch_fn
) -> list[dict]:
    """Get workspace members from cache or fetch and cache.

    Uses an asyncio.Lock per cache_key so that concurrent callers on a cache
    miss wait for a single fetch instead of all hitting the API simultaneously.
    """
    redis = get_redis()

    cached = await redis.get(cache_key)
    if cached:
        return json.loads(cached)

    if cache_key not in _fetch_locks:
        _fetch_locks[cache_key] = asyncio.Lock()

    async with _fetch_locks[cache_key]:
        # Double-check: another coroutine may have populated the cache while we waited
        cached = await redis.get(cache_key)
        if cached:
            return json.loads(cached)

        candidates = await _iter_active_candidates(session, provider)
        if not candidates:
            return []

        members: list[dict] = []
        deactivated = 0
        for integration_id, token in candidates:
            try:
                members = await fetch_fn(token)
            except _TokenDead as e:
                # This token is permanently dead. Mark the row inactive +
                # record the error so the user sees "Reconnect needed" in the
                # frontend Integrations page, then try the next candidate.
                await record_integration_error(session, integration_id, str(e))
                await session.execute(
                    update(Integration)
                    .where(Integration.id == integration_id)
                    .values(is_active=False)
                )
                await session.flush()
                deactivated += 1
                logger.warning(
                    "Deactivated dead workspace integration",
                    provider=provider,
                    integration_id=str(integration_id),
                    reason=str(e),
                )
                continue
            # Reached here without _TokenDead → token worked (members may still
            # be empty if the workspace genuinely has no users, or if the API
            # had a transient hiccup). Either way, don't churn through the
            # rest of the candidates: stop and let the empty-cache TTL absorb
            # the next request.
            break

        if deactivated:
            # Persist the is_active=false writes. The session is FastAPI
            # dependency-managed and would commit at request end, but the
            # next request's `_iter_active_candidates` needs to see them.
            try:
                await session.commit()
            except Exception as e:
                logger.error("Failed to commit integration deactivations", error=str(e))

        if members:
            await redis.set(cache_key, json.dumps(members), ex=CACHE_TTL)
        else:
            await redis.set(cache_key, "[]", ex=CACHE_TTL_EMPTY)

        logger.info("Fetched workspace members", provider=provider, count=len(members))
        return members


async def _get_all_members(session: AsyncSession) -> list[dict]:
    """Get all workspace members. Slack is the SSOT for collaborators."""
    return await _get_cached_or_fetch(session, "slack", SLACK_MEMBERS_KEY, _fetch_slack_members)


# Public alias for cross-module use
get_all_members = _get_all_members


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def get_member_avatar(email: str, session: AsyncSession) -> str | None:
    """Look up a user's avatar URL from the Slack member cache."""
    all_members = await _get_all_members(session)
    email_lower = email.lower()
    for m in all_members:
        if m.get("email", "").lower() == email_lower:
            return m.get("avatar_url")
    # Try matching by local part (e.g. searched by work email but Slack has a personal one)
    local = email_lower.split("@")[0]
    for m in all_members:
        if m.get("email", "").lower().split("@")[0] == local:
            return m.get("avatar_url")
    return None


async def search_workspace_members(
    query: str, session: AsyncSession
) -> list[dict]:
    """Search workspace members by name or email, deduped per person.

    A person often appears twice in a Slack workspace: once under their work
    address and once under a personal one. When PRIMARY_EMAIL_DOMAIN is set, the
    address on that domain wins, and the personal duplicate is folded into it by
    matching display name, first name, or the email local-part. With no primary
    domain configured every distinct address is kept as-is.

    EMAIL_ALIASES optionally maps a known secondary address to its canonical one,
    as a JSON object of {"personal@example.com": "work@example.com"}, for people
    whose names do not line up across accounts.
    """
    all_members = await _get_all_members(session)

    primary_domain = PRIMARY_EMAIL_DOMAIN
    if not primary_domain:
        seen: set[str] = set()
        deduped = []
        for member in all_members:
            email = member.get("email", "").lower()
            if email in seen:
                continue
            seen.add(email)
            deduped.append(member)
        return _filter_members(deduped, query)

    suffix = f"@{primary_domain}"
    # Index canonical addresses by display name, first name, and local-part so a
    # personal duplicate can be resolved back to the primary account.
    primary_emails: dict[str, str] = {}
    for m in all_members:
        email = m.get("email", "")
        if email.lower().endswith(suffix):
            name = (m.get("name") or "").strip().lower()
            local = email.split("@")[0].lower()
            if name:
                primary_emails[name] = email
                first = name.split()[0]
                if first:
                    primary_emails[first] = email
            primary_emails[local] = email

    seen_emails: set[str] = set()
    deduped: list[dict] = []
    for member in all_members:
        email = member.get("email", "").lower()
        name = (member.get("name") or "").strip().lower()

        if email in seen_emails:
            continue

        # Secondary address: fold it into the primary account when we can find one.
        if not email.endswith(suffix):
            canonical = EMAIL_ALIASES.get(email)
            if not canonical:
                local = email.split("@")[0].lower()
                canonical = (
                    primary_emails.get(name)
                    or primary_emails.get(name.split()[0] if name else "")
                    or primary_emails.get(local)
                )
            if canonical:
                if canonical.lower() in seen_emails:
                    continue
                member = dict(member)
                member["email"] = canonical
                email = canonical.lower()

        seen_emails.add(email)
        deduped.append(member)

    return _filter_members(deduped, query)


async def get_required_integrations(
    email: str, session: AsyncSession
) -> list[str]:
    """
    Determine which integrations should be required for a user.
    Google and Slack are always required.
    Linear is required only if the user's email is in the org.
    Notion is opt-in — never auto-required (the org-membership probe iterated
    Notion integrations and burned single-use refresh tokens, deactivating
    healthy users; users now connect Notion explicitly if they want it).
    """
    required = ["google", "slack"]

    linear_members = await _get_cached_or_fetch(
        session, "linear", LINEAR_MEMBERS_KEY, _fetch_linear_members
    )
    if any(m["email"] == email.lower() for m in linear_members):
        required.append("linear")

    return required
