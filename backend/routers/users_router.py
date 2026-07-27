"""
Users Router — profile management and settings endpoints.
"""

import os
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from sqlalchemy import update as sa_update, cast, Date
from sqlalchemy.ext.asyncio import AsyncSession

from pydantic import BaseModel
from sqlalchemy import func, select

from sqlalchemy import delete as sa_delete

from db import get_db
from db.models import ActivityLog, ActivityTopic, User, UsageLog, Workflow, WorkflowRun
from middleware.auth import get_current_user
from models.schemas import UserProfile, UserSettingsResponse, UserSettingsUpdate, UserUpdate
from routers.surveys_router import is_survey_admin
from services.anthropic_pricing import estimate_cost_usd
from services.auth_service import AuthUser
from services.smart_onboarding_service import run_smart_onboarding, run_early_profile_research
from services.user_service import get_or_create_user, update_user
from services.workspace_member_service import get_member_avatar

# Email of the instance administrator. Empty (the default) means no user is
# treated as an admin; set ADMIN_EMAIL to enable admin-only surfaces.
ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "")

# Approximate cost per token (USD) for estimation
_COST_PER_INPUT_TOKEN = 15.0 / 1_000_000   # $15 per 1M input tokens (Opus)
_COST_PER_OUTPUT_TOKEN = 75.0 / 1_000_000  # $75 per 1M output tokens (Opus)

logger = structlog.get_logger()

router = APIRouter()


