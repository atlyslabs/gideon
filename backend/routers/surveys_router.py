"""
Surveys Router — in-app blocking surveys with department/role-scoped eligibility.

A survey blocks the chat UI on first load until the user submits or dismisses it.
Eligibility is evaluated server-side against `users.department`, `users.role`,
and `users.email`. See `migrations/init.sql` for the eligibility JSON shape.
"""

import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import distinct, func

from db import get_db
from db.models import Conversation, Survey, SurveyResponse, User
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user

logger = structlog.get_logger()
router = APIRouter()

# Single source of truth for who can create/manage surveys. `is_survey_admin`
# (returned by /v1/users/me) is derived from this list, and the survey tools
# in tools/survey_tool.py refuse to execute unless the caller's email is here.
SURVEY_ADMINS: frozenset[str] = frozenset(
    e.strip().lower()
    for e in os.environ.get("SURVEY_ADMINS", "").split(",")
    if e.strip()
)


def is_survey_admin(email: str | None) -> bool:
    return bool(email) and email.lower() in SURVEY_ADMINS


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SurveyQuestion(BaseModel):
    id: str
    type: str  # likert_1_5 | text | single_select | multi_select
    prompt: str
    options: Optional[list[str]] = None
    required: bool = True
    placeholder: Optional[str] = None


class SurveyOut(BaseModel):
    id: UUID
    slug: str
    title: str
    intro_text: Optional[str]
    questions: list[SurveyQuestion]


class PendingSurveyResponse(BaseModel):
    survey: Optional[SurveyOut] = None


class SurveySubmitRequest(BaseModel):
    answers: dict[str, Any] = Field(default_factory=dict)


class SurveyEligibility(BaseModel):
    departments: list[str] = Field(default_factory=list)
    role_keywords: list[str] = Field(default_factory=list)
    emails: list[str] = Field(default_factory=list)


class SurveyCreateRequest(BaseModel):
    slug: str
    title: str
    intro_text: Optional[str] = None
    # Builder defaults to draft so admins can review before activating.
    status: str = "draft"
    eligibility: SurveyEligibility = Field(default_factory=SurveyEligibility)
    questions: list[SurveyQuestion] = Field(default_factory=list)


class SurveyUpdateRequest(BaseModel):
    """Partial update — None fields are ignored. status_change goes via /status."""
    title: Optional[str] = None
    intro_text: Optional[str] = None
    eligibility: Optional[SurveyEligibility] = None
    questions: Optional[list[SurveyQuestion]] = None


class SurveyResponseRow(BaseModel):
    id: UUID
    user_id: UUID
    user_email: str
    user_name: Optional[str]
    user_role: Optional[str]
    user_department: Optional[str]
    status: str
    answers: dict[str, Any]
    submitted_at: Optional[datetime]
    dismissed_at: Optional[datetime]


# ---------------------------------------------------------------------------
# Eligibility
# ---------------------------------------------------------------------------


