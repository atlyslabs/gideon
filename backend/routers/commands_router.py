"""
Commands Router — slash command manifest (read-only, used by the chat
dropdown) plus CRUD for user-defined custom commands.

Built-in commands live in code at `services.slash_commands.BUILTIN_COMMANDS`.
Users can override a built-in by creating a custom command with the same
slug — the merge happens in `slash_commands.list_commands_for_user`.
"""

from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from db.models import CustomSlashCommand
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.slash_commands import (
    BUILTIN_COMMANDS,
    SLUG_RE,
    list_commands_for_user,
)
from services.user_service import get_or_create_user

logger = structlog.get_logger()
router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SlashCommandManifestEntry(BaseModel):
    slug: str
    name: str
    description: str
    param_hint: str
    is_builtin: bool


class CustomCommandResponse(BaseModel):
    id: UUID
    slug: str
    name: str
    description: Optional[str]
    param_hint: Optional[str]
    prompt_template: str
    overrides_builtin: bool  # True if a built-in with the same slug exists
    created_at: datetime
    updated_at: datetime


class CustomCommandCreate(BaseModel):
    slug: str = Field(..., max_length=64)
    name: str = Field(..., max_length=255)
    description: Optional[str] = None
    param_hint: Optional[str] = None
    prompt_template: str


class CustomCommandUpdate(BaseModel):
    # Slug is intentionally NOT updatable — it's the lookup key chat_service
    # uses on every send. Renaming a slug would silently break user muscle
    # memory and any chat history that references the old slug.
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    param_hint: Optional[str] = None
    prompt_template: Optional[str] = None


# ---------------------------------------------------------------------------
# Manifest (read-only, used by chat dropdown)
# ---------------------------------------------------------------------------


_BUILTIN_SLUGS: set[str] = {c["slug"] for c in BUILTIN_COMMANDS}


@router.get("/manifest", response_model=list[SlashCommandManifestEntry])
async def get_commands_manifest(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Merged slash command registry for this user: built-ins + custom
    commands they've created. The full `prompt_template` is intentionally
    omitted from this response — the FE only needs slug/name/description/
    param_hint to render the dropdown. Expansion happens server-side."""
    user = await get_or_create_user(session, auth_user)
    commands = await list_commands_for_user(user.id, session)
    return [
        SlashCommandManifestEntry(
            slug=c["slug"],
            name=c["name"],
            description=c["description"],
            param_hint=c["param_hint"],
            is_builtin=c["is_builtin"],
        )
        for c in commands
    ]


# ---------------------------------------------------------------------------
# Custom command CRUD (user-defined only — built-ins are not editable)
# ---------------------------------------------------------------------------


def _to_response(row: CustomSlashCommand) -> CustomCommandResponse:
    return CustomCommandResponse(
        id=row.id,
        slug=row.slug,
        name=row.name,
        description=row.description,
        param_hint=row.param_hint,
        prompt_template=row.prompt_template,
        overrides_builtin=row.slug in _BUILTIN_SLUGS,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("", response_model=list[CustomCommandResponse])
async def list_custom_commands(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List the user's custom commands only. Built-ins are not returned here
    (they're not editable). Use /manifest for the full list shown in chat.

    Returns [] if the `custom_slash_commands` table doesn't exist yet
    (migration pending) instead of 500ing the page."""
    user = await get_or_create_user(session, auth_user)
    try:
        result = await session.execute(
            select(CustomSlashCommand)
            .where(CustomSlashCommand.user_id == user.id)
            .order_by(CustomSlashCommand.slug)
        )
        rows = list(result.scalars().all())
    except (ProgrammingError, OperationalError) as e:
        await session.rollback()
        logger.warning(
            "list_custom_commands: table missing, returning empty list",
            error=str(e)[:200],
        )
        return []
    return [_to_response(r) for r in rows]


@router.post("", response_model=CustomCommandResponse, status_code=201)
async def create_custom_command(
    body: CustomCommandCreate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user = await get_or_create_user(session, auth_user)

    slug = body.slug.strip().lower()
    if not SLUG_RE.match(slug):
        raise HTTPException(
            status_code=422,
            detail="Slug must start with a letter and contain only lowercase letters, digits, and hyphens (max 64 chars).",
        )
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Name is required.")
    template = (body.prompt_template or "").strip()
    if not template:
        raise HTTPException(status_code=422, detail="Prompt template is required.")

    row = CustomSlashCommand(
        user_id=user.id,
        slug=slug,
        name=name,
        description=(body.description or "").strip() or None,
        param_hint=(body.param_hint or "").strip() or None,
        prompt_template=template,
    )
    session.add(row)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"You already have a custom command with slug '{slug}'.",
        )
    await session.refresh(row)
    return _to_response(row)


@router.patch("/{command_id}", response_model=CustomCommandResponse)
async def update_custom_command(
    command_id: UUID,
    body: CustomCommandUpdate,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(CustomSlashCommand).where(
            CustomSlashCommand.id == command_id,
            CustomSlashCommand.user_id == user.id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Custom command not found.")

    if body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(status_code=422, detail="Name cannot be empty.")
        row.name = new_name
    if body.description is not None:
        row.description = body.description.strip() or None
    if body.param_hint is not None:
        row.param_hint = body.param_hint.strip() or None
    if body.prompt_template is not None:
        new_template = body.prompt_template.strip()
        if not new_template:
            raise HTTPException(status_code=422, detail="Prompt template cannot be empty.")
        row.prompt_template = new_template
    row.updated_at = datetime.now(timezone.utc)
    await session.flush()
    await session.refresh(row)
    return _to_response(row)


@router.delete("/{command_id}", status_code=204)
async def delete_custom_command(
    command_id: UUID,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(CustomSlashCommand).where(
            CustomSlashCommand.id == command_id,
            CustomSlashCommand.user_id == user.id,
        )
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="Custom command not found.")
    await session.delete(row)
    await session.flush()
