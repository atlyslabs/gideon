"""
Events Service — optional product analytics for user activity.

All events are fire-and-forget (never block the request).
No PII is sent — only anonymized user IDs, event names, and non-sensitive properties.

Analytics are OFF by default. Gideon never phones home unless you configure a
backend explicitly via `ANALYTICS_BACKEND`:

    none      (default) — events are dropped; only a debug log line is emitted.
    posthog             — POST to PostHog's capture API. Requires POSTHOG_API_KEY
                          (and optionally POSTHOG_HOST for self-hosted instances).
    webhook             — POST the raw event JSON to ANALYTICS_WEBHOOK_URL, so you
                          can forward it into any pipeline you already run.

To add your own sink, implement a branch in `_send` — every `track_*` helper in
this module routes through it, so nothing else needs to change.
"""

import asyncio
import os
from typing import Any

import httpx
import structlog

from config import IS_PRODUCTION

logger = structlog.get_logger()

ANALYTICS_BACKEND = os.environ.get("ANALYTICS_BACKEND", "none").strip().lower()
ANALYTICS_WEBHOOK_URL = os.environ.get("ANALYTICS_WEBHOOK_URL", "")
POSTHOG_API_KEY = os.environ.get("POSTHOG_API_KEY", "")
POSTHOG_HOST = os.environ.get("POSTHOG_HOST", "https://us.i.posthog.com").rstrip("/")

_EVENT_TIMEOUT_SEC = 3.0


async def _send(event_name: str, user_id: str, properties: dict[str, Any] | None = None, raw_name: bool = False):
    """Send a single event to the configured analytics backend. Never raises —
    failures are logged and swallowed, since analytics must never break a request.

    raw_name=True skips the 'gideon_' prefix (used when publishing into an event
    namespace shared with another service)."""
    name = event_name if raw_name else f"gideon_{event_name}"

    if ANALYTICS_BACKEND in ("", "none", "off", "disabled"):
        logger.debug("Event (analytics disabled)", event_name=name)
        return

    try:
        if ANALYTICS_BACKEND == "posthog":
            if not POSTHOG_API_KEY:
                return
            payload = {
                "api_key": POSTHOG_API_KEY,
                "event": name,
                "distinct_id": user_id,
                "properties": properties or {},
            }
            url = f"{POSTHOG_HOST}/capture/"
        elif ANALYTICS_BACKEND == "webhook":
            if not ANALYTICS_WEBHOOK_URL:
                return
            payload = {
                "event": name,
                "user_id": user_id,
                "properties": properties or {},
            }
            url = ANALYTICS_WEBHOOK_URL
        else:
            logger.warning("Unknown ANALYTICS_BACKEND; event dropped", backend=ANALYTICS_BACKEND)
            return

        async with httpx.AsyncClient(timeout=_EVENT_TIMEOUT_SEC) as client:
            await client.post(url, json=payload)
    except Exception as e:
        logger.warning("Analytics event failed", event_name=name, error=str(e))


# Module-level set holds strong refs to fire-and-forget tasks so the event loop
# doesn't GC them mid-send (ASYNC300).
_background_tasks: set[asyncio.Task] = set()


def track(event_name: str, user_id: str, properties: dict[str, Any] | None = None, raw_name: bool = False):
    """Fire-and-forget event tracking. Safe to call from anywhere."""
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(_send(event_name, user_id, properties, raw_name=raw_name))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)
    except RuntimeError:
        pass  # No event loop — skip silently


# ─── Convenience helpers ────────────────────────────────────────────────────

def track_chat_sent(
    user_id: str,
    conversation_id: str | None = None,
    is_new: bool = False,
    has_mentions: bool = False,
    has_files: bool = False,
):
    track("chat_sent", user_id, {
        "conversation_id": conversation_id,
        "is_new_conversation": is_new,
        "has_mentions": has_mentions,
        "has_files": has_files,
    })


def track_tool_used(user_id: str, tool_name: str = "", provider: str = "", success: bool = True, latency_ms: int = 0):
    track("tool_used", user_id, {
        "tool": tool_name,
        "provider": provider,
        "success": success,
        "latency_ms": latency_ms,
    })


def track_integration_connected(user_id: str, provider: str = ""):
    track("integration_connected", user_id, {"provider": provider})


def track_integration_disconnected(user_id: str, provider: str = ""):
    track("integration_disconnected", user_id, {"provider": provider})


def track_onboarding_completed(user_id: str):
    track("onboarding_completed", user_id)


def track_todo_created(user_id: str, source: str = "manual"):
    track("todo_created", user_id, {"source": source})


def track_todo_completed(user_id: str, source: str = "manual"):
    track("todo_completed", user_id, {"source": source})


def track_suggestion_accepted(user_id: str):
    track("suggestion_accepted", user_id)


