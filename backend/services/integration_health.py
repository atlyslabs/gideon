"""
Integration Health — helpers to track and clear integration errors.

Used by background crons (activity ingestion, email auto-drafts, meeting prep)
and tool_executor to record token refresh failures / auth errors so the frontend
can surface them as notification badges.
"""

from datetime import datetime, timezone
from uuid import UUID

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import Integration

logger = structlog.get_logger()


async def record_integration_error(
    session: AsyncSession,
    integration_id: UUID,
    error_message: str,
) -> None:
    """Increment error_count and set last_error on an integration."""
    await session.execute(
        update(Integration)
        .where(Integration.id == integration_id)
        .values(
            error_count=Integration.error_count + 1,
            last_error_at=datetime.now(timezone.utc),
            last_error=error_message[:500],
        )
    )
    await session.flush()


async def clear_integration_errors(
    session: AsyncSession,
    integration_id: UUID,
) -> None:
    """Reset error tracking after a successful operation."""
    await session.execute(
        update(Integration)
        .where(Integration.id == integration_id)
        .values(
            error_count=0,
            last_error_at=None,
            last_error=None,
        )
    )
    await session.flush()
