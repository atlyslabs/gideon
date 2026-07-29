"""
Hub Router — aggregated data endpoints for the frontend hub panel.
Provides emails, meetings, to-do items, and a combined summary.
"""

import os
import asyncio
from services.slack_client import get_slack_client
import re
from datetime import datetime, timezone, timedelta
from typing import Optional
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select, update, delete, and_, case, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
import structlog

from db import get_db
from db.database import AsyncPostgresSessionGideon
from db.models import ActivityLog, Conversation, EmailAutoDraft, Integration, MeetingPrep, Message, Todo, TodoSuggestion, TodoUpdate, User
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.delegation_service import (
    DelegationError,
    compute_next_reminder_at,
    delegate_or_reassign,
    get_slack_bot_token,
    get_slack_user_token,
    lookup_slack_user_ids,
    normalize_cadence,
    notify_completion,
    post_completion_in_thread,
    post_reassignment_in_thread,
)
from services.encryption_service import encrypt, decrypt
from services.email_auto_draft_service import delete_gmail_draft, update_gmail_draft
from services.oauth.google_oauth import GoogleOAuthProvider
from services.slack_format import markdown_to_slack_mrkdwn
from services.user_service import get_or_create_user

logger = structlog.get_logger()

# Base URL for deep-linking to a Slack message, e.g.
# "https://myworkspace.slack.com/archives". Unset (default) omits source links.
SLACK_ARCHIVE_BASE = os.environ.get("SLACK_ARCHIVE_BASE", "").rstrip("/")


router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class EmailItem(BaseModel):
    id: str
    subject: str
    sender: str
    snippet: str
    date: str
    is_read: bool


class EmailsResponse(BaseModel):
    connected: bool
    items: list[EmailItem] = []


class MeetingAttendeeItem(BaseModel):
    model_config = {"populate_by_name": True}
    email: str
    display_name: Optional[str] = None
    response_status: str = "needsAction"
    is_self: bool = Field(False, alias="self", serialization_alias="self")


class MeetingItem(BaseModel):
    id: str
    title: str
    start_time: str
    end_time: str
    location: str
    meet_link: str = ""
    attendees_count: int
    attendees: list[MeetingAttendeeItem] = []


class MeetingsResponse(BaseModel):
    connected: bool
    items: list[MeetingItem] = []
    date: Optional[str] = None


class TodoItem(BaseModel):
    id: str
    title: str
    status: Optional[str] = None
    due_date: Optional[str] = None
    url: str = ""
    last_edited: Optional[str] = None


class TodosResponse(BaseModel):
    connected: bool
    items: list[TodoItem] = []


class HubSummaryResponse(BaseModel):
    emails: EmailsResponse
    meetings: MeetingsResponse
    todos: TodosResponse


# --- Local Todo CRUD models ---

class DelegateeOut(BaseModel):
    name: str
    email: str
    avatar_url: Optional[str] = None


class TodoOut(BaseModel):
    id: UUID
    text: str = Field(validation_alias="content")
    completed: bool
    priority: str
    source: str
    source_detail: Optional[str] = None
    assigned_to_gideon: bool = False
    assigned_conversation_id: Optional[str] = None
    agent_status: Optional[str] = None
    parent_id: Optional[str] = None
    snoozed_until: Optional[str] = None
    delegated_to: Optional[list[DelegateeOut]] = None
    delegated_from_user_name: Optional[str] = None
    slack_channel_id: Optional[str] = None
    slack_thread_ts: Optional[str] = None
    reminder_cadence: Optional[str] = None
    subtasks: list["TodoOut"] = []
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_todo(cls, todo) -> "TodoOut":
        """Build TodoOut from a Todo ORM instance, converting UUIDs and datetimes."""
        delegated_from_name = None
        if todo.delegated_from_user_id:
            delegated_from_name = todo.delegated_from_user.name if todo.delegated_from_user else None
        return cls(
            id=todo.id,
            content=todo.content,
            completed=todo.completed,
            priority=todo.priority,
            source=todo.source,
            source_detail=todo.source_detail,
            assigned_to_gideon=todo.assigned_to_gideon,
            assigned_conversation_id=todo.assigned_conversation_id,
            agent_status=todo.agent_status,
            parent_id=str(todo.parent_id) if todo.parent_id else None,
            snoozed_until=todo.snoozed_until.isoformat() if todo.snoozed_until else None,
            delegated_to=todo.delegated_to,
            delegated_from_user_name=delegated_from_name,
            slack_channel_id=todo.slack_channel_id,
            slack_thread_ts=todo.slack_thread_ts,
            reminder_cadence=todo.reminder_cadence,
            subtasks=[cls.from_todo(s) for s in (todo.subtasks or [])],
            created_at=todo.created_at,
            updated_at=todo.updated_at,
        )


# Resolve forward reference for recursive subtasks field
TodoOut.model_rebuild()


class TodoCreateRequest(BaseModel):
    text: str
    priority: str = Field(default="none", pattern="^(none|low|medium|high|urgent)$")
    parent_id: Optional[UUID] = None


class TodoUpdateRequest(BaseModel):
    text: Optional[str] = None
    completed: Optional[bool] = None
    priority: Optional[str] = Field(default=None, pattern="^(none|low|medium|high|urgent)$")
    source_detail: Optional[str] = None
    snoozed_until: Optional[str] = None  # ISO datetime string, or null to unsnooze


class TodoListResponse(BaseModel):
    todos: list[TodoOut]
    delegated: list[TodoOut] = []
    delegated_to_me: list[TodoOut] = []
    snoozed: list[TodoOut] = []
    count: int


# --- Suggestion models ---

class SuggestionOut(BaseModel):
    id: UUID
    text: str = Field(validation_alias="content")
    detail: Optional[str] = None
    source_integration: Optional[str] = None
    source_url: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True
        populate_by_name = True

    @classmethod
    def from_db(cls, s: TodoSuggestion) -> "SuggestionOut":
        """Build the response, parsing the trailing tool-marker out of `detail`
        and turning it into a clickable source_url for Slack / Gmail.
        """
        clean_detail = s.detail or ""
        source_url: Optional[str] = None

        slack_match = re.search(
            r"\[Use read_slack_thread with channel=([^\s]+) and thread_ts=([^\]]+)\]",
            clean_detail,
        )
        gmail_match = re.search(
            r"\[Use read_email with email_id=([^\]]+)\]",
            clean_detail,
        )
        if slack_match:
            channel, ts = slack_match.group(1), slack_match.group(2).strip()
            ts_no_dot = ts.replace(".", "")
            source_url = f"{SLACK_ARCHIVE_BASE}/{channel}/p{ts_no_dot}" if SLACK_ARCHIVE_BASE else ""
            clean_detail = re.sub(
                r"\n?\[Use read_slack_thread[^\]]+\]", "", clean_detail
            ).strip()
        elif gmail_match:
            msg_id = gmail_match.group(1).strip()
            source_url = f"https://mail.google.com/mail/u/0/#inbox/{msg_id}"
            clean_detail = re.sub(
                r"\n?\[Use read_email[^\]]+\]", "", clean_detail
            ).strip()

        return cls(
            id=s.id,
            text=s.content,
            detail=clean_detail or None,
            source_integration=s.source_integration,
            source_url=source_url,
            created_at=s.created_at,
        )


