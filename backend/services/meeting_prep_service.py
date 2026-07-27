"""
Meeting Prep Service -- auto-generates meeting preparation notes for upcoming
calendar events.

Called from the data scheduler every 10 minutes. For each user with Google
connected, looks at events in the next 7 days and generates prep immediately
when a new event is detected. Re-generates if the event is updated (new
attendees, location, description, etc.).
"""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import aiohttp
import structlog
from services.auth_service import email_domain_allowed
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.llm import get_spec, llm
from db.database import AsyncPostgresSessionGideon
from db.models import ActivityLog, Integration, MeetingPrep, User
from services.encryption_service import decrypt, encrypt
from services.events_service import track_error
from services.integration_health import clear_integration_errors
from services.graphiti_service import search_memories
from services.oauth.granola_oauth import GranolaOAuthProvider
from services.oauth.token_refresh import get_valid_access_token
from services.usage_service import log_usage
from services.workspace_member_service import get_all_members, get_user_token
from tools.granola_tool import mcp_tool_call, extract_text_content
from tools.linear_tool import linear_gql

logger = structlog.get_logger()

LOOKAHEAD_MINUTES = 7 * 24 * 60  # 7 days — generate prep as soon as event is detected

PREP_SYSTEM_PROMPT = """You are a meeting prep assistant with access to the user's emails, Slack messages, Linear issues, activity history, and semantic memories. Use ALL of this to generate an extremely specific, data-grounded meeting prep.

Write like a sharp chief of staff who has read every email, every Slack thread, and every ticket. Be direct, opinionated, and reference real data.

Format your response as markdown:

**TL;DR**
2-3 sentences. What this meeting is really about, what's at stake, and the one thing the user must walk in knowing. Reference specific facts.

**Key context**
- Specific facts, decisions, numbers, action items from emails/Slack/Linear related to this meeting
- Quote relevant messages or reference ticket IDs if they add context
- Include timelines, deadlines, blockers, status of open work
- If there are unresolved threads or pending decisions, call them out

**What to nail in this call:**
- 3-7 specific, actionable bullet points grounded in real data
- Reference people, ticket IDs, deadlines, documents, numbers by name
- If something shipped, say when. If something slipped, say why.
- If there's a decision needed, frame it clearly

**Attendees**
- For each attendee: who they are, their role, and what's specifically relevant about them for THIS meeting
- Reference recent interactions, shared projects, or open items with them

Rules:
- Be EXTREMELY SPECIFIC. Mention names, dates, links, ticket IDs, numbers.
- Every bullet must be grounded in real context from the provided data.
- If you found emails, Slack messages, Linear issues, or memories about this topic, reference them directly.
- No generic advice like "prepare talking points" or "review agenda". Only real, specific prep.
- No fluff. No filler. No "consider discussing". Tell the user exactly what matters.
- If the data is thin, say so honestly rather than padding with generic advice.
- Do NOT use em dashes."""


# ---------------------------------------------------------------------------
# Token helpers (same pattern as hub_router / smart_onboarding_service)
# ---------------------------------------------------------------------------

async def _get_google_integration(
    session: AsyncSession, user_id: UUID
) -> Optional[Integration]:
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "google",
            Integration.is_active == True,
        )
    )
    return result.scalar_one_or_none()


# Google tokens are resolved via the shared lock-coordinated helper
# `services.oauth.token_refresh.get_valid_access_token("google", user_id)`.
# It manages its own short-lived DB sessions + a Redis lock, records the
# integration error and flips `is_active=False` on a confirmed-dead refresh
# token (invalid_grant), which breaks the retry-every-cycle spiral this
# service used to cause with an inline `GoogleOAuthProvider().refresh_...`.
# Do NOT call it while this service holds an open DB transaction.


# ---------------------------------------------------------------------------
# Integration context search -- emails + Slack about the meeting topic
# ---------------------------------------------------------------------------

async def _search_email_context(
    access_token: str, meeting_title: str, attendee_emails: list[str],
    since: Optional[datetime] = None,
) -> str:
    """Search Gmail for emails related to the meeting topic and attendees.

    If `since` is provided (recurring meetings), scopes search to that date.
    Otherwise defaults to last 14 days.
    """
    try:
        creds = Credentials(token=access_token)
        service = build("gmail", "v1", credentials=creds, cache_discovery=False)

        # Build search query: topic keywords + attendee emails
        title_words = [w for w in meeting_title.split() if len(w) > 3]
        query_parts = []
        if title_words:
            query_parts.append(f"({' OR '.join(title_words[:5])})")
        for email in attendee_emails[:3]:
            query_parts.append(f"from:{email} OR to:{email}")
        if since:
            date_str = since.strftime("%Y/%m/%d")
            query = " ".join(query_parts) + f" after:{date_str}"
        else:
            query = " ".join(query_parts) + " newer_than:365d"

        def _search_sync():
            results = service.users().messages().list(
                userId="me", q=query, maxResults=15
            ).execute()
            messages = []
            for msg_meta in results.get("messages", [])[:15]:
                msg = service.users().messages().get(
                    userId="me", id=msg_meta["id"], format="metadata",
                    metadataHeaders=["Subject", "From", "Date"],
                ).execute()
                headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
                snippet = msg.get("snippet", "")
                messages.append(
                    f"- **{headers.get('Subject', '(no subject)')}** from {headers.get('From', '?')} ({headers.get('Date', '')}): {snippet[:200]}"
                )
            return messages

        messages = await asyncio.get_running_loop().run_in_executor(None, _search_sync)
        if messages:
            return "**Related emails (last year):**\n" + "\n".join(messages)
    except Exception as e:
        logger.debug("Email context search failed for meeting prep", error=str(e))
    return ""


