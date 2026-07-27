"""
Workflow Suggestion Service — analyzes a user's connected integration data
and auto-generates 1-2 workflow suggestions via Claude.

Triggered once when a user reaches 2+ connected integrations.
"""

import asyncio
import base64
import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import httpx
import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from services.slack_client import get_slack_client
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.llm import get_spec, llm
from services.llm.types import ToolCall
from db.models import Integration, User, Workflow
from services.encryption_service import decrypt, encrypt
from services.oauth.google_oauth import GoogleOAuthProvider

logger = structlog.get_logger()

# Fetch limits
GMAIL_FETCH_LIMIT = 20
SLACK_CHANNELS_LIMIT = 5
SLACK_MESSAGES_PER_CHANNEL = 20
CALENDAR_DAYS_AHEAD = 7

SYSTEM_PROMPT = """You are an AI workflow designer. Based on the user's work data (emails, calendar, Slack messages, Notion pages), suggest 1-2 automated workflows that would genuinely save them time.

Return a JSON array of workflow objects. For each workflow, return a JSON object with:
- name: short descriptive name (e.g. "Weekly Progress Report", "Meeting Prep Brief")
- description: 1-2 sentence description
- prompt: detailed instructions for what the workflow should do, referencing specific meetings, projects, people, or pages you found in their data
- when_type: "schedule" or "trigger"
- schedule_interval: number (if schedule)
- schedule_unit: "day" or "week" (if schedule)
- schedule_day_of_week: "MO"|"TU"|"WE"|"TH"|"FR"|"SA"|"SU" or null (if schedule, only for weekly)
- schedule_time: "HH:MM" 24h format (if schedule)
- trigger_integration: "google_calendar"|"gmail"|"slack" or null (if trigger)
- trigger_event: "before_event"|"new_email"|"new_message" or null (if trigger)
- trigger_minutes_before: number or null (if trigger)
- integrations_used: list of provider strings ["google", "slack", "notion", "linear", "github"]

Return a JSON array of 1-2 workflow objects. Only suggest workflows that use integrations the user actually has connected. Make the prompts specific and actionable based on the actual data you see."""


# ---------------------------------------------------------------------------
# Integration helpers (mirrors skill_learning_service / hub_router patterns)
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


async def _get_google_token(
    session: AsyncSession, integration: Integration
) -> Optional[str]:
    """Decrypt Google access token, refreshing if expired."""
    try:
        if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
            if not integration.refresh_token:
                return None
            try:
                new_tokens = await GoogleOAuthProvider().refresh_access_token(
                    decrypt(integration.refresh_token)
                )
                integration.access_token = encrypt(new_tokens.access_token)
                if new_tokens.refresh_token:
                    integration.refresh_token = encrypt(new_tokens.refresh_token)
                integration.token_expiry = new_tokens.token_expiry
                await session.flush()
                return new_tokens.access_token
            except Exception as e:
                logger.error("Google token refresh failed during workflow suggestion", error=str(e))
                return None
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Google token for workflow suggestion", error=str(e))
        return None


async def _get_slack_token(integration: Integration) -> Optional[str]:
    try:
        token = integration.access_token or integration.bot_token
        return decrypt(token)
    except Exception as e:
        logger.error("Failed to decrypt Slack token for workflow suggestion", error=str(e))
        return None


async def _get_notion_token(integration: Integration) -> Optional[str]:
    try:
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Notion token for workflow suggestion", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

