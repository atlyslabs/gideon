"""
Admin alerts — fire-and-forget Slack DMs for operational issues that need a
human (e.g. GitHub org OAuth approval revoked).

Dedup is Redis-based so we don't spam on every failing request during the
outage window. Tasks are kept in a module-level set so asyncio doesn't
garbage-collect them mid-send (ASYNC300).
"""

import os
import asyncio
from typing import Optional

import structlog
from services.slack_client import get_slack_client
from sqlalchemy import select

from db.database import AsyncPostgresSessionGideon
from db.models import Integration
from db.redis import get_redis
from services.encryption_service import decrypt

logger = structlog.get_logger()

# Slack user ID for the admin who should get operational alerts.
# Hardcoded (not secret) so it works in every environment without extra config.
ADMIN_ALERT_SLACK_USER_ID = os.environ.get("ADMIN_ALERT_SLACK_USER_ID", "")

_notification_tasks: set[asyncio.Task] = set()


def _discard_task(task: asyncio.Task) -> None:
    _notification_tasks.discard(task)


def schedule_admin_alert(alert_key: str, message: str, dedup_ttl_seconds: int = 86400) -> None:
    """Fire-and-forget an admin Slack DM. Deduped in Redis so repeated triggers
    inside `dedup_ttl_seconds` only send once.

    Args:
        alert_key: stable key for this alert type (e.g. 'github_sso_revoked').
            Used as the Redis dedup key.
        message: full Slack message body (markdown-ish supported).
        dedup_ttl_seconds: how long to suppress duplicate alerts. Default 24h.
    """
    task = asyncio.create_task(_send_admin_alert(alert_key, message, dedup_ttl_seconds))
    _notification_tasks.add(task)
    task.add_done_callback(_discard_task)


async def _send_admin_alert(alert_key: str, message: str, dedup_ttl_seconds: int) -> None:
    redis_key = f"admin_alert:{alert_key}"
    redis = None
    dedup_claimed = False
    try:
        redis = get_redis()
        claimed = await redis.set(redis_key, "1", nx=True, ex=dedup_ttl_seconds)
        if not claimed:
            logger.info("Admin alert suppressed by dedup", alert_key=alert_key)
            return
        dedup_claimed = True
    except Exception as e:
        logger.warning("Admin alert dedup check failed, proceeding", error=str(e))

    async def _release_dedup() -> None:
        """Release the dedup key so the next trigger can retry immediately
        instead of being suppressed for the full TTL on a failed send."""
        if not dedup_claimed or redis is None:
            return
        try:
            await redis.delete(redis_key)
        except Exception:
            pass

    bot_token = await _get_admin_bot_token()
    if not bot_token:
        logger.warning("No Slack bot token available for admin alert", alert_key=alert_key)
        await _release_dedup()
        return

    try:
        client = get_slack_client(token=bot_token)
        dm = await client.conversations_open(users=ADMIN_ALERT_SLACK_USER_ID)
        channel_id = dm["channel"]["id"]
        await client.chat_postMessage(
            channel=channel_id,
            text=message,
            unfurl_links=False,
        )
        logger.info("Admin alert sent", alert_key=alert_key)
    except Exception as e:
        logger.error("Admin alert send failed", alert_key=alert_key, error=str(e))
        await _release_dedup()


async def _get_admin_bot_token() -> Optional[str]:
    """Fetch any active Slack integration that actually has a bot_token.
    Short DB session, closed before the Slack API call (per DB guidelines).
    Filters in SQL so we don't pick a bot-token-less integration and give up
    while other integrations have a usable token.
    """
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        result = await session.execute(
            select(Integration)
            .where(
                Integration.provider == "slack",
                Integration.is_active == True,
                Integration.bot_token.isnot(None),
            )
            .limit(5)  # a few candidates in case one is corrupted / unreadable
        )
        integrations = list(result.scalars().all())

    for integration in integrations:
        try:
            token = decrypt(integration.bot_token)
            if token:
                return token
        except Exception:
            continue
    return None
