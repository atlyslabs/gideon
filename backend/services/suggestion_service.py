"""
Suggestion Service — fetches recent emails and Slack messages, then uses
Claude to identify actionable items the user should follow up on.
"""

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import session_manager
from db.models import ActivityLog, Todo, TodoSuggestion
from services.llm import get_spec, llm
from services.usage_service import log_usage

logger = structlog.get_logger()

INBOX_FETCH_LIMIT = 20
FALLBACK_LOOKBACK_HOURS = 3  # fallback when no last_scheduler_run


# ---------------------------------------------------------------------------
# Data fetchers (read from activity_log — no external API calls)
# ---------------------------------------------------------------------------

async def _fetch_inbox_emails(
    session: AsyncSession, user_id: UUID, since: datetime, limit: int = INBOX_FETCH_LIMIT,
) -> list[dict]:
    """Read recent received emails from activity_log (already ingested by activity_ingestion_service).
    Avoids hitting Gmail API a second time.
    """
    result = await session.execute(
        select(ActivityLog).where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "gmail",
            ActivityLog.event_type == "email_received",
            ActivityLog.created_at >= since,
        ).order_by(ActivityLog.created_at.desc()).limit(limit)
    )
    activities = result.scalars().all()

    emails = []
    for act in activities:
        meta = act.extra_data or {}
        emails.append({
            "from": meta.get("from", ""),
            "subject": act.title or "",
            "date": str(act.created_at) if act.created_at else "",
            "snippet": act.summary or "",
            "body": act.summary or "",  # snippet is sufficient for action item detection
            "is_unread": "UNREAD" in (meta.get("labels") or []),
            "reference_id": act.reference_id or "",
        })

    logger.info("Fetched inbox emails for suggestions from activity_log", count=len(emails), user_id=str(user_id))
    return emails


async def _fetch_slack_messages(
    session: AsyncSession, user_id: UUID, since: datetime,
) -> list[dict]:
    """Read recent Slack activities from the DB (already ingested by activity_ingestion_service).

    This avoids hitting the Slack API a second time (ingestion already ran),
    eliminating the rate-limit contention that occurred when both ingestion
    and suggestion generation used the same user token in quick succession.
    """
    result = await session.execute(
        select(ActivityLog).where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "slack",
            ActivityLog.created_at >= since,
        ).order_by(ActivityLog.created_at.desc()).limit(200)
    )
    activities = result.scalars().all()

    messages = []
    for act in activities:
        metadata = act.extra_data or {}
        ref = act.reference_id or ""
        messages.append({
            "channel": metadata.get("channel", "unknown"),
            "text": (act.summary or act.title or "")[:2000],
            "ts": ref.split(":")[-1] if ":" in ref else "",
            "user": metadata.get("sender", ""),
            "is_from_me": act.event_type == "message_sent",
            "reference_id": ref,
        })

    logger.info("Fetched Slack messages for suggestions from DB", count=len(messages), user_id=str(user_id))
    return messages


# ---------------------------------------------------------------------------
# Claude analysis
# ---------------------------------------------------------------------------

