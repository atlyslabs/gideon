"""
Workflows Router — CRUD, activation, manual run, and run history/approval
for automated AI workflows.
"""

from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

import asyncio
import json

from config.integration_registry import VALID_INTEGRATIONS
from db import get_db
from db.models import Conversation, Message, Workflow, WorkflowRun
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.oauth.token_refresh import get_valid_access_token
from services.user_service import get_or_create_user
from services.workflow_run_policy import (
    MAX_WALL_CLOCK_SECONDS,
    STUCK_HEARTBEAT_SECONDS,
)
from tools.pdf_tool import (
    _markdown_to_html,
    _render_html_to_pdf_bytes,
    _safe_pdf_filename,
    _upload_pdf_to_drive_sync,
)

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class WorkflowCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    prompt: str = Field(default="")
    when_type: str = Field(default="schedule")
    schedule_start_date: Optional[date] = None
    schedule_interval: int = Field(default=1, ge=1)
    schedule_unit: str = Field(default="day")
    schedule_day_of_week: Optional[str] = None
    schedule_days_of_week: Optional[list[str]] = None
    schedule_time: str = Field(default="00:30")
    trigger_integration: Optional[str] = None
    trigger_event: Optional[str] = None
    trigger_minutes_before: Optional[int] = None
    trigger_filter: Optional[dict[str, Any]] = None
    integrations_used: list[str] = Field(default_factory=list)
    approval_mode: str = Field(default="auto")
    alert_on: str = Field(default="failure")
    # LLM provider for runs: "auto" (default — Kimi, may auto-pin to Anthropic on
    # chronic escalation), "anthropic" (pin to Claude 1M), or "kimi" (force Kimi).
    preferred_provider: Optional[str] = None
    active_until: Optional[datetime] = None


class WorkflowUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    prompt: Optional[str] = None
    when_type: Optional[str] = None
    schedule_start_date: Optional[date] = None
    schedule_interval: Optional[int] = Field(None, ge=1)
    schedule_unit: Optional[str] = None
    schedule_day_of_week: Optional[str] = None
    schedule_days_of_week: Optional[list[str]] = None
    schedule_time: Optional[str] = None
    trigger_integration: Optional[str] = None
    trigger_event: Optional[str] = None
    trigger_minutes_before: Optional[int] = None
    trigger_filter: Optional[dict[str, Any]] = None
    integrations_used: Optional[list[str]] = None
    approval_mode: Optional[str] = None
    alert_on: Optional[str] = None
    preferred_provider: Optional[str] = None  # "auto" (=NULL) | "anthropic" | "kimi"
    active_until: Optional[datetime] = None  # pass null to clear (= forever)


class WorkflowRunResponse(BaseModel):
    id: str
    workflow_id: str
    workflow_name: Optional[str] = None
    triggered_at: datetime
    completed_at: Optional[datetime]
    status: str
    output: Optional[str]
    error: Optional[str]
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    report_markdown: Optional[str] = None
    # Set if the user has already clicked "Follow up" on this run — points to
    # the spawned chat conversation. Null otherwise. The FE uses this to swap
    # the button text from "Follow up in chat" to "Continue chat".
    conversation_id: Optional[str] = None
    # True when this run still has the full message history available (newer
    # runs); false when only `report_markdown` is available (legacy runs).
    # Drives a small "limited context" hint in the FE.
    has_replay_messages: bool = False
    created_at: datetime

    class Config:
        from_attributes = True


class WorkflowResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    prompt: str
    when_type: str
    schedule_start_date: Optional[date]
    schedule_interval: int
    schedule_unit: str
    schedule_day_of_week: Optional[str]
    schedule_days_of_week: Optional[list[str]] = None
    schedule_time: str
    trigger_integration: Optional[str]
    trigger_event: Optional[str]
    trigger_minutes_before: Optional[int]
    trigger_filter: Optional[dict[str, Any]]
    integrations_used: list[str]
    status: str
    approval_mode: str
    alert_on: str = "failure"
    # "auto" when unpinned (NULL in DB), else "anthropic" / "kimi".
    preferred_provider: str = "auto"
    active_until: Optional[datetime]
    link_shared: bool = False
    # Most-recent run summary, for the workflows-list card status line.
    # Null when the workflow has never run. `last_run_at` is the completion time
    # when available, else the trigger time (for in-flight runs).
    last_run_status: Optional[str] = None
    last_run_at: Optional[datetime] = None
    last_run_id: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowResponse]
    count: int


class WorkflowRunListResponse(BaseModel):
    runs: list[WorkflowRunResponse]
    count: int


class WorkflowActionResponse(BaseModel):
    workflow_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

VALID_WHEN_TYPES = {"schedule", "trigger"}
VALID_SCHEDULE_UNITS = {"minute", "hour", "day", "week"}
# Minimum interval for sub-daily schedules. Workflow runs take ~30-90s and cost
# tokens, so a sub-5-minute cadence would overlap and pile up. Enforced (clamped)
# wherever a "minute"-unit workflow is created/updated.
MIN_MINUTE_INTERVAL = 5
VALID_APPROVAL_MODES = {"auto"}  # "manual" removed: workflows are automatic by design
VALID_ALERT_MODES = {"none", "failure", "success", "both"}
# Run-provider preference. "auto" is stored as NULL (executor picks Kimi and may
# auto-pin to Anthropic on chronic escalation); "anthropic"/"kimi" are explicit pins.
VALID_PREFERRED_PROVIDERS = {"auto", "anthropic", "kimi"}
# Keys we accept on `integrations_used`. Includes both the umbrella "google"
# (legacy / from-description output) and the per-tool subkeys that the
# preview-steps parser produces (gmail, google_sheets, etc.) — the workflow
# card icons render at the subkey level so storing them is the right level.
# Maps preview-steps tool names → the key we store on `integrations_used`.
# `ai`, `web_search`, `code_execution` are intentionally absent — they are
# orchestration primitives, not integrations the user has to connect.
WORKFLOW_TOOL_TO_INTEGRATION = {
    "gmail": "gmail",
    "google_calendar": "google_calendar",
    "google_drive": "google_drive",
    "google_sheets": "google_sheets",
    "google_docs": "google_docs",
    "slack": "slack",
    "notion": "notion",
    "linear": "linear",
    "github": "github",
    "grafana": "grafana",
}
VALID_TRIGGER_INTEGRATIONS = {"google_calendar", "gmail", "slack"}
VALID_TRIGGER_EVENTS = {"before_event", "new_email", "new_message"}
VALID_DAYS_OF_WEEK = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}


