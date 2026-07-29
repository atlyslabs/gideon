"""
User Service — get-or-create local user from identity-provider data, update profile.
"""

from typing import Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User
from services.auth_service import AuthUser

logger = structlog.get_logger()


async def get_or_create_user(session: AsyncSession, auth_user: AuthUser) -> User:
    """
    Find local user by auth_provider_id. Create if first login.
    Handles concurrent INSERT race via savepoint + retry on IntegrityError.
    """
    result = await session.execute(
        select(User).where(User.auth_provider_id == auth_user.id)
    )
    user = result.scalar_one_or_none()

    if user:
        return user

    # First login — create local record
    display_name = None
    if auth_user.first_name:
        display_name = auth_user.first_name
        if auth_user.last_name:
            display_name += f" {auth_user.last_name}"

    try:
        async with session.begin_nested():
            user = User(
                auth_provider_id=auth_user.id,
                email=auth_user.email,
                name=display_name,
            )
            session.add(user)
            await session.flush()
        await session.refresh(user)
        logger.info("New gideon user created", user_id=str(user.id))
        return user
    except IntegrityError:
        # Concurrent request already created the user -- savepoint already
        # rolled back by begin_nested() context manager; outer transaction intact
        result = await session.execute(
            select(User).where(User.auth_provider_id == auth_user.id)
        )
        user = result.scalar_one_or_none()
        if user:
            return user
        raise


async def update_user(
    session: AsyncSession,
    auth_provider_id: str,
    name: Optional[str] = None,
    persona_text: Optional[str] = None,
    timezone: Optional[str] = None,
    preferred_communication: Optional[str] = None,
) -> User:
    """Update gideon-specific user fields."""
    values = {}
    if name is not None:
        values["name"] = name
    if persona_text is not None:
        values["persona_text"] = persona_text
    if timezone is not None:
        values["timezone"] = timezone
    if preferred_communication is not None:
        values["preferred_communication"] = preferred_communication

    if values:
        await session.execute(
            update(User).where(User.auth_provider_id == auth_provider_id).values(**values)
        )

    result = await session.execute(
        select(User).where(User.auth_provider_id == auth_provider_id)
    )
    return result.scalar_one()