async def _generate_suggestions_with_claude(
    emails: list[dict],
    slack_messages: list[dict],
) -> tuple[list[dict], object]:
    """Send collected messages to Claude to identify actionable items.
    Returns (suggestions, usage) where usage is the Anthropic Usage object."""
    content_parts = []

    if emails:
        email_text = "\n\n---\n\n".join(
            f"**From:** {e['from']}\n**Subject:** {e['subject']}\n**Date:** {e['date']}\n"
            f"**Unread:** {e['is_unread']}\n**reference_id:** {e.get('reference_id', '')}\n\n{e['body']}"
            for e in emails
        )
        content_parts.append(f"### Recent Inbox Emails ({len(emails)} messages)\n\n{email_text}")

    if slack_messages:
        slack_text = "\n\n".join(
            f"- **#{m['channel']}** (from {'me' if m['is_from_me'] else 'other'}) [ref:{m.get('reference_id', '')}]: {m['text']}"
            for m in slack_messages
        )
        content_parts.append(f"### Recent Slack Messages ({len(slack_messages)} messages)\n\n{slack_text}")

    if not content_parts:
        return [], None

    messages_text = "\n\n".join(content_parts)

    spec = get_spec("suggestions")

    system = (
        "You are a productivity assistant. The user will provide you with their recent emails and Slack messages. "
        "Identify ONLY items that require the user's direct action. Be very selective — quality over quantity.\n\n"
        "INCLUDE only if:\n"
        "- Someone directly asked the user a question or made a request TO THEM\n"
        "- An email is addressed to the user (not CC'd) and needs a reply\n"
        "- A clear deadline or action item was assigned to the user by name\n"
        "- A direct message or @mention that needs a response\n\n"
        "EXCLUDE:\n"
        "- General channel chatter, announcements, FYIs, newsletters\n"
        "- Emails where the user is CC'd or in a group thread with no direct ask\n"
        "- Automated notifications (bot messages, CI/CD, alerts)\n"
        "- Messages the user already replied to\n"
        "- Vague or low-priority items\n\n"
        "Return 0-3 items. It is perfectly fine to return an empty array if nothing needs action.\n"
        "For each item return: text (short actionable title), detail (1-2 sentence context), "
        "source_integration ('gmail' or 'slack'), reference_id (the reference_id from the source message). "
        "Return ONLY a JSON array, no markdown fences, no extra text. Example:\n"
        '[{"text": "Reply to budget approval request", "detail": "CFO sent a budget review email 2 days ago that is still unread.", "source_integration": "gmail", "reference_id": "18f3a2b1c4d5e6f7"}]'
    )

    response = await llm.complete(
        spec,
        messages=[{"role": "user", "content": messages_text}],
        system=system,
        timeout=150.0,  # was 60s — large input (up to 200 Slack msgs) + Kimi reasoning
    )

    # Prefer provider-native usage (preserves advisor iterations on Anthropic);
    # fall back to neutral Usage for OpenAI/Kimi.
    usage = getattr(response.raw, "usage", None) or response.usage
    raw = response.text.strip()

    # Strip markdown fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()

    try:
        suggestions = json.loads(raw)
        if not isinstance(suggestions, list):
            logger.error("Claude returned non-list for suggestions", raw=raw[:200])
            return [], usage
        return suggestions, usage
    except json.JSONDecodeError:
        logger.error("Failed to parse Claude suggestions JSON", raw=raw[:200])
        return [], usage


# ---------------------------------------------------------------------------
# Main orchestrator (called as BackgroundTask)
# ---------------------------------------------------------------------------

MAX_ACTIVE_SUGGESTIONS = 10


def _is_duplicate(new_text: str, existing_texts: list[str], threshold: float = 0.6) -> bool:
    """Check if new_text is similar to any existing text (simple word overlap)."""
    new_words = set(new_text.lower().split())
    for existing in existing_texts:
        existing_words = set(existing.lower().split())
        if not new_words or not existing_words:
            continue
        overlap = len(new_words & existing_words) / max(len(new_words), len(existing_words))
        if overlap >= threshold:
            return True
    return False