async def _get_slack_user_token(session: AsyncSession, user_id: UUID) -> Optional[str]:
    """Read + decrypt the user's Slack user token (search API needs a user token).

    Pre-fetched sequentially so `_search_slack_context` can run in the context
    fan-out without holding (or sharing) a DB session.
    """
    try:
        integration = await session.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "slack",
                Integration.is_active == True,
            )
        )
        slack_int = integration.scalar_one_or_none()
        if not slack_int or not slack_int.access_token:
            return None
        return decrypt(slack_int.access_token)
    except Exception as e:
        logger.debug("Slack token lookup failed for meeting prep", error=str(e))
        return None


async def _search_slack_context(
    token: str, meeting_title: str,
    since: Optional[datetime] = None,
) -> str:
    """Search Slack for messages related to the meeting topic.

    If `since` is provided (recurring meetings), scopes search to that date.
    Takes a pre-fetched token — no DB session (runs in the parallel fan-out).
    """
    try:
        title_words = [w for w in meeting_title.split() if len(w) > 3]
        if not title_words:
            return ""

        query = " ".join(title_words[:4])
        if since:
            query += f" after:{since.strftime('%Y-%m-%d')}"
        async with aiohttp.ClientSession() as http:
            async with http.get(
                "https://slack.com/api/search.messages",
                params={"query": query, "count": 5, "sort": "timestamp"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                if not data.get("ok"):
                    return ""
                matches = data.get("messages", {}).get("matches", [])
                if not matches:
                    return ""
                lines = []
                for m in matches[:5]:
                    user_name = m.get("username", "someone")
                    text = m.get("text", "")[:200]
                    channel = m.get("channel", {}).get("name", "")
                    lines.append(f"- **{user_name}** in #{channel}: {text}")
                return "**Related Slack messages:**\n" + "\n".join(lines)
    except Exception as e:
        logger.debug("Slack context search failed for meeting prep", error=str(e))
    return ""


async def _get_granola_token(session: AsyncSession, user_id: UUID) -> Optional[str]:
    """Read + (inline) refresh the user's Granola token.

    Pre-fetched sequentially — the fan-out helper `_search_granola_context`
    must not touch the shared DB session. Granola's refresh path is left as-is
    (single-use rotating MCP tokens with their own semantics).
    """
    try:
        result = await session.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "granola",
                Integration.is_active == True,
            )
        )
        granola_int = result.scalar_one_or_none()
        if not granola_int or not granola_int.access_token:
            return None

        token = decrypt(granola_int.access_token)

        # Refresh token if expired
        if granola_int.token_expiry and datetime.now(timezone.utc) >= granola_int.token_expiry:
            if granola_int.refresh_token:
                try:
                    new_tokens = await GranolaOAuthProvider().refresh_access_token(
                        decrypt(granola_int.refresh_token)
                    )
                    granola_int.access_token = encrypt(new_tokens.access_token)
                    if new_tokens.refresh_token:
                        granola_int.refresh_token = encrypt(new_tokens.refresh_token)
                    granola_int.token_expiry = new_tokens.token_expiry or (datetime.now(timezone.utc) + timedelta(hours=1))
                    await session.flush()
                    token = new_tokens.access_token
                except Exception as e:
                    logger.debug("Granola token refresh failed for meeting prep", error=str(e))
                    track_error(str(user_id), "token_refresh_failed", provider="granola", tool_name="meeting_prep", detail=str(e)[:500])
                    return None
            else:
                return None
        return token
    except Exception as e:
        logger.debug("Granola token lookup failed for meeting prep", error=str(e))
        return None


async def _search_granola_context(token: str, meeting_title: str) -> str:
    """Search Granola for notes from the last occurrence of this recurring meeting.

    Takes a pre-fetched token — no DB session (runs in the parallel fan-out).
    """
    try:
        # Search for meetings matching this title
        result = await mcp_tool_call(token, "get_meetings", {"query": meeting_title})
        text = extract_text_content(result)
        if text:
            # Truncate to keep context reasonable
            return f"**Notes from previous occurrence (via Granola):**\n{text[:3000]}"
    except Exception as e:
        logger.debug("Granola context search failed for meeting prep", error=str(e))
    return ""


