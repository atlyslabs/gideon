"""
Smart Onboarding Service — gathers data from Google, Slack, and Notion,
analyzes it with Claude to build a rich user profile, and generates
personalized workflow suggestions.

Triggered as a background task after the user connects all 3 required
integrations during onboarding.
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
from services.llm import get_spec, llm
from db.models import Integration, User, Workflow
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from services.encryption_service import decrypt, encrypt
from services.events_service import track_error
from services.oauth.google_oauth import GoogleOAuthProvider
from services.slack_client import get_slack_client
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = structlog.get_logger()

# Fetch limits
GMAIL_FETCH_LIMIT = 30
CALENDAR_DAYS_BACK = 14
SLACK_CHANNELS_LIMIT = 10
SLACK_MESSAGES_PER_CHANNEL = 30
NOTION_PAGE_LIMIT = 15

SYSTEM_PROMPT = """You are an AI onboarding analyst. You are given data from a new user's Google (Gmail + Calendar), Slack, and Notion accounts. Analyze this data to build a comprehensive profile of the user and suggest personalized workflows.

Return a JSON object with two keys:

1. "profile" — a markdown string containing:
   - Role and seniority level (inferred from email patterns, meeting types, etc.)
   - Team/department
   - Key collaborators (names + relationship context, e.g. "manager", "direct report", "cross-functional partner")
   - Recurring responsibilities (e.g. "runs weekly standup", "reviews PRs", "sends weekly report")
   - Projects they're working on (inferred from email subjects, Notion pages, Slack channels)
   - Communication patterns (e.g. "heavy email user", "prefers Slack", "sends updates every Monday")

2. "workflows" — an array of exactly 2 workflow objects, each with:
   - name: short descriptive name (e.g. "Morning Briefing", "Weekly Team Summary")
   - description: 1-2 sentence description
   - prompt: detailed instructions for what the workflow should do, referencing specific meetings, projects, people, or channels you found in their data
   - schedule_time: "HH:MM" 24h format
   - schedule_unit: "day" or "week"
   - schedule_interval: number (1 for daily, 1 for weekly)
   - schedule_day_of_week: "MO"|"TU"|"WE"|"TH"|"FR"|"SA"|"SU" or null (only for weekly)
   - integrations_used: list of provider strings from ["google", "slack", "notion", "linear"]

Make workflows specific and actionable based on actual patterns found. For example, if you see they have a daily standup at 10 AM, suggest a "Standup Prep" workflow at 9:45 AM. If they send weekly reports, suggest a "Weekly Report Draft" workflow.

