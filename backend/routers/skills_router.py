"""
Skills Router — CRUD + learning endpoints for AI skills.
Skills are learnable behaviors that analyze user communication patterns.
"""

from datetime import datetime
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from db import get_db
from db.models import Skill
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user

logger = structlog.get_logger()

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = None
    prompt: str = Field(..., min_length=1)
    integrations_used: list[str] = Field(default_factory=list)


class SkillUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    description: Optional[str] = None
    prompt: Optional[str] = Field(None, min_length=1)
    integrations_used: Optional[list[str]] = None
    memory_content: Optional[str] = None


class SkillResponse(BaseModel):
    id: str
    name: str
    description: Optional[str]
    prompt: str
    integrations_used: list[str]
    memory_content: Optional[str]
    is_default: bool
    last_run_at: Optional[datetime]
    status: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SkillListResponse(BaseModel):
    skills: list[SkillResponse]
    count: int


class SkillLearnResponse(BaseModel):
    skill_id: str
    status: str
    message: str


# ---------------------------------------------------------------------------
# Default skill definitions
# ---------------------------------------------------------------------------

DEFAULT_SKILLS = [
    {
        "name": "Email Tone",
        "description": "Learns your email communication style, tone, and writing patterns.",
        "prompt": (
            "Learn my email communication style, tone, and writing patterns. "
            "Analyze how I write emails including greeting patterns, sign-offs, "
            "sentence structure, vocabulary, formality level, and how my style "
            "varies by recipient or context."
        ),
        "integrations_used": ["google"],
    },
    {
        "name": "Slack Tone",
        "description": "Learns your Slack messaging style, tone, and communication patterns.",
        "prompt": (
            "Learn my Slack communication style, tone, and messaging patterns. "
            "Analyze how I write Slack messages including message length, emoji usage, "
            "thread vs channel behavior, formality shifts by channel or DM, formatting "
            "habits, and how my style varies by context."
        ),
        "integrations_used": ["slack"],
    },
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _skill_to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        id=str(skill.id),
        name=skill.name,
        description=skill.description,
        prompt=skill.prompt,
        integrations_used=skill.integrations_used or [],
        memory_content=skill.memory_content,
        is_default=skill.is_default or False,
        last_run_at=skill.last_run_at,
        status=skill.status or "idle",
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


async def _run_learning_in_background(skill_id: UUID, user_id: UUID):
    """Run skill learning in a separate DB session (for background tasks)."""
    from db.database import AsyncPostgresSessionGideon
    from services.skill_learning_service import run_skill_learning

    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        try:
            await run_skill_learning(skill_id, user_id, session)
        except Exception as e:
            logger.error("Background skill learning failed", skill_id=str(skill_id), error=str(e))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("", response_model=SkillListResponse)
async def list_skills(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all skills for the current user."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Skill)
        .where(Skill.user_id == user.id)
        .order_by(Skill.created_at)
    )
    skills = result.scalars().all()

    return SkillListResponse(
        skills=[_skill_to_response(s) for s in skills],
        count=len(skills),
    )


@router.get("/{skill_id}", response_model=SkillResponse)
async def get_skill(
    skill_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get a single skill with its learned memory content."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == user.id)
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    return _skill_to_response(skill)


@router.post("", response_model=SkillResponse, status_code=201)
async def create_skill(
    body: SkillCreate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Create a custom skill."""
    user = await get_or_create_user(session, auth_user)

    # Validate integrations_used values
    valid_integrations = {"google", "slack", "notion", "linear", "github"}
    invalid = set(body.integrations_used) - valid_integrations
    if invalid:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid integrations: {', '.join(invalid)}. Valid options: {', '.join(sorted(valid_integrations))}",
        )

    skill = Skill(
        user_id=user.id,
        name=body.name,
        description=body.description,
        prompt=body.prompt,
        integrations_used=body.integrations_used,
        is_default=False,
    )
    session.add(skill)
    await session.flush()
    await session.refresh(skill)

    return _skill_to_response(skill)


@router.patch("/{skill_id}", response_model=SkillResponse)
async def update_skill(
    skill_id: UUID,
    body: SkillUpdate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update a skill's name, prompt, or integrations."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == user.id)
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    if body.integrations_used is not None:
        valid_integrations = {"google", "slack", "notion", "linear", "github"}
        invalid = set(body.integrations_used) - valid_integrations
        if invalid:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid integrations: {', '.join(invalid)}. Valid options: {', '.join(sorted(valid_integrations))}",
            )

    if body.name is not None:
        skill.name = body.name
    if body.description is not None:
        skill.description = body.description
    if body.prompt is not None:
        skill.prompt = body.prompt
    if body.integrations_used is not None:
        skill.integrations_used = body.integrations_used
    if body.memory_content is not None:
        skill.memory_content = body.memory_content

    await session.flush()
    await session.refresh(skill)

    return _skill_to_response(skill)


@router.delete("/{skill_id}", status_code=204)
async def delete_skill(
    skill_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a skill."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == user.id)
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    await session.execute(
        delete(Skill).where(Skill.id == skill_id)
    )


@router.post("/{skill_id}/learn", response_model=SkillLearnResponse)
async def trigger_learning(
    skill_id: UUID,
    background_tasks: BackgroundTasks,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Trigger the learning process for a skill.
    Fetches messages from connected integrations and analyzes patterns via Claude.
    Runs in the background — poll GET /skills/{id} for status updates.
    """
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Skill).where(Skill.id == skill_id, Skill.user_id == user.id)
    )
    skill = result.scalar_one_or_none()
    if not skill:
        raise HTTPException(status_code=404, detail="Skill not found")

    if skill.status == "learning":
        # Auto-reset if stuck for more than 5 minutes
        from datetime import datetime, timezone, timedelta
        if skill.updated_at and datetime.now(timezone.utc) - skill.updated_at.replace(tzinfo=timezone.utc) > timedelta(minutes=5):
            skill.status = "idle"
            await session.flush()
        else:
            raise HTTPException(status_code=409, detail="Skill is already learning")

    # Mark as learning immediately so the frontend sees the state change
    skill.status = "learning"
    await session.flush()

    # Run learning in background with its own DB session
    background_tasks.add_task(_run_learning_in_background, skill.id, user.id)

    return SkillLearnResponse(
        skill_id=str(skill.id),
        status="learning",
        message="Learning started. Poll GET /skills/{id} for progress.",
    )


@router.post("/initialize-defaults", response_model=SkillListResponse)
async def initialize_default_skills(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """
    Create default Email Tone + Slack Tone skills for the user.
    Idempotent — skips skills that already exist by name.
    Called after first login or when connecting a new integration.
    """
    user = await get_or_create_user(session, auth_user)

    # Check which defaults already exist
    result = await session.execute(
        select(Skill).where(
            Skill.user_id == user.id,
            Skill.is_default == True,
        )
    )
    existing = {s.name for s in result.scalars().all()}

    created = []
    for default in DEFAULT_SKILLS:
        if default["name"] in existing:
            continue

        skill = Skill(
            user_id=user.id,
            name=default["name"],
            description=default["description"],
            prompt=default["prompt"],
            integrations_used=default["integrations_used"],
            is_default=True,
        )
        session.add(skill)
        created.append(skill)

    if created:
        await session.flush()
        for s in created:
            await session.refresh(s)

    # Return all skills (existing + newly created)
    result = await session.execute(
        select(Skill)
        .where(Skill.user_id == user.id)
        .order_by(Skill.created_at)
    )
    all_skills = result.scalars().all()

    return SkillListResponse(
        skills=[_skill_to_response(s) for s in all_skills],
        count=len(all_skills),
    )