async def _search_linear_context(
    token: str, meeting_title: str,
    attendee_emails: list[str],
) -> str:
    """Search Linear for issues related to meeting attendees and topic.

    Takes a pre-fetched token (via `get_user_token(session, user_id, "linear")`
    in the sequential read phase) — no DB session in the parallel fan-out.
    """
    try:
        # Search by meeting title keywords
        title_words = [w for w in meeting_title.split() if len(w) > 3]
        all_issues = []

        # Search by title keywords
        if title_words:
            query = " ".join(title_words[:4])
            data = await linear_gql(token, """
                query SearchIssues($query: String!, $first: Int!) {
                    searchIssues(term: $query, first: $first) {
                        nodes {
                            identifier, title, state { name }, priority
                            assignee { name, email }
                            team { name }
                            updatedAt, url
                        }
                    }
                }
            """, {"query": query, "first": 10})
            all_issues.extend((data.get("searchIssues") or {}).get("nodes", []))

        # Also search by attendee names for their recent active work
        for email in attendee_emails[:2]:
            name_part = email.split("@")[0]
            data = await linear_gql(token, """
                query SearchIssues($query: String!, $first: Int!) {
                    searchIssues(term: $query, first: $first) {
                        nodes {
                            identifier, title, state { name }, priority
                            assignee { name, email }
                            team { name }
                            updatedAt, url
                        }
                    }
                }
            """, {"query": name_part, "first": 5})
            all_issues.extend((data.get("searchIssues") or {}).get("nodes", []))

        if not all_issues:
            return ""

        # Dedup by identifier
        seen = set()
        lines = []
        for issue in all_issues:
            ident = issue.get("identifier", "")
            if ident in seen:
                continue
            seen.add(ident)
            state = (issue.get("state") or {}).get("name", "")
            assignee = (issue.get("assignee") or {}).get("name", "")
            team = (issue.get("team") or {}).get("name", "")
            lines.append(f"- **{ident}**: {issue.get('title', '')} [{state}] (assigned: {assignee or 'unassigned'}, team: {team})")

        if lines:
            return "**Related Linear issues:**\n" + "\n".join(lines[:15])
    except Exception as e:
        logger.debug("Linear context search failed for meeting prep", error=str(e))
    return ""


async def _search_memory_context(user_id: UUID, meeting_title: str, attendee_names: list[str]) -> str:
    """Search mem0 memories for context about meeting topic and attendees."""
    try:
        all_memories = []

        # Search by meeting title
        memories = await search_memories(str(user_id), meeting_title, limit=5)
        all_memories.extend(memories)

        # Search by attendee names
        for name in attendee_names[:3]:
            if name and len(name) > 2:
                memories = await search_memories(str(user_id), name, limit=3)
                all_memories.extend(memories)

        if not all_memories:
            return ""

        # Dedup by content
        seen = set()
        lines = []
        for mem in all_memories:
            content = mem.get("content", "")
            if not content or content in seen:
                continue
            seen.add(content)
            lines.append(f"- {content}")

        if lines:
            return "**Relevant memories:**\n" + "\n".join(lines[:10])
    except Exception as e:
        logger.debug("Memory context search failed for meeting prep", error=str(e))
    return ""


async def _search_activity_context(
    session: AsyncSession, user_id: UUID, attendee_emails: list[str],
) -> str:
    """Search activity_log for recent interactions with attendees."""
    try:
        if not attendee_emails:
            return ""

        # Look for activities mentioning attendees in the last 30 days.
        # Title is encrypted, so we load recent activities once and Python-filter per attendee.
        cutoff = datetime.now(timezone.utc) - timedelta(days=30)
        result = await session.execute(
            select(ActivityLog)
            .where(
                ActivityLog.user_id == user_id,
                ActivityLog.created_at >= cutoff,
            )
            .order_by(ActivityLog.created_at.desc())
            .limit(500)
        )
        recent = result.scalars().all()

        lines = []
        for email in attendee_emails[:3]:
            name_part = email.split("@")[0].lower()
            matched = 0
            for a in recent:
                if matched >= 5:
                    break
                if name_part in (a.title or "").lower():
                    date_str = a.created_at.strftime("%b %d") if a.created_at else ""
                    lines.append(f"- [{a.source}/{a.event_type}] {a.title} ({date_str})")
                    matched += 1

        if lines:
            return "**Recent activity with attendees (last 30 days):**\n" + "\n".join(lines[:15])
    except Exception as e:
        logger.debug("Activity context search failed for meeting prep", error=str(e))
    return ""