Return ONLY the JSON object. No preamble, no analysis, no markdown fences, no extra text. Start your response with { and end with }."""


# ---------------------------------------------------------------------------
# Integration helpers (same pattern as workflow_suggestion_service.py)
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
        if (
            integration.token_expiry
            and datetime.now(timezone.utc) >= integration.token_expiry
        ):
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
                logger.error(
                    "Google token refresh failed during smart onboarding", error=str(e)
                )
                return None
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error(
            "Failed to decrypt Google token for smart onboarding", error=str(e)
        )
        return None


async def _get_slack_token(integration: Integration) -> Optional[str]:
    """Decrypt Slack token, preferring user token over bot token."""
    try:
        if integration.access_token:
            return decrypt(integration.access_token)
        if integration.bot_token:
            return decrypt(integration.bot_token)
        return None
    except Exception as e:
        logger.error("Failed to decrypt Slack token for smart onboarding", error=str(e))
        return None


async def _get_notion_token(integration: Integration) -> Optional[str]:
    try:
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error(
            "Failed to decrypt Notion token for smart onboarding", error=str(e)
        )
        return None


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------


async def _fetch_sent_emails_with_token(access_token: str, user_id: UUID) -> list[dict]:
    """Fetch recent sent emails (last 2 weeks, metadata format) using a pre-resolved token."""
    try:

        def _fetch_all_sync():
            creds = Credentials(token=access_token)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)

            results = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    maxResults=GMAIL_FETCH_LIMIT,
                    q="in:sent newer_than:14d",
                )
                .execute()
            )

            msg_refs = results.get("messages", [])
            if not msg_refs:
                return []

            # Batch fetch all messages in one HTTP roundtrip
            emails = []
            batch = service.new_batch_http_request()

            def _handle_msg(request_id, response, exception):
                if exception or not response:
                    return
                headers = {
                    h["name"].lower(): h["value"]
                    for h in response.get("payload", {}).get("headers", [])
                }
                emails.append(
                    {
                        "to": headers.get("to", ""),
                        "subject": headers.get("subject", ""),
                        "date": headers.get("date", ""),
                    }
                )

            for ref in msg_refs:
                batch.add(
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=ref["id"],
                        format="metadata",
                        metadataHeaders=["To", "Subject", "Date"],
                    ),
                    callback=_handle_msg,
                )
            batch.execute()
            return emails

        emails = await asyncio.to_thread(_fetch_all_sync)
        logger.info(
            "Fetched sent emails for smart onboarding",
            count=len(emails),
            user_id=str(user_id),
        )
        return emails

    except Exception as e:
        logger.error("Failed to fetch sent emails for smart onboarding", error=str(e))
        return []


async def _fetch_calendar_events_with_token(
    access_token: str, user_id: UUID
) -> list[dict]:
    """Fetch calendar events from the past 14 days using a pre-resolved token."""
    try:
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        now = datetime.now(timezone.utc)
        start_date = now - timedelta(days=CALENDAR_DAYS_BACK)

        results = await asyncio.to_thread(
            service.events()
            .list(
                calendarId="primary",
                timeMin=start_date.isoformat(),
                timeMax=now.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute
        )

        events = []
        for event in results.get("items", []):
            start = event.get("start", {})
            end_time = event.get("end", {})
            attendees = event.get("attendees", [])
            events.append(
                {
                    "title": event.get("summary", "(No title)"),
                    "start": start.get("dateTime") or start.get("date", ""),
                    "end": end_time.get("dateTime") or end_time.get("date", ""),
                    "attendees": [a.get("email", "") for a in attendees[:10]],
                    "recurring": bool(event.get("recurringEventId")),
                    "organizer": event.get("organizer", {}).get("email", ""),
                }
            )

        logger.info(
            "Fetched calendar events for smart onboarding",
            count=len(events),
            user_id=str(user_id),
        )
        return events

    except Exception as e:
        logger.error(
            "Failed to fetch calendar events for smart onboarding", error=str(e)
        )
        return []


async def _fetch_slack_channels_with_token(token: str, user_id: UUID) -> list[dict]:
    """Fetch user's top Slack channels with message counts using a pre-resolved token."""
    try:
        client = get_slack_client(token=token)

        # Get channels the user is in (no user= param, uses token identity)
        channels_resp = await client.users_conversations(
            types="public_channel,private_channel",
            limit=SLACK_CHANNELS_LIMIT,
        )
        channels = channels_resp.get("channels", [])

        channel_data = []
        for channel in channels:
            channel_info = {
                "name": channel.get("name", "unknown"),
                "purpose": channel.get("purpose", {}).get("value", ""),
                "num_members": channel.get("num_members", 0),
            }
            channel_data.append(channel_info)

        logger.info(
            "Fetched Slack channels for smart onboarding",
            count=len(channel_data),
            user_id=str(user_id),
        )
        return channel_data

    except Exception as e:
        logger.error(
            "Failed to fetch Slack channels for smart onboarding", error=str(e)
        )
        return []


async def _fetch_notion_pages_with_token(
    access_token: str, user_id: UUID
) -> list[dict]:
    """Fetch recent Notion pages and databases via MCP."""
    try:
        from tools.notion_tool import notion_mcp_search

        pages = await notion_mcp_search(
            access_token, query="", page_size=NOTION_PAGE_LIMIT
        )
        logger.info(
            "Fetched Notion pages for smart onboarding",
            count=len(pages),
            user_id=str(user_id),
        )
        return pages
    except Exception as e:
        logger.error("Failed to fetch Notion pages for smart onboarding", error=str(e))
        return []


async def _fetch_linear_issues_with_token(
    access_token: str, user_id: UUID
) -> list[dict]:
    """Fetch recent Linear issues assigned to the user using a pre-resolved token."""
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
        data = await _linear_gql(access_token, query)
        assigned = (data.get("viewer") or {}).get("assignedIssues")
        nodes = assigned.get("nodes", []) if assigned else []

        issues = [
            {
                "identifier": n.get("identifier", ""),
                "title": n.get("title", ""),
                "status": (n.get("state") or {}).get("name", ""),
                "team": (n.get("team") or {}).get("name", ""),
            }
            for n in nodes
        ]

        logger.info(
            "Fetched Linear issues for smart onboarding",
            count=len(issues),
            user_id=str(user_id),
        )
        return issues

    except Exception as e:
        logger.error("Failed to fetch Linear issues for smart onboarding", error=str(e))
        return []


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------