class SuggestionListResponse(BaseModel):
    suggestions: list[SuggestionOut]
    count: int


class GenerateResponse(BaseModel):
    status: str


# ---------------------------------------------------------------------------
# Helpers — token retrieval & refresh
# ---------------------------------------------------------------------------

async def _get_integration(
    session: AsyncSession,
    user_id: UUID,
    provider: str,
) -> Optional[Integration]:
    """Fetch the active integration row for a user + provider."""
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
            Integration.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def _get_google_token(
    session: AsyncSession,
    integration: Integration,
) -> Optional[str]:
    """
    Decrypt the Google access token, refreshing it first if expired.
    Returns None on failure so the caller can degrade gracefully.
    """
    try:
        # Check expiry and refresh if needed (mirrors tool_executor.py)
        if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
            if not integration.refresh_token:
                logger.warning("Google token expired, no refresh token")
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
                logger.error("Google token refresh failed", error=str(e))
                return None

        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Google token", error=str(e))
        return None


async def _get_notion_token(integration: Integration) -> Optional[str]:
    """Decrypt Notion access token."""
    try:
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Notion token", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Data fetching — reuses patterns from existing tools
# ---------------------------------------------------------------------------

async def _fetch_emails(
    session: AsyncSession,
    user_id: UUID,
) -> EmailsResponse:
    """Read recent emails from activity_log (populated by background ingestion)."""
    # Check if google integration exists
    integration = await _get_integration(session, user_id, "google")
    if not integration:
        return EmailsResponse(connected=False)

    result = await session.execute(
        select(ActivityLog)
        .where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "gmail",
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(15)
    )
    activities = result.scalars().all()

    items: list[EmailItem] = []
    for a in activities:
        meta = a.extra_data or {}
        labels = meta.get("labels", [])
        items.append(EmailItem(
            id=str(a.reference_id or a.id),
            subject=a.title or "",
            sender=meta.get("from", ""),
            snippet=a.summary or "",
            date=a.created_at.isoformat() if a.created_at else "",
            is_read="UNREAD" not in labels,
        ))

    return EmailsResponse(connected=True, items=items)


async def _fetch_meetings(
    session: AsyncSession,
    user_id: UUID,
) -> MeetingsResponse:
    """Read today's meetings from activity_log (populated by background ingestion)."""
    from zoneinfo import ZoneInfo
    from db.models import User

    integration = await _get_integration(session, user_id, "google")
    if not integration:
        return MeetingsResponse(connected=False)

    # Get user timezone for correct "today" comparison
    user_result = await session.execute(select(User.timezone).where(User.id == user_id))
    user_tz_str = user_result.scalar_one_or_none() or "UTC"
    try:
        user_tz = ZoneInfo(user_tz_str)
    except Exception:
        user_tz = ZoneInfo("UTC")

    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(user_tz)
    today_local = now_local.date()

    result = await session.execute(
        select(ActivityLog)
        .where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "calendar",
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
    )
    activities = result.scalars().all()

    items: list[MeetingItem] = []
    for a in activities:
        meta = a.extra_data or {}
        start_time = meta.get("start_time", "")
        end_time = meta.get("end_time", "")

        # Skip all-day events (date-only, no "T" = no time component)
        if not start_time or "T" not in start_time:
            continue

        # Filter to today's upcoming events only (using user's local timezone)
        try:
            st = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            if st.tzinfo is None:
                st = st.replace(tzinfo=timezone.utc)
            if st.astimezone(user_tz).date() != today_local:
                continue
            # Skip past meetings (ended more than 30 min ago)
            if end_time and "T" in end_time:
                et = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if et.tzinfo is None:
                    et = et.replace(tzinfo=timezone.utc)
                if et < now_utc - timedelta(minutes=30):
                    continue
        except (ValueError, TypeError):
            continue

        # Build attendee list -- support both old format (list of emails) and new (list of dicts)
        raw_attendees = meta.get("attendees", [])
        attendee_items = []
        for att in raw_attendees:
            if isinstance(att, dict):
                attendee_items.append(MeetingAttendeeItem(
                    email=att.get("email", ""),
                    display_name=att.get("display_name"),
                    response_status=att.get("response_status", "needsAction"),
                    **{"self": att.get("self", False)},
                ))
            elif isinstance(att, str):
                attendee_items.append(MeetingAttendeeItem(email=att))

        items.append(MeetingItem(
            id=str(a.reference_id or a.id),
            title=a.title or "(No title)",
            start_time=start_time,
            end_time=end_time,
            location=meta.get("location", ""),
            meet_link=meta.get("meet_link", ""),
            attendees_count=len(raw_attendees),
            attendees=attendee_items,
        ))

    # Sort by start_time ascending
    items.sort(key=lambda m: m.start_time or "")

    return MeetingsResponse(
        connected=True,
        items=items,
        date=now_local.strftime("%Y-%m-%d"),
    )


async def _fetch_todos(
    session: AsyncSession,
    user_id: UUID,
) -> TodosResponse:
    """
    Fetch to-do items from Notion.
    Strategy: search for databases that look like task lists, then query
    the first match for its entries.
    """
    integration = await _get_integration(session, user_id, "notion")
    if not integration:
        return TodosResponse(connected=False)

    access_token = await _get_notion_token(integration)
    if not access_token:
        return TodosResponse(connected=False)

    try:
        import httpx
        from tools.notion_tool import NOTION_API, _notion_headers

        headers = _notion_headers(access_token)

        async with httpx.AsyncClient(timeout=10.0) as client:
            # Step 1: search for databases that look like task/todo lists
            search_resp = await client.post(
                f"{NOTION_API}/search",
                headers=headers,
                json={
                    "query": "",
                    "filter": {"value": "database", "property": "object"},
                    "page_size": 5,
                },
            )

        if search_resp.status_code != 200:
            logger.error("Notion search failed", status=search_resp.status_code)
            return TodosResponse(connected=True, items=[])

        databases = search_resp.json().get("results", [])
        if not databases:
            return TodosResponse(connected=True, items=[])

        # Pick the first database (most relevant) and query its entries
        db_id = databases[0]["id"]

        async with httpx.AsyncClient(timeout=10.0) as client:
            query_resp = await client.post(
                f"{NOTION_API}/databases/{db_id}/query",
                headers=headers,
                json={"page_size": 15},
            )

        if query_resp.status_code != 200:
            logger.error("Notion database query failed", status=query_resp.status_code)
            return TodosResponse(connected=True, items=[])

        items: list[TodoItem] = []
        for page in query_resp.json().get("results", []):
            props = page.get("properties", {})

            # Extract title
            title = ""
            for prop in props.values():
                if prop.get("type") == "title":
                    title = "".join(
                        t.get("plain_text", "") for t in prop.get("title", [])
                    )
                    break

            # Extract status (status, select, or checkbox property)
            status = None
            for prop_name, prop in props.items():
                ptype = prop.get("type")
                if ptype == "status" and prop.get("status"):
                    status = prop["status"].get("name")
                    break
                if ptype == "select" and prop_name.lower() in ("status", "state"):
                    sel = prop.get("select")
                    if sel:
                        status = sel.get("name")
                    break
                if ptype == "checkbox" and prop_name.lower() in ("done", "complete", "completed"):
                    status = "Done" if prop.get("checkbox") else "Not started"
                    break

            # Extract due date
            due_date = None
            for prop_name, prop in props.items():
                if prop.get("type") == "date" and prop_name.lower() in (
                    "due", "due date", "deadline", "date",
                ):
                    date_obj = prop.get("date")
                    if date_obj:
                        due_date = date_obj.get("start")
                    break

            items.append(TodoItem(
                id=page["id"],
                title=title,
                status=status,
                due_date=due_date,
                url=page.get("url", ""),
                last_edited=page.get("last_edited_time"),
            ))

        return TodosResponse(connected=True, items=items)

    except Exception as e:
        logger.error("Failed to fetch todos for hub", error=str(e))
        return TodosResponse(connected=True, items=[])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/emails", response_model=EmailsResponse)
