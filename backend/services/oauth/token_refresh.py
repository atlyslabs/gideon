"""Lock-coordinated OAuth token refresh.

Problem: several providers (Notion MCP, Granola MCP, etc.) issue single-use
refresh tokens. When the chat agent dispatches multiple tool calls in parallel,
each call's session may see the same expired token and race to refresh it.
The first refresh succeeds and rotates the refresh_token; the second sends the
now-spent refresh_token and gets `400 invalid_grant`. Worst case, a losing
session's rollback leaves a stale refresh_token persisted, and every
subsequent refresh fails until the user reconnects manually.

Fix:
1. Redis lock per (provider, user_id). First caller refreshes; the rest wait
   and re-read the freshly-persisted token from DB.
2. Retry-with-reread on `invalid_grant`. If we still hit the race (lock
   outage, TTL expiry under a slow refresh), catch `invalid_grant` and open
   a fresh DB session to see whether someone else already rotated the token.
3. Short-lived dedicated session for the refresh so its COMMIT is visible to
   other fresh sessions — long-lived agent-loop sessions only flush, so
   their writes aren't visible across sessions until the outer commit.

All MCP providers (notion, granola, posthog, figma, semrush) plus linear and
google share this helper. Callers get back a plain access_token string or
None (connect missing / refresh permanently failed → surface reconnect).
"""

import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any
from uuid import UUID, uuid4

import structlog
from sqlalchemy import select

from db.database import session_manager
from db.models import Integration
from db.redis import get_redis
from services.encryption_service import encrypt, decrypt
from services.integration_alerts import alert_integration_dead

logger = structlog.get_logger()

# Lock TTL must comfortably EXCEED the provider refresh HTTP timeout + the DB
# write, or the lock can expire mid-refresh and a second caller refreshes the
# SAME single-use refresh token -> one of them gets invalid_grant and the RT is
# burned. Notion's refresh httpx timeout is 10s, so 25s leaves margin.
_LOCK_TTL_SEC = 25
_LOCK_WAIT_TIMEOUT_SEC = 12.0  # max time a losing caller waits for the winner
_LOCK_POLL_INTERVAL_SEC = 0.1
_EXPIRY_BUFFER_SEC = 30       # refresh if token expires within this many seconds
# Transient refresh failures (5xx / timeout / network) do NOT consume the RT —
# the provider never processed it — so they're safe to retry. invalid_grant is
# the ONLY signal the RT is actually spent/dead.
_REFRESH_TRANSIENT_RETRIES = 2
_REFRESH_RETRY_DELAY_S = 1.5
# After a successful rotation the new tokens MUST be persisted, else the now-dead
# old RT is what's stored. Retry the write before giving up.
_WRITE_RETRIES = 2
_LOCK_KEY_PREFIX = "gideon:oauth_refresh"


def _lock_key(provider: str, user_id: UUID) -> str:
    return f"{_LOCK_KEY_PREFIX}:{provider}:{user_id}"


async def _release_lock(redis_client, key: str, value: str) -> None:
    """Lua compare-and-delete so we never release someone else's lock after a TTL expiry."""
    lua = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        await redis_client.eval(lua, 1, key, value)
    except Exception:
        pass  # lock will expire via TTL


def _get_refresher(provider: str):
    """Return the OAuth provider instance that knows how to refresh this token.
    Returns None for providers we don't refresh automatically (slack, github, recruitee)."""
    if provider == "notion":
        from services.oauth.notion_oauth import NotionOAuthProvider
        return NotionOAuthProvider()
    if provider == "granola":
        from services.oauth.granola_oauth import GranolaOAuthProvider
        return GranolaOAuthProvider()
    if provider == "posthog":
        from services.oauth.posthog_oauth import PostHogOAuthProvider
        return PostHogOAuthProvider()
    if provider == "figma":
        from services.oauth.figma_oauth import FigmaOAuthProvider
        return FigmaOAuthProvider()
    if provider == "semrush":
        from services.oauth.semrush_oauth import SemrushOAuthProvider
        return SemrushOAuthProvider()
    if provider == "ramp":
        from services.oauth.ramp_oauth import RampOAuthProvider
        return RampOAuthProvider()
    if provider == "linear":
        from services.oauth.linear_oauth import LinearOAuthProvider
        return LinearOAuthProvider()
    if provider == "google":
        from services.oauth.google_oauth import GoogleOAuthProvider
        return GoogleOAuthProvider()
    if provider == "meta_ads":
        from services.oauth.meta_ads_oauth import MetaAdsOAuthProvider
        return MetaAdsOAuthProvider()
    return None