def _strip_analysis(text: str) -> str:
    """Strip <analysis>...</analysis> scratchpad blocks from Claude's response."""
    return re.sub(r"<analysis>[\s\S]*?</analysis>", "", text).strip()


def _extract_json_object(text: str) -> Optional[dict]:
    """Safely extract a JSON object from Claude's response."""
    raw_text = text
    text = _strip_analysis(text).strip()
    if text != raw_text.strip():
        logger.info("_strip_analysis modified text", before_len=len(raw_text), after_len=len(text))

    # Try direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # Try extracting from markdown code fence
    # Strip fences first, then parse the inner content
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    stripped = re.sub(r"\n?```\s*$", "", stripped).strip()
    if stripped != text.strip():
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Try finding a valid JSON object by scanning { positions from the end
    # (the actual JSON is typically at the end, after any preamble text)
    for i in range(len(text) - 1, -1, -1):
        if text[i] == "}":
            # Find the matching opening brace
            for j in range(i, -1, -1):
                if text[j] == "{":
                    candidate = text[j : i + 1]
                    try:
                        parsed = json.loads(candidate)
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        continue
            break  # Only try the last } block

    return None


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------


async def _research_profile_with_web(
    name: str,
    email: str,
    role: str | None = None,
    department: str | None = None,
    linkedin_url: str | None = None,
) -> tuple[str, object]:
    """Build a profile via Exa search + Claude synthesis. Returns (markdown_profile, usage)."""
    from services.exa_service import exa_search, format_exa_results

    identity_lines = [
        f"Name: {name or 'Unknown'}",
        f"Email: {email}",
    ]
    if role:
        identity_lines.append(f"Role: {role}")
    if department:
        identity_lines.append(f"Department: {department}")
    if linkedin_url:
        identity_lines.append(f"LinkedIn: {linkedin_url}")

    identity_block = "\n".join(identity_lines)

    # Step 1: Exa search for the person (people category, full text for rich profiles)
    if linkedin_url:
        # Targeted LinkedIn fetch -- use the URL directly as query for best match
        exa_results = await exa_search(
            query=f"{name} {linkedin_url}",
            num_results=3,
            max_chars=4000,
            category="people",
            include_domains=["linkedin.com"],
            use_full_text=True,
        )
    else:
        # Broader people search
        exa_results = await exa_search(
            query=f"{name} {email} professional background",
            num_results=5,
            max_chars=3000,
            category="people",
            use_full_text=True,
        )

    context = format_exa_results(exa_results) if exa_results else "No search results found."

    # Step 2: Claude synthesizes the search results into a profile
    prompt = (
        f"Compile a structured professional background profile for this person "
        f"based on the search results below.\n\n"
        f"Person:\n{identity_block}\n\n"
        f"Search results:\n{context}\n\n"
        f"Include: professional background, current role, company, career history, education, "
        f"digital presence (LinkedIn, GitHub), and skills. "
        f"Format as clean markdown. Only include confident information. "
        f"Do NOT add disclaimers or caveats or any introductory lines. Give just the content."
    )

    from dataclasses import replace as _replace
    # 16384: Kimi reasoning_content eats the budget before the profile text; a
    # 2000 cap risked an empty/truncated profile. The output itself stays short.
    profile_spec = _replace(get_spec("smart_onboarding"), max_tokens=16384)
    response = await llm.complete(
        profile_spec,
        messages=[{"role": "user", "content": prompt}],
        timeout=240.0,  # was 120s — 2000-token profile synthesis on Kimi reasoning
    )

    usage = getattr(response.raw, "usage", None) or response.usage
    return response.text, usage


async def _analyze_workspace_data(
    data_text: str,
    role: str | None = None,
    department: str | None = None,
) -> tuple[Optional[dict], object]:
    """Call 2: Analyze integration data (contains PII). No web search. Returns (result_dict, usage)."""

    # Prepend role/department context so Claude can tailor workflows
    user_context = ""
    if role or department:
        parts = []
        if role:
            parts.append(f"Role: {role}")
        if department:
            parts.append(f"Department: {department}")
        user_context = "## User Context\n" + " | ".join(parts) + "\n\n"

    # Use tool_use to force structured JSON output -- no parsing issues
    onboarding_tool = {
        "name": "save_onboarding_result",
        "description": "Save the onboarding analysis result with user profile and workflow suggestions.",
        "input_schema": {
            "type": "object",
            "properties": {
                "profile": {
                    "type": "string",
                    "description": "Markdown profile of the user",
                },
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
                        "required": ["name", "prompt", "schedule_time", "schedule_unit"],
                    },
                    "description": "Exactly 2 workflow suggestions",
                },
            },
            "required": ["profile", "workflows"],
        },
    }

    from dataclasses import replace as _replace
    analyze_spec = _replace(get_spec("smart_onboarding"), max_tokens=16384)
    response = await llm.complete(
        analyze_spec,
        messages=[{"role": "user", "content": user_context + data_text}],
        system=SYSTEM_PROMPT,
        tools=[onboarding_tool],
        force_tool="save_onboarding_result",
        timeout=300.0,
    )

    usage = getattr(response.raw, "usage", None) or response.usage

    # Extract the tool call input -- guaranteed valid JSON by the API
    result = None
    for tc in response.tool_calls:
        if tc.name == "save_onboarding_result":
            result = tc.input
            break

    if not result:
        logger.error("Smart onboarding: no tool call in response")
        return None, usage

    logger.info(
        "Smart onboarding result",
        profile_length=len(result.get("profile", "")),
        workflow_count=len(result.get("workflows", [])),
    )

    return result, usage


