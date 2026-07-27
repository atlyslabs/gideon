"""Admin alerts for integration-health events.

Currently: when a user's OAuth integration is confirmed dead and deactivated
(needs a manual reconnect), Gideon DMs a configured admin so the breakage is
caught proactively instead of surfacing only when a workflow next fails.

The DM is sent as GIDEON itself (the `GIDEON_BOT_TOKEN` bot identity), not via
any user's per-user Slack OAuth token. Everything here is fire-and-forget and
never raises into the caller.
"""

import asyncio
import os
from uuid import UUID

import structlog
from slack_sdk.web.async_client import AsyncWebClient
from services.slack_client import get_slack_client
from sqlalchemy import select

from config import GIDEON_ADMIN_ALERT_EMAIL, GIDEON_BOT_TOKEN
from db.database import session_manager
from db.models import User, Workflow, WorkflowRun
from services.delegation_service import SLACK_ID_OVERRIDES

logger = structlog.get_logger()

_GIDEON_APP_URL = os.environ.get("GIDEON_APP_URL", "http://localhost:3000")


def _slack_escape(s: str) -> str:
    """Escape Slack's three control chars in user-controlled text so a value like
    `Report <prod>` or an error containing `<int>` isn't parsed as a mention/link.
    Must run on the raw field BEFORE it's embedded in the mrkdwn message."""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

# Providers whose death triggers an admin DM. Notion has the rotating
# single-use refresh-token spiral that prompted this; add others as needed.
_ALERTED_PROVIDERS = {"notion"}

# Strong refs so fire-and-forget tasks aren't GC'd mid-flight (ASYNC300).
_alert_tasks: set[asyncio.Task] = set()
_workflow_alert_tasks: set[asyncio.Task] = set()


def alert_integration_dead(provider: str, user_id: UUID, reason: str) -> None:
    """Fire-and-forget: DM the admin (as Gideon) that `user_id`'s `provider`
    integration went dead and needs a reconnect.

    Safe to call from anywhere — never blocks, never raises. No-op for providers
    not in `_ALERTED_PROVIDERS` and when no Gideon bot token is configured.
    """
    if provider not in _ALERTED_PROVIDERS or not GIDEON_BOT_TOKEN:
        return
    task = asyncio.create_task(_send_dead_integration_dm(provider, user_id, reason))
    _alert_tasks.add(task)
    task.add_done_callback(_alert_tasks.discard)


async def _resolve_admin_slack_id(client: AsyncWebClient) -> str | None:
    """Resolve the admin's Slack user id, preferring the static override map."""
    if GIDEON_ADMIN_ALERT_EMAIL in SLACK_ID_OVERRIDES:
        return SLACK_ID_OVERRIDES[GIDEON_ADMIN_ALERT_EMAIL]
    try:
        resp = await client.users_lookupByEmail(email=GIDEON_ADMIN_ALERT_EMAIL)
        if resp.get("ok"):
            return resp["user"]["id"]
    except Exception as e:
        logger.warning("integration-dead alert: admin Slack lookup failed", error_type=type(e).__name__)
    return None


async def _affected_user_label(user_id: UUID) -> str:
    """Human label for the affected user (name > email > uuid). Read-only."""
    try:
        async with session_manager.get_session() as s:
            u = (await s.execute(select(User).where(User.id == user_id))).scalars().first()
        if u:
            return u.name or u.email or str(user_id)
    except Exception as e:
        logger.warning("integration-dead alert: affected-user lookup failed", error_type=type(e).__name__)
    return str(user_id)


async def _send_dead_integration_dm(provider: str, user_id: UUID, reason: str) -> None:
    try:
        client = get_slack_client(token=GIDEON_BOT_TOKEN)
        admin_slack_id = await _resolve_admin_slack_id(client)
        if not admin_slack_id:
            logger.warning("integration-dead alert: could not resolve admin Slack id")
            return

        who = await _affected_user_label(user_id)
        label = provider.title()  # provider is our own constant, not user input
        # Slack mrkdwn directly, no markdown converter — keeps user fields literal.
        text = (
            f":warning: *{label} token needs reconnect*\n"
            f"*{_slack_escape(who)}*'s {label} integration just went dead and was auto-deactivated, "
            f"so it stopped spamming the dead token. They need to reconnect {label} in "
            f"Gideon (Integrations) to restore it.\n"
            f"_Reason: {_slack_escape(reason[:200])}_"
        )

        opened = await client.conversations_open(users=admin_slack_id)
        channel_id = opened.get("channel", {}).get("id")
        if not channel_id:
            logger.warning("integration-dead alert: conversations_open returned no channel")
            return
        await client.chat_postMessage(channel=channel_id, text=text)
        logger.info(
            "integration-dead admin alert sent",
            provider=provider, affected_user_id=str(user_id),
        )
    except Exception as e:
        logger.error("integration-dead alert failed", provider=provider, error=str(e)[:160])


def alert_workflow_failed(run_id: UUID, error: str) -> None:
    """Fire-and-forget: DM the admin (as Gideon) that a workflow run failed —
    for ANY user, so the admin sees every failure, not just their own. Hooked at
    the single failure chokepoint (`_fail_run`). Never blocks/raises; no-op when
    no Gideon bot token is configured. Workflow name + owner are resolved from
    the run id so callers only pass the run + error."""
    if not GIDEON_BOT_TOKEN:
        return
    task = asyncio.create_task(_send_workflow_failed_dm(run_id, error))
    _workflow_alert_tasks.add(task)
    task.add_done_callback(_workflow_alert_tasks.discard)


async def _send_workflow_failed_dm(run_id: UUID, error: str) -> None:
    try:
        async with session_manager.get_session() as s:
            run = (
                await s.execute(select(WorkflowRun).where(WorkflowRun.id == run_id))
            ).scalars().first()
            if not run:
                return
            workflow = (
                await s.execute(select(Workflow).where(Workflow.id == run.workflow_id))
            ).scalars().first()
            name = (workflow.name if workflow else None) or "Untitled workflow"
            user_id = run.user_id
            workflow_id = run.workflow_id

        client = get_slack_client(token=GIDEON_BOT_TOKEN)
        admin_slack_id = await _resolve_admin_slack_id(client)
        if not admin_slack_id:
            logger.warning("workflow-failed alert: could not resolve admin Slack id")
            return

        who = await _affected_user_label(user_id)
        run_url = f"{_GIDEON_APP_URL}/workflows/{workflow_id}/runs/{run_id}"
        # Written in Slack mrkdwn directly and sent WITHOUT the markdown converter:
        # the user-controlled fields would otherwise be interpreted as markdown
        # (e.g. `[x](url)` -> a real Slack link), so we keep them literal. Fields
        # are control-char escaped via _slack_escape.
        text = (
            f":rotating_light: *Workflow failed*\n"
            f"*{_slack_escape(name)}* ({_slack_escape(who)}) failed.\n"
            f"_{_slack_escape((error or 'Unknown error')[:300])}_\n"
            f"{run_url}"
        )
        opened = await client.conversations_open(users=admin_slack_id)
        channel_id = opened.get("channel", {}).get("id")
        if not channel_id:
            logger.warning("workflow-failed alert: conversations_open returned no channel")
            return
        await client.chat_postMessage(channel=channel_id, text=text)
        logger.info("workflow-failed admin alert sent", run_id=str(run_id), affected_user_id=str(user_id))
    except Exception as e:
        logger.error("workflow-failed alert failed", run_id=str(run_id), error=str(e)[:160])