def _is_invalid_grant(exc: BaseException) -> bool:
    """True if the error looks like the refresh_token was already consumed/revoked.

    Matches on the specific tokens OAuth servers use for spent/revoked refresh
    tokens. Previously also matched any string containing " 400", which false-
    matched things like "timeout after 400ms". We now anchor on status-code
    context: "400" only counts when it's adjacent to "bad request" or "status"
    or the refresh helper's own "token refresh failed:" prefix.
    """
    import re as _re
    s = str(exc).lower()
    if "invalid_grant" in s or "invalid refresh token" in s:
        return True
    # Match "... 400 Bad Request", "status: 400", or our own refresh error
    # message pattern "token refresh failed: 400".
    return bool(_re.search(r"(\b400 bad request\b|status[=: ]\s*400\b|refresh failed:\s*400\b)", s))


async def _fetch_current_integration(provider: str, user_id: UUID) -> Integration | None:
    """Read the integration from a fresh short-lived session so we see any
    COMMITTED writes from other sessions that just refreshed.

    Uses order-by-updated_at + first() rather than scalar_one_or_none(): if a
    duplicate active row ever exists (e.g. a reconnect that raced the delete),
    scalar_one_or_none() would RAISE and crash every refresh. first() degrades
    to "use the most recently updated row" instead.
    """
    async with session_manager.get_session() as s:
        result = await s.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == provider,
                Integration.is_active == True,
            ).order_by(Integration.updated_at.desc()).limit(1)
        )
        return result.scalars().first()


async def _mark_needs_reconnect(provider: str, user_id: UUID, reason: str) -> None:
    """Flag the integration as needing a manual reconnect: deactivate it and
    record the error. Called ONLY when the refresh token is confirmed dead
    (invalid_grant) or the rotated token was lost — i.e. no automated recovery
    is possible. This BREAKS THE SPIRAL: with is_active=False, the next
    get_valid_access_token short-circuits to None instead of hammering the
    provider's token endpoint with the dead RT, and the UI surfaces a reconnect
    prompt. A reconnect re-inserts the row with is_active=True."""
    try:
        async with session_manager.get_session() as s:
            result = await s.execute(
                select(Integration).where(
                    Integration.user_id == user_id,
                    Integration.provider == provider,
                    Integration.is_active == True,
                ).order_by(Integration.updated_at.desc()).limit(1)
            )
            integ = result.scalars().first()
            if integ:
                integ.is_active = False
                integ.error_count = (integ.error_count or 0) + 1
                integ.last_error_at = datetime.now(timezone.utc)
                integ.last_error = reason[:500]
        if integ:
            # Real active->inactive transition: alert the admin (fire-and-forget,
            # Notion only). Won't re-fire — once inactive, _fetch_current_integration
            # returns None and we never re-enter this path for the same death.
            alert_integration_dead(provider, user_id, reason)
        logger.warning(
            "Integration deactivated — reconnect required",
            provider=provider, user_id=str(user_id), reason=reason[:160],
        )
    except Exception as e:
        logger.error(
            "Failed to flag integration needs-reconnect",
            provider=provider, user_id=str(user_id), error=str(e)[:160],
        )


def _valid_token_or_none(integration: Integration | None) -> str | None:
    """If the integration's access_token hasn't expired (with buffer), return it decrypted."""
    if not integration or not integration.access_token:
        return None
    now = datetime.now(timezone.utc)
    if integration.token_expiry and integration.token_expiry > now + timedelta(seconds=_EXPIRY_BUFFER_SEC):
        try:
            return decrypt(integration.access_token)
        except Exception:
            return None
    return None


def _short_circuit_after_wait(
    integration: Integration | None, *, force_refresh: bool, original_access_token: str | None
) -> str | None:
    """Decide whether a post-lock-wait re-read lets us skip refreshing.

    Normal case: return the token if it's fresh by expiry (another caller may
    have refreshed it while we waited on the lock — pick that up rather than
    burn the single-use RT again).

    force_refresh case (reactive 401): the stored token can be fresh BY EXPIRY
    yet rejected by the provider, so a plain freshness check would short-circuit
    and loop the 401 forever. Only short-circuit if the token VALUE actually
    changed from what we came in with — i.e. someone else genuinely refreshed it.
    If it's the same token we set out to force-refresh, fall through and refresh.
    """
    if not force_refresh:
        return _valid_token_or_none(integration)
    # force_refresh: only skip the refresh if another caller GENUINELY rotated the
    # token while we waited on the lock. Compare the decrypted token VALUES, not
    # the stored ciphertext blobs. (The blobs are deterministic on read — same
    # stored value read twice is byte-identical — so a ciphertext compare also
    # works today, but comparing values is unambiguous, survives a future move to
    # an encrypt-on-read column type, and correctly handles a re-encrypt of the
    # same plaintext.) If we can't confirm a real change, fall through and refresh
    # — the safe default for a token the provider just rejected.
    fresh = _valid_token_or_none(integration)
    if fresh is None:
        return None
    try:
        original_plain = decrypt(original_access_token) if original_access_token else None
    except Exception:
        original_plain = None
    if original_plain is None or fresh == original_plain:
        return None
    return fresh