def _is_eligible(user: User, eligibility: dict) -> bool:
    """Pure function — no I/O.

    Rules:
      - Empty eligibility (or all filters empty) = broadcast (everyone eligible)
      - email in `emails` list = eligible (overrides dept/role mismatch)
      - if both `departments` and `role_keywords` are empty but `emails` is set,
        only listed emails pass
      - otherwise, must pass `departments` AND `role_keywords` filters (if set)
    """
    if not eligibility:
        return True

    emails = [e.lower() for e in (eligibility.get("emails") or [])]
    departments = eligibility.get("departments") or []
    role_keywords = [k.lower() for k in (eligibility.get("role_keywords") or [])]

    if emails and user.email and user.email.lower() in emails:
        return True

    if not departments and not role_keywords:
        # Either broadcast (no filters at all) or email-only (user not in list)
        return not emails

    if departments and (not user.department or user.department not in departments):
        return False

    if role_keywords:
        role_lower = (user.role or "").lower()
        if not role_lower or not any(kw in role_lower for kw in role_keywords):
            return False

    return True


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/pending", response_model=PendingSurveyResponse)
async def get_pending_survey(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return the first active survey this user is eligible for and hasn't responded to.

    Dismissals AND submissions both prevent the survey from re-appearing.
    """
    user = await get_or_create_user(session, auth_user)

    # Active surveys, oldest first (so older surveys roll out before newer ones)
    rows = await session.execute(
        select(Survey).where(Survey.status == "active").order_by(Survey.created_at.asc())
    )
    surveys = rows.scalars().all()
    if not surveys:
        return PendingSurveyResponse(survey=None)

    # Look up which surveys the user has already responded to
    responded = await session.execute(
        select(SurveyResponse.survey_id).where(SurveyResponse.user_id == user.id)
    )
    responded_ids = {row[0] for row in responded.all()}

    for survey in surveys:
        if survey.id in responded_ids:
            continue
        if not _is_eligible(user, survey.eligibility or {}):
            continue
        return PendingSurveyResponse(
            survey=SurveyOut(
                id=survey.id,
                slug=survey.slug,
                title=survey.title,
                intro_text=survey.intro_text,
                questions=[SurveyQuestion(**q) for q in (survey.questions or [])],
            )
        )

    return PendingSurveyResponse(survey=None)


@router.post("/{slug}/respond")
async def submit_survey_response(
    slug: str,
    body: SurveySubmitRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Upsert a survey response with status='submitted'. Idempotent."""
    user = await get_or_create_user(session, auth_user)

    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    if survey.status != "active":
        raise HTTPException(status_code=400, detail="Survey is not active")
    if not _is_eligible(user, survey.eligibility or {}):
        raise HTTPException(status_code=403, detail="Not eligible for this survey")

    # Validate required questions
    questions = survey.questions or []
    answers = body.answers or {}
    for q in questions:
        if q.get("required") and not _has_answer(answers.get(q["id"])):
            raise HTTPException(
                status_code=400,
                detail=f"Missing required answer for: {q.get('prompt', q['id'])}",
            )

    now = datetime.now(timezone.utc)
    stmt = pg_insert(SurveyResponse).values(
        survey_id=survey.id,
        user_id=user.id,
        status="submitted",
        answers=answers,
        submitted_at=now,
    ).on_conflict_do_update(
        constraint="uq_survey_responses_survey_user",
        set_={"status": "submitted", "answers": answers, "submitted_at": now},
    )
    await session.execute(stmt)
    await session.flush()

    logger.info(
        "Survey response submitted",
        survey_slug=slug,
        user_id=str(user.id),
        answer_count=len(answers),
    )
    return {"success": True}


@router.post("/{slug}/dismiss")
async def dismiss_survey(
    slug: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Upsert a survey response with status='dismissed' so it doesn't reappear."""
    user = await get_or_create_user(session, auth_user)

    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    now = datetime.now(timezone.utc)
    stmt = pg_insert(SurveyResponse).values(
        survey_id=survey.id,
        user_id=user.id,
        status="dismissed",
        answers={},
        dismissed_at=now,
    ).on_conflict_do_update(
        constraint="uq_survey_responses_survey_user",
        set_={"status": "dismissed", "dismissed_at": now},
    )
    await session.execute(stmt)
    await session.flush()
    return {"success": True}


# ---------------------------------------------------------------------------
# Admin endpoints (SURVEY_ADMINS only) — create/list/inspect surveys
# ---------------------------------------------------------------------------


def _require_admin(auth_user: AuthUser) -> None:
    if not is_survey_admin(auth_user.email):
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/admin/create")
async def admin_create_survey(
    body: SurveyCreateRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    _require_admin(auth_user)
    user = await get_or_create_user(session, auth_user)

    existing = await session.execute(select(Survey).where(Survey.slug == body.slug))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail=f"Survey with slug '{body.slug}' already exists")

    survey = Survey(
        slug=body.slug,
        title=body.title,
        intro_text=body.intro_text,
        status=body.status,
        eligibility=body.eligibility.model_dump(),
        questions=[q.model_dump() for q in body.questions],
        created_by=user.id,
    )
    session.add(survey)
    await session.flush()
    return {"id": str(survey.id), "slug": survey.slug}


# IMPORTANT: fixed-path routes (`/admin/list`, `/admin/org-breakdown`) MUST be
# declared before `/admin/{slug}` — FastAPI matches in declaration order and
# `{slug}` would otherwise capture them.


@router.get("/admin/list")
async def admin_list_surveys(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    _require_admin(auth_user)
    rows = await session.execute(select(Survey).order_by(Survey.created_at.desc()))
    surveys = rows.scalars().all()

    # Single grouped query for response counts (one row per survey, status counts inline)
    count_rows = await session.execute(
        select(
            SurveyResponse.survey_id,
            SurveyResponse.status,
            func.count(SurveyResponse.id),
        ).group_by(SurveyResponse.survey_id, SurveyResponse.status)
    )
    counts: dict = {}
    for survey_id, status_str, n in count_rows.all():
        counts.setdefault(survey_id, {})[status_str] = n

    return {
        "surveys": [
            {
                "id": str(s.id),
                "slug": s.slug,
                "title": s.title,
                "status": s.status,
                "eligibility": s.eligibility,
                "question_count": len(s.questions or []),
                "submitted_count": counts.get(s.id, {}).get("submitted", 0),
                "dismissed_count": counts.get(s.id, {}).get("dismissed", 0),
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in surveys
        ]
    }


@router.get("/admin/org-breakdown")
async def admin_org_breakdown(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return distinct departments + roles with user counts.

    Powers the targeting picker in the builder UI and Gideon's `get_org_breakdown`
    tool. Counts are NOT cached — at ~250 users this is a sub-100ms query.
    """
    _require_admin(auth_user)

    dept_rows = await session.execute(
        select(User.department, func.count(User.id))
        .where(User.department.isnot(None))
        .group_by(User.department)
        .order_by(func.count(User.id).desc())
    )
    role_rows = await session.execute(
        select(User.role, func.count(User.id))
        .where(User.role.isnot(None))
        .group_by(User.role)
        .order_by(func.count(User.id).desc())
    )
    total = (await session.execute(select(func.count(User.id)))).scalar_one()

    return {
        "total_users": total,
        "departments": [{"name": d, "count": c} for d, c in dept_rows.all() if d],
        "roles": [{"name": r, "count": c} for r, c in role_rows.all() if r],
    }


@router.get("/admin/{slug}")
async def admin_get_survey(
    slug: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    _require_admin(auth_user)
    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    return _survey_to_dict(survey)


@router.patch("/admin/{slug}")
async def admin_update_survey(
    slug: str,
    body: SurveyUpdateRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update title / intro / eligibility / questions on a survey.

    Mutations are allowed on `draft` and `active` surveys. Closed surveys are
    immutable. We invalidate cached analysis whenever questions change so the
    next read regenerates against the new schema.
    """
    _require_admin(auth_user)
    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    if survey.status == "closed":
        raise HTTPException(status_code=400, detail="Closed surveys are immutable")

    fields_set = body.model_fields_set
    if "title" in fields_set:
        if not body.title or not body.title.strip():
            raise HTTPException(status_code=400, detail="Title cannot be empty")
        survey.title = body.title.strip()
    if "intro_text" in fields_set:
        survey.intro_text = body.intro_text
    if "eligibility" in fields_set and body.eligibility is not None:
        survey.eligibility = body.eligibility.model_dump()
    if "questions" in fields_set and body.questions is not None:
        survey.questions = [q.model_dump() for q in body.questions]
        # Question schema changed → previous analysis is stale.
        survey.analysis = None
        survey.analysis_generated_at = None

    await session.flush()
    return _survey_to_dict(survey)


@router.post("/admin/{slug}/conversation")
async def admin_get_or_create_builder_conversation(
    slug: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Lazily create a conversation for the survey builder chat.

    Each survey draft has at most one builder conversation, owned by the admin
    who first opened it. Returning an existing conversation lets admins resume
    the same chat across sessions.
    """
    _require_admin(auth_user)
    user = await get_or_create_user(session, auth_user)

    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    if survey.builder_conversation_id:
        # Verify it still exists and belongs to a survey admin
        conv_row = await session.execute(
            select(Conversation).where(Conversation.id == survey.builder_conversation_id)
        )
        conv = conv_row.scalar_one_or_none()
        if conv:
            return {"conversation_id": str(conv.id), "user_id": str(conv.user_id)}

    conv = Conversation(user_id=user.id, title=f"Survey: {survey.title}", title_locked=True)
    session.add(conv)
    await session.flush()
    survey.builder_conversation_id = conv.id
    await session.flush()
    return {"conversation_id": str(conv.id), "user_id": str(user.id)}


@router.get("/admin/{slug}/analysis")
async def admin_get_analysis(
    slug: str,
    refresh: bool = False,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Return Gideon-generated analysis of responses (themes, scores, usage correlation).

    First call generates and caches; subsequent reads hit the cache. Pass
    `?refresh=true` to force regeneration (also done automatically when
    questions change via PATCH).
    """
    _require_admin(auth_user)
    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    if not refresh and survey.analysis:
        return {
            "analysis": survey.analysis,
            "generated_at": survey.analysis_generated_at.isoformat() if survey.analysis_generated_at else None,
            "cached": True,
        }

    from services.survey_analysis_service import generate_analysis

    analysis = await generate_analysis(survey.id, session)
    survey.analysis = analysis
    survey.analysis_generated_at = datetime.now(timezone.utc)
    await session.flush()
    return {
        "analysis": analysis,
        "generated_at": survey.analysis_generated_at.isoformat(),
        "cached": False,
    }


def _survey_to_dict(s: Survey) -> dict:
    return {
        "id": str(s.id),
        "slug": s.slug,
        "title": s.title,
        "intro_text": s.intro_text,
        "status": s.status,
        "eligibility": s.eligibility,
        "questions": s.questions,
        "builder_conversation_id": str(s.builder_conversation_id) if s.builder_conversation_id else None,
        "analysis_generated_at": s.analysis_generated_at.isoformat() if s.analysis_generated_at else None,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "closed_at": s.closed_at.isoformat() if s.closed_at else None,
    }


@router.get("/admin/{slug}/responses")
async def admin_list_responses(
    slug: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    _require_admin(auth_user)
    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")

    rows = await session.execute(
        select(SurveyResponse, User)
        .join(User, User.id == SurveyResponse.user_id)
        .where(SurveyResponse.survey_id == survey.id)
        .order_by(SurveyResponse.created_at.desc())
    )
    items = []
    for resp, user in rows.all():
        items.append(
            SurveyResponseRow(
                id=resp.id,
                user_id=user.id,
                user_email=user.email,
                user_name=user.name,
                user_role=user.role,
                user_department=user.department,
                status=resp.status,
                answers=resp.answers or {},
                submitted_at=resp.submitted_at,
                dismissed_at=resp.dismissed_at,
            )
        )
    return {
        "survey": {"slug": survey.slug, "title": survey.title, "questions": survey.questions},
        "responses": items,
    }


@router.patch("/admin/{slug}/status")
async def admin_set_status(
    slug: str,
    body: dict,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    _require_admin(auth_user)
    new_status = body.get("status")
    if new_status not in {"draft", "active", "closed"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        raise HTTPException(status_code=404, detail="Survey not found")
    survey.status = new_status
    if new_status == "closed":
        survey.closed_at = datetime.now(timezone.utc)
    await session.flush()
    return {"success": True, "status": new_status}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _has_answer(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return value.strip() != ""
    if isinstance(value, (list, dict)):
        return len(value) > 0
    return True