def validate_workflow_fields(
    when_type: Optional[str] = None,
    schedule_unit: Optional[str] = None,
    schedule_interval: Optional[int] = None,
    schedule_day_of_week: Optional[str] = None,
    schedule_days_of_week: Optional[list[str]] = None,
    schedule_time: Optional[str] = None,
    approval_mode: Optional[str] = None,
    alert_on: Optional[str] = None,
    integrations_used: Optional[list[str]] = None,
    trigger_integration: Optional[str] = None,
    trigger_event: Optional[str] = None,
    preferred_provider: Optional[str] = None,
):
    """Validate enum-like fields. Raises HTTPException on bad values."""
    if when_type is not None and when_type not in VALID_WHEN_TYPES:
        raise HTTPException(422, f"Invalid when_type. Must be one of: {', '.join(VALID_WHEN_TYPES)}")

    if schedule_unit is not None and schedule_unit not in VALID_SCHEDULE_UNITS:
        raise HTTPException(422, f"Invalid schedule_unit. Must be one of: {', '.join(sorted(VALID_SCHEDULE_UNITS))}")

    if schedule_unit == "minute" and schedule_interval is not None and schedule_interval < MIN_MINUTE_INTERVAL:
        raise HTTPException(
            422,
            f"schedule_interval for the 'minute' unit must be at least {MIN_MINUTE_INTERVAL} "
            "(runs take ~30-90s, so a faster cadence would overlap).",
        )

    if schedule_day_of_week is not None and schedule_day_of_week not in VALID_DAYS_OF_WEEK:
        raise HTTPException(422, f"Invalid schedule_day_of_week. Must be one of: {', '.join(sorted(VALID_DAYS_OF_WEEK))}")

    if schedule_days_of_week is not None:
        invalid = set(schedule_days_of_week) - VALID_DAYS_OF_WEEK
        if invalid:
            raise HTTPException(422, f"Invalid schedule_days_of_week: {', '.join(sorted(invalid))}. Each must be one of: {', '.join(sorted(VALID_DAYS_OF_WEEK))}")

    if schedule_time is not None:
        parts = schedule_time.split(":")
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            raise HTTPException(422, "Invalid schedule_time. Must be HH:MM format.")
        hh, mm = int(parts[0]), int(parts[1])
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise HTTPException(422, "Invalid schedule_time. Hours must be 0-23, minutes 0-59.")

    if approval_mode is not None and approval_mode not in VALID_APPROVAL_MODES:
        raise HTTPException(422, f"Invalid approval_mode. Must be one of: {', '.join(VALID_APPROVAL_MODES)}")

    if alert_on is not None and alert_on not in VALID_ALERT_MODES:
        raise HTTPException(422, f"Invalid alert_on. Must be one of: {', '.join(sorted(VALID_ALERT_MODES))}")

    if preferred_provider is not None and preferred_provider not in VALID_PREFERRED_PROVIDERS:
        raise HTTPException(422, f"Invalid preferred_provider. Must be one of: {', '.join(sorted(VALID_PREFERRED_PROVIDERS))}")

    if integrations_used is not None:
        invalid = set(integrations_used) - VALID_INTEGRATIONS
        if invalid:
            raise HTTPException(422, f"Invalid integrations: {', '.join(invalid)}. Valid: {', '.join(VALID_INTEGRATIONS)}")

    if trigger_integration is not None and trigger_integration not in VALID_TRIGGER_INTEGRATIONS:
        raise HTTPException(422, f"Invalid trigger_integration. Must be one of: {', '.join(VALID_TRIGGER_INTEGRATIONS)}")

    if trigger_event is not None and trigger_event not in VALID_TRIGGER_EVENTS:
        raise HTTPException(422, f"Invalid trigger_event. Must be one of: {', '.join(VALID_TRIGGER_EVENTS)}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _workflow_to_response(
    wf: Workflow, last_run: Optional[WorkflowRun] = None
) -> WorkflowResponse:
    return WorkflowResponse(
        id=str(wf.id),
        name=wf.name,
        description=wf.description,
        prompt=wf.prompt or "",
        when_type=wf.when_type or "schedule",
        schedule_start_date=wf.schedule_start_date,
        schedule_interval=wf.schedule_interval or 1,
        schedule_unit=wf.schedule_unit or "day",
        schedule_day_of_week=wf.schedule_day_of_week,
        schedule_days_of_week=wf.schedule_days_of_week,
        schedule_time=wf.schedule_time or "00:30",
        trigger_integration=wf.trigger_integration,
        trigger_event=wf.trigger_event,
        trigger_minutes_before=wf.trigger_minutes_before,
        trigger_filter=wf.trigger_filter,
        integrations_used=wf.integrations_used or [],
        status=wf.status or "inactive",
        approval_mode=wf.approval_mode or "auto",
        alert_on=wf.alert_on or "failure",
        preferred_provider=wf.preferred_provider or "auto",
        active_until=wf.active_until,
        link_shared=bool(wf.link_shared),
        last_run_status=(last_run.status or "running") if last_run else None,
        last_run_at=(last_run.completed_at or last_run.triggered_at) if last_run else None,
        last_run_id=str(last_run.id) if last_run else None,
        created_at=wf.created_at,
        updated_at=wf.updated_at,
    )


def _run_to_response(run: WorkflowRun, workflow_name: Optional[str] = None) -> WorkflowRunResponse:
    return WorkflowRunResponse(
        id=str(run.id),
        workflow_id=str(run.workflow_id),
        workflow_name=workflow_name,
        triggered_at=run.triggered_at,
        completed_at=run.completed_at,
        status=run.status or "running",
        output=run.output,
        error=run.error,
        artifacts=list(run.artifacts or []),
        report_markdown=run.report_markdown,
        conversation_id=str(run.conversation_id) if run.conversation_id else None,
        has_replay_messages=bool(run.replay_messages),
        created_at=run.created_at,
    )


_STEP_TEXT_CAP = 240


def _tool_result_ok(content: Any) -> Optional[bool]:
    """Map a tool_result's content to ok / error / unknown. The workflow loop
    stores results as ``json.dumps(result)``; an error result is ``{"error": ...}``."""
    try:
        parsed = content if isinstance(content, (dict, list)) else json.loads(content)
    except Exception:
        return None
    if isinstance(parsed, dict) and parsed.get("error"):
        return False
    return True