async def _find_last_recurring_info(
    session: AsyncSession, user_id: UUID, recurring_event_id: str, current_event_id: str,
) -> tuple[Optional[datetime], str]:
    """Find the start time and prep content of the previous occurrence of this recurring meeting.

    Google Calendar recurring instance IDs are formatted as "{recurringEventId}_{date}".
    We search activity_log for other instances in the same series, then look up
    the corresponding meeting prep.

    Returns (last_date, previous_prep_content).
    """
    # Strip the @google.com domain if present in the recurring event ID
    base_id = recurring_event_id.split("@")[0] if "@" in recurring_event_id else recurring_event_id

    # Find previous instances of this recurring event from activity_log
    result = await session.execute(
        select(ActivityLog.reference_id, ActivityLog.extra_data)
        .where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "calendar",
            ActivityLog.reference_id.like(f"{base_id}%"),
            ActivityLog.reference_id != current_event_id,
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(1)
    )
    row = result.first()
    if not row or not row[1]:
        return None, ""

    prev_event_id = row[0]
    start_str = row[1].get("start_time", "")
    last_date = None
    if start_str:
        try:
            last_date = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass

    # Look up the previous meeting's prep content
    prev_prep_content = ""
    if prev_event_id:
        prep_result = await session.execute(
            select(MeetingPrep.prep_content)
            .where(
                MeetingPrep.user_id == user_id,
                MeetingPrep.event_id == prev_event_id,
                MeetingPrep.status == "completed",
            )
        )
        prep_row = prep_result.first()
        if prep_row and prep_row[0]:
            prev_prep_content = prep_row[0]

    return last_date, prev_prep_content


# ---------------------------------------------------------------------------
# Calendar fetch
# ---------------------------------------------------------------------------

async def _fetch_upcoming_events(
    access_token: str, lookahead_minutes: int = LOOKAHEAD_MINUTES
) -> list[dict]:
    """Fetch calendar events starting in the next N minutes."""
    def _fetch_sync():
        creds = Credentials(token=access_token)
        service = build("calendar", "v3", credentials=creds, cache_discovery=False)

        now = datetime.now(timezone.utc)
        time_min = now.isoformat()
        time_max = (now + timedelta(minutes=lookahead_minutes)).isoformat()

        events = []
        page_token = None
        while True:
            results = service.events().list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
                pageToken=page_token,
            ).execute()

            for event in results.get("items", []):
                # Skip all-day events (no dateTime, only date)
                start = event.get("start", {})
                if not start.get("dateTime"):
                    continue

                attendees = [
                    {
                        "email": a.get("email", ""),
                        "name": a.get("displayName", ""),
                        "response_status": a.get("responseStatus", ""),
                    }
                    for a in event.get("attendees", [])
                ]

                events.append({
                    "id": event["id"],
                    "title": event.get("summary", "(No title)"),
                    "description": event.get("description", ""),
                    "start": start.get("dateTime", ""),
                    "end": event.get("end", {}).get("dateTime", ""),
                    "attendees": attendees,
                    "organizer": event.get("organizer", {}).get("email", ""),
                    "location": event.get("location", ""),
                    "html_link": event.get("htmlLink", ""),
                    "updated": event.get("updated", ""),
                    "recurring_event_id": event.get("recurringEventId"),
                })

            page_token = results.get("nextPageToken")
            if not page_token:
                break

        return events

    return await asyncio.to_thread(_fetch_sync)


# ---------------------------------------------------------------------------
# Attendee context builder
# ---------------------------------------------------------------------------

async def _build_attendee_context(
    attendees: list[dict], user_email: str, all_members: list[dict],
    user_id: Optional[UUID] = None,
) -> str:
    """Build context for each attendee. Internal = Slack cache, external = web search.

    `all_members` is pre-fetched via `get_all_members(session)` during the
    sequential read phase — this function does Exa + LLM calls and must not
    hold a DB session.
    """
    member_lookup = {m["email"].lower(): m for m in all_members}

    internal_lines = []
    external_emails = []

    for att in attendees:
        email = att.get("email", "").lower()
        name = att.get("name", "")
        if not email or email == user_email.lower():
            continue

        if email in member_lookup:
            member = member_lookup[email]
            display_name = member.get("name", name) or email
            internal_lines.append(f"- {display_name} ({email}) -- internal team member")
        elif email_domain_allowed(email):
            internal_lines.append(f"- {name or email} ({email}) -- internal team member")
        else:
            external_emails.append({"email": email, "name": name})

    context_parts = []
    if internal_lines:
        context_parts.append("Internal attendees:\n" + "\n".join(internal_lines))

    # For external attendees, use web search to find background
    if external_emails:
        external_context = await _research_external_attendees(external_emails, user_id=user_id)
        if external_context:
            context_parts.append("External attendees:\n" + external_context)

    return "\n\n".join(context_parts) if context_parts else "No additional attendee information available."