async def _fetch_sent_emails(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch recent sent emails for workflow analysis."""
    integration = await _get_integration(session, user_id, "google")
    if not integration:
        return []

    access_token = await _get_google_token(session, integration)
    if not access_token:
        return []

    try:
        def _fetch_all_sync():
            creds = Credentials(token=access_token)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)

            results = service.users().messages().list(
                userId="me", maxResults=GMAIL_FETCH_LIMIT, q="in:sent"
            ).execute()

            msg_refs = results.get("messages", [])
            if not msg_refs:
                return []

            emails = []
            for ref in msg_refs:
                msg = service.users().messages().get(
                    userId="me", id=ref["id"], format="full",
                ).execute()

                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }

                body = ""
                payload = msg.get("payload", {})
                if payload.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
                elif payload.get("parts"):
                    for part in payload["parts"]:
                        if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                            body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                            break

                emails.append({
                    "to": headers.get("to", ""),
                    "subject": headers.get("subject", ""),
                    "date": headers.get("date", ""),
                    "body": body[:2000],
                })
            return emails

        emails = await asyncio.to_thread(_fetch_all_sync)
        logger.info("Fetched sent emails for workflow suggestion", count=len(emails), user_id=str(user_id))
        return emails

    except Exception as e:
        logger.error("Failed to fetch sent emails for workflow suggestion", error=str(e))
        return []


async def _fetch_calendar_events(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch today's events + upcoming week's events."""
    integration = await _get_integration(session, user_id, "google")
    if not integration:
        return []

    access_token = await _get_google_token(session, integration)
    if not access_token:
        return []

    try:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        now = datetime.now(timezone.utc)
        end_date = now + timedelta(days=CALENDAR_DAYS_AHEAD)

        results = await asyncio.to_thread(
            service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=end_date.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=30,
            ).execute
        )

        events = []
        for event in results.get("items", []):
            start = event.get("start", {})
            end = event.get("end", {})
            attendees = event.get("attendees", [])
            events.append({
                "title": event.get("summary", "(No title)"),
                "start": start.get("dateTime") or start.get("date", ""),
                "end": end.get("dateTime") or end.get("date", ""),
                "location": event.get("location", ""),
                "description": (event.get("description") or "")[:500],
                "attendees": [a.get("email", "") for a in attendees[:10]],
                "recurring": bool(event.get("recurringEventId")),
            })

        logger.info("Fetched calendar events for workflow suggestion", count=len(events), user_id=str(user_id))
        return events

    except Exception as e:
        logger.error("Failed to fetch calendar events for workflow suggestion", error=str(e))
        return []


async def _fetch_slack_messages(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch user's recent Slack messages from top channels."""
    integration = await _get_integration(session, user_id, "slack")
    if not integration:
        return []

    token = await _get_slack_token(integration)
    if not token:
        return []

    try:
        client = get_slack_client(token=token)

        auth_resp = await client.auth_test()
        slack_user_id = auth_resp.get("user_id")
        if not slack_user_id:
            return []

        channels_resp = await client.conversations_list(
            limit=SLACK_CHANNELS_LIMIT,
            types="public_channel,private_channel",
        )
        channels = channels_resp.get("channels", [])

        all_messages = []
        for channel in channels:
            try:
                history = await client.conversations_history(
                    channel=channel["id"],
                    limit=SLACK_MESSAGES_PER_CHANNEL,
                )
                for msg in history.get("messages", []):
                    if msg.get("user") == slack_user_id and msg.get("text"):
                        all_messages.append({
                            "channel": channel.get("name", "unknown"),
                            "text": msg["text"][:1500],
                            "ts": msg.get("ts", ""),
                        })
            except Exception:
                continue

        logger.info("Fetched Slack messages for workflow suggestion", count=len(all_messages), user_id=str(user_id))
        return all_messages

    except Exception as e:
        logger.error("Failed to fetch Slack messages for workflow suggestion", error=str(e))
        return []


async def _fetch_notion_data(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch Notion databases and recent pages the user works with."""
    integration = await _get_integration(session, user_id, "notion")
    if not integration:
        return []

    access_token = await _get_notion_token(integration)
    if not access_token:
        return []

    try:
        from tools.notion_tool import notion_mcp_search
        pages = await notion_mcp_search(access_token, query="", page_size=10)
        logger.info("Fetched Notion data for workflow suggestion", count=len(pages), user_id=str(user_id))
        return pages
    except Exception as e:
        logger.error("Failed to fetch Notion data for workflow suggestion", error=str(e))
        return []


async def _fetch_linear_issues(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch recent Linear issues assigned to the user."""
    integration = await _get_integration(session, user_id, "linear")
    if not integration:
        return []

    try:
        token = decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Linear token for workflow suggestion", error=str(e))
        return []

    try:
        from tools.linear_tool import _linear_gql

        query = """
        query {
            viewer {
                assignedIssues(first: 15, orderBy: updatedAt) {
                    nodes {
                        identifier
                        title
                        state { name }
                        priority
                        team { name }
                        updatedAt
                    }
                }
            }
        }
        """
        data = await _linear_gql(token, query)
        assigned = (data.get("viewer") or {}).get("assignedIssues")
        nodes = assigned.get("nodes", []) if assigned else []

        issues = [
            {
                "identifier": n.get("identifier", ""),
                "title": n.get("title", ""),
                "status": (n.get("state") or {}).get("name", ""),
                "priority": n.get("priority", 0),
                "team": (n.get("team") or {}).get("name", ""),
            }
            for n in nodes
        ]

        logger.info("Fetched Linear issues for workflow suggestion", count=len(issues), user_id=str(user_id))
        return issues

    except Exception as e:
        logger.error("Failed to fetch Linear issues for workflow suggestion", error=str(e))
        return []


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

def _strip_analysis(text: str) -> str:
    """Strip <analysis>...</analysis> scratchpad blocks from Claude's response."""
    return re.sub(r"<analysis>[\s\S]*?</analysis>", "", text).strip()


def _extract_json_array(text: str) -> Optional[list]:
    """Safely extract a JSON array from Claude's response, handling markdown fences."""
    # Strip analysis scratchpad before parsing
    text = _strip_analysis(text).strip()
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fence
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(1).strip())
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try finding first [ ... ] block
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return None


async def _analyze_with_claude(
    data_text: str, connected_providers: list[str]
) -> tuple[list[dict], object]:
    """Send collected data to Claude and parse workflow suggestions.
    Returns (suggestions, usage)."""
    user_message = (
        f"Connected integrations: {', '.join(connected_providers)}\n\n"
        f"## User's Work Data\n\n{data_text}"
    )

    suggestion_tool = {
        "name": "save_workflow_suggestions",
        "description": "Save the suggested workflows.",
        "input_schema": {
            "type": "object",
            "properties": {
                "workflows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "description": {"type": "string"},
                            "prompt": {"type": "string"},
                            "schedule_time": {"type": "string"},
                            "schedule_unit": {"type": "string", "enum": ["day", "week"]},
                            "schedule_interval": {"type": "integer"},
                            "schedule_day_of_week": {"type": ["string", "null"]},
                            "integrations_used": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["name", "prompt"],
                    },
                },
            },
            "required": ["workflows"],
        },
    }

    response = await llm.complete(
        get_spec("workflow_suggestions"),
        messages=[{"role": "user", "content": user_message}],
        system=SYSTEM_PROMPT,
        tools=[suggestion_tool],
        force_tool="save_workflow_suggestions",
        timeout=300.0,
    )

    usage = getattr(response.raw, "usage", None) or response.usage
    workflows = []
    for tc in response.tool_calls:
        if tc.name == "save_workflow_suggestions":
            workflows = tc.input.get("workflows", [])
            break

    if not workflows:
        logger.error("No workflow suggestions returned from Claude")
        return [], usage

    # Validate and cap at 2
    valid = []
    for w in workflows[:2]:
        if not isinstance(w, dict):
            continue
        if not w.get("name") or not w.get("prompt"):
            continue
        # Only keep integrations that are actually connected
        w["integrations_used"] = [
            p for p in (w.get("integrations_used") or [])
            if p in connected_providers
        ]
        valid.append(w)

    return valid, usage


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

