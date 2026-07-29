"""
Share notification service — DMs Gideon-bot Slack messages to users who
have just been added to a conversation's `shared_with_emails` list.

Flow:
  1. The share-update endpoint computes (current shared - already notified)
  2. Calls `schedule_share_notifications()` as fire-and-forget
  3. The background task does ALL Slack HTTP outside any open DB session
     (CLAUDE.md rule: never hold a DB connection across external I/O)
  4. For each new email: look up Slack user by email, open a DM, post a
     Block Kit message with owner name + chat title + "Open chat" button
  5. ONLY successfully-delivered DMs get their email hash recorded in
     `share_notified_email_hashes`. Transient Slack failures (rate-limit,
     5xx, network) are NOT recorded, so the next share-update retries.
  6. The hash-merge is an atomic Postgres `UPDATE ... = ARRAY(SELECT
     DISTINCT unnest(... || $new))` so concurrent share-updates can't
     clobber each other's hashes.

Dedupe is append-only: once an email hash lands in
`share_notified_email_hashes`, removing them from `shared_with_emails`
and re-adding does NOT re-trigger a DM. The owner can shuffle the share
list freely without spamming.

PII hygiene: only hashes are persisted; logs use the first 12 chars of
the hash as a stable correlation key (never raw emails).

Bot token comes from the OWNER's Slack integration (same pattern as
delegation_service). The DM is sent through their workspace's Gideon
bot install. If the owner has no active Slack integration we skip
silently AND don't record the hash — when they connect Slack later, a
re-share will retry.
"""

import os
import asyncio
import hashlib
from typing import Optional
from uuid import UUID

import structlog
from slack_sdk.errors import SlackApiError
from services.slack_client import get_slack_client
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncPostgresSessionGideon
from db.models import Conversation, Integration, User
from services.encryption_service import decrypt

logger = structlog.get_logger()

# Strong refs for fire-and-forget tasks — asyncio only weakly references
# tasks, and an unreferenced task can be GC'd mid-execution.
_background_tasks: set[asyncio.Task] = set()

GIDEON_BASE_URL = os.environ.get("GIDEON_APP_URL", "http://localhost:3000")


def hash_email(email: str) -> str:
    """SHA256 hex of the lowercased + stripped email. Used as the storage
    key in `share_notified_email_hashes` so we don't persist plaintext
    PII. Not a security-grade secret; just a stable equality identifier."""
    normalized = (email or "").strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _redacted(email: str) -> str:
    """Log-safe identifier for a single recipient — first 12 hex chars of
    the email hash. Stable across runs, never reveals the address."""
    return hash_email(email)[:12]


async def _owner_bot_token(session: AsyncSession, owner_id: UUID) -> Optional[str]:
    """Return the decrypted bot token for the conversation owner's Slack
    install, or None if they have no active Slack integration."""
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == owner_id,
            Integration.provider == "slack",
            Integration.is_active == True,  # noqa: E712
            Integration.bot_token.isnot(None),
        ).limit(1)
    )
    integ = result.scalar_one_or_none()
    if not integ or not integ.bot_token:
        return None
    try:
        return decrypt(integ.bot_token)
    except Exception as e:
        logger.warning("share_notification: failed to decrypt bot token", err=str(e))
        return None


def _build_share_blocks(owner_name: str, chat_title: str, chat_url: str) -> list[dict]:
    """Block Kit payload for the share DM. Section text on top, primary
    action button beneath."""
    title_display = chat_title.strip() if chat_title and chat_title.strip() else "Untitled chat"
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{owner_name}* shared a Gideon chat with you:\n"
                    f"*{title_display}*"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open chat"},
                    "url": chat_url,
                    "style": "primary",
                }
            ],
        },
    ]


# Slack error codes we treat as PERMANENT for this recipient — no point
# retrying on every subsequent share-edit because the answer won't change
# until something outside our control does (user joins the workspace,
# gets re-enabled, etc.). We still record the hash for these so we don't
# loop, even though the DM didn't actually land. If the user joins the
# workspace later, the owner can re-share and we'll find them.
_PERMANENT_SLACK_ERRORS = {
    "users_not_found",
    "user_disabled",
    "user_not_visible",
}