def track_suggestion_dismissed(user_id: str):
    track("suggestion_dismissed", user_id)


def track_memory_created(user_id: str, source: str = "auto"):
    track("memory_created", user_id, {"source": source})


def track_memory_deleted(user_id: str):
    track("memory_deleted", user_id)


def track_workflow_created(user_id: str):
    track("workflow_created", user_id)


def track_workflow_triggered(user_id: str, workflow_id: str, trigger_type: str = "manual"):
    track("workflow_triggered", user_id, {"workflow_id": workflow_id, "trigger_type": trigger_type})


def track_email_draft_sent(user_id: str):
    track("email_draft_sent", user_id)


def track_email_draft_skipped(user_id: str):
    track("email_draft_skipped", user_id)


def track_meeting_prep_viewed(user_id: str, event_id: str = ""):
    track("meeting_prep_viewed", user_id, {"event_id": event_id})


def track_trigger_received(user_id: str, source: str = ""):
    track("trigger_received", user_id, {"source": source})


def track_todo_delegated(user_id: str, delegatee_count: int = 0):
    track("todo_delegated", user_id, {"delegatee_count": delegatee_count})


def track_conversation_shared(user_id: str):
    track("conversation_shared", user_id)


def track_file_uploaded(user_id: str, mime_type: str = "", size_bytes: int = 0):
    track("file_uploaded", user_id, {"mime_type": mime_type, "size_bytes": size_bytes})


def track_error(
    user_id: str,
    error_type: str,
    *,
    provider: str = "",
    tool_name: str = "",
    user_email: str = "",
    detail: str = "",
):
    """Track a user-facing error. No chat content — only error classification + context for triage."""
    props: dict[str, Any] = {"error_type": error_type}
    if provider:
        props["provider"] = provider
    if tool_name:
        props["tool_name"] = tool_name
    if user_email:
        props["user_email"] = user_email
    if detail:
        props["detail"] = detail[:500]
    track("error", user_id, props)


def track_llm_provider_fallback(
    user_id: str,
    *,
    feature: str,            # "chat" | "workflow"
    from_provider: str,      # provider that failed, e.g. "kimi" | "openai"
    to_provider: str = "anthropic",
    reason: str = "",        # "overload" | "context" | "timeout" | "server_error" | "other"
    round_num: int = 0,
):
    """Track a reactive LLM provider reroute (primary failed → fell back to a
    backup). One row per reroute, so counting `gideon_llm_provider_fallback`
    answers 'how often is <from_provider> failing' + 'how often are we routing'.
    A reroute only fires after the primary's retries are exhausted, so this is
    the *meaningful* failure signal, not transient blips (those stay in Loki as
    `llm transient error — retrying`). NOT emitted via track_error — a successful
    reroute is a recovery, not a user-facing error, and shouldn't inflate error
    dashboards."""
    track("llm_provider_fallback", user_id, {
        "feature": feature,
        "from_provider": from_provider,
        "to_provider": to_provider,
        "reason": reason,
        "round": round_num,
    })


# ─── Daily usage batch ────────────────────────────────────────────────────

# Per-model rates (USD per 1M tokens). Keys are the canonical model strings
# stored in usage_log.model. Cache-read is 10% of input; cache-create is 1.25x.
# Keep this aligned with services/gideon-ui/src/app/components/UsagePage.tsx.
_MODEL_RATES = {
    "claude-opus-4-6":           {"input": 15.0, "output": 75.0},
    "claude-opus-4-7":           {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-6":         {"input": 3.0,  "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 1.0,  "output": 5.0},
}
# Fallback when an unknown model id slips in — Sonnet is the executor default.
_DEFAULT_RATE = {"input": 3.0, "output": 15.0}
_COST_PER_WEB_SEARCH = 10.0 / 1_000  # $10 per 1K searches


def _rate_for(model: str) -> dict:
    return _MODEL_RATES.get(model or "", _DEFAULT_RATE)


def _row_cost(model: str, input_t: int, output_t: int, cache_r: int, cache_c: int) -> float:
    rate = _rate_for(model)
    input_per = rate["input"] / 1_000_000
    output_per = rate["output"] / 1_000_000
    return (
        input_t * input_per
        + output_t * output_per
        + cache_r * (input_per * 0.1)    # cache reads are 10% of input
        + cache_c * (input_per * 1.25)   # cache creation is 1.25x input
    )


