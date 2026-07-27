"""
Trigger Listener — polls for trigger-based workflow events and executes
the associated workflows.

Supported triggers:
  - google_calendar / before_event : calendar event about to start
  - gmail / new_email              : new emails received
  - slack / new_message            : new Slack messages

Designed to run as a long-lived asyncio task started from the FastAPI lifespan.
"""

import asyncio
import base64
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from services.slack_client import get_slack_client
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import session_manager
from db.models import Integration, Workflow, WorkflowRun
from services.encryption_service import decrypt
from services.oauth.token_refresh import get_valid_access_token
from services.workflow_execution_service import execute_workflow

logger = structlog.get_logger()

# Poll interval: 2 minutes
POLL_INTERVAL_SECONDS = 120

# Dedup window: skip if a run exists for this workflow within the last 30 min
DEDUP_WINDOW_MINUTES = 30

# In-memory tracking for email/slack last-check timestamps per workflow_id
_last_email_check: dict[str, float] = {}   # workflow_id_str -> epoch
_last_slack_check: dict[str, float] = {}   # workflow_id_str -> epoch


# ---------------------------------------------------------------------------
# Integration helpers (reuse patterns from workflow_execution_service)
# ---------------------------------------------------------------------------

async def _get_integration(
    session: AsyncSession, user_id: UUID, provider: str
) -> Optional[Integration]:
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
            Integration.is_active == True,
        )
    )
    return result.scalar_one_or_none()


# Google tokens are resolved via the shared lock-coordinated helper
# `services.oauth.token_refresh.get_valid_access_token("google", user_id)`.
# It manages its own short-lived DB sessions + a Redis lock, records the
# integration error and flips `is_active=False` on a confirmed-dead refresh
# token (invalid_grant), which breaks the retry-every-cycle spiral the old
# inline `GoogleOAuthProvider().refresh_access_token` here used to cause.
# Do NOT call it while holding an open DB session/transaction.


