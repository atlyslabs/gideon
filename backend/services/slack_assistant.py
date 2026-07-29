"""Slack AI-assistant (Agents & AI Apps) helpers.

Thin async wrappers over the ``assistant.threads.*`` APIs that drive the native
"thinking" status shimmer, the thread title, and suggested prompts inside
Gideon's assistant pane.

Every call is best-effort and no-ops on failure, so the bot keeps working even
before the assistant feature is enabled in the Slack app config: until the
"Agent & AI Apps" toggle is on AND the ``assistant:write`` scope is granted,
these methods return ``not_allowed_token_type`` / ``method_not_supported`` and
we simply swallow it. That lets the backend ship ahead of the Slack-side config.

Requires bot scope ``assistant:write`` and the app configured as an AI assistant.
"""

from typing import List, Optional

import structlog

from services.slack_client import get_slack_client

logger = structlog.get_logger()

# Shown as "Gideon <status>" under the composer while a turn is in flight.
ASSISTANT_THINKING_STATUS = "is thinking…"


async def set_status(
    bot_token: str,
    channel_id: str,
    thread_ts: str,
    status: str,
    *,
    loading_messages: Optional[List[str]] = None,
) -> None:
    """Set (or, with an empty string, clear) the assistant thread status shimmer."""
    if not (bot_token and channel_id and thread_ts):
        return
    try:
        client = get_slack_client(bot_token)
        kwargs = {"channel_id": channel_id, "thread_ts": thread_ts, "status": status}
        if loading_messages:
            kwargs["loading_messages"] = loading_messages
        await client.assistant_threads_setStatus(**kwargs)
    except Exception as e:
        # Assistant not enabled yet, or a transient Slack error — never fatal.
        logger.debug(
            "assistant setStatus failed (assistant may be disabled)", error=str(e)[:120]
        )


async def clear_status(bot_token: str, channel_id: str, thread_ts: str) -> None:
    """Clear the status shimmer (an empty status string)."""
    await set_status(bot_token, channel_id, thread_ts, "")


async def set_suggested_prompts(
    bot_token: str,
    channel_id: str,
    thread_ts: str,
    prompts: List[dict],
    *,
    title: Optional[str] = None,
) -> None:
    """Show suggested starter prompts at the top of a fresh assistant thread."""
    if not (bot_token and channel_id and thread_ts and prompts):
        return
    try:
        client = get_slack_client(bot_token)
        kwargs = {"channel_id": channel_id, "thread_ts": thread_ts, "prompts": prompts}
        if title:
            kwargs["title"] = title
        await client.assistant_threads_setSuggestedPrompts(**kwargs)
    except Exception as e:
        logger.debug("assistant setSuggestedPrompts failed", error=str(e)[:120])


async def set_title(
    bot_token: str, channel_id: str, thread_ts: str, title: str
) -> None:
    """Set the assistant thread title (shown in the thread history list)."""
    if not (bot_token and channel_id and thread_ts and title):
        return
    try:
        client = get_slack_client(bot_token)
        await client.assistant_threads_setTitle(
            channel_id=channel_id, thread_ts=thread_ts, title=title
        )
    except Exception as e:
        logger.debug("assistant setTitle failed", error=str(e)[:120])
