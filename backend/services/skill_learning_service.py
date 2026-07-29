"""
Skill Learning Service — fetches user messages from integrations and
sends them to Claude for communication-style analysis.
"""

import asyncio
import base64
from datetime import datetime, timezone
from functools import partial
from typing import Optional
from uuid import UUID

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk.web.async_client import AsyncWebClient
from services.slack_client import get_slack_client
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from services.llm import get_spec, llm
from db.models import Integration, Skill
from services.encryption_service import decrypt, encrypt
from services.events_service import track_error
from services.oauth.google_oauth import GoogleOAuthProvider

logger = structlog.get_logger()

# How many messages to fetch per integration for analysis
GMAIL_FETCH_LIMIT = 20
SLACK_CHANNELS_LIMIT = 10
SLACK_MESSAGES_PER_CHANNEL = 30


# ---------------------------------------------------------------------------
# Integration helpers (mirrors hub_router patterns)
# ---------------------------------------------------------------------------

async def _get_integration(
    session: AsyncSession, user_id: UUID, provider: str
) -> Optional[Integration]:
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == provider,
            Integration.is_active == True,
        )
    )
    return result.scalar_one_or_none()


async def _get_google_token(
    session: AsyncSession, integration: Integration
) -> Optional[str]:
    """Decrypt Google access token, refreshing if expired."""
    try:
        if integration.token_expiry and datetime.now(timezone.utc) >= integration.token_expiry:
            if not integration.refresh_token:
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
                logger.error("Google token refresh failed during skill learning", error=str(e))
                return None
        return decrypt(integration.access_token)
    except Exception as e:
        logger.error("Failed to decrypt Google token for skill learning", error=str(e))
        return None