async def _get_slack_token(integration: Integration) -> Optional[str]:
    try:
        token = integration.bot_token or integration.access_token
        return decrypt(token)
    except Exception as e:
        logger.error("Trigger: failed to decrypt Slack token", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Dedup helper — check if workflow already ran recently
# ---------------------------------------------------------------------------

async def _has_recent_run(
    session: AsyncSession, workflow_id: UUID, minutes: int = DEDUP_WINDOW_MINUTES
) -> bool:
    """Return True if a WorkflowRun exists for this workflow within the last N minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    result = await session.execute(
        select(func.count(WorkflowRun.id)).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.triggered_at >= cutoff,
        )
    )
    count = result.scalar_one()
    return count > 0


# ---------------------------------------------------------------------------
# Fetch active trigger workflows
# ---------------------------------------------------------------------------

async def _get_trigger_workflows(
    session: AsyncSession,
    trigger_integration: str,
    trigger_event: str,
) -> list[Workflow]:
    # Auto-deactivate workflows whose `active_until` has passed.
    now = datetime.now(timezone.utc)
    await session.execute(
        update(Workflow)
        .where(
            Workflow.status == "active",
            Workflow.active_until.isnot(None),
            Workflow.active_until <= now,
        )
        .values(status="inactive")
    )

    result = await session.execute(
        select(Workflow).where(
            Workflow.when_type == "trigger",
            Workflow.status == "active",
            Workflow.trigger_integration == trigger_integration,
            Workflow.trigger_event == trigger_event,
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Execute a triggered workflow in its own session
# ---------------------------------------------------------------------------

async def _execute_triggered_workflow(workflow_id: UUID, user_id: UUID) -> None:
    """Run execute_workflow in a fresh session so failures are isolated."""
    async with session_manager.get_session() as session:
        try:
            await execute_workflow(workflow_id, user_id, session)
            logger.info(
                "Trigger: workflow executed",
                workflow_id=str(workflow_id),
                user_id=str(user_id),
            )
        except Exception as e:
            logger.error(
                "Trigger: workflow execution failed",
                workflow_id=str(workflow_id),
                error=str(e),
            )


# ---------------------------------------------------------------------------
# Calendar triggers
# ---------------------------------------------------------------------------

async def _check_calendar_triggers() -> None:
    """
    For each active calendar-trigger workflow, fetch upcoming Google Calendar
    events and fire the workflow if an event starts within trigger_minutes_before.
    """
    # Short-lived session: fetch the workflow list, then release the connection
    # before iterating. Per-workflow Google I/O must NOT run inside this session.
    async with session_manager.get_session() as session:
        workflows = await _get_trigger_workflows(session, "google_calendar", "before_event")
    if not workflows:
        return

    logger.debug("Trigger: checking calendar triggers", count=len(workflows))

    for workflow in workflows:
        try:
            await _process_calendar_workflow(workflow)
        except Exception as e:
            logger.error(
                "Trigger: calendar check failed for workflow",
                workflow_id=str(workflow.id),
                error=str(e),
            )


async def _process_calendar_workflow(workflow: Workflow) -> None:
    minutes_before = workflow.trigger_minutes_before or 15
    trigger_filter = workflow.trigger_filter or {}

    # Skip if recently run — short session, released before any external I/O
    async with session_manager.get_session() as session:
        if await _has_recent_run(session, workflow.id):
            return

    # Google token via the shared refresh helper (manages its own sessions;
    # returns None when the integration is missing or needs reconnect).
    access_token = await get_valid_access_token("google", workflow.user_id)
    if not access_token:
        return

    # Fetch events in the upcoming window: now -> now + minutes_before + 5 min buffer
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(minutes=minutes_before + 5)

    try:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        calendar_id = trigger_filter.get("calendar", "primary")
        if calendar_id == "all":
            calendar_id = "primary"

        results = await asyncio.to_thread(
            service.events().list(
                calendarId=calendar_id,
                timeMin=now.isoformat(),
                timeMax=window_end.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=20,
            ).execute
        )

        events = results.get("items", [])
    except Exception as e:
        logger.error(
            "Trigger: failed to fetch calendar events",
            workflow_id=str(workflow.id),
            error=str(e),
        )
        return

    if not events:
        return

    # Check if any event starts within the trigger_minutes_before window
    for event in events:
        start_str = event.get("start", {}).get("dateTime")
        if not start_str:
            # All-day events don't have dateTime — skip
            continue

        event_start = datetime.fromisoformat(start_str)
        diff_minutes = (event_start - now).total_seconds() / 60

        if 0 <= diff_minutes <= minutes_before:
            # Event is about to start — trigger the workflow
            logger.info(
                "Trigger: calendar event matched",
                workflow_id=str(workflow.id),
                event_summary=event.get("summary", ""),
                minutes_until_start=round(diff_minutes, 1),
            )
            await _execute_triggered_workflow(workflow.id, workflow.user_id)
            # Only fire once per poll cycle per workflow
            break


# ---------------------------------------------------------------------------
# Email triggers
# ---------------------------------------------------------------------------

async def _check_email_triggers() -> None:
    """
    For each active email-trigger workflow, check Gmail for new emails
    since the last check and fire the workflow if any match.
    """
    # Short-lived session: fetch the workflow list, then release the connection
    # before iterating. Per-workflow Gmail I/O must NOT run inside this session.
    async with session_manager.get_session() as session:
        workflows = await _get_trigger_workflows(session, "gmail", "new_email")
    if not workflows:
        return

    logger.debug("Trigger: checking email triggers", count=len(workflows))

    for workflow in workflows:
        try:
            await _process_email_workflow(workflow)
        except Exception as e:
            logger.error(
                "Trigger: email check failed for workflow",
                workflow_id=str(workflow.id),
                error=str(e),
            )


async def _process_email_workflow(workflow: Workflow) -> None:
    wf_key = str(workflow.id)
    trigger_filter = workflow.trigger_filter or {}

    # Skip if recently run — short session, released before any external I/O
    async with session_manager.get_session() as session:
        if await _has_recent_run(session, workflow.id):
            return

    # Google token via the shared refresh helper (manages its own sessions;
    # returns None when the integration is missing or needs reconnect).
    access_token = await get_valid_access_token("google", workflow.user_id)
    if not access_token:
        return

    # Determine last check time — default to 5 minutes ago for first run
    last_check = _last_email_check.get(wf_key, time.time() - 300)

    # Build Gmail query: emails after last check timestamp
    after_epoch = int(last_check)
    query_parts = [f"after:{after_epoch}"]

    # Apply filter: specific sender
    from_filter = trigger_filter.get("from")
    if from_filter and from_filter != "all":
        query_parts.append(f"from:{from_filter}")

    # Label/category filter
    label_filter = trigger_filter.get("label")
    if label_filter:
        query_parts.append(f"label:{label_filter}")

    query_str = " ".join(query_parts)

    try:
        creds = Credentials(token=access_token)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        results = await asyncio.to_thread(
            service.users().messages().list(
                userId="me", maxResults=5, q=query_str,
            ).execute
        )

        messages = results.get("messages", [])
    except Exception as e:
        logger.error(
            "Trigger: failed to fetch emails",
            workflow_id=str(workflow.id),
            error=str(e),
        )
        return

    # Update last check regardless of results
    _last_email_check[wf_key] = time.time()

    if not messages:
        return

    logger.info(
        "Trigger: new emails found",
        workflow_id=str(workflow.id),
        count=len(messages),
    )
    await _execute_triggered_workflow(workflow.id, workflow.user_id)


# ---------------------------------------------------------------------------
# Slack triggers
# ---------------------------------------------------------------------------

async def _check_slack_triggers() -> None:
    """
    For each active Slack-trigger workflow, check for new messages
    since the last check and fire the workflow if any match.
    """
    # Short-lived session: fetch the workflow list, then release the connection
    # before iterating. Per-workflow Slack I/O must NOT run inside this session.
    async with session_manager.get_session() as session:
        workflows = await _get_trigger_workflows(session, "slack", "new_message")
    if not workflows:
        return

    logger.debug("Trigger: checking Slack triggers", count=len(workflows))

    for workflow in workflows:
        try:
            await _process_slack_workflow(workflow)
        except Exception as e:
            logger.error(
                "Trigger: Slack check failed for workflow",
                workflow_id=str(workflow.id),
                error=str(e),
            )


async def _process_slack_workflow(workflow: Workflow) -> None:
    wf_key = str(workflow.id)
    trigger_filter = workflow.trigger_filter or {}

    # All DB reads happen in a short-lived session that is released BEFORE any
    # Slack API call — holding a pool connection across external I/O (per
    # workflow, per tick) is exactly what CLAUDE.md rule #3 forbids.
    async with session_manager.get_session() as session:
        # Skip if recently run
        if await _has_recent_run(session, workflow.id):
            return

        # Get Slack integration + decrypt the token while the session is open.
        integration = await _get_integration(session, workflow.user_id, "slack")
        if not integration:
            return

        token = await _get_slack_token(integration)
    if not token:
        return

    # Determine last check time — default to 5 minutes ago for first run
    last_check = _last_slack_check.get(wf_key, time.time() - 300)
    oldest_ts = str(last_check)

    try:
        client = get_slack_client(token=token)

        channel_filter = trigger_filter.get("channel")
        dm_filter = trigger_filter.get("dm", False)

        found_new = False

        if channel_filter:
            # Check a specific channel
            try:
                history = await client.conversations_history(
                    channel=channel_filter,
                    oldest=oldest_ts,
                    limit=5,
                )
                if history.get("messages"):
                    found_new = True
            except Exception as e:
                logger.error(
                    "Trigger: failed to fetch Slack channel history",
                    channel=channel_filter,
                    error=str(e),
                )
        elif dm_filter:
            # Check DMs (IMs)
            try:
                conversations = await client.conversations_list(
                    types="im",
                    limit=10,
                )
                for conv in conversations.get("channels", []):
                    history = await client.conversations_history(
                        channel=conv["id"],
                        oldest=oldest_ts,
                        limit=3,
                    )
                    if history.get("messages"):
                        found_new = True
                        break
            except Exception as e:
                logger.error("Trigger: failed to fetch Slack DMs", error=str(e))
        else:
            # Default: check recent channels for any new messages
            try:
                conversations = await client.conversations_list(
                    types="public_channel,private_channel",
                    limit=10,
                )
                for conv in conversations.get("channels", []):
                    try:
                        history = await client.conversations_history(
                            channel=conv["id"],
                            oldest=oldest_ts,
                            limit=3,
                        )
                        if history.get("messages"):
                            found_new = True
                            break
                    except Exception:
                        continue
            except Exception as e:
                logger.error("Trigger: failed to list Slack channels", error=str(e))

    except Exception as e:
        logger.error(
            "Trigger: Slack check error",
            workflow_id=str(workflow.id),
            error=str(e),
        )
        return

    # Update last check regardless
    _last_slack_check[wf_key] = time.time()

    if not found_new:
        return

    logger.info(
        "Trigger: new Slack messages found",
        workflow_id=str(workflow.id),
    )
    await _execute_triggered_workflow(workflow.id, workflow.user_id)


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

async def trigger_listener_loop() -> None:
    """
    Long-lived loop that polls for trigger-based workflow events every 2 minutes.
    Started as an asyncio task from the FastAPI lifespan.
    """
    logger.info("Trigger listener started")

    # Small initial delay to let the app fully start
    await asyncio.sleep(10)

    while True:
        try:
            await _check_calendar_triggers()
        except Exception as e:
            logger.error("Trigger listener: calendar check error", error=str(e))

        try:
            await _check_email_triggers()
        except Exception as e:
            logger.error("Trigger listener: email check error", error=str(e))

        try:
            await _check_slack_triggers()
        except Exception as e:
            logger.error("Trigger listener: Slack check error", error=str(e))

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