async def get_valid_access_token(
    provider: str, user_id: UUID, *, force_refresh: bool = False
) -> str | None:
    """Return a currently-valid access_token for (provider, user_id).

    - If the stored token is still fresh, returns it immediately (unless
      `force_refresh` — see below).
    - If it's expired or about to expire, coordinates a Redis-locked refresh:
      first caller refreshes and commits; concurrent callers wait on the lock,
      then re-read DB to pick up the freshly-rotated token.
    - On invalid_grant, re-reads once in case someone else raced ahead; if the
      RT is confirmed dead, flags the integration needs-reconnect and returns
      None (callers surface a reconnect prompt).
    - Returns None if the integration is missing, can't be refreshed, or is
      permanently revoked.

    `force_refresh=True`: skip the "still fresh by expiry" short-circuit and
    refresh anyway. Used by the reactive-401 path — a token can be rejected by
    the provider (revoked server-side) while our stored token_expiry still says
    it's valid, which would otherwise loop on 401 forever.
    """
    integration = await _fetch_current_integration(provider, user_id)
    if not integration:
        return None

    if not force_refresh:
        fresh = _valid_token_or_none(integration)
        if fresh is not None:
            return fresh

    if not integration.refresh_token:
        return None  # can't refresh without a refresh_token (e.g., Slack, Recruitee)

    refresher = _get_refresher(provider)
    if refresher is None:
        # Provider doesn't support programmatic refresh; return whatever we have
        # (caller will surface reconnect if a tool call then 401s).
        try:
            return decrypt(integration.access_token) if integration.access_token else None
        except Exception:
            return None

    redis_client = get_redis()
    key = _lock_key(provider, user_id)

    # Remember the token we came in with so the post-wait re-checks can tell
    # "someone else refreshed while we waited" (value changed -> use it) from
    # "still the same token we must force-refresh" (value unchanged -> refresh).
    original_access_token = integration.access_token

    # Wait for any in-flight refresh to finish before we check the DB again.
    waited = 0.0
    while waited < _LOCK_WAIT_TIMEOUT_SEC:
        try:
            holder = await redis_client.get(key)
        except Exception:
            holder = None
        if not holder:
            break
        await asyncio.sleep(_LOCK_POLL_INTERVAL_SEC)
        waited += _LOCK_POLL_INTERVAL_SEC

    # After waiting, re-check — maybe the lock holder just persisted a new token.
    integration = await _fetch_current_integration(provider, user_id)
    fresh = _short_circuit_after_wait(
        integration, force_refresh=force_refresh, original_access_token=original_access_token
    )
    if fresh is not None:
        return fresh
    if not integration or not integration.refresh_token:
        return None

    # Try to acquire the lock ourselves and do the refresh.
    lock_value = str(uuid4())
    acquired = False
    redis_failed = False
    try:
        acquired = bool(await redis_client.set(key, lock_value, nx=True, ex=_LOCK_TTL_SEC))
    except Exception as e:
        logger.warning("Redis lock acquire failed; proceeding unlocked", provider=provider, error=str(e))
        redis_failed = True

    # If Redis is up and the lock is held by another caller, DO NOT race a
    # competing refresh call — single-use refresh tokens will be burned.
    # Wait again for the holder, then re-read DB. If still stale, surface
    # reconnect instead of stampeding Notion's token endpoint.
    if not acquired and not redis_failed:
        waited2 = 0.0
        while waited2 < _LOCK_WAIT_TIMEOUT_SEC:
            try:
                holder = await redis_client.get(key)
            except Exception:
                holder = None
            if not holder:
                break
            await asyncio.sleep(_LOCK_POLL_INTERVAL_SEC)
            waited2 += _LOCK_POLL_INTERVAL_SEC
        recovered_integ = await _fetch_current_integration(provider, user_id)
        recovered = _short_circuit_after_wait(
            recovered_integ, force_refresh=force_refresh, original_access_token=original_access_token
        )
        if recovered is not None:
            return recovered
        logger.warning(
            "Refresh lock held by another caller and DB still stale after wait — giving up",
            provider=provider, user_id=str(user_id),
        )
        return None

    try:
        # 1) Read the current refresh token in a short-lived session, then
        #    RELEASE it before the external provider call. No HTTP inside an open
        #    DB txn (a slow refresh would hold a connection idle-in-transaction),
        #    and for single-use refresh tokens this keeps the read→write window
        #    as tight as possible so a lost write can't strand a dead RT.
        async with session_manager.get_session() as s:
            result = await s.execute(
                select(Integration).where(
                    Integration.user_id == user_id,
                    Integration.provider == provider,
                    Integration.is_active == True,
                ).order_by(Integration.updated_at.desc()).limit(1)
            )
            integ = result.scalars().first()
            if not integ or not integ.refresh_token:
                return None
            # Unless forced, bail if another caller committed a fresh token while
            # we were acquiring the lock. (force_refresh = the token was just
            # rejected by the provider, so a "not-expired" token can't be trusted.)
            if not force_refresh:
                fresh = _valid_token_or_none(integ)
                if fresh is not None:
                    return fresh
            current_refresh_token = decrypt(integ.refresh_token)

        # 2) Refresh against the provider OUTSIDE any DB session/transaction.
        #    Retry TRANSIENT failures (5xx/timeout/network) — the provider never
        #    processed the request so the RT is still valid. invalid_grant is the
        #    ONLY signal the RT is actually spent/dead.
        new_tokens = None
        for attempt in range(_REFRESH_TRANSIENT_RETRIES + 1):
            try:
                new_tokens = await refresher.refresh_access_token(current_refresh_token)
                break
            except Exception as e:
                if _is_invalid_grant(e):
                    # A concurrent caller may have rotated it (then ours is just
                    # stale, not dead) — re-read once before declaring it dead.
                    newest = await _fetch_current_integration(provider, user_id)
                    recovered = _valid_token_or_none(newest)
                    if recovered is not None:
                        logger.info("Recovered fresh token after invalid_grant",
                                    provider=provider, user_id=str(user_id))
                        return recovered
                    # Confirmed dead → break the spiral: deactivate + prompt
                    # reconnect so we stop hammering the token endpoint.
                    await _mark_needs_reconnect(
                        provider, user_id,
                        "refresh_token rejected (invalid_grant) — manual reconnect required",
                    )
                    return None
                if attempt < _REFRESH_TRANSIENT_RETRIES:
                    logger.warning("OAuth refresh transient error — retrying",
                                   provider=provider, user_id=str(user_id),
                                   attempt=attempt + 1, error=str(e)[:160])
                    await asyncio.sleep(_REFRESH_RETRY_DELAY_S * (attempt + 1))
                    continue
                # Transient but out of retries — do NOT mark dead (RT still valid),
                # just fail this call; a later call will try again.
                logger.warning("OAuth refresh failed (transient, retries exhausted)",
                               provider=provider, user_id=str(user_id), error=str(e)[:160])
                return None

        # 3) Persist the rotated tokens. The old RT is now consumed, so this write
        #    MUST land — retry it. If it ultimately fails the stored RT is dead, so
        #    flag needs-reconnect (still return the new access token so THIS
        #    request succeeds). On success, also clear stale error state.
        write_ok = False
        for attempt in range(_WRITE_RETRIES + 1):
            try:
                async with session_manager.get_session() as s:
                    result = await s.execute(
                        select(Integration).where(
                            Integration.user_id == user_id,
                            Integration.provider == provider,
                            Integration.is_active == True,
                        ).order_by(Integration.updated_at.desc()).limit(1)
                    )
                    integ = result.scalars().first()
                    if integ:
                        integ.access_token = encrypt(new_tokens.access_token)
                        if new_tokens.refresh_token:
                            integ.refresh_token = encrypt(new_tokens.refresh_token)
                        integ.token_expiry = new_tokens.token_expiry or (
                            datetime.now(timezone.utc) + timedelta(hours=1)
                        )
                        integ.error_count = 0
                        integ.last_error = None
                        integ.last_error_at = None
                write_ok = True
                break
            except Exception as e:
                if attempt < _WRITE_RETRIES:
                    await asyncio.sleep(_REFRESH_RETRY_DELAY_S * (attempt + 1))
                    continue
                logger.error(
                    "CRITICAL: failed to persist rotated token after provider refresh — "
                    "stored refresh_token is now stale; flagging needs-reconnect",
                    provider=provider, user_id=str(user_id), error=str(e)[:160],
                )
        if write_ok:
            logger.info("OAuth token refreshed under lock", provider=provider, user_id=str(user_id))
        else:
            await _mark_needs_reconnect(
                provider, user_id,
                "rotated token could not be persisted — manual reconnect required",
            )
        return new_tokens.access_token
    finally:
        if acquired:
            await _release_lock(redis_client, key, lock_value)