async def _research_external_attendees(
    attendees: list[dict], user_id: Optional[UUID] = None
) -> str:
    """Research external attendees via Exa search + Claude synthesis (max 3)."""
    from services.exa_service import exa_search, format_exa_results

    if not attendees:
        return ""

    # Limit to 3 external attendees to keep cost reasonable
    to_research = attendees[:3]
    # Attendee blurbs are 1-2 sentences from search results — a simple
    # summarization with no need for deep reasoning. Disable reasoning here
    # (on Kimi it adds ~60s/attendee for no quality gain, and would otherwise
    # need a 16K budget so the trace doesn't truncate the blurb). 200 tokens is
    # ample once reasoning is off. No-op on Anthropic/OpenAI.
    spec = get_spec("meeting_prep")
    from dataclasses import replace
    attendee_spec = replace(spec, max_tokens=200, disable_reasoning=True)

    lines = []
    for att in to_research:
        name = att.get("name", "")
        email = att.get("email", "")
        display = name or email
        query = f"{name} {email}" if name else email

        try:
            # Step 1: Exa search for the person (people category, full text for richer profiles)
            exa_results = await exa_search(
                query=query,
                num_results=3,
                max_chars=1500,
                category="people",
                use_full_text=True,
            )

            if not exa_results:
                lines.append(f"- {display} ({email}): No public profile found")
                continue

            # Step 2: synthesize the search results
            context = format_exa_results(exa_results)
            response = await llm.complete(
                attendee_spec,
                messages=[{
                    "role": "user",
                    "content": (
                        f"Based on these search results, write 1-2 sentences about this person's "
                        f"professional background (role, company, relevance): {query}\n\n"
                        f"Search results:\n{context}\n\n"
                        f"If nothing relevant, say 'No public profile found.'"
                    ),
                }],
                timeout=120.0,
            )

            text = response.text.strip()

            lines.append(f"- {display} ({email}): {text}")

            if user_id:
                usage = getattr(response.raw, "usage", None) or response.usage
                log_usage(user_id, attendee_spec.model, "meeting_prep_attendee_research", usage)

        except Exception as e:
            logger.warning("Attendee research failed", error_type=type(e).__name__)
            lines.append(f"- {display} ({email}): No background available")

    # Note remaining unresearched attendees
    remaining = len(attendees) - len(to_research)
    if remaining > 0:
        lines.append(f"- ... and {remaining} more external attendee(s)")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core meeting prep generation
# ---------------------------------------------------------------------------