async def generate_suggestions(
    user_id: UUID,
    session: AsyncSession,
) -> None:
    """
    Fetch data from all connected integrations, send to Claude,
    and create inactive Workflow records.
    """
    # Load user
    result = await session.execute(
        select(User).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        logger.error("User not found for workflow suggestion", user_id=str(user_id))
        return

    if user.auto_workflows_generated:
        logger.info("Auto workflows already generated, skipping", user_id=str(user_id))
        return

    # Determine connected providers
    integrations_result = await session.execute(
        select(Integration.provider).where(
            Integration.user_id == user_id,
            Integration.is_active == True,
        )
    )
    connected_providers = [row[0] for row in integrations_result.all()]

    if len(connected_providers) < 2:
        logger.info("Not enough integrations for workflow suggestion", count=len(connected_providers))
        return

    # Fetch data from all connected integrations in parallel (resilient — failures return [])
    fetch_tasks = {}
    if "google" in connected_providers:
        fetch_tasks["emails"] = _fetch_sent_emails(session, user_id)
        fetch_tasks["calendar"] = _fetch_calendar_events(session, user_id)
    if "slack" in connected_providers:
        fetch_tasks["slack"] = _fetch_slack_messages(session, user_id)
    if "notion" in connected_providers:
        fetch_tasks["notion"] = _fetch_notion_data(session, user_id)
    if "linear" in connected_providers:
        fetch_tasks["linear"] = _fetch_linear_issues(session, user_id)

    # Run all fetches concurrently
    keys = list(fetch_tasks.keys())
    results = await asyncio.gather(*fetch_tasks.values(), return_exceptions=True)
    fetched_data = {}
    for key, res in zip(keys, results):
        if isinstance(res, Exception):
            logger.error("Fetch failed for workflow suggestion", source=key, error=str(res))
            fetched_data[key] = []
        else:
            fetched_data[key] = res

    # Build text summary for Claude
    content_parts = []

    emails = fetched_data.get("emails", [])
    if emails:
        email_text = "\n\n---\n\n".join(
            f"**To:** {e['to']}\n**Subject:** {e['subject']}\n**Date:** {e['date']}\n\n{e['body']}"
            for e in emails
        )
        content_parts.append(f"### Sent Emails ({len(emails)} messages)\n\n{email_text}")

    calendar = fetched_data.get("calendar", [])
    if calendar:
        cal_text = "\n".join(
            f"- **{ev['title']}** | {ev['start']} → {ev['end']} | "
            f"Attendees: {', '.join(ev['attendees'][:5]) or 'none'} | "
            f"{'Recurring' if ev['recurring'] else 'One-time'}"
            + (f" | {ev['description']}" if ev['description'] else "")
            for ev in calendar
        )
        content_parts.append(f"### Calendar Events (next {CALENDAR_DAYS_AHEAD} days, {len(calendar)} events)\n\n{cal_text}")

    slack = fetched_data.get("slack", [])
    if slack:
        slack_text = "\n".join(
            f"- **#{m['channel']}**: {m['text']}" for m in slack
        )
        content_parts.append(f"### Slack Messages ({len(slack)} messages)\n\n{slack_text}")

    notion = fetched_data.get("notion", [])
    if notion:
        notion_text = "\n".join(
            f"- [{n['type']}] **{n['title']}** (last edited: {n['last_edited']})"
            for n in notion
        )
        content_parts.append(f"### Notion Pages & Databases ({len(notion)} items)\n\n{notion_text}")

    linear = fetched_data.get("linear", [])
    if linear:
        linear_text = "\n".join(
            f"- **{i['identifier']}** {i['title']} | {i['status']} | Team: {i['team']}"
            for i in linear
        )
        content_parts.append(f"### Linear Issues ({len(linear)} assigned)\n\n{linear_text}")

    if not content_parts:
        logger.warning("No data fetched from any integration, skipping workflow suggestion", user_id=str(user_id))
        # Still mark as generated so we don't retry endlessly
        await session.execute(
            update(User).where(User.id == user_id).values(auto_workflows_generated=True)
        )
        return

    data_text = "\n\n".join(content_parts)

    # Analyze with Claude
    workflow_suggestions, usage = await _analyze_with_claude(data_text, connected_providers)

    # Log usage (fire-and-forget)
    if usage:
        from services.usage_service import log_usage
        log_usage(user_id, get_spec("workflow_suggestions").model, "workflow_suggestions", usage)

    if not workflow_suggestions:
        logger.warning("Claude returned no valid workflow suggestions", user_id=str(user_id))
        await session.execute(
            update(User).where(User.id == user_id).values(auto_workflows_generated=True)
        )
        return

    # Create Workflow records
    today = date.today()
    for w in workflow_suggestions:
        workflow = Workflow(
            user_id=user_id,
            name=w.get("name", "Suggested Workflow"),
            description=w.get("description"),
            prompt=w.get("prompt", ""),
            when_type=w.get("when_type", "schedule"),
            # Schedule fields
            schedule_start_date=today if w.get("when_type") == "schedule" else None,
            schedule_interval=w.get("schedule_interval", 1) if w.get("when_type") == "schedule" else None,
            schedule_unit=w.get("schedule_unit", "day") if w.get("when_type") == "schedule" else None,
            schedule_day_of_week=w.get("schedule_day_of_week") if w.get("when_type") == "schedule" else None,
            schedule_time=w.get("schedule_time", "09:00") if w.get("when_type") == "schedule" else None,
            # Trigger fields
            trigger_integration=w.get("trigger_integration") if w.get("when_type") == "trigger" else None,
            trigger_event=w.get("trigger_event") if w.get("when_type") == "trigger" else None,
            trigger_minutes_before=w.get("trigger_minutes_before") if w.get("when_type") == "trigger" else None,
            integrations_used=w.get("integrations_used", []),
            status="inactive",
        )
        session.add(workflow)
        logger.info(
            "Created suggested workflow",
            name=w.get("name"),
            when_type=w.get("when_type"),
            user_id=str(user_id),
        )

    # Mark user as having had auto-workflows generated
    await session.execute(
        update(User).where(User.id == user_id).values(auto_workflows_generated=True)
    )

    logger.info("Auto workflow suggestions complete", count=len(workflow_suggestions), user_id=str(user_id))
