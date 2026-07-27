"""
conftest.py — shared fixtures for Gideon integration tests.

These are LIVE integration tests that hit real APIs using real user tokens
stored in the Gideon database. They require:
  - A running Postgres with Gideon schema
  - A valid ENCRYPTION_KEY env var
  - Connected integrations for the test user
"""

import os
import sys
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

# Ensure the gideon package root is importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.database import session_manager
from db.models import Integration
from services.encryption_service import decrypt
from tools.tool_registry import get_tool_by_name


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The user these live tests run as. Set TEST_USER_ID to a user in your own
# database that has the relevant integrations connected.
TEST_USER_ID = UUID(os.environ.get("TEST_USER_ID", "00000000-0000-0000-0000-000000000001"))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def user_id() -> UUID:
    return TEST_USER_ID


@pytest_asyncio.fixture
async def db_session():
    """Yield a real async DB session from the Gideon session manager."""
    async with session_manager.get_session() as session:
        yield session


async def _resolve_token(session: AsyncSession, user_id: UUID, provider: str) -> str | None:
    """Look up and decrypt the user's OAuth token for a given provider."""
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
            Integration.is_active == True,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        return None

    # For Slack prefer user token, fall back to bot token
    if provider == "slack":
        raw = integration.access_token or integration.bot_token
    else:
        raw = integration.access_token

    if not raw:
        return None

    # Check if Google token needs refresh
    if provider == "google" and integration.token_expiry:
        from datetime import datetime, timezone
        if datetime.now(timezone.utc) >= integration.token_expiry:
            if integration.refresh_token:
                from services.oauth.google_oauth import GoogleOAuthProvider
                from services.encryption_service import encrypt
                new_tokens = await GoogleOAuthProvider().refresh_access_token(
                    decrypt(integration.refresh_token)
                )
                integration.access_token = encrypt(new_tokens.access_token)
                if new_tokens.refresh_token:
                    integration.refresh_token = encrypt(new_tokens.refresh_token)
                integration.token_expiry = new_tokens.token_expiry
                await session.flush()
                return new_tokens.access_token
            else:
                return None

    return decrypt(raw)


@pytest_asyncio.fixture
async def google_token(db_session, user_id) -> str | None:
    return await _resolve_token(db_session, user_id, "google")


@pytest_asyncio.fixture
async def slack_token(db_session, user_id) -> str | None:
    return await _resolve_token(db_session, user_id, "slack")


@pytest_asyncio.fixture
async def linear_token(db_session, user_id) -> str | None:
    return await _resolve_token(db_session, user_id, "linear")


@pytest_asyncio.fixture
async def github_token(db_session, user_id) -> str | None:
    return await _resolve_token(db_session, user_id, "github")


@pytest_asyncio.fixture
async def notion_token(db_session, user_id) -> str | None:
    return await _resolve_token(db_session, user_id, "notion")


# ---------------------------------------------------------------------------
# Helper to execute any tool by name
# ---------------------------------------------------------------------------


async def run_tool(tool_name: str, params: dict, token: str) -> dict:
    """Execute a tool directly with the given token. For external (OAuth) tools."""
    tool = get_tool_by_name(tool_name)
    assert tool is not None, f"Tool {tool_name!r} not found in registry"
    return await tool.execute(params, token)


async def run_internal_tool(
    tool_name: str, params: dict, user_id: UUID, session: AsyncSession, **kwargs
) -> dict:
    """Execute an internal tool (no OAuth token, uses DB session)."""
    tool = get_tool_by_name(tool_name)
    assert tool is not None, f"Tool {tool_name!r} not found in registry"
    from tools.internal_tools import InternalTool
    assert isinstance(tool, InternalTool), f"{tool_name} is not an InternalTool"
    return await tool.execute_internal(params, user_id, session, **kwargs)