async def generate_meeting_prep(
    user_id: UUID,
    event_id: str,
    event_data: dict,
    session: AsyncSession,
) -> Optional[MeetingPrep]:
    """Generate a meeting prep document for a single calendar event.

    Returns the MeetingPrep record, or None if one already exists or generation fails.
    """
    # Check if prep already exists and completed
    result = await session.execute(
        select(MeetingPrep).where(
            MeetingPrep.user_id == user_id,
            MeetingPrep.event_id == event_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing and existing.status == "completed":
        logger.debug("Meeting prep already exists", event_id=event_id, user_id=str(user_id))
        return None
    if existing and existing.status in ("failed", "pending"):
        logger.info("Retrying meeting prep generation", event_id=event_id, status=existing.status)
        await session.delete(existing)
        await session.flush()
    elif existing and existing.status == "generating":
        stale = existing.updated_at and (datetime.now(timezone.utc) - existing.updated_at) > timedelta(minutes=10)
        if stale:
            logger.info("Retrying stale generating prep", event_id=event_id)
            await session.delete(existing)
            await session.flush()
        else:
            return None

    # Get user info
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        return None

    # Parse event start time
    event_start_str = event_data.get("start", "")
    try:
        event_start = datetime.fromisoformat(event_start_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        event_start = datetime.now(timezone.utc)

    # Create the record with status=generating (savepoint for concurrent insert dedup)
    try:
        async with session.begin_nested():
            prep = MeetingPrep(
                user_id=user_id,
                event_id=event_id,
                title=event_data.get("title", "(No title)"),
                attendees=event_data.get("attendees", []),
                event_start=event_start,
                status="generating",
            )
            session.add(prep)
    except IntegrityError:
        logger.debug("Meeting prep already being generated", event_id=event_id)
        return None
    await session.refresh(prep)

    try:
        # ------------------------------------------------------------------
        # Phase 1 — sequential DB reads on the shared session. Everything the
        # external context fan-out needs (tokens, member cache, recurring
        # info, activity context) is materialized here as PLAIN DATA, then
        # the transaction is committed before any external I/O begins.
        #
        # This also fixes a P1: the fan-out below used to pass this SAME
        # AsyncSession to multiple asyncio.create_task-launched helpers that
        # ran concurrently — concurrent use of one AsyncSession is undefined
        # behavior. The helpers are now session-free.
        # ------------------------------------------------------------------
        meeting_title = event_data.get("title", "")
        attendee_emails = [
            a.get("email", "") for a in event_data.get("attendees", [])
            if a.get("email") and not a.get("self")
        ]

        # Extract attendee names for memory search
        attendee_names = [
            a.get("name", "").strip() or a.get("email", "").split("@")[0]
            for a in event_data.get("attendees", [])
            if a.get("email") and a.get("email", "").lower() != user.email.lower()
        ]

        # Detect recurring meeting and find last occurrence for scoped search
        recurring_event_id = event_data.get("recurring_event_id")
        is_recurring = bool(recurring_event_id)
        since_date: Optional[datetime] = None
        prev_prep_content = ""
        if is_recurring:
            since_date, prev_prep_content = await _find_last_recurring_info(
                session, user_id, recurring_event_id, event_id
            )
            if since_date:
                logger.info(
                    "Recurring meeting detected, scoping context search",
                    event_id=event_id, since=str(since_date),
                    has_prev_prep=bool(prev_prep_content),
                )

        all_members = await get_all_members(session)
        slack_token = await _get_slack_user_token(session, user_id)
        linear_token = await get_user_token(session, user_id, "linear")
        granola_token = (
            await _get_granola_token(session, user_id) if is_recurring else None
        )
        activity_context = await _search_activity_context(session, user_id, attendee_emails)

        # Drop the open implicit transaction before ANY external I/O: attendee
        # research (Exa + LLM), the context fan-out (Gmail/Slack/Linear/
        # Graphiti/Granola), and the two `llm.complete` calls below can each
        # run 20-60s+, comfortably crossing Postgres's
        # `idle_in_transaction_session_timeout = 30s` and triggering the
        # "Meeting prep generation failed ... PendingRollbackError" cascade
        # observed in Loki on 2026-05-28 around 05:03 UTC. After commit, the
        # next `session.flush()` opens a fresh txn for the prep write below.
        # See CONTRIBUTING.md, database conventions.
        #
        # CAVEAT: if a caller ever invokes this function from inside
        # `session.begin_nested()`, `session.commit()` would commit the OUTER
        # transaction and release the caller's savepoint, breaking its
        # "rollback on failure" contract — so skip the commit in that case
        # (the savepoint boundary releases on its own __aexit__).
        if not session.in_nested_transaction():
            await session.commit()

        # ------------------------------------------------------------------
        # Phase 2 — external I/O, no DB transaction held.
        # ------------------------------------------------------------------
        # Google token via the shared lock-coordinated helper (manages its own
        # short-lived sessions; must not run inside an open transaction).
        access_token = await get_valid_access_token("google", user_id)

        # Build attendee context (Exa search + LLM synthesis for externals)
        attendee_context = await _build_attendee_context(
            event_data.get("attendees", []),
            user.email,
            all_members,
            user_id=user_id,
        )

        # Parallel context search: email + Slack + Linear + memories + Granola
        # (activity was already fetched sequentially — it is DB-only)
        tasks = {}
        if access_token:
            tasks["email"] = asyncio.create_task(
                _search_email_context(access_token, meeting_title, attendee_emails, since=since_date)
            )
        if slack_token:
            tasks["slack"] = asyncio.create_task(
                _search_slack_context(slack_token, meeting_title, since=since_date)
            )
        if linear_token:
            tasks["linear"] = asyncio.create_task(
                _search_linear_context(linear_token, meeting_title, attendee_emails)
            )
        tasks["memory"] = asyncio.create_task(
            _search_memory_context(user_id, meeting_title, attendee_names)
        )
        if granola_token:
            tasks["granola"] = asyncio.create_task(
                _search_granola_context(granola_token, meeting_title)
            )

        contexts = {}
        for key, task in tasks.items():
            try:
                contexts[key] = await task
            except Exception as e:
                logger.debug("Context search failed", source=key, error=str(e))
                contexts[key] = ""

        email_context = contexts.get("email", "")
        slack_context = contexts.get("slack", "")
        linear_context = contexts.get("linear", "")
        memory_context = contexts.get("memory", "")
        granola_context = contexts.get("granola", "")

        # Build the prompt for Claude
        event_time = event_start_str
        description = event_data.get("description", "").strip()
        location = event_data.get("location", "").strip()

        user_prompt_parts = [
            f"Prepare me for this meeting:\n",
            f"**Title:** {meeting_title or '(No title)'}",
            f"**Time:** {event_time}",
        ]
        if is_recurring:
            since_label = since_date.strftime("%b %d") if since_date else "unknown"
            user_prompt_parts.append(f"**Recurring meeting** (last occurrence: {since_label})")
        if location:
            user_prompt_parts.append(f"**Location:** {location}")
        if description:
            user_prompt_parts.append(f"**Description:** {description}")

        user_prompt_parts.append(f"\n**Attendee Research:**\n{attendee_context}")

        if prev_prep_content:
            # Truncate to keep within reasonable token limits
            user_prompt_parts.append(
                f"\n**Prep notes from last occurrence:**\n{prev_prep_content[:3000]}"
            )
        if memory_context:
            user_prompt_parts.append(f"\n{memory_context}")
        if activity_context:
            user_prompt_parts.append(f"\n{activity_context}")
        if linear_context:
            user_prompt_parts.append(f"\n{linear_context}")
        if granola_context:
            user_prompt_parts.append(f"\n{granola_context}")
        if email_context:
            user_prompt_parts.append(f"\n{email_context}")
        if slack_context:
            user_prompt_parts.append(f"\n{slack_context}")

        user_name = user.name or user.email
        system_prompt = PREP_SYSTEM_PROMPT
        if is_recurring:
            system_prompt += (
                "\n\nThis is a RECURRING meeting. Focus on what changed since the last occurrence. "
                "Include a **Since last meeting** section with key updates, decisions, and action items "
                "from emails, Slack, and meeting notes since then. Skip generic attendee introductions "
                "for people the user meets regularly."
            )
        system_prompt = system_prompt.replace(
            "You have access to",
            f"You are preparing {user_name} for a meeting. You have access to",
        )

        # NOTE: the session's read transaction was already committed at the
        # end of Phase 1 (before the context fan-out), so no DB connection is
        # pinned across the two llm.complete calls below.

        from dataclasses import replace as _replace
        prep_spec = get_spec("meeting_prep")
        response = await llm.complete(
            # 16384: the prep keeps reasoning ON (it benefits from it), so the
            # budget must cover ~5K reasoning_content + the ~4K prep. A 4000 cap
            # let the reasoning trace truncate the prep to empty on Kimi.
            _replace(prep_spec, max_tokens=16384),
            messages=[{"role": "user", "content": "\n".join(user_prompt_parts)}],
            system=system_prompt,
            # 240s: the prep is a large reasoning generation; on Kimi-k2.6
            # (reasoning + slower than Sonnet) 120s was occasionally timing out.
            timeout=240.0,
        )

        prep_content = response.text.strip()

        # Generate a one-liner summary from the full prep (not just title/description).
        # 1-2 sentence distillation — reasoning off (no benefit, and keeps the 150
        # budget from being eaten by the trace). No-op on Anthropic/OpenAI.
        summary_response = await llm.complete(
            _replace(prep_spec, max_tokens=150, disable_reasoning=True),
            messages=[{
                "role": "user",
                "content": (
                    f"Write a sharp, actionable one-liner (1-2 sentences, max 30 words) summarizing WHY this meeting matters and what to focus on. "
                    f"Be specific: mention names, key topics, action items. No generic descriptions. No quotes, no prefix.\n\n"
                    f"Meeting: {event_data.get('title', '')}\n"
                    f"Prep notes:\n{prep_content[:1000]}"
                ),
            }],
            timeout=180.0,
        )

        summary = summary_response.text.strip()

        # Update the record
        prep.prep_content = prep_content
        prep.summary = summary
        prep.status = "completed"
        prep.updated_at = datetime.now(timezone.utc)
        await session.flush()

        # Log usage (fire-and-forget)
        prep_usage = getattr(response.raw, "usage", None) or response.usage
        summary_usage = getattr(summary_response.raw, "usage", None) or summary_response.usage
        log_usage(user_id, prep_spec.model, "meeting_prep", prep_usage)
        log_usage(user_id, prep_spec.model, "meeting_prep_summary", summary_usage)

        logger.info(
            "Meeting prep generated",
            event_id=event_id,
            user_id=str(user_id),
            title=event_data.get("title", ""),
        )
        return prep

    except Exception as e:
        logger.error(
            "Meeting prep generation failed",
            event_id=event_id,
            user_id=str(user_id),
            error=str(e),
        )
        prep.status = "failed"
        prep.updated_at = datetime.now(timezone.utc)
        await session.flush()
        return None


# ---------------------------------------------------------------------------
# Scheduler entry point
# ---------------------------------------------------------------------------

PREP_CONCURRENCY = 10


async def auto_generate_upcoming_preps() -> None:
    """Called from the data scheduler. For each user with Google connected,
    fetches upcoming events (next 7 days via LOOKAHEAD_MINUTES) and generates prep for any without one.
    Users are processed in parallel, bounded by PREP_CONCURRENCY.
    """
    sm = AsyncPostgresSessionGideon()

    # Step 1: Collect eligible users (have active Google integration)
    user_ids: list[tuple[UUID, str]] = []
    async with sm.get_session() as session:
        result = await session.execute(
            select(User.id, User.email).where(
                User.id.in_(
                    select(Integration.user_id).where(
                        Integration.provider == "google",
                        Integration.is_active == True,
                    ).distinct()
                ),
                User.onboarding_completed == True,
                User.feature_meeting_prep == True,
            )
        )
        user_ids = [(row[0], row[1]) for row in result.all()]

    if not user_ids:
        return

    logger.info("Meeting prep: checking upcoming events", user_count=len(user_ids))

    async def _process_user_prep(uid: UUID, user_email: str, semaphore: asyncio.Semaphore) -> None:
        async with semaphore:
            try:
                # Resolve the Google token via the shared lock-coordinated
                # helper BEFORE opening any DB session (it manages its own
                # short-lived sessions + Redis lock). A confirmed-dead refresh
                # token is marked needs-reconnect inside the helper
                # (is_active=False + error recorded), so the user simply drops
                # out of the eligible set instead of erroring every cycle.
                access_token = await get_valid_access_token("google", uid)
                if not access_token:
                    return

                # Fetch upcoming events — external I/O, still no session held
                events = await _fetch_upcoming_events(access_token)

                async with sm.get_session() as session:
                    # Token is valid -- clear any prior errors
                    integration = await _get_google_integration(session, uid)
                    if integration and integration.error_count and integration.error_count > 0:
                        await clear_integration_errors(session, integration.id)

                    if not events:
                        return

                    # Check which events already have preps (with timestamps and status for update detection)
                    event_ids = [e["id"] for e in events]
                    existing_result = await session.execute(
                        select(MeetingPrep.event_id, MeetingPrep.created_at, MeetingPrep.status).where(
                            MeetingPrep.user_id == uid,
                            MeetingPrep.event_id.in_(event_ids),
                        )
                    )
                    existing_preps = {row[0]: {"created_at": row[1], "status": row[2]} for row in existing_result.all()}

                    # Generate or regenerate preps
                    for event in events:
                        # Skip events with fewer than 2 attendees (likely just a reminder)
                        if len(event.get("attendees", [])) < 2:
                            continue

                        needs_generation = False
                        existing = existing_preps.get(event["id"])
                        if not existing:
                            needs_generation = True
                        elif existing["status"] in ("failed", "pending"):
                            needs_generation = True
                        elif existing["status"] == "completed" and event.get("updated"):
                            # Event was updated after prep was generated -- regenerate
                            event_updated = datetime.fromisoformat(event["updated"].replace("Z", "+00:00"))
                            if existing["created_at"] and event_updated > existing["created_at"]:
                                logger.info("Event updated since prep was generated, regenerating",
                                            event_id=event["id"], event_updated=str(event_updated))
                                needs_generation = True

                        if not needs_generation:
                            continue

                        # For regeneration (event changed → existing completed
                        # prep must be replaced): delete the old prep and COMMIT
                        # it before generating, rather than wrapping the delete +
                        # generation in one savepoint. generate_meeting_prep runs
                        # two ~120s LLM calls; holding a transaction (or
                        # savepoint) open across them trips Postgres's
                        # idle_in_transaction_session_timeout (30s). The earlier
                        # "skip commit when nested" guard avoided breaking the
                        # caller's savepoint but kept the whole regen inside an
                        # open transaction — same root problem. Committing the
                        # delete first means generate_meeting_prep runs with no
                        # outer transaction and manages its own short-lived
                        # placeholder commit + post-LLM write. Tradeoff: if
                        # generation crashes, the stale prep is already gone and
                        # the next scheduler cycle regenerates it — acceptable for
                        # a background, idempotent job.
                        # Only the COMPLETED-but-stale case needs the caller to
                        # delete first: generate_meeting_prep early-returns None
                        # for a completed prep (won't replace it), so we clear it
                        # here. failed/pending/stale-generating rows are deleted
                        # INSIDE generate_meeting_prep, so we must NOT delete them
                        # here.
                        is_completed_regen = (
                            existing is not None and existing.get("status") == "completed"
                        )

                        try:
                            if is_completed_regen:
                                # Scope the delete narrowly so an overlapping
                                # scheduler run can't lose a row. We delete ONLY
                                # a completed prep that is no newer than the one
                                # we saw in the snapshot (`created_at`). A
                                # concurrent worker's freshly-inserted row is
                                # either status="generating" (excluded by the
                                # status filter) or has a newer created_at
                                # (excluded by the timestamp filter) — so this
                                # delete never removes another worker's new row.
                                del_where = [
                                    MeetingPrep.user_id == uid,
                                    MeetingPrep.event_id == event["id"],
                                    MeetingPrep.status == "completed",
                                ]
                                snap_created = existing.get("created_at")
                                if snap_created is not None:
                                    del_where.append(MeetingPrep.created_at <= snap_created)
                                await session.execute(delete(MeetingPrep).where(*del_where))
                                await session.commit()
                            await generate_meeting_prep(uid, event["id"], event, session)
                        except Exception as e:
                            logger.error(
                                "Failed to generate prep for event",
                                event_id=event["id"],
                                user_id=str(uid),
                                error=str(e),
                            )

            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    "Meeting prep: user processing failed",
                    user_id=str(uid),
                    error=str(e),
                )

    semaphore = asyncio.Semaphore(PREP_CONCURRENCY)
    tasks = [_process_user_prep(uid, email, semaphore) for uid, email in user_ids]
    await asyncio.gather(*tasks, return_exceptions=True)