def _progress_steps(messages: Optional[list]) -> list[dict]:
    """Turn a (possibly in-progress) replay message list into a compact, ordered
    activity feed for the live "watch it run" view: thinking/text snippets and
    tool calls with ok / error / pending status. A tool_use with no matching
    tool_result yet (the in-flight call) is surfaced as pending (ok=None)."""
    if not messages:
        return []
    # Pair tool_use_id -> result status across all tool_result blocks.
    results: dict[str, Optional[bool]] = {}
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                results[b.get("tool_use_id")] = _tool_result_ok(b.get("content"))

    steps: list[dict] = []
    round_idx = 0
    for m in messages:
        if not isinstance(m, dict) or m.get("role") != "assistant":
            continue
        round_idx += 1
        content = m.get("content")
        if isinstance(content, str):
            if content.strip():
                steps.append({"round": round_idx, "kind": "text", "text": content[:_STEP_TEXT_CAP]})
            continue
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "thinking":
                txt = (b.get("thinking") or "").strip()
                if txt:
                    steps.append({"round": round_idx, "kind": "thinking", "text": txt[:_STEP_TEXT_CAP]})
            elif btype == "text":
                txt = (b.get("text") or "").strip()
                if txt:
                    steps.append({"round": round_idx, "kind": "text", "text": txt[:_STEP_TEXT_CAP]})
            elif btype in ("tool_use", "server_tool_use"):
                steps.append({
                    "round": round_idx,
                    "kind": "tool",
                    "name": b.get("name") or "tool",
                    "ok": results.get(b.get("id")),  # True / False / None(pending)
                })
    return steps


async def _get_user_workflow(
    session: AsyncSession, user_id: UUID, workflow_id: UUID
) -> Workflow:
    """Load a workflow belonging to the user, or raise 404."""
    result = await session.execute(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.user_id == user_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


async def _run_workflow_in_background(workflow_id: UUID, user_id: UUID, run_id: UUID):
    """Execute a workflow run in a separate DB session (for background tasks)."""
    from db.database import AsyncPostgresSessionGideon
    from services.workflow_execution_service import execute_workflow_for_run

    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        try:
            await execute_workflow_for_run(workflow_id, user_id, run_id, session)
        except Exception as e:
            logger.error(
                "Background workflow execution failed",
                workflow_id=str(workflow_id),
                error=str(e),
            )


# ---------------------------------------------------------------------------
# Endpoints — Workflow CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=WorkflowListResponse)
async def list_workflows(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all workflows for the current user."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Workflow)
        .where(Workflow.user_id == user.id)
        .order_by(Workflow.created_at.desc())
    )
    workflows = result.scalars().all()

    # Latest run per workflow in one query (Postgres DISTINCT ON). Used for the
    # "last run succeeded/failed at …" status line on each card.
    run_rows = await session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.user_id == user.id)
        .order_by(WorkflowRun.workflow_id, WorkflowRun.triggered_at.desc())
        .distinct(WorkflowRun.workflow_id)
    )
    latest_run_by_wf = {r.workflow_id: r for r in run_rows.scalars().all()}

    return WorkflowListResponse(
        workflows=[
            _workflow_to_response(wf, latest_run_by_wf.get(wf.id)) for wf in workflows
        ],
        count=len(workflows),
    )