async def send_daily_usage_batch() -> None:
    """Query usage_log for yesterday, aggregate per user, emit a batch of events.

    Called once daily by the scheduler. Each user gets one
    ``gideon_daily_usage`` event with token counts, cost estimate,
    conversation count, and active integration count.
    """
    if not IS_PRODUCTION:
        logger.debug("Daily usage batch skipped (not production)")
        return

    from datetime import datetime, timedelta, timezone
    from sqlalchemy import func, select, cast, Date
    from db.database import AsyncPostgresSessionGideon
    from db.models import UsageLog, Conversation, Integration, Message, User

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date()
    sm = AsyncPostgresSessionGideon()

    try:
        async with sm.get_session() as session:
            # --- Token usage aggregates per (user, model) so cost is per-model rate ---
            per_model_rows = await session.execute(
                select(
                    UsageLog.user_id,
                    UsageLog.model,
                    func.sum(UsageLog.input_tokens).label("input_tokens"),
                    func.sum(UsageLog.output_tokens).label("output_tokens"),
                    func.sum(UsageLog.cache_read_tokens).label("cache_read"),
                    func.sum(UsageLog.cache_creation_tokens).label("cache_create"),
                    func.sum(UsageLog.web_search_requests).label("web_searches"),
                    func.count(UsageLog.id).label("api_calls"),
                ).where(
                    cast(UsageLog.created_at, Date) == yesterday,
                ).group_by(UsageLog.user_id, UsageLog.model)
            )

            # Roll up to per-user totals and accumulate cost using per-model rates.
            usage_by_user: dict = {}
            for row in per_model_rows.all():
                uid = row.user_id
                input_t = row.input_tokens or 0
                output_t = row.output_tokens or 0
                cache_r = row.cache_read or 0
                cache_c = row.cache_create or 0
                web_s = row.web_searches or 0
                api_calls = row.api_calls or 0
                cost = _row_cost(row.model or "", input_t, output_t, cache_r, cache_c)

                agg = usage_by_user.setdefault(uid, {
                    "input_tokens": 0, "output_tokens": 0, "cache_read": 0,
                    "cache_create": 0, "web_searches": 0, "api_calls": 0, "cost": 0.0,
                })
                agg["input_tokens"] += input_t
                agg["output_tokens"] += output_t
                agg["cache_read"] += cache_r
                agg["cache_create"] += cache_c
                agg["web_searches"] += web_s
                agg["api_calls"] += api_calls
                agg["cost"] += cost

            if not usage_by_user:
                logger.info("Daily usage batch: no usage yesterday", date=str(yesterday))
                return

            # --- Conversations created yesterday per user ---
            conv_rows = await session.execute(
                select(
                    Conversation.user_id,
                    func.count(Conversation.id).label("conversations"),
                ).where(
                    cast(Conversation.created_at, Date) == yesterday,
                ).group_by(Conversation.user_id)
            )
            convs_by_user = {row.user_id: row.conversations for row in conv_rows.all()}

            # --- Messages sent yesterday per user (join via conversation) ---
            msg_rows = await session.execute(
                select(
                    Conversation.user_id,
                    func.count(Message.id).label("messages"),
                ).join(
                    Conversation, Message.conversation_id == Conversation.id,
                ).where(
                    cast(Message.created_at, Date) == yesterday,
                    Message.role == "user",
                ).group_by(Conversation.user_id)
            )
            msgs_by_user = {row.user_id: row.messages for row in msg_rows.all()}

            # --- Active integrations per user (current, not historical) ---
            integ_rows = await session.execute(
                select(
                    Integration.user_id,
                    func.count(Integration.id).label("integrations"),
                ).where(
                    Integration.is_active == True,
                    Integration.user_id.in_(list(usage_by_user.keys())),
                ).group_by(Integration.user_id)
            )
            integs_by_user = {row.user_id: row.integrations for row in integ_rows.all()}

            # --- User emails for analytics identification ---
            user_rows = await session.execute(
                select(User.id, User.email).where(
                    User.id.in_(list(usage_by_user.keys()))
                )
            )
            email_by_user = {row.id: row.email for row in user_rows.all()}

        # --- Send one event per user ---
        sent = 0
        for uid, agg in usage_by_user.items():
            cost = agg["cost"] + agg["web_searches"] * _COST_PER_WEB_SEARCH
            props = {
                "date": str(yesterday),
                "input_tokens": agg["input_tokens"],
                "output_tokens": agg["output_tokens"],
                "cache_read_tokens": agg["cache_read"],
                "cache_creation_tokens": agg["cache_create"],
                "web_search_requests": agg["web_searches"],
                "api_calls": agg["api_calls"],
                "estimated_cost_usd": round(cost, 4),
                "conversations": convs_by_user.get(uid, 0),
                "messages_sent": msgs_by_user.get(uid, 0),
                "active_integrations": integs_by_user.get(uid, 0),
                "user_email": email_by_user.get(uid, ""),
            }
            await _send("daily_usage", str(uid), props)
            sent += 1

        logger.info("Daily usage batch sent", date=str(yesterday), users=sent)

    except Exception as e:
        logger.error("Daily usage batch failed", error=str(e))