async def _dm_recipient(
    token: str,
    recipient_email: str,
    owner_name: str,
    chat_title: str,
    chat_url: str,
) -> tuple[bool, bool]:
    """Look up the recipient by email, open a DM, post the share message.

    Returns `(delivered, permanent_failure)`:
      - `(True, False)` — DM successfully posted, record the hash
      - `(False, True)` — recipient not in workspace / disabled, record
        the hash so we don't retry every share-edit
      - `(False, False)` — transient failure (network, rate-limit,
        5xx), do NOT record, retry on next share-update
    """
    client = get_slack_client(token=token)
    redacted = _redacted(recipient_email)
    try:
        lookup = await client.users_lookupByEmail(email=recipient_email)
        slack_user_id = lookup.get("user", {}).get("id")
        if not slack_user_id:
            logger.info("share_notification: recipient missing Slack id", recipient=redacted)
            return False, True
    except SlackApiError as e:
        err = e.response.get("error", str(e)) if e.response else str(e)
        permanent = err in _PERMANENT_SLACK_ERRORS
        logger.info(
            "share_notification: users.lookupByEmail failed",
            recipient=redacted,
            err=err,
            permanent=permanent,
        )
        return False, permanent
    except Exception as e:
        # Network / unexpected — treat as transient.
        logger.warning(
            "share_notification: users.lookupByEmail raised",
            recipient=redacted,
            err=str(e),
        )
        return False, False

    try:
        opened = await client.conversations_open(users=slack_user_id)
        channel_id = opened.get("channel", {}).get("id")
        if not channel_id:
            return False, False
        await client.chat_postMessage(
            channel=channel_id,
            text=f"{owner_name} shared a Gideon chat with you: {chat_url}",
            blocks=_build_share_blocks(owner_name, chat_title, chat_url),
        )
        return True, False
    except SlackApiError as e:
        err = e.response.get("error", str(e)) if e.response else str(e)
        permanent = err in _PERMANENT_SLACK_ERRORS
        logger.warning(
            "share_notification: DM post failed",
            recipient=redacted,
            err=err,
            permanent=permanent,
        )
        return False, permanent
    except Exception as e:
        logger.warning(
            "share_notification: DM post raised",
            recipient=redacted,
            err=str(e),
        )
        return False, False


async def _merge_notified_hashes(conversation_id: UUID, hashes: list[str]) -> None:
    """Atomically append + dedupe new hashes into
    `share_notified_email_hashes`. Concurrent-task safe — two notify
    tasks for the same conversation can run in parallel and neither
    will clobber the other's writes.

    Uses a Postgres-native expression so the merge is a single statement
    inside a tight transaction. No SELECT/modify-in-Python/UPDATE race."""
    if not hashes:
        return
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        await session.execute(
            text(
                "UPDATE conversations "
                "SET share_notified_email_hashes = ARRAY("
                "  SELECT DISTINCT e FROM unnest("
                "    COALESCE(share_notified_email_hashes, '{}') || :new_hashes"
                "  ) AS e"
                ") "
                "WHERE id = :conv_id"
            ),
            {"new_hashes": hashes, "conv_id": str(conversation_id)},
        )
        await session.commit()


async def _notify_and_persist(conversation_id: UUID, new_emails: list[str]) -> None:
    """Background task body. Three phases — read-fast, network, write-fast
    — so we never hold a DB connection across Slack HTTP calls (CLAUDE.md
    Database Management rule #3: no external integration points inside an
    open DB session)."""
    if not new_emails:
        return

    # ── Phase 1: short session, read-only.
    chat_title: str
    owner_name: str
    chat_url: str
    token: Optional[str]
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        result = await session.execute(
            select(Conversation).where(Conversation.id == conversation_id)
        )
        conv = result.scalar_one_or_none()
        if not conv:
            return
        owner_result = await session.execute(
            select(User).where(User.id == conv.user_id)
        )
        owner = owner_result.scalar_one_or_none()
        if not owner:
            return
        token = await _owner_bot_token(session, owner.id)
        chat_url = f"{GIDEON_BASE_URL}/chat/{conv.id}"
        owner_name = owner.name or owner.email
        chat_title = conv.title or ""

    # Session closed. Everything below runs OUTSIDE a DB connection.

    if not token:
        # Owner has no active Slack integration — nothing to send. Don't
        # mark anyone notified; if they connect later, a re-share retries.
        logger.info(
            "share_notification: owner has no Slack integration, skipping",
            conversation_id=str(conversation_id),
        )
        return

    # ── Phase 2: Slack network calls.
    delivered_hashes: list[str] = []
    for email in new_emails:
        delivered, permanent_failure = await _dm_recipient(
            token, email, owner_name, chat_title, chat_url,
        )
        # Record hash only when (a) we successfully delivered, or
        # (b) Slack says the recipient is permanently unreachable.
        # Transient failures fall through unrecorded so the next
        # share-update gets a fresh attempt.
        if delivered or permanent_failure:
            delivered_hashes.append(hash_email(email))

    # ── Phase 3: short session, atomic merge.
    await _merge_notified_hashes(conversation_id, delivered_hashes)


def schedule_share_notifications(
    conversation_id: UUID,
    new_emails: list[str],
) -> None:
    """Fire-and-forget entry point. Call this from the share-update
    endpoint AFTER committing the new `shared_with_emails`. Keeps a
    strong reference to the task so asyncio doesn't GC it mid-flight."""
    if not new_emails:
        return
    task = asyncio.create_task(_notify_and_persist(conversation_id, new_emails))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