@router.get("/{workflow_id}", response_model=WorkflowResponse)
async def get_workflow(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get a single workflow."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)
    return _workflow_to_response(workflow)


# ---------------------------------------------------------------------------
# Template view + clone (shared workflow links)
# ---------------------------------------------------------------------------

class WorkflowTemplateResponse(BaseModel):
    """Read-only template shape of a workflow. No owner identity, no runs.
    Returned to any logged-in user so they can preview a workflow from a
    shared link before cloning it into their own account.
    """
    id: str
    name: str
    description: Optional[str]
    prompt: str
    when_type: str
    schedule_start_date: Optional[date]
    schedule_interval: int
    schedule_unit: str
    schedule_day_of_week: Optional[str]
    schedule_days_of_week: Optional[list[str]] = None
    schedule_time: str
    trigger_integration: Optional[str]
    trigger_event: Optional[str]
    trigger_minutes_before: Optional[int]
    trigger_filter: Optional[dict[str, Any]]
    integrations_used: list[str]
    approval_mode: str


@router.get("/template/{workflow_id}", response_model=WorkflowTemplateResponse)
async def get_workflow_template(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Fetch the template shape of a workflow by ID for a non-owner.

    The owner of the workflow must have opted in to sharing by flipping
    `link_shared=True` (the Share button does this). Without that opt-in,
    this endpoint 404s even with a valid UUID — the UUID alone is NOT a
    bearer token. Owner identity and run history are never exposed.
    """
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Owners can always fetch their own workflow's template view. Non-owners
    # need an explicit link_shared=True opt-in. Returning 404 (not 403) so we
    # don't leak existence of private workflows to anyone probing by UUID.
    if workflow.user_id != user.id and not workflow.link_shared:
        raise HTTPException(status_code=404, detail="Workflow not found")

    return WorkflowTemplateResponse(
        id=str(workflow.id),
        name=workflow.name,
        description=workflow.description,
        prompt=workflow.prompt or "",
        when_type=workflow.when_type,
        schedule_start_date=workflow.schedule_start_date,
        schedule_interval=workflow.schedule_interval,
        schedule_unit=workflow.schedule_unit,
        schedule_day_of_week=workflow.schedule_day_of_week,
        schedule_days_of_week=workflow.schedule_days_of_week,
        schedule_time=workflow.schedule_time,
        trigger_integration=workflow.trigger_integration,
        trigger_event=workflow.trigger_event,
        trigger_minutes_before=workflow.trigger_minutes_before,
        trigger_filter=workflow.trigger_filter,
        integrations_used=workflow.integrations_used or [],
        approval_mode=workflow.approval_mode,
    )


class ShareSettingsRequest(BaseModel):
    link_shared: bool


@router.patch("/{workflow_id}/share", response_model=WorkflowResponse)
async def update_workflow_share(
    workflow_id: UUID,
    body: ShareSettingsRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Toggle the `link_shared` opt-in flag. Owner only.

    Flipping to true lets anyone with the workflow URL view the template and
    clone it into their account. Flipping back to false revokes that access;
    previously-made clones keep working (they live under the cloner's user_id).
    """
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)
    workflow.link_shared = bool(body.link_shared)
    await session.flush()
    await session.refresh(workflow)
    return _workflow_to_response(workflow)


@router.post("/clone/{workflow_id}", response_model=WorkflowResponse, status_code=201)
async def clone_workflow(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Clone a workflow into the current user's account. The clone starts
    INACTIVE so the recipient reviews the prompt and schedule before it ever
    runs (they must explicitly activate it). Original workflow is untouched.

    Non-owners can only clone workflows whose owner has set `link_shared=True`
    (the Share button does this). Without that opt-in, 404.
    """
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Workflow).where(Workflow.id == workflow_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise HTTPException(status_code=404, detail="Workflow not found")

    if source.user_id != user.id and not source.link_shared:
        # Non-owner, no opt-in → 404 (not 403) to avoid leaking existence.
        raise HTTPException(status_code=404, detail="Workflow not found")

    clone = Workflow(
        user_id=user.id,
        name=source.name,
        description=source.description,
        prompt=source.prompt,
        when_type=source.when_type,
        schedule_start_date=source.schedule_start_date,
        schedule_interval=source.schedule_interval,
        schedule_unit=source.schedule_unit,
        schedule_day_of_week=source.schedule_day_of_week,
        schedule_days_of_week=source.schedule_days_of_week,
        schedule_time=source.schedule_time,
        trigger_integration=source.trigger_integration,
        trigger_event=source.trigger_event,
        trigger_minutes_before=source.trigger_minutes_before,
        trigger_filter=source.trigger_filter,
        integrations_used=source.integrations_used or [],
        alert_on=source.alert_on or "failure",
        status="inactive",       # recipient must explicitly activate
        active_until=None,
    )
    session.add(clone)
    await session.flush()
    await session.refresh(clone)

    from services.events_service import track_workflow_created
    track_workflow_created(str(user.id))
    return _workflow_to_response(clone)


@router.post("", response_model=WorkflowResponse, status_code=201)
async def create_workflow(
    body: WorkflowCreate,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Create a new workflow."""
    user = await get_or_create_user(session, auth_user)

    validate_workflow_fields(
        when_type=body.when_type,
        schedule_unit=body.schedule_unit,
        schedule_interval=body.schedule_interval,
        schedule_day_of_week=body.schedule_day_of_week,
        schedule_days_of_week=body.schedule_days_of_week,
        schedule_time=body.schedule_time,
        approval_mode=body.approval_mode,
        alert_on=body.alert_on,
        integrations_used=body.integrations_used,
        trigger_integration=body.trigger_integration,
        trigger_event=body.trigger_event,
        preferred_provider=body.preferred_provider,
    )

    workflow = Workflow(
        user_id=user.id,
        name=body.name,
        description=body.description,
        prompt=body.prompt,
        when_type=body.when_type,
        schedule_start_date=body.schedule_start_date,
        schedule_interval=body.schedule_interval,
        schedule_unit=body.schedule_unit,
        schedule_day_of_week=body.schedule_day_of_week,
        schedule_days_of_week=body.schedule_days_of_week,
        schedule_time=body.schedule_time,
        trigger_integration=body.trigger_integration,
        trigger_event=body.trigger_event,
        trigger_minutes_before=body.trigger_minutes_before,
        trigger_filter=body.trigger_filter,
        integrations_used=body.integrations_used,
        approval_mode=body.approval_mode,
        alert_on=body.alert_on,
        preferred_provider=(
            None if (body.preferred_provider or "auto") == "auto" else body.preferred_provider
        ),
        status="active",
        active_until=body.active_until,
    )
    session.add(workflow)
    await session.flush()
    await session.refresh(workflow)

    if (workflow.prompt or "").strip():
        background_tasks.add_task(_refresh_integrations_used_bg, workflow.id)

    from services.events_service import track_workflow_created
    track_workflow_created(str(user.id))
    return _workflow_to_response(workflow)


@router.patch("/{workflow_id}", response_model=WorkflowResponse)
async def update_workflow(
    workflow_id: UUID,
    body: WorkflowUpdate,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update a workflow's configuration."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)
    prompt_before = workflow.prompt or ""

    # Validate the EFFECTIVE post-update unit+interval, not just the body. A
    # partial PATCH ({"schedule_unit": "minute"} alone, or {"schedule_interval":
    # 1} on an already-minute workflow) would otherwise slip past the 5-minute
    # floor because the other field is None in the body. Merge body over the
    # current row first.
    effective_unit = body.schedule_unit if body.schedule_unit is not None else workflow.schedule_unit
    effective_interval = (
        body.schedule_interval if body.schedule_interval is not None else workflow.schedule_interval
    )

    validate_workflow_fields(
        when_type=body.when_type,
        schedule_unit=effective_unit,
        schedule_interval=effective_interval,
        schedule_day_of_week=body.schedule_day_of_week,
        schedule_days_of_week=body.schedule_days_of_week,
        schedule_time=body.schedule_time,
        approval_mode=body.approval_mode,
        alert_on=body.alert_on,
        integrations_used=body.integrations_used,
        trigger_integration=body.trigger_integration,
        trigger_event=body.trigger_event,
        preferred_provider=body.preferred_provider,
    )

    if body.name is not None:
        workflow.name = body.name
    if body.description is not None:
        workflow.description = body.description
    if body.prompt is not None:
        workflow.prompt = body.prompt
    if body.when_type is not None:
        workflow.when_type = body.when_type
    if body.schedule_start_date is not None:
        workflow.schedule_start_date = body.schedule_start_date
    if body.schedule_interval is not None:
        workflow.schedule_interval = body.schedule_interval
    if body.schedule_unit is not None:
        workflow.schedule_unit = body.schedule_unit
    if body.schedule_day_of_week is not None:
        workflow.schedule_day_of_week = body.schedule_day_of_week
    # Use model_fields_set so passing an explicit `null` clears the filter
    # (vs omitting the key, which leaves the existing value unchanged).
    if "schedule_days_of_week" in body.model_fields_set:
        workflow.schedule_days_of_week = body.schedule_days_of_week
    if body.schedule_time is not None:
        workflow.schedule_time = body.schedule_time
    if body.trigger_integration is not None:
        workflow.trigger_integration = body.trigger_integration
    if body.trigger_event is not None:
        workflow.trigger_event = body.trigger_event
    if body.trigger_minutes_before is not None:
        workflow.trigger_minutes_before = body.trigger_minutes_before
    if body.trigger_filter is not None:
        workflow.trigger_filter = body.trigger_filter
    if body.integrations_used is not None:
        workflow.integrations_used = body.integrations_used
    if body.approval_mode is not None:
        workflow.approval_mode = body.approval_mode
    if body.alert_on is not None:
        workflow.alert_on = body.alert_on
    # "auto" clears the pin (NULL) → executor picks Kimi and may re-auto-pin;
    # "anthropic"/"kimi" are explicit. Only apply when the key was sent.
    if "preferred_provider" in body.model_fields_set:
        workflow.preferred_provider = (
            None if (body.preferred_provider or "auto") == "auto" else body.preferred_provider
        )
    if "active_until" in body.model_fields_set:
        workflow.active_until = body.active_until  # may be None to clear (= forever)

    await session.flush()
    await session.refresh(workflow)

    # If the prompt changed (or this is the first save with a prompt), re-derive
    # `integrations_used` from the new prompt off the request path.
    new_prompt = (workflow.prompt or "").strip()
    if new_prompt and new_prompt != prompt_before.strip():
        background_tasks.add_task(_refresh_integrations_used_bg, workflow.id)

    return _workflow_to_response(workflow)


@router.delete("/{workflow_id}", status_code=204)
async def delete_workflow(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a workflow and all its runs (cascade)."""
    user = await get_or_create_user(session, auth_user)
    _ = await _get_user_workflow(session, user.id, workflow_id)

    await session.execute(
        delete(Workflow).where(Workflow.id == workflow_id, Workflow.user_id == user.id)
    )


# ---------------------------------------------------------------------------
# Endpoints — Activate / Deactivate
# ---------------------------------------------------------------------------

@router.post("/{workflow_id}/activate", response_model=WorkflowActionResponse)
async def activate_workflow(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Set a workflow's status to active."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    workflow.status = "active"
    await session.flush()

    return WorkflowActionResponse(
        workflow_id=str(workflow.id),
        status="active",
        message="Workflow activated.",
    )


@router.post("/{workflow_id}/deactivate", response_model=WorkflowActionResponse)
async def deactivate_workflow(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Set a workflow's status to inactive."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    workflow.status = "inactive"
    await session.flush()

    return WorkflowActionResponse(
        workflow_id=str(workflow.id),
        status="inactive",
        message="Workflow deactivated.",
    )


# ---------------------------------------------------------------------------
# Endpoints — Manual Run
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Endpoints — Preview Steps (AI-parsed visualization)
# ---------------------------------------------------------------------------

class StepPreview(BaseModel):
    tool: str
    label: str

class PreviewStepsRequest(BaseModel):
    prompt: str = Field(..., min_length=1)

class PreviewStepsResponse(BaseModel):
    steps: list[StepPreview]


_PREVIEW_PROMPT = """Analyze this workflow instruction and break it into discrete sequential steps.
For each step, identify which tool/integration it uses.

Valid tool values: gmail, slack, google_calendar, google_drive, google_sheets, google_docs, notion, linear, github, grafana, web_search, code_execution, ai

Use "ai" for steps that are pure reasoning/filtering/analysis with no external tool.

Return ONLY a JSON array, no other text:
[{"tool": "gmail", "label": "Read unread emails from inbox"}, {"tool": "ai", "label": "Filter for investor-related emails"}, ...]

Keep labels short (under 60 chars). 3-8 steps typical.

Workflow instruction:
"""


_PREVIEW_TOOL_SCHEMA = {
    "name": "save_steps",
    "description": "Save the parsed workflow steps.",
    "input_schema": {
        "type": "object",
        "properties": {
            "steps": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string"},
                        "label": {"type": "string"},
                    },
                    "required": ["tool", "label"],
                },
            },
        },
        "required": ["steps"],
    },
}


async def _run_preview_steps(prompt_text: str) -> list[dict]:
    """Run the Haiku step-extraction call. Raises on any error so callers can
    decide whether to surface or swallow."""
    from config import get_anthropic_client, Models

    client = get_anthropic_client(timeout=30.0)
    response = await client.messages.create(
        model=Models.HAIKU,
        max_tokens=1024,
        messages=[{"role": "user", "content": _PREVIEW_PROMPT + prompt_text}],
        tools=[_PREVIEW_TOOL_SCHEMA],
        tool_choice={"type": "tool", "name": "save_steps"},
    )
    for block in response.content:
        if block.type == "tool_use" and block.name == "save_steps":
            return list(block.input.get("steps", []) or [])
    raise RuntimeError("No steps returned")


def _derive_integrations_from_steps(steps: list[dict]) -> list[str]:
    """Map preview-step tool names to the integration keys we render on the
    workflow card. Preserves first-seen order, dedupes, drops unknown tools."""
    seen: set[str] = set()
    out: list[str] = []
    for step in steps:
        tool = (step.get("tool") or "").strip().lower()
        key = WORKFLOW_TOOL_TO_INTEGRATION.get(tool)
        if key and key not in seen:
            seen.add(key)
            out.append(key)
    return out


async def _refresh_integrations_used_bg(workflow_id: UUID) -> None:
    """Re-derive `integrations_used` from the workflow's prompt and patch the
    row. Runs as a FastAPI BackgroundTask so save latency stays unaffected and
    no DB session is held during the Haiku call (per CLAUDE.md DB rules).
    Silent on failure — the card just keeps its prior icon set."""
    from db.database import AsyncPostgresSessionGideon

    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        wf = await session.get(Workflow, workflow_id)
        if not wf or not (wf.prompt or "").strip():
            return
        prompt_text = wf.prompt
        prior = list(wf.integrations_used or [])
    # session closed before external call

    try:
        steps = await _run_preview_steps(prompt_text)
    except Exception as e:
        logger.warning("derive integrations_used failed", workflow_id=str(workflow_id), err=str(e))
        return

    derived = _derive_integrations_from_steps(steps)
    if not derived or derived == prior:
        return

    # Stale-prompt guard. Between the read above and this write, the user may
    # have saved a new prompt — that save fires its own BG task. If the prompt
    # has changed, skip the write: our `derived` reflects the OLD prompt and
    # would clobber the icons with stale content, and the newer task will
    # catch up. We can't put `prompt == prompt_text` in a SQL WHERE because
    # the column is Fernet-encrypted (non-deterministic ciphertext), so we
    # compare in Python after a fresh decrypted read.
    async with sm.get_session() as session:
        wf = await session.get(Workflow, workflow_id)
        if not wf:
            return
        if (wf.prompt or "") != prompt_text:
            logger.info(
                "skip derive write: prompt changed mid-flight",
                workflow_id=str(workflow_id),
            )
            return
        wf.integrations_used = derived
        await session.flush()


@router.post("/preview-steps", response_model=PreviewStepsResponse)
async def preview_steps(
    body: PreviewStepsRequest,
    auth_user: AuthUser = Depends(get_current_user),
):
    """Parse a workflow prompt into discrete visualization steps using AI."""
    try:
        steps = await _run_preview_steps(body.prompt)
        return PreviewStepsResponse(steps=[StepPreview(**s) for s in steps])
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Preview steps failed", error=str(e))
        raise HTTPException(500, "Failed to parse workflow steps")


# ---------------------------------------------------------------------------
# Endpoints — Generate from description
# ---------------------------------------------------------------------------

class FromDescriptionRequest(BaseModel):
    description: str = Field(..., min_length=1)
    connected_integrations: list[str] = Field(default_factory=list)


_GENERATE_PROMPT = """You are a workflow configuration generator. The user describes a repetitive task they want automated. Generate a workflow configuration.

Connected integrations: {integrations}

Return ONLY a JSON object (no markdown, no explanation):
{{
  "name": "Short workflow name (under 50 chars)",
  "description": "One-line description",
  "prompt": "Detailed instruction for the AI agent to execute this workflow. Be specific about what to do, what tools to use, and what output to produce.",
  "schedule_unit": "minute", "hour", "day", or "week",
  "schedule_interval": 1,
  "schedule_time": "HH:MM" (24h format, pick a sensible default; ignored for minute/hour units),
  "schedule_day_of_week": null or "MO"/"TU"/"WE"/"TH"/"FR"/"SA"/"SU" (only if weekly),
  "integrations_used": ["google", "slack", etc. — only from the connected list]
}}

Use "minute" or "hour" when the task should repeat through the day ("every 30 minutes", "every 2 hours") — these ignore schedule_time. Use "day"/"week" for once-a-day/week tasks at a fixed schedule_time. The minimum interval for "minute" is 5.

User's task description:
"""


@router.post("/from-description", response_model=WorkflowResponse, status_code=201)
async def create_workflow_from_description(
    body: FromDescriptionRequest,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Generate and create a workflow from a natural language task description."""
    from config import get_anthropic_client, Models

    user = await get_or_create_user(session, auth_user)

    integrations_str = ", ".join(body.connected_integrations) if body.connected_integrations else "google, slack"
    prompt_text = _GENERATE_PROMPT.format(integrations=integrations_str) + body.description

    generate_tool = {
        "name": "save_workflow",
        "description": "Save the generated workflow configuration.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "description": {"type": "string"},
                "prompt": {"type": "string"},
                "schedule_unit": {"type": "string", "enum": ["minute", "hour", "day", "week"]},
                "schedule_interval": {"type": "integer"},
                "schedule_time": {"type": "string"},
                "schedule_day_of_week": {"type": ["string", "null"]},
                "integrations_used": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "prompt", "schedule_time", "schedule_unit"],
        },
    }

    client = get_anthropic_client(timeout=30.0)
    try:
        response = await client.messages.create(
            model=Models.HAIKU,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt_text}],
            tools=[generate_tool],
            tool_choice={"type": "tool", "name": "save_workflow"},
        )
        config = None
        for block in response.content:
            if block.type == "tool_use" and block.name == "save_workflow":
                config = block.input
                break
        if not config:
            raise HTTPException(500, "Failed to generate workflow configuration")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Workflow generation failed", error=str(e))
        raise HTTPException(500, "Failed to generate workflow")

    # Validate and normalize
    schedule_unit = config.get("schedule_unit", "day")
    if schedule_unit not in VALID_SCHEDULE_UNITS:
        schedule_unit = "day"

    try:
        schedule_interval = max(1, int(config.get("schedule_interval", 1)))
    except (ValueError, TypeError):
        schedule_interval = 1
    # Clamp sub-5-minute cadences up to the floor (runs overlap below it).
    if schedule_unit == "minute":
        schedule_interval = max(MIN_MINUTE_INTERVAL, schedule_interval)

    schedule_time = config.get("schedule_time", "09:00")
    if not isinstance(schedule_time, str) or len(schedule_time) != 5:
        schedule_time = "09:00"

    schedule_dow = config.get("schedule_day_of_week")
    if schedule_dow and schedule_dow not in ("MO", "TU", "WE", "TH", "FR", "SA", "SU"):
        schedule_dow = None

    integrations_used = [
        p for p in (config.get("integrations_used") or [])
        if p in VALID_INTEGRATIONS
    ]

    workflow = Workflow(
        user_id=user.id,
        name=config.get("name", "Custom Workflow")[:255],
        description=config.get("description"),
        prompt=config.get("prompt", body.description),
        when_type="schedule",
        schedule_start_date=date.today(),
        schedule_interval=schedule_interval,
        schedule_unit=schedule_unit,
        # day-of-week only applies to weekly schedules
        schedule_day_of_week=schedule_dow if schedule_unit == "week" else None,
        schedule_time=schedule_time,
        integrations_used=integrations_used,
        status="active",
    )
    session.add(workflow)
    await session.flush()
    await session.refresh(workflow)

    if (workflow.prompt or "").strip():
        background_tasks.add_task(_refresh_integrations_used_bg, workflow.id)

    logger.info("Workflow created from description", name=workflow.name, user_id=str(user.id))
    return _workflow_to_response(workflow)