async def get_emails(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Recent emails from Gmail for the hub panel."""
    user = await get_or_create_user(session, auth_user)
    return await _fetch_emails(session, user.id)


@router.get("/meetings", response_model=MeetingsResponse)
async def get_meetings(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Today's calendar events for the hub panel."""
    user = await get_or_create_user(session, auth_user)
    return await _fetch_meetings(session, user.id)


@router.get("/todos/notion", response_model=TodosResponse)
async def get_notion_todos(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """To-do items from Notion for the hub panel."""
    user = await get_or_create_user(session, auth_user)
    return await _fetch_todos(session, user.id)


@router.get("/summary", response_model=HubSummaryResponse)
async def get_summary(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Combined hub summary — emails, meetings, and todos fetched in parallel."""
    user = await get_or_create_user(session, auth_user)

    # Run serially — AsyncSession is not concurrency-safe
    emails = await _fetch_emails(session, user.id)
    meetings = await _fetch_meetings(session, user.id)
    todos = await _fetch_todos(session, user.id)

    return HubSummaryResponse(
        emails=emails,
        meetings=meetings,
        todos=todos,
    )


# ---------------------------------------------------------------------------
# Todo CRUD
# ---------------------------------------------------------------------------

@router.get("/todos", response_model=TodoListResponse)
async def list_todos(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List top-level todos split into active, delegated, delegated-to-me, and snoozed."""
    user = await get_or_create_user(session, auth_user)
    now = datetime.now(timezone.utc)

    # Only top-level todos (subtasks are loaded via relationship)
    result = await session.execute(
        select(Todo)
        .options(selectinload(Todo.subtasks), selectinload(Todo.delegated_from_user))
        .where(Todo.user_id == user.id, Todo.parent_id.is_(None))
        .order_by(
            case((Todo.completed == False, 0), else_=1),
            Todo.created_at.desc(),
        )
    )
    all_todos = result.scalars().unique().all()

    active: list[TodoOut] = []
    delegated: list[TodoOut] = []
    delegated_to_me: list[TodoOut] = []
    snoozed: list[TodoOut] = []

    for t in all_todos:
        todo_out = TodoOut.from_todo(t)
        # Completed rows always land in `active` so the FE's completedTodos
        # filter (sortedTodos.filter(t => t.completed)) picks them up. Without
        # this, a delegated row that gets marked done stays pinned in the
        # Delegated / Delegated-to-me section forever.
        # Mirrors stay "delegated to me" even if the delegator's original was
        # deleted — `delegated_from_todo_id` is `ON DELETE SET NULL`, but the
        # `delegated_from_user_id` survives, so route on either field.
        is_mirror = bool(t.delegated_from_todo_id or t.delegated_from_user_id)
        if t.completed:
            active.append(todo_out)
        elif t.snoozed_until and t.snoozed_until > now:
            snoozed.append(todo_out)
        elif is_mirror:
            delegated_to_me.append(todo_out)
        elif t.delegated_to:
            delegated.append(todo_out)
        else:
            active.append(todo_out)

    total = len(active) + len(delegated) + len(delegated_to_me) + len(snoozed)
    return TodoListResponse(
        todos=active,
        delegated=delegated,
        delegated_to_me=delegated_to_me,
        snoozed=snoozed,
        count=total,
    )


@router.post("/todos", response_model=TodoOut, status_code=201)
async def create_todo(
    body: TodoCreateRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Create a new todo (optionally as a sub-todo)."""
    user = await get_or_create_user(session, auth_user)

    if body.parent_id:
        # Validate parent exists and belongs to this user
        parent_result = await session.execute(
            select(Todo).where(Todo.id == body.parent_id, Todo.user_id == user.id)
        )
        parent = parent_result.scalar_one_or_none()
        if not parent:
            raise HTTPException(status_code=404, detail="Parent todo not found")
        if parent.delegated_from_todo_id:
            raise HTTPException(status_code=403, detail="Cannot add subtasks to a todo delegated to you — it is read-only")

    todo = Todo(
        user_id=user.id,
        content=body.text,
        priority=body.priority,
        parent_id=body.parent_id,
    )
    session.add(todo)
    await session.flush()
    await session.refresh(todo)

    from services.events_service import track_todo_created
    track_todo_created(str(user.id), source="manual")
    return TodoOut.from_todo(todo)


@router.patch("/todos/{todo_id}", response_model=TodoOut)
async def update_todo(
    todo_id: UUID,
    body: TodoUpdateRequest,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update an existing todo."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo)
        .options(selectinload(Todo.delegated_from_user))
        .where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    update_data = body.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No fields to update")

    # Delegated-to-me mirrors are largely read-only. The only allowed mutation
    # is marking `completed=true` so the delegatee can mark a task done from
    # Gideon and let the delegator know. Reopening (completed=false) is
    # rejected — uncompleting a mirror would leave the delegator's original
    # out of sync since propagation only fires on the false→true transition.
    if todo.delegated_from_todo_id:
        if set(update_data.keys()) - {"completed"} or update_data.get("completed") is not True:
            raise HTTPException(
                status_code=403,
                detail="This todo was delegated to you — only marking it complete is allowed here. Use the Slack thread for other updates.",
            )

    # Map Pydantic field "text" to ORM attribute "content"
    if "text" in update_data:
        update_data["content"] = update_data.pop("text")

    # Parse snoozed_until: ISO string -> datetime or None
    if "snoozed_until" in update_data:
        raw = update_data["snoozed_until"]
        if raw is None:
            update_data["snoozed_until"] = None
        else:
            try:
                update_data["snoozed_until"] = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid snoozed_until format, expected ISO datetime")

    update_data["updated_at"] = datetime.now(timezone.utc)

    was_incomplete = not todo.completed
    await session.execute(
        update(Todo)
        .where(Todo.id == todo_id, Todo.user_id == user.id)
        .values(**update_data)
    )
    await session.refresh(todo)

    if was_incomplete and update_data.get("completed"):
        from services.events_service import track_todo_completed
        track_todo_completed(str(user.id), source=todo.source or "manual")
        await _propagate_delegation_completion(session, todo, completed_by=user, background_tasks=background_tasks)
    return TodoOut.from_todo(todo)


async def _propagate_delegation_completion(
    session: AsyncSession,
    todo: Todo,
    completed_by: User,
    background_tasks: BackgroundTasks,
) -> None:
    """When a delegated todo is marked done, mirror the completion to the
    paired side (delegator <-> delegatee mirror), post a 'Marked done by X'
    message in the delegation Slack thread, and DM the other party with a
    permalink back to that thread.

    `todo` is the row that was just completed (could be either the delegator's
    original or a delegatee's mirror). `completed_by` is the actor who clicked
    Done. Slack announcements are queued on `background_tasks` so they only
    fire after the request's DB commit — never for changes that rolled back.
    """
    now = datetime.now(timezone.utc)
    completed_by_name = completed_by.name or completed_by.email or "Someone"

    # Case 1: a mirror just got completed -> mark the original done and DM the delegator.
    if todo.delegated_from_todo_id:
        original_res = await session.execute(
            select(Todo).where(Todo.id == todo.delegated_from_todo_id)
        )
        original = original_res.scalar_one_or_none()
        if original and not original.completed:
            await session.execute(
                update(Todo)
                .where(Todo.id == original.id)
                .values(completed=True, next_reminder_at=None, updated_at=now)
            )
            session.add(TodoUpdate(
                todo_id=original.id,
                author_name=completed_by_name,
                author_email=completed_by.email,
                content="Marked done by the delegatee.",
                source="delegation",
            ))
            delegator_res = await session.execute(select(User).where(User.id == original.user_id))
            delegator = delegator_res.scalar_one_or_none()
            if delegator:
                background_tasks.add_task(
                    _announce_completion,
                    delegator_user_id=delegator.id,
                    channel_id=original.slack_channel_id,
                    thread_ts=original.slack_thread_ts,
                    completed_by_user=completed_by,
                    notify_user=delegator,
                    todo_text=original.content or todo.content or "",
                    direction="delegatee_completed",
                )
        return

    # Case 2: an outbound delegation just got completed by the delegator -> mark
    # all linked mirrors done and DM each delegatee.
    if todo.delegated_to:
        mirrors_res = await session.execute(
            select(Todo).where(Todo.delegated_from_todo_id == todo.id)
        )
        mirrors = list(mirrors_res.scalars().all())
        delegatee_users: list[User] = []
        for mirror in mirrors:
            if mirror.completed:
                continue
            await session.execute(
                update(Todo)
                .where(Todo.id == mirror.id)
                .values(completed=True, updated_at=now)
            )
            session.add(TodoUpdate(
                todo_id=mirror.id,
                author_name=completed_by_name,
                author_email=completed_by.email,
                content="Marked done by the delegator.",
                source="delegation",
            ))
            delegatee_res = await session.execute(select(User).where(User.id == mirror.user_id))
            delegatee = delegatee_res.scalar_one_or_none()
            if delegatee:
                delegatee_users.append(delegatee)
        # Drop any pending reminder cursor on the original.
        await session.execute(
            update(Todo).where(Todo.id == todo.id).values(next_reminder_at=None)
        )
        # Post once in the delegation thread + DM each delegatee in the
        # background. Done in a single helper so the permalink is fetched
        # once and reused across N DMs.
        if todo.slack_channel_id and todo.slack_thread_ts:
            background_tasks.add_task(
                _announce_delegator_completion,
                delegator_user_id=todo.user_id,
                channel_id=todo.slack_channel_id,
                thread_ts=todo.slack_thread_ts,
                completed_by_user=completed_by,
                delegatee_users=delegatee_users,
                todo_text=todo.content or "",
            )


async def _announce_completion(
    delegator_user_id: UUID,
    channel_id: Optional[str],
    thread_ts: Optional[str],
    completed_by_user: User,
    notify_user: User,
    todo_text: str,
    direction: str,
) -> None:
    """Post completion in the delegation thread + DM `notify_user` with a
    permalink back to the thread. Used when a delegatee completes a mirror."""
    completed_by_name = completed_by_user.name or completed_by_user.email or "Someone"
    permalink: Optional[str] = None
    if channel_id and thread_ts:
        permalink = await post_completion_in_thread(
            delegator_user_id=delegator_user_id,
            channel_id=channel_id,
            thread_ts=thread_ts,
            completed_by_name=completed_by_name,
            completed_by_user_id=completed_by_user.id,
        )
    await notify_completion(
        completed_by_user=completed_by_user,
        notify_user=notify_user,
        todo_text=todo_text,
        direction=direction,
        thread_permalink=permalink,
    )


async def _announce_delegator_completion(
    delegator_user_id: UUID,
    channel_id: str,
    thread_ts: str,
    completed_by_user: User,
    delegatee_users: list[User],
    todo_text: str,
) -> None:
    """Post once in the delegation thread + DM every delegatee with the same
    permalink. Used when the delegator marks the outbound todo done."""
    completed_by_name = completed_by_user.name or completed_by_user.email or "Someone"
    permalink = await post_completion_in_thread(
        delegator_user_id=delegator_user_id,
        channel_id=channel_id,
        thread_ts=thread_ts,
        completed_by_name=completed_by_name,
        completed_by_user_id=completed_by_user.id,
    )
    for delegatee in delegatee_users:
        await notify_completion(
            completed_by_user=completed_by_user,
            notify_user=delegatee,
            todo_text=todo_text,
            direction="delegator_completed",
            thread_permalink=permalink,
        )


@router.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(
    todo_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a todo."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo).where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    # Active delegated-to-me mirrors are read-only — the delegator is still
    # tracking the task on their side. Once the mirror is completed the link
    # is conceptually done, so allow the delegatee to clear it from their
    # Completed bucket.
    if todo.delegated_from_todo_id and not todo.completed:
        raise HTTPException(status_code=403, detail="This todo was delegated to you — it is read-only and cannot be deleted.")

    await session.execute(
        delete(Todo).where(Todo.id == todo_id, Todo.user_id == user.id)
    )


# ---------------------------------------------------------------------------
# Snooze / Unsnooze
# ---------------------------------------------------------------------------

class SnoozeRequest(BaseModel):
    until: str  # ISO datetime string


@router.post("/todos/{todo_id}/snooze", response_model=TodoOut)
async def snooze_todo(
    todo_id: UUID,
    body: SnoozeRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Snooze a todo until a given datetime."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo)
        .options(selectinload(Todo.delegated_from_user))
        .where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    if todo.delegated_from_todo_id:
        raise HTTPException(status_code=403, detail="This todo was delegated to you — it is read-only.")

    try:
        snoozed_until = datetime.fromisoformat(body.until.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid datetime format, expected ISO datetime")

    now = datetime.now(timezone.utc)
    await session.execute(
        update(Todo)
        .where(Todo.id == todo_id, Todo.user_id == user.id)
        .values(snoozed_until=snoozed_until, updated_at=now)
    )
    await session.refresh(todo)

    return TodoOut.from_todo(todo)


@router.post("/todos/{todo_id}/unsnooze", response_model=TodoOut)
async def unsnooze_todo(
    todo_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Clear snooze on a todo."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo)
        .options(selectinload(Todo.delegated_from_user))
        .where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    if todo.delegated_from_todo_id:
        raise HTTPException(status_code=403, detail="This todo was delegated to you — it is read-only.")

    now = datetime.now(timezone.utc)
    await session.execute(
        update(Todo)
        .where(Todo.id == todo_id, Todo.user_id == user.id)
        .values(snoozed_until=None, updated_at=now)
    )
    await session.refresh(todo)

    return TodoOut.from_todo(todo)


# ---------------------------------------------------------------------------
# Assign to Gideon
# ---------------------------------------------------------------------------

class AssignResponse(BaseModel):
    conversation_id: str
    status: str


@router.post("/todos/{todo_id}/assign", response_model=AssignResponse)
async def assign_todo_to_gideon(
    todo_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Assign a todo to Gideon — creates conversation + user message.
    The frontend resumes streaming via /chat/send with resume=True."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo).where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    if todo.assigned_to_gideon and todo.agent_status == "working":
        raise HTTPException(status_code=400, detail="Todo is already being worked on")

    # Create a conversation for the agent work
    title = (todo.content[:50] + "...") if len(todo.content) > 50 else todo.content
    conv = Conversation(user_id=user.id, title=title)
    session.add(conv)
    await session.flush()
    await session.refresh(conv)

    # Build initial message content
    initial_content = todo.content
    if todo.source_detail:
        initial_content += f"\n\n---\nContext:\n{todo.source_detail}"

    # Store the initial user message
    msg = Message(
        conversation_id=conv.id,
        role="user",
        content=initial_content,
    )
    session.add(msg)

    # Update todo with assignment info
    todo.assigned_to_gideon = True
    todo.assigned_conversation_id = str(conv.id)
    todo.agent_status = "working"
    todo.updated_at = datetime.now(timezone.utc)

    # Commit explicitly so the data is visible to stream_chat's separate session
    await session.commit()

    return AssignResponse(conversation_id=str(conv.id), status="working")


# ---------------------------------------------------------------------------
# Delegate Todo
# ---------------------------------------------------------------------------

class DelegateRequest(BaseModel):
    delegatees: list[DelegateeOut]  # [{name, email}]
    message: Optional[str] = None
    # One of {"never", "daily", "every_2_days", "weekly"}. Anything else is
    # normalized to None (no reminders). Default preserves the historical
    # behavior of not auto-nudging.
    reminder_cadence: Optional[str] = None
    # Optional ISO datetime. When set, anchors the first reminder to this time
    # (instead of `now + cadence_delta`). Subsequent reminders still bump by
    # the cadence interval. If `reminder_cadence` is None, this becomes a
    # one-shot follow-up — `next_reminder_at` is cleared after the first fire.
    follow_up_at: Optional[str] = None


class DelegateResponse(BaseModel):
    status: str
    delegated_to: list[DelegateeOut]
    # Delegatees who have no Gideon account yet — they got the Slack DM but no
    # mirror was created in their Gideon hub. Empty when everyone is on Gideon.
    not_on_gideon: list[DelegateeOut] = []


class TodoUpdateOut(BaseModel):
    id: UUID
    author_name: str
    author_email: Optional[str] = None
    content: str
    source: str
    created_at: datetime

    class Config:
        from_attributes = True


class TodoUpdatesResponse(BaseModel):
    updates: list[TodoUpdateOut]
    count: int


@router.post("/todos/{todo_id}/delegate", response_model=DelegateResponse)
async def delegate_todo(
    todo_id: UUID,
    body: DelegateRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delegate (or re-delegate) a todo via Slack group DM.

    Thin wrapper around `services.delegation_service.delegate_or_reassign`.
    The same function is invoked by the LLM `delegate_todo` tool so both
    paths share semantics: REPLACE-on-delegatees, fresh-DM-on-change,
    notice-in-old-thread, mirror cleanup.
    """
    user = await get_or_create_user(session, auth_user)
    # delegate_or_reassign opens its OWN session for Phase 1, so if
    # get_or_create_user just inserted a new row we have to commit the
    # request-scoped session first — otherwise the helper's fresh
    # connection won't see the new user and returns 404. Also drops the
    # connection back to the pool before the multi-second Slack roundtrip.
    await session.commit()
    try:
        outcome = await delegate_or_reassign(
            user_id=user.id,
            todo_id=todo_id,
            delegatees=[
                {"name": d.name, "email": d.email, "avatar_url": d.avatar_url}
                for d in body.delegatees
            ],
            message=body.message,
            reminder_cadence=body.reminder_cadence,
            follow_up_at_iso=body.follow_up_at,
        )
    except DelegationError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)
    return DelegateResponse(
        status=outcome.status,
        delegated_to=[
            DelegateeOut(name=d.name, email=d.email, avatar_url=d.avatar_url)
            for d in outcome.delegated_to
        ],
        not_on_gideon=[
            DelegateeOut(name=d.name, email=d.email, avatar_url=d.avatar_url)
            for d in outcome.not_on_gideon
        ],
    )


@router.get("/todos/{todo_id}/updates", response_model=TodoUpdatesResponse)
async def get_todo_updates(
    todo_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get timeline updates for a delegated todo."""
    user = await get_or_create_user(session, auth_user)

    # Verify todo belongs to user
    result = await session.execute(
        select(Todo).where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Todo not found")

    result = await session.execute(
        select(TodoUpdate)
        .where(TodoUpdate.todo_id == todo_id)
        .order_by(TodoUpdate.created_at.asc())
    )
    updates = result.scalars().all()

    return TodoUpdatesResponse(
        updates=[TodoUpdateOut.model_validate(u) for u in updates],
        count=len(updates),
    )


@router.post("/todos/{todo_id}/nudge", status_code=200)
async def nudge_todo(
    todo_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Send a nudge message in the delegation Slack thread."""

    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Todo).where(Todo.id == todo_id, Todo.user_id == user.id)
    )
    todo = result.scalar_one_or_none()
    if not todo:
        raise HTTPException(status_code=404, detail="Todo not found")

    if not todo.slack_channel_id or not todo.slack_thread_ts:
        raise HTTPException(status_code=400, detail="No Slack thread linked to this todo")

    slack_token = await get_slack_user_token(session, user.id) or await get_slack_bot_token(session, user.id)
    if not slack_token:
        raise HTTPException(status_code=400, detail="Slack not connected")

    # Build mentions for delegatees — cache-first via the shared helper so
    # we only hit users.lookupByEmail for delegatees missing slack_user_id.
    # Newly-resolved IDs get persisted below so subsequent nudges hit cache.
    from services.delegation_service import resolve_delegatee_slack_ids_cached
    bot_token = await get_slack_bot_token(session, user.id) or slack_token
    ids, updated_delegatees = await resolve_delegatee_slack_ids_cached(todo, bot_token)
    mentions = " ".join(f"<@{sid}>" for sid in ids)
    nudge_text = f"{mentions} any updates on this?" if mentions else "Any updates on this?"

    client = get_slack_client(token=slack_token)
    try:
        await client.chat_postMessage(
            channel=todo.slack_channel_id,
            thread_ts=todo.slack_thread_ts,
            text=nudge_text,
        )
    except Exception as e:
        logger.error("Failed to send nudge", error=str(e))
        raise HTTPException(status_code=500, detail="Failed to send nudge message")

    # Persist any slack_user_ids the helper just resolved via API so future
    # nudges/reminders on this todo skip the lookup. Done AFTER the post so a
    # failed post doesn't write half-state, though even on failure the cache
    # would be valid — keeping the conservative ordering for now.
    if updated_delegatees is not None:
        await session.execute(
            update(Todo)
            .where(Todo.id == todo_id)
            .values(delegated_to=updated_delegatees)
        )

    # Log nudge in timeline
    nudge_entry = TodoUpdate(
        todo_id=todo_id,
        author_name=user.name or user.email,
        author_email=user.email,
        content="Sent a status check",
        source="nudge",
    )
    session.add(nudge_entry)
    await session.flush()

    return {"status": "nudged"}


# ---------------------------------------------------------------------------
# Suggestions
# ---------------------------------------------------------------------------

@router.get("/suggestions", response_model=SuggestionListResponse)
async def list_suggestions(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get active suggestions (not dismissed, not added as todo)."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(TodoSuggestion)
        .where(
            TodoSuggestion.user_id == user.id,
            TodoSuggestion.dismissed == False,
            TodoSuggestion.added_as_todo == False,
        )
        .order_by(TodoSuggestion.created_at.desc())
    )
    suggestions = result.scalars().all()

    return SuggestionListResponse(
        suggestions=[SuggestionOut.from_db(s) for s in suggestions],
        count=len(suggestions),
    )


@router.post("/suggestions/generate", response_model=GenerateResponse)
async def generate_suggestions(
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Trigger AI suggestion generation in background."""
    from services.suggestion_service import generate_suggestions as _generate

    user = await get_or_create_user(session, auth_user)
    user_id = user.id

    # generate_suggestions manages its own short-lived sessions (no connection
    # held across its LLM call), so it can be scheduled directly.
    background_tasks.add_task(_generate, user_id)

    return GenerateResponse(status="generating")


@router.post("/suggestions/{suggestion_id}/add", response_model=TodoOut)
async def add_suggestion_as_todo(
    suggestion_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Convert a suggestion to a todo."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(TodoSuggestion).where(
            TodoSuggestion.id == suggestion_id,
            TodoSuggestion.user_id == user.id,
        )
    )
    suggestion = result.scalar_one_or_none()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    if suggestion.added_as_todo:
        raise HTTPException(status_code=400, detail="Suggestion already added as todo")

    # Create todo from suggestion
    todo = Todo(
        user_id=user.id,
        content=suggestion.content,
        priority="none",
        source="suggestion",
        source_detail=suggestion.detail,
    )
    session.add(todo)

    # Mark suggestion as added
    suggestion.added_as_todo = True
    await session.flush()
    await session.refresh(todo)

    from services.events_service import track_suggestion_accepted
    track_suggestion_accepted(str(user.id))
    return TodoOut.from_todo(todo)


@router.post("/suggestions/{suggestion_id}/dismiss", status_code=204)
async def dismiss_suggestion(
    suggestion_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Dismiss a suggestion."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(TodoSuggestion).where(
            TodoSuggestion.id == suggestion_id,
            TodoSuggestion.user_id == user.id,
        )
    )
    suggestion = result.scalar_one_or_none()
    if not suggestion:
        raise HTTPException(status_code=404, detail="Suggestion not found")

    suggestion.dismissed = True
    await session.flush()

    from services.events_service import track_suggestion_dismissed
    track_suggestion_dismissed(str(user.id))


# ---------------------------------------------------------------------------
# Meeting Prep
# ---------------------------------------------------------------------------

class MeetingPrepResponse(BaseModel):
    id: UUID
    event_id: str
    title: str
    summary: Optional[str] = None
    prep_content: Optional[str] = None
    attendees: Optional[list] = None
    event_start: str
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


@router.get("/meetings/{event_id}/prep", response_model=MeetingPrepResponse)
async def get_meeting_prep(
    event_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get the AI-generated meeting prep for a specific calendar event."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(MeetingPrep).where(
            MeetingPrep.user_id == user.id,
            MeetingPrep.event_id == event_id,
        )
    )
    prep = result.scalar_one_or_none()
    if not prep:
        raise HTTPException(status_code=404, detail="Meeting prep not found for this event")

    from services.events_service import track_meeting_prep_viewed
    track_meeting_prep_viewed(str(user.id), event_id=event_id)

    return MeetingPrepResponse(
        id=prep.id,
        event_id=prep.event_id,
        title=prep.title,
        summary=prep.summary,
        prep_content=prep.prep_content,
        attendees=prep.attendees,
        event_start=prep.event_start.isoformat() if prep.event_start else "",
        status=prep.status,
        created_at=prep.created_at,
        updated_at=prep.updated_at,
    )


# ---------------------------------------------------------------------------
# Analytics — Google Sheets export
# ---------------------------------------------------------------------------

class CreateSheetRequest(BaseModel):
    title: str
    columns: list[str]
    rows: list[dict]

class CreateSheetResponse(BaseModel):
    spreadsheet_id: str
    url: str

@router.post("/analytics/create-sheet", response_model=CreateSheetResponse)
async def create_analytics_sheet(
    body: CreateSheetRequest,
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """Create a Google Sheet from analytics query results."""
    import asyncio
    user = await get_or_create_user(session, auth_user)

    # Get Google token
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user.id,
            Integration.provider == "google",
            Integration.is_active == True,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail="Google integration not connected")

    token = decrypt(integration.access_token)

    # Refresh if expired
    if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
        if integration.refresh_token:
            new_tokens = await GoogleOAuthProvider().refresh_access_token(decrypt(integration.refresh_token))
            integration.access_token = encrypt(new_tokens.access_token)
            if new_tokens.refresh_token:
                integration.refresh_token = encrypt(new_tokens.refresh_token)
            integration.token_expiry = new_tokens.token_expiry
            token = new_tokens.access_token

    # Commit DB changes (token refresh) and close session before external API calls
    await session.commit()

    from tools.google_sheets_tool import _get_sheets_service

    def _create_and_populate():
        service = _get_sheets_service(token)

        # Create spreadsheet
        spreadsheet = service.spreadsheets().create(body={
            "properties": {"title": body.title},
            "sheets": [{"properties": {"title": "Data"}}],
        }).execute()

        spreadsheet_id = spreadsheet["spreadsheetId"]
        url = spreadsheet.get("spreadsheetUrl", f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}")

        # Write header + rows
        values = [body.columns]
        for row in body.rows:
            values.append([row.get(col, "") for col in body.columns])

        service.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id,
            range="Data!A1",
            valueInputOption="USER_ENTERED",
            body={"values": values},
        ).execute()

        # Move to Gideon/Sheets/ folder
        try:
            from services.file_tracking_service import move_file_to_gideon_folder
            drive_url = move_file_to_gideon_folder(token, spreadsheet_id, "Sheets")
            if drive_url:
                url = drive_url
        except Exception:
            pass

        return {"spreadsheet_id": spreadsheet_id, "url": url}

    result = await asyncio.to_thread(_create_and_populate)
    return CreateSheetResponse(**result)


# ---------------------------------------------------------------------------
# Email Auto-Drafts
# ---------------------------------------------------------------------------

class EmailDraftItem(BaseModel):
    id: str
    email_id: str
    thread_id: str | None = None
    sender_name: str | None = None
    sender_email: str
    sender_avatar_url: str | None = None
    subject: str
    email_body: str | None = None
    email_date: str | None = None
    draft_body: str | None = None
    status: str = "ready"


class EmailDraftsResponse(BaseModel):
    items: list[EmailDraftItem] = []
    count: int = 0


@router.get("/email-drafts", response_model=EmailDraftsResponse)
async def get_email_drafts(
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """List ready auto-draft replies for the current user."""
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(EmailAutoDraft).where(
            EmailAutoDraft.user_id == user.id,
            EmailAutoDraft.status == "ready",
        ).order_by(EmailAutoDraft.email_date.desc())
    )
    drafts = result.scalars().all()

    # Deduplicate by thread: keep only the latest draft per thread_id.
    # Drafts are ordered by email_date DESC, so first seen per thread is newest.
    # Duplicates are marked skipped and their Gmail drafts cleaned up.
    seen_threads: set[str] = set()
    deduped: list = []
    duplicates: list = []
    for d in drafts:
        if d.thread_id and d.thread_id in seen_threads:
            duplicates.append(d)
            continue
        if d.thread_id:
            seen_threads.add(d.thread_id)
        deduped.append(d)

    # Build response BEFORE commit (avoids expire-on-commit lazy loads)
    items = [
        EmailDraftItem(
            id=str(d.id),
            email_id=d.email_id,
            thread_id=d.thread_id,
            sender_name=d.sender_name,
            sender_email=d.sender_email,
            sender_avatar_url=d.sender_avatar_url,
            subject=d.subject,
            email_body=d.email_body,
            email_date=d.email_date.isoformat() if d.email_date else None,
            draft_body=d.draft_body,
            status=d.status,
        )
        for d in deduped
    ]

    # Mark duplicates as skipped and clean up Gmail drafts fire-and-forget
    if duplicates:
        gmail_draft_ids_to_delete: list[str] = []
        for d in duplicates:
            if d.gmail_draft_id:
                gmail_draft_ids_to_delete.append(d.gmail_draft_id)
            d.status = "skipped"
            d.gmail_draft_id = None
            d.updated_at = datetime.now(timezone.utc)
        await session.commit()

        if gmail_draft_ids_to_delete:
            async def _cleanup_gmail_drafts(uid, draft_ids):
                sm = AsyncPostgresSessionGideon()
                async with sm.get_session() as s:
                    int_result = await s.execute(
                        select(Integration).where(
                            Integration.user_id == uid,
                            Integration.provider == "google",
                            Integration.is_active == True,
                        )
                    )
                    integration = int_result.scalar_one_or_none()
                    if integration:
                        token = await _get_google_token(s, integration)
                        if token:
                            for gid in draft_ids:
                                try:
                                    await asyncio.to_thread(delete_gmail_draft, token, gid)
                                except Exception as e:
                                    logger.debug("Failed to delete duplicate Gmail draft", gmail_draft_id=gid, error=str(e))

            asyncio.create_task(_cleanup_gmail_drafts(user.id, gmail_draft_ids_to_delete))

    return EmailDraftsResponse(items=items, count=len(items))


@router.post("/email-drafts/{draft_id}/send")
async def send_email_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """Send an auto-draft reply via Gmail."""
    import base64
    from email.mime.text import MIMEText

    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(EmailAutoDraft).where(
            EmailAutoDraft.id == UUID(draft_id),
            EmailAutoDraft.user_id == user.id,
        )
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Get Google token
    int_result = await session.execute(
        select(Integration).where(
            Integration.user_id == user.id,
            Integration.provider == "google",
            Integration.is_active == True,
        )
    )
    integration = int_result.scalar_one_or_none()
    if not integration:
        raise HTTPException(status_code=400, detail="Google not connected")

    token = decrypt(integration.access_token)
    if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
        if integration.refresh_token:
            new_tokens = await GoogleOAuthProvider().refresh_access_token(decrypt(integration.refresh_token))
            integration.access_token = encrypt(new_tokens.access_token)
            if new_tokens.refresh_token:
                integration.refresh_token = encrypt(new_tokens.refresh_token)
            integration.token_expiry = new_tokens.token_expiry
            await session.flush()
            token = new_tokens.access_token
        else:
            raise HTTPException(status_code=400, detail="Google token expired. Please reconnect.")

    user_name = user.name or ""

    def _send():
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        service = build("gmail", "v1", credentials=Credentials(token=token), cache_discovery=False)

        # Get original message for reply headers
        original = service.users().messages().get(
            userId="me", id=draft.email_id, format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID"],
        ).execute()
        headers = {h["name"].lower(): h["value"] for h in original.get("payload", {}).get("headers", [])}

        reply_to = headers.get("from", draft.sender_email)
        subject = headers.get("subject", draft.subject)
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        message_id = headers.get("message-id", "")

        # Get user's display name for the From header
        profile = service.users().getProfile(userId="me").execute()
        user_email = profile.get("emailAddress", "")

        # Build CC list: original To + Cc, excluding the user's own email and the sender (who goes in To)
        from email.utils import parseaddr
        original_to = headers.get("to", "")
        original_cc = headers.get("cc", "")
        all_recipients = [r.strip() for r in f"{original_to},{original_cc}".split(",") if r.strip()]
        sender_addr = parseaddr(reply_to)[1].lower()
        cc_list = []
        for r in all_recipients:
            addr = parseaddr(r)[1].lower()
            if addr and addr != user_email.lower() and addr != sender_addr:
                cc_list.append(r)

        msg = MIMEText(draft.draft_body, "plain")
        if user_name:
            msg["from"] = f"{user_name} <{user_email}>"
        msg["to"] = reply_to
        if cc_list:
            msg["cc"] = ", ".join(cc_list)
        msg["subject"] = subject
        if message_id:
            msg["In-Reply-To"] = message_id
            msg["References"] = message_id

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
        service.users().messages().send(
            userId="me",
            body={"raw": raw, "threadId": draft.thread_id or ""},
        ).execute()

    await asyncio.to_thread(_send)

    # Capture data needed after session close
    stale_gmail_draft_id = draft.gmail_draft_id

    draft.status = "sent"
    draft.gmail_draft_id = None
    draft.updated_at = datetime.now(timezone.utc)

    # Auto-skip sibling drafts for the same thread (duplicate cleanup)
    sibling_gmail_draft_ids: list[str] = []
    if draft.thread_id:
        sib_result = await session.execute(
            select(EmailAutoDraft).where(
                EmailAutoDraft.user_id == user.id,
                EmailAutoDraft.thread_id == draft.thread_id,
                EmailAutoDraft.id != UUID(draft_id),
                EmailAutoDraft.status == "ready",
            )
        )
        for sib in sib_result.scalars().all():
            if sib.gmail_draft_id:
                sibling_gmail_draft_ids.append(sib.gmail_draft_id)
            sib.status = "skipped"
            sib.gmail_draft_id = None
            sib.updated_at = datetime.now(timezone.utc)

    await session.commit()

    # Clean up Gmail drafts AFTER session is closed
    draft_ids_to_delete = sibling_gmail_draft_ids
    if stale_gmail_draft_id:
        draft_ids_to_delete.append(stale_gmail_draft_id)
    for gid in draft_ids_to_delete:
        try:
            await asyncio.to_thread(delete_gmail_draft, token, gid)
        except Exception as e:
            logger.debug("Failed to delete Gmail draft after send", gmail_draft_id=gid, error=str(e))

    from services.events_service import track_email_draft_sent
    track_email_draft_sent(str(user.id))
    return {"ok": True}


@router.post("/email-drafts/{draft_id}/skip")
async def skip_email_draft(
    draft_id: str,
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """Skip/dismiss an auto-draft."""
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(EmailAutoDraft).where(
            EmailAutoDraft.id == UUID(draft_id),
            EmailAutoDraft.user_id == user.id,
        )
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")

    # Collect all Gmail draft IDs that need deleting
    stale_gmail_draft_id = draft.gmail_draft_id

    draft.status = "skipped"
    draft.gmail_draft_id = None
    draft.updated_at = datetime.now(timezone.utc)

    # Auto-skip sibling drafts for the same thread (duplicate cleanup)
    sibling_gmail_draft_ids: list[str] = []
    if draft.thread_id:
        sib_result = await session.execute(
            select(EmailAutoDraft).where(
                EmailAutoDraft.user_id == user.id,
                EmailAutoDraft.thread_id == draft.thread_id,
                EmailAutoDraft.id != UUID(draft_id),
                EmailAutoDraft.status == "ready",
            )
        )
        for sib in sib_result.scalars().all():
            if sib.gmail_draft_id:
                sibling_gmail_draft_ids.append(sib.gmail_draft_id)
            sib.status = "skipped"
            sib.gmail_draft_id = None
            sib.updated_at = datetime.now(timezone.utc)

    # Build full list of Gmail drafts to delete
    all_gmail_ids = list(sibling_gmail_draft_ids)
    if stale_gmail_draft_id:
        all_gmail_ids.append(stale_gmail_draft_id)

    # Fetch token only if there are Gmail drafts to clean up
    token = None
    if all_gmail_ids:
        try:
            int_result = await session.execute(
                select(Integration).where(
                    Integration.user_id == user.id,
                    Integration.provider == "google",
                    Integration.is_active == True,
                )
            )
            integration = int_result.scalar_one_or_none()
            if integration:
                token = await _get_google_token(session, integration)
        except Exception:
            pass

    await session.commit()

    # Delete Gmail drafts AFTER session is closed
    if token and all_gmail_ids:
        for gid in all_gmail_ids:
            try:
                await asyncio.to_thread(delete_gmail_draft, token, gid)
            except Exception as e:
                logger.debug("Failed to delete Gmail draft after skip", gmail_draft_id=gid, error=str(e))

    from services.events_service import track_email_draft_skipped
    track_email_draft_skipped(str(user.id))
    return {"ok": True}


class UpdateDraftBody(BaseModel):
    draft_body: str


@router.patch("/email-drafts/{draft_id}")
async def update_email_draft(
    draft_id: str,
    body: UpdateDraftBody,
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """Update the draft body (user edited before sending). Syncs to Gmail draft if persisted."""
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(EmailAutoDraft).where(
            EmailAutoDraft.id == UUID(draft_id),
            EmailAutoDraft.user_id == user.id,
        )
    )
    draft = result.scalar_one_or_none()
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    draft.draft_body = body.draft_body
    draft.updated_at = datetime.now(timezone.utc)

    # Capture data for Gmail sync after session close
    gmail_draft_id = draft.gmail_draft_id
    token = None
    draft_thread_id = draft.thread_id or ""
    draft_email_id = draft.email_id
    draft_user_name = user.name or ""
    if gmail_draft_id:
        try:
            int_result = await session.execute(
                select(Integration).where(
                    Integration.user_id == user.id,
                    Integration.provider == "google",
                    Integration.is_active == True,
                )
            )
            integration = int_result.scalar_one_or_none()
            if integration:
                token = await _get_google_token(session, integration)
        except Exception:
            pass

    await session.commit()

    # Sync edit to Gmail draft AFTER session is closed
    if gmail_draft_id and token:
        try:
            await asyncio.to_thread(
                update_gmail_draft, token, gmail_draft_id,
                body.draft_body, draft_thread_id, draft_email_id, draft_user_name,
            )
        except Exception:
            pass  # Gmail draft may have been deleted by user; cleared on send/skip

    return {"ok": True}


# ---------------------------------------------------------------------------
# Weather (cached from background poller)
# ---------------------------------------------------------------------------

@router.get("/weather")
async def get_weather(
    session: AsyncSession = Depends(get_db),
    auth_user: AuthUser = Depends(get_current_user),
):
    """Return cached weather for the user's city."""
    from services.weather_service import get_cached_weather, fetch_and_cache_weather

    user = await get_or_create_user(session, auth_user)
    city = user.city
    if not city:
        return {"weather": None}

    # Try Redis cache first
    cached = await get_cached_weather(city)
    if cached:
        return {"weather": cached}

    # Cache miss (new city, poller hasn't run yet) -- fetch on demand and cache
    data = await fetch_and_cache_weather(city)
    return {"weather": data}