async def generate_suggestions(user_id: UUID, since: datetime | None = None) -> None:
    """
    Full suggestion generation flow:
    1. Check how many active suggestions exist (max 5)
    2. Fetch inbox emails + Slack messages
    3. Send to Claude for analysis
    4. Dedup against existing todos and suggestions
    5. Create new TodoSuggestion records (up to max 5 total active)

    Manages its own DB sessions so no connection is held across the LLM call
    (see CONTRIBUTING.md, database conventions): a short read session (committed/closed before
    `llm.complete`, which can run up to 150s), then a short write session for
    the new suggestions + fingerprint.
    """
    try:
        # Use provided since or fall back to default lookback
        if since is None:
            since = datetime.now(timezone.utc) - timedelta(hours=FALLBACK_LOOKBACK_HOURS)

        # --- Phase 1: reads (+ stale-suggestion sweep) in a short session,
        # committed and closed before the LLM call ---
        async with session_manager.get_session() as session:
            # Auto-dismiss stale suggestions (older than 24h)
            stale_cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            stale_result = await session.execute(
                update(TodoSuggestion)
                .where(
                    TodoSuggestion.user_id == user_id,
                    TodoSuggestion.dismissed == False,
                    TodoSuggestion.added_as_todo == False,
                    TodoSuggestion.source_integration != "_meta",
                    TodoSuggestion.created_at < stale_cutoff,
                )
                .values(dismissed=True)
            )
            if stale_result.rowcount:
                logger.info("Auto-dismissed stale suggestions", count=stale_result.rowcount, user_id=str(user_id))

            # Count current active suggestions
            active_count_result = await session.execute(
                select(func.count(TodoSuggestion.id)).where(
                    TodoSuggestion.user_id == user_id,
                    TodoSuggestion.dismissed == False,
                    TodoSuggestion.added_as_todo == False,
                )
            )
            active_count = active_count_result.scalar() or 0

            if active_count >= MAX_ACTIVE_SUGGESTIONS:
                logger.info("Max suggestions reached, skipping generation", user_id=str(user_id), active=active_count)
                return

            slots_available = MAX_ACTIVE_SUGGESTIONS - active_count

            # Sequential on purpose: an AsyncSession must not be used
            # concurrently (the old asyncio.gather shared this session across
            # two coroutines). Both are cheap indexed selects.
            emails = await _fetch_inbox_emails(session, user_id, since=since)
            slack_messages = await _fetch_slack_messages(session, user_id, since=since)

            last_suggestion = await session.execute(
                select(TodoSuggestion.detail).where(
                    TodoSuggestion.user_id == user_id,
                    TodoSuggestion.source_integration == "_meta",
                )
            )
            last_meta = last_suggestion.scalar_one_or_none()

        if not emails and not slack_messages:
            logger.info("No messages to generate suggestions from", user_id=str(user_id))
            return

        # Skip Claude call if message count matches last run (nothing new arrived)
        msg_fingerprint = f"e{len(emails)}s{len(slack_messages)}"
        if last_meta == msg_fingerprint:
            logger.info("No new messages since last run, skipping Claude call", user_id=str(user_id), fingerprint=msg_fingerprint)
            return

        # --- Phase 2: LLM analysis — no DB session held ---
        suggestions, usage = await _generate_suggestions_with_claude(emails, slack_messages)

        # Log usage (fire-and-forget)
        if usage:
            log_usage(user_id, get_spec("suggestions").model, "suggestions", usage)

        if not suggestions:
            logger.info("Claude returned no suggestions", user_id=str(user_id))
            return

        # --- Phase 3: dedup reads + writes in a fresh short session ---
        async with session_manager.get_session() as session:
            # Gather existing texts for dedup (last 30 days of suggestions + active todos)
            dedup_cutoff = datetime.now(timezone.utc) - timedelta(days=30)
            existing_suggestions = await session.execute(
                select(TodoSuggestion.content).where(
                    TodoSuggestion.user_id == user_id,
                    TodoSuggestion.created_at >= dedup_cutoff,
                )
            )
            existing_todos = await session.execute(
                select(Todo.content).where(
                    Todo.user_id == user_id,
                    Todo.completed == False,
                )
            )
            existing_texts = [r[0] for r in existing_suggestions.all()] + [r[0] for r in existing_todos.all()]

            # Filter and limit
            created = 0
            for s in suggestions:
                if created >= slots_available:
                    break
                text = s.get("text", "").strip()
                if not text:
                    continue
                if _is_duplicate(text, existing_texts):
                    logger.debug("Skipping duplicate suggestion", text=text)
                    continue

                # Build detail with reference_id for direct tool access when assigned to Gideon
                detail = s.get("detail", "")
                ref_id = s.get("reference_id", "")
                source = s.get("source_integration", "")
                if ref_id and source == "gmail":
                    detail += f"\n[Use read_email with email_id={ref_id}]"
                elif ref_id and source == "slack":
                    parts = ref_id.split(":")
                    if len(parts) == 2:
                        detail += f"\n[Use read_slack_thread with channel={parts[0]} and thread_ts={parts[1]}]"

                suggestion = TodoSuggestion(
                    user_id=user_id,
                    content=text,
                    detail=detail,
                    source_integration=source,
                )
                session.add(suggestion)
                existing_texts.append(text)  # prevent intra-batch duplicates
                created += 1

            # Update fingerprint so next run skips if nothing changed
            if last_meta is not None:
                await session.execute(
                    update(TodoSuggestion).where(
                        TodoSuggestion.user_id == user_id,
                        TodoSuggestion.source_integration == "_meta",
                    ).values(detail=msg_fingerprint)
                )
            else:
                session.add(TodoSuggestion(
                    user_id=user_id,
                    content="_suggestion_meta",
                    detail=msg_fingerprint,
                    source_integration="_meta",
                    dismissed=True,
                ))

            await session.flush()
        logger.info("Generated todo suggestions", count=created, user_id=str(user_id))

    except Exception as e:
        logger.error("Suggestion generation failed", user_id=str(user_id), error=str(e))
        raise