@router.get("/me", response_model=UserProfile)
async def get_my_profile(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get current user's profile. Auto-creates local record on first call."""
    user = await get_or_create_user(session, auth_user)
    avatar_url = await get_member_avatar(user.email, session)
    return UserProfile(
        id=user.id,
        auth_provider_id=user.auth_provider_id,
        email=user.email,
        name=user.name,
        avatar_url=avatar_url,
        persona_text=user.persona_text,
        timezone=user.timezone,
        preferred_communication=user.preferred_communication or "gmail",
        onboarding_completed=user.onboarding_completed or False,
        roles=auth_user.roles,
        is_survey_admin=is_survey_admin(auth_user.email),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.patch("/me", response_model=UserProfile)
async def update_my_profile(
    body: UserUpdate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update persona, timezone, preferred channel, or name."""
    # Ensure user exists
    await get_or_create_user(session, auth_user)

    user = await update_user(
        session,
        auth_provider_id=auth_user.id,
        name=body.name,
        persona_text=body.persona_text,
        timezone=body.timezone,
        preferred_communication=body.preferred_communication.value if body.preferred_communication else None,
    )
    avatar_url = await get_member_avatar(user.email, session)
    return UserProfile(
        id=user.id,
        auth_provider_id=user.auth_provider_id,
        email=user.email,
        name=user.name,
        avatar_url=avatar_url,
        persona_text=user.persona_text,
        timezone=user.timezone,
        preferred_communication=user.preferred_communication or "gmail",
        onboarding_completed=user.onboarding_completed or False,
        roles=auth_user.roles,
        is_survey_admin=is_survey_admin(auth_user.email),
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.patch("/me/onboarding-complete")
async def complete_onboarding(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Mark current user's onboarding as completed."""
    user = await get_or_create_user(session, auth_user)

    await session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(onboarding_completed=True)
    )
    await session.flush()

    from services.events_service import track_onboarding_completed
    track_onboarding_completed(str(user.id))
    return {"success": True}


# ---------------------------------------------------------------------------
# Required integrations (workspace membership check)
# ---------------------------------------------------------------------------


@router.get("/me/required-integrations")
async def get_required_integrations(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return which integrations this user should connect, based on workspace membership."""
    from services.workspace_member_service import get_required_integrations as _get_required
    required = await _get_required(auth_user.email, session)
    return {"required": required}


# Settings endpoints
# ---------------------------------------------------------------------------


@router.get("/me/settings", response_model=UserSettingsResponse)
async def get_my_settings(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get current user's settings."""
    user = await get_or_create_user(session, auth_user)

    return UserSettingsResponse(
        name=user.name,
        email=user.email,
        timezone=user.timezone or "UTC",
        city=user.city,
        role=user.role,
        department=user.department,
        linkedin_url=user.linkedin_url,
        preferred_communication=user.preferred_communication or "gmail",
        background_info=user.background_info,
        background_generated=user.background_generated or False,
        feature_action_items=user.feature_action_items if user.feature_action_items is not None else True,
        feature_email_drafts=user.feature_email_drafts if user.feature_email_drafts is not None else True,
        feature_meeting_prep=user.feature_meeting_prep if user.feature_meeting_prep is not None else True,
        feature_dev_console=user.feature_dev_console if user.feature_dev_console is not None else False,
        onboarding_stage=user.onboarding_stage,
    )


@router.patch("/me/settings", response_model=UserSettingsResponse)
async def update_my_settings(
    body: UserSettingsUpdate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update user settings. Name and preferred_communication are updatable. Email is not."""
    user = await get_or_create_user(session, auth_user)

    values = {}
    if body.name is not None:
        values["name"] = body.name
    if body.timezone is not None:
        values["timezone"] = body.timezone
    if body.city is not None:
        values["city"] = body.city
    if body.role is not None:
        values["role"] = body.role
    if body.department is not None:
        values["department"] = body.department
    if body.linkedin_url is not None:
        values["linkedin_url"] = body.linkedin_url
    if body.background_info is not None:
        values["background_info"] = body.background_info
    if body.preferred_communication is not None:
        values["preferred_communication"] = body.preferred_communication.value
    if body.feature_action_items is not None:
        values["feature_action_items"] = body.feature_action_items
    if body.feature_email_drafts is not None:
        values["feature_email_drafts"] = body.feature_email_drafts
    if body.feature_meeting_prep is not None:
        values["feature_meeting_prep"] = body.feature_meeting_prep
    if body.feature_dev_console is not None:
        values["feature_dev_console"] = body.feature_dev_console

    if values:
        await session.execute(
            sa_update(User)
            .where(User.auth_provider_id == auth_user.id)
            .values(**values)
        )
        await session.flush()
        await session.refresh(user)

    return UserSettingsResponse(
        name=user.name,
        email=user.email,
        timezone=user.timezone or "UTC",
        city=user.city,
        role=user.role,
        department=user.department,
        linkedin_url=user.linkedin_url,
        preferred_communication=user.preferred_communication or "gmail",
        background_info=user.background_info,
        background_generated=user.background_generated or False,
        feature_action_items=user.feature_action_items if user.feature_action_items is not None else True,
        feature_email_drafts=user.feature_email_drafts if user.feature_email_drafts is not None else True,
        feature_meeting_prep=user.feature_meeting_prep if user.feature_meeting_prep is not None else True,
        feature_dev_console=user.feature_dev_console if user.feature_dev_console is not None else False,
        onboarding_stage=user.onboarding_stage,
    )


@router.post("/me/generate-background", response_model=UserSettingsResponse)
async def trigger_background_research(
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Manually trigger background research for the current user.
    Resets background_generated so it re-runs even if previously completed.
    """
    user = await get_or_create_user(session, auth_user)

    # Reset flag so it runs fresh
    await session.execute(
        sa_update(User)
        .where(User.id == user.id)
        .values(background_generated=False, background_info=None)
    )
    await session.flush()
    await session.refresh(user)

    background_tasks.add_task(run_smart_onboarding, user.id)

    return UserSettingsResponse(
        name=user.name,
        email=user.email,
        preferred_communication=user.preferred_communication or "gmail",
        background_info=None,
        background_generated=False,
        feature_action_items=user.feature_action_items if user.feature_action_items is not None else True,
        feature_email_drafts=user.feature_email_drafts if user.feature_email_drafts is not None else True,
        feature_meeting_prep=user.feature_meeting_prep if user.feature_meeting_prep is not None else True,
        feature_dev_console=user.feature_dev_console if user.feature_dev_console is not None else False,
        onboarding_stage=user.onboarding_stage,
    )


@router.post("/me/profile-research")
async def trigger_profile_research(
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Start web profile research early (during onboarding step 2).
    Runs in background so it's ready by step 3.
    """
    user = await get_or_create_user(session, auth_user)
    background_tasks.add_task(run_early_profile_research, user.id)
    return {"status": "started"}


@router.post("/me/smart-onboarding")
async def trigger_smart_onboarding(
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Trigger smart onboarding: gather data from Google, Slack, and Notion,
    analyze with Claude, and generate a rich user profile + personalized workflows.
    Runs as a background task.
    """
    user = await get_or_create_user(session, auth_user)
    background_tasks.add_task(run_smart_onboarding, user.id)
    logger.info("Smart onboarding triggered", user_id=str(user.id))
    return {"status": "started"}


def _period_filter(period: str):
    """Return a created_at filter clause based on the period string, or None for 'all'."""
    now = datetime.now(timezone.utc)
    if period == "week":
        return UsageLog.created_at >= now - timedelta(days=7)
    elif period == "month":
        first_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        return UsageLog.created_at >= first_of_month
    else:  # "all"
        return None


def _month_filter(year: int, month: int) -> list:
    """Return filter clauses for a specific year+month."""
    from calendar import monthrange
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    _, last_day = monthrange(year, month)
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
    return [UsageLog.created_at >= start, UsageLog.created_at <= end]


@router.get("/me/usage")
async def get_my_usage(
    period: str = Query(default="month", pattern="^(week|month|all)$"),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get token usage summary for the current user with period filtering."""
    user = await get_or_create_user(session, auth_user)

    base_filter = [UsageLog.user_id == user.id]
    period_clause = _period_filter(period)
    if period_clause is not None:
        base_filter.append(period_clause)

    # Totals
    result = await session.execute(
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("total_input"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("total_output"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("total_cache_read"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("total_cache_creation"),
            func.coalesce(func.sum(UsageLog.web_search_requests), 0).label("total_web_searches"),
            func.count(UsageLog.id).label("total_requests"),
        ).where(*base_filter)
    )
    totals = result.one()

    # By use case
    by_case_result = await session.execute(
        select(
            UsageLog.use_case,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*base_filter).group_by(UsageLog.use_case)
    )
    by_case = [
        {"use_case": row[0], "input_tokens": row[1], "output_tokens": row[2], "requests": row[3]}
        for row in by_case_result.all()
    ]

    # By model
    by_model_result = await session.execute(
        select(
            UsageLog.model,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*base_filter).group_by(UsageLog.model)
    )
    by_model = [
        {"model": row[0], "input_tokens": row[1], "output_tokens": row[2], "requests": row[3]}
        for row in by_model_result.all()
    ]

    # By day (last 30 days max)
    thirty_days_ago = datetime.now(timezone.utc) - timedelta(days=30)
    day_filter = list(base_filter) + [UsageLog.created_at >= thirty_days_ago]
    by_day_result = await session.execute(
        select(
            cast(UsageLog.created_at, Date).label("date"),
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*day_filter)
        .group_by(cast(UsageLog.created_at, Date))
        .order_by(cast(UsageLog.created_at, Date))
    )
    by_day = [
        {"date": str(row[0]), "input_tokens": row[1], "output_tokens": row[2], "requests": row[3]}
        for row in by_day_result.all()
    ]

    total_input = totals[0]
    total_output = totals[1]
    estimated_cost = (total_input * _COST_PER_INPUT_TOKEN) + (total_output * _COST_PER_OUTPUT_TOKEN)

    return {
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": totals[2],
        "total_cache_creation_tokens": totals[3],
        "total_web_searches": totals[4],
        "total_requests": totals[5],
        "estimated_cost_usd": round(estimated_cost, 4),
        "by_use_case": by_case,
        "by_model": by_model,
        "by_day": by_day,
        "period": period,
    }


@router.get("/admin/usage")
async def get_admin_usage(
    year: int = Query(default=None),
    month: int = Query(default=None, ge=1, le=12),
    group_by: str = Query(default="model", pattern="^(model|use_case|user)$"),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get global usage summary across all users. Admin only.
    Supports year+month filtering (defaults to current month).
    group_by controls the daily breakdown dimension.
    """
    if auth_user.email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access required")

    now = datetime.now(timezone.utc)
    if year is None:
        year = now.year
    if month is None:
        month = now.month

    base_filter = _month_filter(year, month)

    # Global totals
    result = await session.execute(
        select(
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("total_input"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("total_output"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("total_cache_read"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("total_cache_creation"),
            func.coalesce(func.sum(UsageLog.web_search_requests), 0).label("total_web_searches"),
            func.count(UsageLog.id).label("total_requests"),
        ).where(*base_filter)
    )
    totals = result.one()

    total_input = totals[0]
    total_output = totals[1]

    # By user — include cache columns so per-bucket cost can be computed
    # accurately (Sonnet cache writes alone account for a meaningful chunk
    # of monthly spend; dropping them from per-bucket aggregates makes the
    # slice numbers ~30% low vs the totals.)
    by_user_result = await session.execute(
        select(
            UsageLog.user_id,
            User.name,
            User.email,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.count(UsageLog.id).label("requests"),
        )
        .join(User, UsageLog.user_id == User.id)
        .where(*base_filter)
        .group_by(UsageLog.user_id, User.name, User.email)
        .order_by(func.sum(
            UsageLog.input_tokens
            + UsageLog.output_tokens
            + UsageLog.cache_read_tokens
            + UsageLog.cache_creation_tokens
        ).desc())
    )
    by_user = [
        {"user_id": str(row[0]), "name": row[1], "email": row[2],
         "input_tokens": row[3], "output_tokens": row[4],
         "cache_read_tokens": row[5], "cache_creation_tokens": row[6],
         "requests": row[7]}
        for row in by_user_result.all()
    ]

    # By use case
    by_case_result = await session.execute(
        select(
            UsageLog.use_case,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*base_filter).group_by(UsageLog.use_case)
    )
    by_use_case = [
        {"use_case": row[0], "input_tokens": row[1], "output_tokens": row[2],
         "cache_read_tokens": row[3], "cache_creation_tokens": row[4],
         "requests": row[5]}
        for row in by_case_result.all()
    ]

    # By model
    by_model_result = await session.execute(
        select(
            UsageLog.model,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*base_filter).group_by(UsageLog.model)
    )
    by_model = [
        {"model": row[0], "input_tokens": row[1], "output_tokens": row[2],
         "cache_read_tokens": row[3], "cache_creation_tokens": row[4],
         "requests": row[5]}
        for row in by_model_result.all()
    ]

    # Daily breakdown grouped by the selected dimension
    date_col = cast(UsageLog.created_at, Date).label("date")
    if group_by == "model":
        group_col = UsageLog.model.label("group_key")
    elif group_by == "use_case":
        group_col = UsageLog.use_case.label("group_key")
    else:  # user
        group_col = User.name.label("group_key")

    daily_query = (
        select(
            date_col,
            group_col,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.count(UsageLog.id).label("requests"),
        ).where(*base_filter)
        .group_by(date_col, group_col)
        .order_by(date_col)
    )
    if group_by == "user":
        daily_query = daily_query.join(User, UsageLog.user_id == User.id)

    daily_result = await session.execute(daily_query)
    by_day = [
        {"date": str(row[0]), "group": row[1],
         "input_tokens": row[2], "output_tokens": row[3],
         "cache_read_tokens": row[4], "cache_creation_tokens": row[5],
         "requests": row[6]}
        for row in daily_result.all()
    ]

    return {
        "total_input_tokens": total_input,
        "total_output_tokens": total_output,
        "total_cache_read_tokens": totals[2],
        "total_cache_creation_tokens": totals[3],
        "total_web_searches": totals[4],
        "total_requests": totals[5],
        "by_user": by_user,
        "by_use_case": by_use_case,
        "by_model": by_model,
        "by_day": by_day,
        "year": year,
        "month": month,
        "group_by": group_by,
    }


@router.get("/admin/workflow-spend")
async def get_admin_workflow_spend(
    year: int = Query(default=None),
    month: int = Query(default=None, ge=1, le=12),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Per-workflow spend breakdown — runs counted by (workflow_id, user_id),
    tokens attributed by time-window overlap with `usage_log` rows where
    `use_case IN ('workflow', 'workflow_advisor')`.

    Caveat: a `usage_log` row that falls inside the time window of two
    overlapping runs for the same user is attributed to BOTH. In practice
    runs are sequential per user (the scheduler doesn't fan out parallel
    runs for the same workflow), so double-attribution is rare. If we need
    bulletproof per-run accounting later, add `workflow_run_id` to
    `usage_log` and backfill — until then, this approximation is what we
    have.
    """
    if auth_user.email != ADMIN_EMAIL:
        raise HTTPException(status_code=403, detail="Admin access required")

    now = datetime.now(timezone.utc)
    if year is None:
        year = now.year
    if month is None:
        month = now.month
    from calendar import monthrange
    start = datetime(year, month, 1, tzinfo=timezone.utc)
    _, last_day = monthrange(year, month)
    end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)

    # Per-workflow run counts in the period (cheap, exact).
    runs_q = (
        select(
            WorkflowRun.workflow_id,
            Workflow.name,
            WorkflowRun.user_id,
            User.name.label("user_name"),
            User.email.label("user_email"),
            func.count(WorkflowRun.id).label("run_count"),
            func.sum(
                func.coalesce(
                    func.extract("epoch", WorkflowRun.completed_at - WorkflowRun.triggered_at),
                    0,
                )
            ).label("total_runtime_seconds"),
            func.max(WorkflowRun.triggered_at).label("last_run_at"),
        )
        .join(Workflow, Workflow.id == WorkflowRun.workflow_id)
        .join(User, User.id == WorkflowRun.user_id)
        .where(WorkflowRun.triggered_at >= start, WorkflowRun.triggered_at <= end)
        .group_by(WorkflowRun.workflow_id, Workflow.name, WorkflowRun.user_id, User.name, User.email)
        .order_by(func.count(WorkflowRun.id).desc())
    )
    runs_rows = (await session.execute(runs_q)).all()

    # Token attribution: join usage_log to workflow_runs by (user_id) + time
    # window, grouped by model so per-model pricing is applied correctly.
    # `workflow_advisor` rows use Opus, `workflow` rows use Sonnet by
    # default — flattening to a single pricing curve mis-ranks Opus-heavy
    # workflows (Opus output is 5× Sonnet output / 75× Sonnet cache-read).
    # Runs whose `completed_at` is NULL (still in flight or crashed) under-
    # count: their usage rows fall outside the window. Acceptable trade-
    # off vs widening with a synthetic ceiling that would over-count other
    # runs.
    tokens_q = (
        select(
            WorkflowRun.workflow_id,
            WorkflowRun.user_id,
            UsageLog.model,
            func.coalesce(func.sum(UsageLog.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(UsageLog.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(UsageLog.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(UsageLog.cache_creation_tokens), 0).label("cache_creation_tokens"),
        )
        .join(
            UsageLog,
            (UsageLog.user_id == WorkflowRun.user_id)
            & (UsageLog.use_case.in_(["workflow", "workflow_advisor"]))
            & (UsageLog.created_at >= WorkflowRun.triggered_at)
            & (UsageLog.created_at <= func.coalesce(WorkflowRun.completed_at, WorkflowRun.triggered_at)),
        )
        .where(WorkflowRun.triggered_at >= start, WorkflowRun.triggered_at <= end)
        .group_by(WorkflowRun.workflow_id, WorkflowRun.user_id, UsageLog.model)
    )
    tokens_rows = (await session.execute(tokens_q)).all()

    # Per-(workflow_id, user_id) → list of per-model token buckets.
    tokens_by_key: dict[tuple, list[dict]] = {}
    for row in tokens_rows:
        key = (row[0], row[1])
        tokens_by_key.setdefault(key, []).append({
            "model": row[2],
            "input_tokens": row[3],
            "output_tokens": row[4],
            "cache_read_tokens": row[5],
            "cache_creation_tokens": row[6],
        })

    # Pricing lives in `services.anthropic_pricing` so this router doesn't
    # carry its own copy of the rate table (and so the "advisor"
    # placeholder used by `usage_service` resolves to Opus rather than the
    # Sonnet fallback).
    by_workflow = []
    for row in runs_rows:
        key = (row[0], row[2])
        model_buckets = tokens_by_key.get(key, [])
        # Roll the per-model buckets up into totals + cost.
        totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0}
        est_cost = 0.0
        for b in model_buckets:
            totals["input_tokens"] += b["input_tokens"]
            totals["output_tokens"] += b["output_tokens"]
            totals["cache_read_tokens"] += b["cache_read_tokens"]
            totals["cache_creation_tokens"] += b["cache_creation_tokens"]
            est_cost += estimate_cost_usd(
                b["model"],
                b["input_tokens"],
                b["output_tokens"],
                b["cache_read_tokens"],
                b["cache_creation_tokens"],
            )
        by_workflow.append({
            "workflow_id": str(row[0]),
            "workflow_name": row[1],
            "user_id": str(row[2]),
            "user_name": row[3],
            "user_email": row[4],
            "run_count": row[5],
            "total_runtime_seconds": float(row[6] or 0),
            "last_run_at": row[7].isoformat() if row[7] else None,
            **totals,
            "by_model": model_buckets,  # surfaced so callers can see Sonnet vs Opus split.
            "est_cost_usd": round(est_cost, 4),
        })

    # Sort by estimated USD cost — the actual "top spenders" question.
    by_workflow.sort(key=lambda r: r["est_cost_usd"], reverse=True)

    return {
        "year": year,
        "month": month,
        "by_workflow": by_workflow,
        "count": len(by_workflow),
    }


@router.post("/me/logout")
async def logout():
    """Logout endpoint. Frontend clears the token; this is a no-op server-side."""
    return {"success": True, "message": "Logged out successfully"}


@router.delete("/me")
async def delete_my_account(
    request: Request,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Permanently delete all user data and the user record.
    Clears: DB rows, mem0 memories, Redis cache (including auth token).

    Most tables CASCADE from users(id), but activity_log and activity_topics
    use plain REFERENCES (no CASCADE), so they must be deleted explicitly
    before the user row.
    """
    user = await get_or_create_user(session, auth_user)
    user_id = user.id
    logger.info("Deleting all user data", user_id=str(user_id))

    # activity_log.topic_id REFERENCES activity_topics(id) (no CASCADE),
    # so activity_log must go first.
    await session.execute(
        sa_delete(ActivityLog).where(ActivityLog.user_id == user_id)
    )
    await session.execute(
        sa_delete(ActivityTopic).where(ActivityTopic.user_id == user_id)
    )

    # Delete user row. Cascades: integrations, conversations (-> messages,
    # uploaded_files, trigger_conversations), skills, workflows (-> runs),
    # todos (-> subtasks, todo_updates), todo_suggestions, memories,
    # email_auto_drafts, meeting_preps, usage_log.
    await session.execute(
        sa_delete(User).where(User.id == user_id)
    )

    # Commit DB changes and release the connection before external I/O
    await session.commit()

    # Clear Graphiti memories (best-effort, don't block on failure).
    try:
        from services.graphiti_service import delete_all_memories as graphiti_delete_all
        await graphiti_delete_all(str(user_id))
    except Exception as e:
        logger.warning("Failed to delete Graphiti memories", user_id=str(user_id), error=str(e))

    # Invalidate auth token cache so concurrent/subsequent requests
    # don't find the deleted user in Redis and try to re-create it
    try:
        from services.user_cache_service import invalidate_cache
        authorization = request.headers.get("authorization", "")
        if authorization:
            await invalidate_cache(authorization)
    except Exception as e:
        logger.warning("Failed to invalidate auth cache", user_id=str(user_id), error=str(e))

    logger.info("User data deleted successfully", user_id=str(user_id))
    return {"success": True}