async def _get_slack_token(integration: Integration) -> Optional[str]:
    """Decrypt Slack user token (preferred) or bot token."""
    try:
        token = integration.access_token or integration.bot_token
        return decrypt(token)
    except Exception as e:
        logger.error("Failed to decrypt Slack token for skill learning", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Gmail batch helpers (module-level to avoid nested functions)
# ---------------------------------------------------------------------------

def _decode_b64(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")


def _find_text_plain_body(payload: dict) -> str:
    """Recursively walk MIME parts to find text/plain body data."""
    if payload.get("mimeType") == "text/plain" and payload.get("body", {}).get("data"):
        return payload["body"]["data"]
    for part in payload.get("parts", []):
        found = _find_text_plain_body(part)
        if found:
            return found
    return ""


def _handle_gmail_batch_msg(request_id, response, exception, *, emails):
    if exception or not response:
        return
    headers = {
        h["name"].lower(): h["value"]
        for h in response.get("payload", {}).get("headers", [])
    }
    body = ""
    payload = response.get("payload", {})
    # Try top-level body first, then recursive MIME walk
    if payload.get("body", {}).get("data"):
        body = _decode_b64(payload["body"]["data"])
    else:
        data = _find_text_plain_body(payload)
        if data:
            body = _decode_b64(data)
    emails.append({
        "to": headers.get("to", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "body": body[:3000],
    })


async def _score_slack_channel(
    channel: dict, *, sem: asyncio.Semaphore, client: AsyncWebClient, slack_user_id: str
) -> tuple[dict, int, list[dict]]:
    async with sem:
        try:
            history = await client.conversations_history(
                channel=channel["id"],
                limit=SLACK_MESSAGES_PER_CHANNEL,
            )
            user_msgs = []
            for m in history.get("messages", []):
                if m.get("user") == slack_user_id and m.get("text"):
                    user_msgs.append({
                        "channel": channel.get("name", "unknown"),
                        "text": m["text"][:2000],
                        "ts": m.get("ts", ""),
                        "is_thread_reply": bool(m.get("thread_ts") and m.get("thread_ts") != m.get("ts")),
                    })
            return (channel, len(user_msgs), user_msgs)
        except Exception:
            return (channel, 0, [])


# ---------------------------------------------------------------------------
# Data fetchers
# ---------------------------------------------------------------------------

def _fetch_sent_emails_sync(access_token: str, limit: int) -> list[dict]:
    """Sync helper: fetch sent emails via Gmail batch API. Runs in a thread."""
    creds = Credentials(token=access_token)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    results = service.users().messages().list(
        userId="me", maxResults=limit, q="in:sent"
    ).execute()

    msg_refs = results.get("messages", [])
    if not msg_refs:
        return []

    emails: list[dict] = []
    batch = service.new_batch_http_request()

    for ref in msg_refs:
        batch.add(
            service.users().messages().get(userId="me", id=ref["id"], format="full"),
            callback=partial(_handle_gmail_batch_msg, emails=emails),
        )
    batch.execute()
    return emails


async def _fetch_sent_emails(
    session: AsyncSession, user_id: UUID, limit: int = GMAIL_FETCH_LIMIT
) -> list[dict]:
    """Fetch the user's sent emails via Gmail API."""
    integration = await _get_integration(session, user_id, "google")
    if not integration:
        return []

    access_token = await _get_google_token(session, integration)
    if not access_token:
        return []

    try:
        emails = await asyncio.to_thread(_fetch_sent_emails_sync, access_token, limit)
        logger.info("Fetched sent emails for skill learning", count=len(emails), user_id=str(user_id))
        return emails

    except Exception as e:
        logger.error("Failed to fetch sent emails for skill learning", error=str(e))
        track_error(str(user_id), "gmail_fetch_failed", provider="google", tool_name="skill_learning", detail=str(e)[:500])
        return []


async def _fetch_slack_messages(
    session: AsyncSession, user_id: UUID
) -> list[dict]:
    """Fetch the user's own Slack messages from recent channels."""
    integration = await _get_integration(session, user_id, "slack")
    if not integration:
        return []

    token = await _get_slack_token(integration)
    if not token:
        return []

    try:
        client = get_slack_client(token=token)

        # Get the authenticated user's Slack user ID
        auth_resp = await client.auth_test()
        slack_user_id = auth_resp.get("user_id")
        if not slack_user_id:
            logger.error("Could not determine Slack user ID")
            return []

        # Use users_conversations to get only channels the user is actually in
        # Note: don't pass user= with user tokens — it's only for bot tokens
        channels_resp = await client.users_conversations(
            limit=30,
            types="public_channel,private_channel",
            exclude_archived=True,
        )
        channels = channels_resp.get("channels", [])

        logger.debug("Slack channels for skill learning (user_conversations)", count=len(channels),
                     names=[c.get("name") for c in channels[:10]])

        # Score channels by user's message count — fetch in parallel (5 concurrent)
        sem = asyncio.Semaphore(5)

        channel_scores = await asyncio.gather(*[
            _score_slack_channel(ch, sem=sem, client=client, slack_user_id=slack_user_id)
            for ch in channels
        ])

        # Sort by user's message count descending, take top N
        sorted_scores = sorted(channel_scores, key=lambda x: x[1], reverse=True)
        top_channels = sorted_scores[:SLACK_CHANNELS_LIMIT]

        logger.debug("Top Slack channels for skill learning",
                     channels=[(c.get("name"), score) for c, score, _ in top_channels])

        # Reuse already-collected messages from scoring phase (no second fetch)
        all_messages = []
        for channel, _, user_msgs in top_channels:
            all_messages.extend(user_msgs)

        logger.info("Fetched Slack messages for skill learning", count=len(all_messages), user_id=str(user_id))
        return all_messages

    except Exception as e:
        logger.error("Failed to fetch Slack messages for skill learning", error=str(e))
        track_error(str(user_id), "slack_fetch_failed", provider="slack", tool_name="skill_learning", detail=str(e)[:500])
        return []


# ---------------------------------------------------------------------------
# Analysis via Claude
# ---------------------------------------------------------------------------

async def _analyze_with_claude(
    skill_prompt: str,
    messages_text: str,
) -> tuple[str, object]:
    """Send collected messages + the skill prompt to Claude for analysis.
    Returns (analysis_text, usage)."""
    system = (
        "You are a communication style analyst. The user will provide you with a collection of their "
        "messages (emails or Slack messages). Analyze the patterns and produce a structured markdown "
        "report that can be used as context for an AI assistant to mimic their communication style.\n\n"
        "Your analysis should be detailed but concise. Use markdown headers and bullet points. "
        "Focus on actionable patterns that an AI could replicate."
    )

    user_message = (
        f"## Task\n{skill_prompt}\n\n"
        f"## Messages to Analyze\n{messages_text}"
    )

    response = await llm.complete(
        get_spec("skill_learning"),
        messages=[{"role": "user", "content": user_message}],
        system=system,
        timeout=300.0,
    )

    usage = getattr(response.raw, "usage", None) or response.usage
    return response.text, usage


# ---------------------------------------------------------------------------
# Main learning orchestrator
# ---------------------------------------------------------------------------

async def run_skill_learning(
    skill_id: UUID,
    user_id: UUID,
    session: AsyncSession,
) -> None:
    """
    Execute the full learning flow for a skill:
    1. Set status to "learning"
    2. Fetch data from integrations
    3. Analyze with Claude
    4. Store results
    """
    # Mark as learning
    await session.execute(
        update(Skill)
        .where(Skill.id == skill_id, Skill.user_id == user_id)
        .values(status="learning")
    )
    await session.flush()

    try:
        # Load the skill
        result = await session.execute(
            select(Skill).where(Skill.id == skill_id, Skill.user_id == user_id)
        )
        skill = result.scalar_one_or_none()
        if not skill:
            logger.error("Skill not found for learning", skill_id=str(skill_id))
            return

        integrations_used = skill.integrations_used or []

        # Collect messages from relevant integrations
        all_content_parts = []

        if "google" in integrations_used:
            emails = await _fetch_sent_emails(session, user_id)
            if emails:
                email_text = "\n\n---\n\n".join(
                    f"**To:** {e['to']}\n**Subject:** {e['subject']}\n**Date:** {e['date']}\n\n{e['body']}"
                    for e in emails
                )
                all_content_parts.append(f"### Sent Emails ({len(emails)} messages)\n\n{email_text}")
            else:
                all_content_parts.append("### Sent Emails\nNo sent emails found or Gmail not connected.")

        if "slack" in integrations_used:
            slack_msgs = await _fetch_slack_messages(session, user_id)
            if slack_msgs:
                slack_text = "\n\n".join(
                    f"- **#{m['channel']}** {'(thread reply)' if m['is_thread_reply'] else ''}: {m['text']}"
                    for m in slack_msgs
                )
                all_content_parts.append(f"### Slack Messages ({len(slack_msgs)} messages)\n\n{slack_text}")
            else:
                all_content_parts.append("### Slack Messages\nNo Slack messages found or Slack not connected.")

        if not all_content_parts:
            await session.execute(
                update(Skill)
                .where(Skill.id == skill_id)
                .values(
                    status="failed",
                    memory_content="No data available. Please connect the required integrations first.",
                )
            )
            return

        messages_text = "\n\n".join(all_content_parts)

        # Analyze with Claude
        analysis, usage = await _analyze_with_claude(skill.prompt, messages_text)

        # Log usage (fire-and-forget)
        if usage:
            from services.usage_service import log_usage
            log_usage(user_id, get_spec("skill_learning").model, "skill_learning", usage)

        # Store results
        now = datetime.now(timezone.utc)
        await session.execute(
            update(Skill)
            .where(Skill.id == skill_id)
            .values(
                memory_content=analysis,
                status="completed",
                last_run_at=now,
                updated_at=now,
            )
        )

        logger.info("Skill learning completed", skill_id=str(skill_id), user_id=str(user_id))

    except Exception as e:
        logger.error("Skill learning failed", skill_id=str(skill_id), error=str(e))
        await session.execute(
            update(Skill)
            .where(Skill.id == skill_id)
            .values(status="failed")
        )
        raise