# ---------------------------------------------------------------------------
# Endpoints — Manual Run
# ---------------------------------------------------------------------------

@router.post("/{workflow_id}/run", response_model=WorkflowActionResponse)
async def run_workflow(
    workflow_id: UUID,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Manually trigger a workflow run (executes in background)."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    # Concurrent-run guard: refuse to start a second run while one is live.
    # A "live" run is running/pending_approval and started within the
    # MAX_WALL_CLOCK_SECONDS execution cap. A `running` row whose heartbeat
    # (last_progress_at, bumped every round) is older than
    # STUCK_HEARTBEAT_SECONDS is abandoned (pod crash) — the scheduler's
    # reaper will handle it, so it doesn't block a manual run.
    # Serialize concurrent manual triggers of the SAME workflow: take a row
    # lock so the live-run check + run creation below are atomic. Without it
    # two simultaneous /run requests both pass the check and both spawn a
    # (potentially 90-min, costly) run. The second caller blocks here until the
    # first commits its WorkflowRun insert, then sees it and gets the 409.
    await session.execute(
        select(Workflow.id).where(Workflow.id == workflow.id).with_for_update()
    )
    now = datetime.now(timezone.utc)
    live_cutoff = now - timedelta(seconds=MAX_WALL_CLOCK_SECONDS)
    result = await session.execute(
        select(WorkflowRun)
        .where(
            WorkflowRun.workflow_id == workflow.id,
            WorkflowRun.status.in_(["running", "pending_approval"]),
            WorkflowRun.triggered_at >= live_cutoff,
        )
        .order_by(WorkflowRun.triggered_at.desc())
    )
    for existing in result.scalars().all():
        if existing.status == "running":
            heartbeat = existing.last_progress_at or existing.triggered_at
            if heartbeat is None or (now - heartbeat).total_seconds() >= STUCK_HEARTBEAT_SECONDS:
                continue  # abandoned run — don't let it block manual runs
        raise HTTPException(
            status_code=409,
            detail=(
                f"This workflow already has a run in progress "
                f"(run {existing.id}, status '{existing.status}'). "
                "Wait for it to finish before starting another."
            ),
        )

    # Create the run record now so it's immediately visible in history
    run = WorkflowRun(
        workflow_id=workflow.id,
        user_id=user.id,
        status="running",
        source="manual",
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)

    background_tasks.add_task(_run_workflow_in_background, workflow.id, user.id, run.id)

    from services.events_service import track_workflow_triggered
    track_workflow_triggered(str(user.id), str(workflow.id))
    return WorkflowActionResponse(
        workflow_id=str(workflow.id),
        status="running",
        message="Workflow run started. Check GET /workflows/{id}/runs for results.",
    )


# ---------------------------------------------------------------------------
# Endpoints — Run History
# ---------------------------------------------------------------------------

@router.get("/{workflow_id}/runs", response_model=WorkflowRunListResponse)
async def list_runs(
    workflow_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all runs for a workflow, ordered by most recent first."""
    user = await get_or_create_user(session, auth_user)
    _ = await _get_user_workflow(session, user.id, workflow_id)

    result = await session.execute(
        select(WorkflowRun)
        .where(WorkflowRun.workflow_id == workflow_id)
        .order_by(WorkflowRun.triggered_at.desc())
    )
    runs = result.scalars().all()

    return WorkflowRunListResponse(
        runs=[_run_to_response(r) for r in runs],
        count=len(runs),
    )


@router.get("/{workflow_id}/runs/{run_id}", response_model=WorkflowRunResponse)
async def get_run(
    workflow_id: UUID,
    run_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get a single run with its output."""
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    result = await session.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.workflow_id == workflow_id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    return _run_to_response(run, workflow_name=workflow.name)


class WorkflowRunProgressResponse(BaseModel):
    status: str
    last_round: Optional[int] = None
    last_progress_at: Optional[datetime] = None
    resume_count: int = 0
    is_active: bool  # True while running/interrupted — the FE keeps polling
    steps: list[dict[str, Any]] = Field(default_factory=list)


@router.get("/{workflow_id}/runs/{run_id}/progress", response_model=WorkflowRunProgressResponse)
async def get_run_progress(
    workflow_id: UUID,
    run_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Live progress for a run — a compact activity feed derived from the
    per-round checkpoint (`replay_messages`). The FE polls this while the run is
    active (running/interrupted) to render the "watch it run" timeline, and stops
    once `is_active` is false."""
    user = await get_or_create_user(session, auth_user)
    _ = await _get_user_workflow(session, user.id, workflow_id)

    run = (
        await session.execute(
            select(WorkflowRun).where(
                WorkflowRun.id == run_id, WorkflowRun.workflow_id == workflow_id
            )
        )
    ).scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    status = run.status or "running"
    return WorkflowRunProgressResponse(
        status=status,
        last_round=run.last_round,
        last_progress_at=run.last_progress_at,
        resume_count=run.resume_count or 0,
        is_active=status in ("running", "interrupted"),
        steps=_progress_steps(run.replay_messages),
    )


class ActiveRunItem(BaseModel):
    run_id: str
    workflow_id: str
    workflow_name: Optional[str] = None
    status: str
    last_round: Optional[int] = None
    triggered_at: datetime


class ActiveRunsResponse(BaseModel):
    runs: list[ActiveRunItem] = Field(default_factory=list)
    count: int


# NOTE: two-segment path on purpose. A single-segment route (e.g. "/active-runs")
# is shadowed by the earlier "/{workflow_id}" param route, which then 422s trying
# to parse "active-runs" as a UUID. "/runs/active" can't match "/{workflow_id}".
@router.get("/runs/active", response_model=ActiveRunsResponse)
async def list_active_runs(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """All of the current user's in-flight runs (running / interrupted), newest
    first. Polled by the global "running workflows" indicator on the chat page."""
    user = await get_or_create_user(session, auth_user)
    rows = (
        await session.execute(
            select(WorkflowRun, Workflow.name)
            .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
            .where(
                WorkflowRun.user_id == user.id,
                WorkflowRun.status.in_(["running", "interrupted"]),
            )
            .order_by(WorkflowRun.triggered_at.desc())
        )
    ).all()
    items = [
        ActiveRunItem(
            run_id=str(run.id),
            workflow_id=str(run.workflow_id),
            workflow_name=name,
            status=run.status or "running",
            last_round=run.last_round,
            triggered_at=run.triggered_at,
        )
        for run, name in rows
    ]
    return ActiveRunsResponse(runs=items, count=len(items))


class RunFollowUpResponse(BaseModel):
    conversation_id: str
    title: str
    seeded_messages: int
    fallback: bool  # true if this run had no replay_messages and we seeded from report_markdown only


@router.post("/{workflow_id}/runs/{run_id}/follow-up", response_model=RunFollowUpResponse)
async def follow_up_on_run(
    workflow_id: UUID,
    run_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Seed a chat conversation from a workflow run so the user can follow up
    on the output. Idempotent — clicking "Follow up" twice returns the same
    conversation. For runs that pre-date `replay_messages` (legacy), falls
    back to a single-assistant-message seed using `report_markdown`.
    """
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    result = await session.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.user_id == user.id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")

    # Idempotent: if a conversation was already spawned for this run, return it.
    if run.conversation_id:
        existing = await session.execute(
            select(Conversation).where(Conversation.id == run.conversation_id)
        )
        existing_conv = existing.scalar_one_or_none()
        if existing_conv:
            msg_count = await session.execute(
                select(Message).where(Message.conversation_id == existing_conv.id)
            )
            return RunFollowUpResponse(
                conversation_id=str(existing_conv.id),
                title=existing_conv.title or "",
                seeded_messages=len(msg_count.scalars().all()),
                fallback=not bool(run.replay_messages),
            )

    # Build the title and create the Conversation
    run_date = (run.completed_at or run.triggered_at or run.created_at).strftime("%b %d, %Y")
    title = f"Follow-up: {workflow.name or 'Workflow'} ({run_date})"
    conv = Conversation(
        user_id=user.id,
        title=title,
        title_locked=True,  # auto-titler must not overwrite this
        workflow_run_id=run.id,
        workflow_id=workflow.id,
    )
    session.add(conv)
    await session.flush()
    await session.refresh(conv)

    # Seed messages
    seeded = 0
    fallback = not bool(run.replay_messages)
    if run.replay_messages and isinstance(run.replay_messages, list):
        seeded = await _seed_messages_from_replay(session, conv.id, run.replay_messages)
    if seeded == 0:
        # Either fallback-only run, or replay_messages was empty/malformed.
        # Seed a synthetic user msg (the original trigger isn't reliably
        # available) and an assistant message containing the report.
        synth_user = Message(
            conversation_id=conv.id,
            role="user",
            content=f"[Workflow trigger: {workflow.name or 'Workflow'}]",
        )
        session.add(synth_user)
        await session.flush()
        if run.report_markdown or run.output:
            asst = Message(
                conversation_id=conv.id,
                role="assistant",
                content=run.report_markdown or run.output or "(no output)",
            )
            session.add(asst)
        seeded = 2 if (run.report_markdown or run.output) else 1
        fallback = True

    # Link the run → conv for idempotency on future clicks.
    await session.execute(
        update(WorkflowRun).where(WorkflowRun.id == run.id).values(conversation_id=conv.id)
    )
    await session.commit()

    logger.info(
        "Workflow run follow-up conversation seeded",
        workflow_id=str(workflow_id),
        run_id=str(run_id),
        conversation_id=str(conv.id),
        seeded_messages=seeded,
        fallback=fallback,
    )
    return RunFollowUpResponse(
        conversation_id=str(conv.id),
        title=title,
        seeded_messages=seeded,
        fallback=fallback,
    )


async def _seed_messages_from_replay(
    session: AsyncSession,
    conversation_id: UUID,
    replay: list[dict],
) -> int:
    """Walk a workflow run's persisted Anthropic-format messages and create
    chat `Message` rows so `_load_history` reconstructs the same context on
    subsequent turns. Returns the number of rows inserted.

    Mapping:
    - `{role: "user", content: <str>}`        → 1 user message
    - `{role: "user", content: [tool_results]}` → N tool messages (one per tool_result)
    - `{role: "assistant", content: [...]}`    → 1 assistant message with text +
       all non-text blocks aggregated into `tool_calls`. Thinking blocks merged
       into the `thinking` text column.
    """
    inserted = 0
    for msg in replay:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user":
            if isinstance(content, str):
                session.add(Message(
                    conversation_id=conversation_id,
                    role="user",
                    content=content,
                ))
                inserted += 1
            elif isinstance(content, list):
                # tool_results from a previous round → one Message per result
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result" and block.get("tool_use_id"):
                        result_payload = block.get("content")
                        # Anthropic `content` for tool_result can be a JSON string
                        # or a list of blocks (image/document); store verbatim.
                        if isinstance(result_payload, str):
                            try:
                                import json as _json
                                parsed = _json.loads(result_payload)
                            except Exception:
                                parsed = {"raw": result_payload}
                        else:
                            parsed = {"blocks": result_payload}
                        session.add(Message(
                            conversation_id=conversation_id,
                            role="tool",
                            tool_results={
                                "tool_use_id": block["tool_use_id"],
                                "result": parsed,
                            },
                        ))
                        inserted += 1
            continue

        if role == "assistant":
            if not isinstance(content, list):
                # Plain text assistant message
                session.add(Message(
                    conversation_id=conversation_id,
                    role="assistant",
                    content=content if isinstance(content, str) else None,
                ))
                inserted += 1
                continue
            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_calls: list[dict] = []
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text":
                    if block.get("text"):
                        text_parts.append(block["text"])
                elif btype == "thinking":
                    if block.get("thinking"):
                        thinking_parts.append(block["thinking"])
                else:
                    # tool_use / server_tool_use / advisor_tool_result /
                    # code_execution_tool_result — all stored verbatim into
                    # tool_calls for `_load_history` to reconstruct.
                    tool_calls.append(block)
            session.add(Message(
                conversation_id=conversation_id,
                role="assistant",
                content="\n\n".join(text_parts) if text_parts else None,
                tool_calls=tool_calls or None,
                thinking="\n\n".join(thinking_parts) if thinking_parts else None,
            ))
            inserted += 1
    await session.flush()
    return inserted


class RunPdfExportResponse(BaseModel):
    file_id: str
    filename: str
    url: str
    size_bytes: int


@router.post("/{workflow_id}/runs/{run_id}/export-pdf", response_model=RunPdfExportResponse)
async def export_run_pdf(
    workflow_id: UUID,
    run_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Render this run's `report_markdown` to PDF and save it to Drive under
    Gideon/PDFs/. One-shot conversion — does not modify the run record. The
    user must have Google connected (we upload via their Drive credentials).

    Session lifecycle: we read everything we need from the request-scoped DB
    session (user, workflow name, report markdown, Google token) and release
    it BEFORE the slow external work (~2-5s WeasyPrint render + Drive
    upload). Holding the session across that window ties up a pool slot for
    no DB reason — see CLAUDE.md "Keep sessions short-lived".
    """
    user = await get_or_create_user(session, auth_user)
    workflow = await _get_user_workflow(session, user.id, workflow_id)

    result = await session.execute(
        select(WorkflowRun).where(
            WorkflowRun.id == run_id,
            WorkflowRun.workflow_id == workflow_id,
        )
    )
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    if not run.report_markdown or not run.report_markdown.strip():
        raise HTTPException(status_code=422, detail="This run has no report to export.")

    access_token = await get_valid_access_token("google", user.id)
    if not access_token:
        raise HTTPException(
            status_code=400,
            detail="Connect Google to export PDFs — the file is uploaded to your Drive.",
        )

    title = (workflow.name or "Workflow Run").strip()
    started = run.started_at or run.created_at
    if started:
        title = f"{title} — {started.strftime('%Y-%m-%d %H:%M')}"
    report_markdown = run.report_markdown

    # Release the DB session before the slow stuff. The decrypted strings
    # above (`title`, `report_markdown`, `access_token`) are detached
    # from the ORM by the time we get here.
    await session.close()

    html = _markdown_to_html(report_markdown, title)
    try:
        pdf_bytes = await asyncio.to_thread(_render_html_to_pdf_bytes, html)
    except Exception as e:
        logger.error("Workflow run PDF render failed", run_id=str(run_id), err=str(e))
        raise HTTPException(status_code=500, detail=f"PDF render failed: {e}") from e

    filename = _safe_pdf_filename(title)
    try:
        uploaded = await asyncio.to_thread(_upload_pdf_to_drive_sync, access_token, pdf_bytes, filename)
    except Exception as e:
        logger.error("Workflow run PDF upload failed", run_id=str(run_id), err=str(e))
        raise HTTPException(status_code=500, detail=f"Drive upload failed: {e}") from e

    return RunPdfExportResponse(
        file_id=uploaded["file_id"],
        filename=filename,
        url=uploaded["url"],
        size_bytes=len(pdf_bytes),
    )