# ---------------------------------------------------------------------------
# Stage tracking helper
# ---------------------------------------------------------------------------


async def _set_stage(user_id: UUID, stage: str) -> None:
    """Update the onboarding_stage column. Uses its own session to commit immediately."""
    from db.database import AsyncPostgresSessionGideon

    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            await session.execute(
                update(User).where(User.id == user_id).values(onboarding_stage=stage)
            )
    except Exception:
        pass  # Non-critical, don't break onboarding


# ---------------------------------------------------------------------------
# Early profile research (triggered from step 2, before Continue)
# ---------------------------------------------------------------------------


async def run_early_profile_research(user_id: UUID) -> None:
    """
    Triggered as soon as the user lands on onboarding step 2.
    Runs web profile research in the background so it's ready by step 3.
    Only runs if background_info is not already set.
    """
    from db.database import AsyncPostgresSessionGideon

    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                return
            if user.background_info:
                logger.info(
                    "Early profile research: already has background",
                    user_id=str(user_id),
                )
                return

            logger.info("Early profile research started", user_id=str(user_id))
            await _set_stage(user_id, "searching_web")
            profile_text, usage = await _research_profile_with_web(
                user.name,
                user.email,
                role=user.role,
                department=user.department,
                linkedin_url=user.linkedin_url,
            )

            if profile_text:
                await _set_stage(user_id, "profile_ready")
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(background_info=profile_text)
                )
                logger.info("Early profile research completed", user_id=str(user_id))

            if usage:
                from services.usage_service import log_usage

                log_usage(
                    user_id, get_spec("smart_onboarding").model, "smart_onboarding_web", usage
                )

    except Exception as e:
        logger.error(
            "Early profile research failed", user_id=str(user_id), error=str(e)
        )
        track_error(
            str(user_id),
            "profile_research_failed",
            tool_name="smart_onboarding",
            detail=str(e)[:500],
        )


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def run_smart_onboarding(user_id: UUID) -> None:
    """
    Triggered after user connects Google + Slack + Notion + Linear during onboarding.
    1. Gather data from all 4 integrations
    2. Analyze with Claude to build rich user profile
    3. Generate personalized workflow suggestions
    4. Store results
    """
    from db.database import AsyncPostgresSessionGideon

    logger.info("Starting smart onboarding", user_id=str(user_id))

    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            # Load user
            result = await session.execute(select(User).where(User.id == user_id))
            user = result.scalar_one_or_none()
            if not user:
                logger.error(
                    "User not found for smart onboarding", user_id=str(user_id)
                )
                return

            if user.auto_workflows_generated and user.background_generated:
                logger.info(
                    "Smart onboarding already completed, skipping", user_id=str(user_id)
                )
                return

            # Resolve all integration tokens sequentially (requires session)
            await _set_stage(user_id, "fetching_data")
            logger.info("Gathering integration data", user_id=str(user_id))

            google_integration = await _get_integration(session, user_id, "google")
            google_token = None
            if google_integration:
                google_token = await _get_google_token(session, google_integration)

            slack_integration = await _get_integration(session, user_id, "slack")
            slack_token = None
            if slack_integration:
                slack_token = await _get_slack_token(slack_integration)

            notion_integration = await _get_integration(session, user_id, "notion")
            notion_token = None
            if notion_integration:
                notion_token = await _get_notion_token(notion_integration)

            linear_integration = await _get_integration(session, user_id, "linear")
            linear_token = None
            if linear_integration:
                try:
                    linear_token = decrypt(linear_integration.access_token)
                    # Proactive refresh if token expired (Linear tokens expire in 24h)
                    if (
                        linear_integration.refresh_token
                        and linear_integration.token_expiry
                    ):
                        from datetime import datetime as dt
                        from datetime import timezone as tz

                        if dt.now(tz.utc) >= linear_integration.token_expiry:
                            try:
                                from services.oauth.linear_oauth import (
                                    LinearOAuthProvider,
                                )

                                new_tokens = (
                                    await LinearOAuthProvider().refresh_access_token(
                                        decrypt(linear_integration.refresh_token)
                                    )
                                )
                                linear_integration.access_token = encrypt(
                                    new_tokens.access_token
                                )
                                if new_tokens.refresh_token:
                                    linear_integration.refresh_token = encrypt(
                                        new_tokens.refresh_token
                                    )
                                linear_integration.token_expiry = (
                                    new_tokens.token_expiry
                                )
                                await session.flush()
                                linear_token = new_tokens.access_token
                                logger.info(
                                    "Linear token refreshed for smart onboarding",
                                    user_id=str(user_id),
                                )
                            except Exception as e:
                                logger.warning(
                                    "Linear refresh failed for smart onboarding",
                                    error=str(e),
                                )
                                linear_token = None
                except Exception as e:
                    logger.error("Failed to decrypt Linear token for smart onboarding")

            # Build parallel tasks using pre-resolved tokens (no session needed)
            tasks = []
            task_names = []

            if google_token:
                tasks.append(_fetch_sent_emails_with_token(google_token, user_id))
                task_names.append("emails")
                tasks.append(_fetch_calendar_events_with_token(google_token, user_id))
                task_names.append("calendar")
            if slack_token:
                tasks.append(_fetch_slack_channels_with_token(slack_token, user_id))
                task_names.append("slack")
            if notion_token:
                tasks.append(_fetch_notion_pages_with_token(notion_token, user_id))
                task_names.append("notion")
            if linear_token:
                tasks.append(_fetch_linear_issues_with_token(linear_token, user_id))
                task_names.append("linear")

            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Map results back by name
            result_map: dict[str, list] = {}
            for i, res in enumerate(results):
                name = task_names[i]
                if isinstance(res, Exception):
                    logger.error(
                        "Smart onboarding fetch failed", source=name, error=str(res)
                    )
                    result_map[name] = []
                else:
                    result_map[name] = res

            emails = result_map.get("emails", [])
            calendar = result_map.get("calendar", [])
            slack_channels = result_map.get("slack", [])
            notion_pages = result_map.get("notion", [])
            linear_issues = result_map.get("linear", [])

            await _set_stage(user_id, "data_gathered")
            logger.info(
                "Integration data gathered",
                emails=len(emails) if isinstance(emails, list) else 0,
                calendar=len(calendar) if isinstance(calendar, list) else 0,
                slack=len(slack_channels) if isinstance(slack_channels, list) else 0,
                notion=len(notion_pages) if isinstance(notion_pages, list) else 0,
                linear=len(linear_issues) if isinstance(linear_issues, list) else 0,
                user_id=str(user_id),
            )

            # Build text summary for Claude
            content_parts = []

            if emails:
                email_lines = []
                for e in emails:
                    email_lines.append(
                        f"- To: {e['to']} | Subject: {e['subject']} | Date: {e['date']}"
                    )
                content_parts.append(
                    f"### Sent Emails (last 2 weeks, {len(emails)} messages)\n\n"
                    + "\n".join(email_lines)
                )

            if calendar:
                cal_lines = []
                for ev in calendar:
                    attendee_str = ", ".join(ev["attendees"][:5]) or "none"
                    recurring_str = "Recurring" if ev["recurring"] else "One-time"
                    cal_lines.append(
                        f"- {ev['title']} | {ev['start']} - {ev['end']} | "
                        f"Attendees: {attendee_str} | {recurring_str} | Organizer: {ev['organizer']}"
                    )
                content_parts.append(
                    f"### Calendar Events (last 2 weeks, {len(calendar)} events)\n\n"
                    + "\n".join(cal_lines)
                )

            if slack_channels:
                slack_lines = []
                for ch in slack_channels:
                    purpose_str = f" — {ch['purpose']}" if ch["purpose"] else ""
                    slack_lines.append(
                        f"- #{ch['name']} ({ch['num_members']} members){purpose_str}"
                    )
                content_parts.append(
                    f"### Slack Channels ({len(slack_channels)} channels)\n\n"
                    + "\n".join(slack_lines)
                )

            if notion_pages:
                notion_lines = []
                for p in notion_pages:
                    notion_lines.append(
                        f"- [{p['type']}] {p['title']} (last edited: {p['last_edited']})"
                    )
                content_parts.append(
                    f"### Notion Pages & Databases ({len(notion_pages)} items)\n\n"
                    + "\n".join(notion_lines)
                )

            if linear_issues:
                linear_lines = []
                for i in linear_issues:
                    linear_lines.append(
                        f"- {i['identifier']} {i['title']} | {i['status']} | Team: {i['team']}"
                    )
                content_parts.append(
                    f"### Linear Issues ({len(linear_issues)} assigned)\n\n"
                    + "\n".join(linear_lines)
                )

            if not content_parts:
                logger.warning(
                    "No data fetched from any integration for smart onboarding",
                    user_id=str(user_id),
                )
                # Mark both flags done so frontend stops polling.
                # No workflows created -- WorkflowsStep will show "No workflows" message.
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(
                        background_generated=True,
                        auto_workflows_generated=True,
                        onboarding_stage="done",
                    )
                )
                return

            data_text = "## Integration Data\n\n" + "\n\n".join(content_parts)

            # Two-phase analysis:
            # 1. Web search on name+email (skip if background_info already set by early research)
            # 2. Workspace analysis on integration data (no web search — contains PII)
            await _set_stage(user_id, "analyzing")
            logger.info("Running smart onboarding analysis", user_id=str(user_id))

            # Reload user to check if early profile research already completed
            await session.refresh(user)
            skip_web = bool(user.background_info)

            tasks = [
                _analyze_workspace_data(
                    data_text, role=user.role, department=user.department
                )
            ]
            if not skip_web:
                tasks.insert(
                    0,
                    _research_profile_with_web(
                        user.name,
                        user.email,
                        role=user.role,
                        department=user.department,
                        linkedin_url=user.linkedin_url,
                    ),
                )

            results = await asyncio.gather(*tasks, return_exceptions=True)

            web_profile = ""
            web_usage = None
            analysis = None
            analysis_usage = None

            if skip_web:
                web_profile = user.background_info or ""
                analysis_result = results[0]
            else:
                web_result = results[0]
                analysis_result = results[1]
                if isinstance(web_result, Exception):
                    logger.error("Web profile research failed", error=str(web_result))
                else:
                    web_profile, web_usage = web_result

            if isinstance(analysis_result, Exception):
                logger.error("Workspace analysis failed", error=str(analysis_result))
            else:
                analysis, analysis_usage = analysis_result

            # Log usage (fire-and-forget)
            from services.usage_service import log_usage

            if web_usage:
                log_usage(
                    user_id, get_spec("smart_onboarding").model, "smart_onboarding_web", web_usage
                )
            if analysis_usage:
                log_usage(
                    user_id,
                    get_spec("smart_onboarding").model,
                    "smart_onboarding_analysis",
                    analysis_usage,
                )

            if not analysis:
                # Still store web profile even if workspace analysis failed
                vals: dict = {
                    "background_generated": True,
                    "auto_workflows_generated": True,
                    "onboarding_stage": "done",
                }
                if web_profile:
                    vals["background_info"] = web_profile
                await session.execute(
                    update(User).where(User.id == user_id).values(**vals)
                )
                logger.warning(
                    "Workspace analysis failed, no workflows generated",
                    user_id=str(user_id),
                    had_web_profile=bool(web_profile),
                )
                return

            # Combine web profile + workspace context
            # NOTE: no _set_stage here -- the main session already holds a row
            # lock on this user from prior updates. Using _set_stage (separate
            # session) would deadlock waiting for the lock.
            workspace_profile = analysis.get("profile", "")
            parts = []
            if web_profile:
                parts.append(web_profile)
            if workspace_profile:
                parts.append(
                    "## Workspace Context (from connected integrations)\n\n"
                    + workspace_profile
                )
            combined_profile = "\n\n---\n\n".join(parts) if parts else ""

            if combined_profile:
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(
                        background_info=combined_profile,
                        background_generated=True,
                    )
                )
                logger.info(
                    "Stored user profile from smart onboarding", user_id=str(user_id)
                )

            # Create workflow records (dedup by name)
            workflows = analysis.get("workflows", [])
            today = date.today()
            created_count = 0

            existing_names_result = await session.execute(
                select(Workflow.name).where(Workflow.user_id == user_id)
            )
            existing_names = {
                str(r[0]).strip().lower() for r in existing_names_result.all() if r[0]
            }

            for w in workflows[:2]:
                if not isinstance(w, dict):
                    continue
                raw_name = w.get("name")
                if (
                    not raw_name
                    or not isinstance(raw_name, str)
                    or not raw_name.strip()
                ):
                    continue
                if not w.get("prompt"):
                    continue
                w["name"] = raw_name.strip()
                if w["name"].lower() in existing_names:
                    continue

                # Only keep integrations that are actually connected
                integrations_used = [
                    p
                    for p in (w.get("integrations_used") or [])
                    if p in ("google", "slack", "notion", "linear")
                ]

                # Validate and normalize schedule fields from Claude output
                schedule_unit = w.get("schedule_unit", "day")
                if schedule_unit not in ("day", "week"):
                    schedule_unit = "day"

                schedule_interval = w.get("schedule_interval", 1)
                if not isinstance(schedule_interval, int) or schedule_interval < 1:
                    schedule_interval = 1

                schedule_time = w.get("schedule_time", "09:00")
                if (
                    not isinstance(schedule_time, str)
                    or len(schedule_time) != 5
                    or ":" not in schedule_time
                ):
                    schedule_time = "09:00"

                schedule_dow = w.get("schedule_day_of_week")
                if schedule_dow and schedule_dow not in (
                    "MO",
                    "TU",
                    "WE",
                    "TH",
                    "FR",
                    "SA",
                    "SU",
                ):
                    schedule_dow = None

                workflow = Workflow(
                    user_id=user_id,
                    name=w.get("name", "Suggested Workflow"),
                    description=w.get("description"),
                    prompt=w.get("prompt", ""),
                    when_type="schedule",
                    schedule_start_date=today,
                    schedule_interval=schedule_interval,
                    schedule_unit=schedule_unit,
                    schedule_day_of_week=schedule_dow,
                    schedule_time=schedule_time,
                    integrations_used=integrations_used,
                    status="inactive",
                )
                session.add(workflow)
                existing_names.add(w["name"].lower())
                created_count += 1
                logger.info(
                    "Created smart onboarding workflow",
                    name=w.get("name"),
                    schedule=f"{w.get('schedule_time')} {w.get('schedule_unit')}",
                    user_id=str(user_id),
                )

            # Flush workflows into DB before marking completion flags.
            # Without this, the frontend can see auto_workflows_generated=True
            # but GET /workflows returns empty because the rows aren't committed yet.
            if created_count > 0:
                await session.flush()

            # Mark as completed (set stage inline, not via _set_stage which deadlocks)
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(
                    auto_workflows_generated=True,
                    background_generated=True,
                    onboarding_stage="done",
                )
            )

            logger.info(
                "Smart onboarding completed",
                profile_length=len(combined_profile),
                workflows_created=created_count,
                user_id=str(user_id),
            )

    except Exception as e:
        logger.error("Smart onboarding failed", user_id=str(user_id), error=str(e))
        track_error(
            str(user_id),
            "smart_onboarding_failed",
            tool_name="smart_onboarding",
            detail=str(e)[:500],
        )
        # Mark as done so frontend doesn't get stuck on loading screen
        try:
            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as session:
                await session.execute(
                    update(User)
                    .where(User.id == user_id)
                    .values(
                        background_generated=True,
                        auto_workflows_generated=True,
                        onboarding_stage="done",
                    )
                )
        except Exception:
            logger.error(
                "Failed to mark smart onboarding flags after error",
                user_id=str(user_id),
            )
        except Exception:
            logger.error(
                "Failed to mark smart onboarding flags after error",
                user_id=str(user_id),
            )
