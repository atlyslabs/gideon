"""
Webhooks Router -- receives external events from Slack Events API and Gmail.
No auth middleware (external webhooks verify themselves via signing secrets).
"""

import os
import asyncio
import functools
from services.slack_client import get_slack_client
import hashlib
import hmac
import json
import re
import time

import structlog
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from config import SLACK_SIGNING_SECRET
from db.database import AsyncPostgresSessionGideon
from db.models import Conversation, Integration, TriggerConversation, User
from db.redis import get_redis
from services.encryption_service import decrypt
from services.slack_mention_resolver import decode_slack_mentions

logger = structlog.get_logger()

router = APIRouter()

# Strong refs for fire-and-forget tasks — asyncio only holds weak refs, so an
# unreferenced task can be GC'd mid-execution and silently disappear. That would
# cause @Gideon mentions to appear to do nothing even though the webhook 200'd.
_background_tasks: set[asyncio.Task] = set()

# Pattern to detect explicit Gideon instructions
TRIGGER_PATTERN = re.compile(
    r"(?:@?gideon|hey gideon|hi gideon|\+gideon)[,:\s]+(.+)",
    re.IGNORECASE | re.DOTALL,
)


# ---------------------------------------------------------------------------
# Slack Events API
# ---------------------------------------------------------------------------

def _verify_slack_signature(body: bytes, timestamp: str, signature: str) -> bool:
    """Verify Slack request signature using HMAC-SHA256."""
    if not SLACK_SIGNING_SECRET:
        logger.warning("SLACK_SIGNING_SECRET not configured")
        return False
    try:
        if abs(time.time() - int(timestamp)) > 300:
            return False
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
    except (ValueError, TypeError, UnicodeDecodeError):
        return False
    computed = "v0=" + hmac.new(
        SLACK_SIGNING_SECRET.encode(), sig_basestring.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(computed, signature)


@router.post("/slack/events")
async def slack_events(request: Request):
    """Handle Slack Events API callbacks."""
    body = await request.body()
    timestamp = request.headers.get("X-Slack-Request-Timestamp", "0")
    signature = request.headers.get("X-Slack-Signature", "")

    if not _verify_slack_signature(body, timestamp, signature):
        return JSONResponse(status_code=401, content={"error": "Invalid signature"})

    payload = json.loads(body)

    # URL verification challenge (one-time setup)
    if payload.get("type") == "url_verification":
        return JSONResponse(content={"challenge": payload["challenge"]})

    if payload.get("type") != "event_callback":
        return JSONResponse(content={"ok": True})

    event = payload.get("event", {})
    event_id = payload.get("event_id", "")
    team_id = payload.get("team_id", "")
    event_type = event.get("type", "")

    # Only handle events we care about
    if event_type not in (
        "app_mention",
        "channel_created",
        "message",
        "assistant_thread_started",
    ):
        return JSONResponse(content={"ok": True})

    # For `message` events, only act on 1:1 DMs to the bot — channel chatter
    # comes through the same event type and would flood the trigger agent.
    # Also filter bot-authored messages (prevents reply-loops) and edits/
    # deletes/joins (`subtype` is set for non-user-typed messages).
    if event_type == "message":
        if event.get("channel_type") != "im":
            return JSONResponse(content={"ok": True})
        if event.get("bot_id") or event.get("subtype"):
            return JSONResponse(content={"ok": True})

    # Dedup via Redis (Slack may retry)
    try:
        redis = get_redis()
        dedup_key = f"slack_event:{event_id}"
        was_set = await redis.set(dedup_key, "1", nx=True, ex=300)
        if not was_set:
            return JSONResponse(content={"ok": True})
    except Exception as e:
        logger.warning("Redis dedup failed, proceeding anyway", error=str(e))

    # Return 200 immediately (Slack requires response within 3 seconds)
    if event_type in ("app_mention", "message"):
        # DMs reuse the mention pipeline — same auth, same trigger agent.
        task = asyncio.create_task(_process_slack_mention(team_id, event))
    elif event_type == "assistant_thread_started":
        # User opened a new Gideon assistant pane — greet with suggested prompts.
        task = asyncio.create_task(_process_assistant_thread_started(team_id, event))
    else:  # channel_created
        task = asyncio.create_task(_process_channel_created(team_id, event))
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return JSONResponse(content={"ok": True})


async def _get_slack_bot_token_for_team(team_id: str) -> str | None:
    """Return a decrypted bot token for the given Slack workspace, or None.

    Slack bots are installed per-workspace, so any active Slack integration
    in `team_id` gives us the shared bot token. Opens and closes its own
    short-lived session so callers don't hold a DB connection across
    external Slack API calls.
    """
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        result = await session.execute(
            select(Integration).where(
                Integration.provider == "slack",
                Integration.is_active == True,
            )
        )
        for integ in result.scalars().all():
            if (integ.extra_data or {}).get("team_id") == team_id and integ.bot_token:
                return decrypt(integ.bot_token)
    return None


async def _process_channel_created(team_id: str, event: dict):
    """Auto-join newly created public channels so @Gideon mentions work without manual /invite."""
    try:
        channel = event.get("channel", {})
        channel_id = channel.get("id", "")
        channel_name = channel.get("name", "")
        if not channel_id or not team_id:
            return

        bot_token = await _get_slack_bot_token_for_team(team_id)
        if not bot_token:
            logger.info("channel_created: no matching Slack integration", team_id=team_id, channel=channel_name)
            return

        from slack_sdk.errors import SlackApiError

        client = get_slack_client(token=bot_token)
        try:
            await client.conversations_join(channel=channel_id)
            logger.info("Auto-joined new public channel", channel=channel_name, channel_id=channel_id, team_id=team_id)
        except SlackApiError as e:
            err = e.response.get("error", str(e))
            if err in ("already_in_channel", "method_not_supported_for_channel_type"):
                return
            logger.warning("channel_created auto-join failed", channel=channel_name, error=err)
    except Exception as e:
        logger.error("channel_created processing failed", error=str(e))


async def _process_assistant_thread_started(team_id: str, event: dict):
    """A user opened a fresh Gideon assistant thread (the AI-app pane).

    We greet it with suggested starter prompts + a title. Best-effort — if the
    assistant feature isn't enabled in the Slack app config yet, the helper
    no-ops. The actual question handling reuses `_process_slack_mention` (the
    follow-up arrives as a normal `message.im` event carrying this thread_ts).
    """
    try:
        at = event.get("assistant_thread", {}) or {}
        channel_id = at.get("channel_id", "")
        thread_ts = at.get("thread_ts", "")
        if not (channel_id and thread_ts):
            return

        bot_token = await _get_slack_bot_token_for_team(team_id)
        if not bot_token:
            return

        from services.slack_assistant import set_suggested_prompts

        await set_suggested_prompts(
            bot_token,
            channel_id,
            thread_ts,
            prompts=[
                {"title": "Summarize a channel", "message": "Summarize the latest activity in #"},
                {"title": "What's on my plate?", "message": "What are my open todos and action items right now?"},
                {"title": "Draft a reply", "message": "Help me draft a reply to "},
                {"title": "Prep for a meeting", "message": "Prep me for my next meeting"},
            ],
            title="How can Gideon help?",
        )
    except Exception as e:
        logger.error("assistant_thread_started processing failed", error=str(e))


GIDEON_SIGNUP_URL = os.environ.get("GIDEON_APP_URL", "http://localhost:3000")
ONBOARD_PROMPT_TTL_SECONDS = 7 * 24 * 3600


async def _send_onboarding_prompt(team_id: str, slack_user_id: str, channel_id: str, thread_ts: str) -> None:
    """Reply to an unknown Slack user with a signup prompt. Deduped for 7 days per (team, user)."""
    dedup_key = f"slack_onboard_prompted:{team_id}:{slack_user_id}"
    dedup_claimed = False
    redis = None
    try:
        redis = get_redis()
        was_set = await redis.set(dedup_key, "1", nx=True, ex=ONBOARD_PROMPT_TTL_SECONDS)
        if not was_set:
            logger.info("Onboarding prompt already sent recently, skipping", team=team_id, user=slack_user_id)
            return
        dedup_claimed = True
    except Exception as e:
        logger.warning("Onboarding dedup check failed, proceeding anyway", error=str(e))

    bot_token = await _get_slack_bot_token_for_team(team_id)
    if not bot_token:
        logger.warning("No bot token available for onboarding prompt", team=team_id)
        # Release the dedup claim so we can retry on the next mention instead of
        # silently suppressing this user for 7 days.
        if dedup_claimed:
            try:
                await redis.delete(dedup_key)
            except Exception:
                pass
        return

    try:
        client = get_slack_client(token=bot_token)
        await client.chat_postEphemeral(
            channel=channel_id,
            user=slack_user_id,
            thread_ts=thread_ts,
            text=(
                f"👋 Hey <@{slack_user_id}>, I don't have a Gideon account for you yet. "
                f"Sign up at {GIDEON_SIGNUP_URL} with your Google account, then tag me again and I'll help."
            ),
        )
        logger.info("Sent Slack onboarding prompt", team=team_id, user=slack_user_id)
    except Exception as e:
        logger.warning("Failed to send onboarding prompt", error=str(e))
        # Clear dedup so the next mention can try again rather than being suppressed.
        if dedup_claimed:
            try:
                await redis.delete(dedup_key)
            except Exception:
                pass


async def _process_slack_mention(team_id: str, event: dict):
    """Background task: process a Slack @Gideon mention OR a DM to the bot."""
    try:
        slack_user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        text = event.get("text", "")
        message_ts = event.get("ts", "")
        is_dm = event.get("channel_type") == "im"

        # Assistant-pane messages arrive as `message.im` events that carry a
        # `thread_ts` (the assistant thread). Each assistant thread is its own
        # Gideon conversation, and the reply MUST be threaded into it — the flat
        # un-threaded DM behavior below would break the pane.
        assistant_thread_ts = event.get("thread_ts") if is_dm else None
        is_assistant = bool(assistant_thread_ts)

        # DM conversation: the whole channel is one persistent conversation —
        # if we keyed on message_ts, each DM line would spawn a fresh chat
        # with no memory of prior turns. Reply un-threaded (thread_ts=None)
        # so the back-and-forth reads like a normal Slack DM.
        # Channel mention: keep the existing thread-per-mention behavior.
        if is_assistant:
            thread_ts = assistant_thread_ts
            source_thread_id = f"dm:{channel_id}:{thread_ts}"
        elif is_dm:
            thread_ts = None
            source_thread_id = f"dm:{channel_id}"
        else:
            thread_ts = event.get("thread_ts") or message_ts  # If not in a thread, start one
            source_thread_id = f"{channel_id}:{thread_ts}"

        # Strip only the LEADING bot mention (Slack sends "<@BOT_ID> instruction").
        # Inline `<@U...>` mentions of other users are kept; they're decoded
        # below inside the downstream session so we don't open a second one.
        # No-op for DMs (no leading mention); regex is idempotent.
        instruction = re.sub(r"^\s*<@[A-Z0-9]+>\s*", "", text).strip()
        if not instruction:
            logger.info("Slack mention with no instruction, ignoring", channel=channel_id)
            return

        logger.info(
            "Processing Slack trigger",
            slack_user=slack_user_id,
            channel=channel_id,
            instruction_length=len(instruction),
        )

        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            # Decode inline `<@U...>` user mentions using the same session —
            # this is a Redis-backed lookup, no external calls are held open.
            instruction = await decode_slack_mentions(instruction, session)

            # Resolve Slack user to Gideon user
            int_result = await session.execute(
                select(Integration).where(
                    Integration.provider == "slack",
                    Integration.is_active == True,
                )
            )
            integrations = int_result.scalars().all()

            user_id = None
            bot_token = None
            for integ in integrations:
                extra = integ.extra_data or {}
                if extra.get("user_id") == slack_user_id:
                    user_id = integ.user_id
                    # Use bot_token for replies (messages come FROM Gideon, not the user)
                    if integ.bot_token:
                        bot_token = decrypt(integ.bot_token)
                    break

            if not user_id:
                logger.info("Slack user not in Gideon — sending onboarding prompt", slack_user=slack_user_id, team=team_id)
                await _send_onboarding_prompt(team_id, slack_user_id, channel_id, thread_ts)
                return

            # Look up or create trigger conversation (with savepoint for
            # concurrent inserts). source_thread_id was computed up top —
            # `dm:<channel>` for DMs (channel-as-conversation), or
            # `<channel>:<thread_ts>` for channel mentions.
            tc_result = await session.execute(
                select(TriggerConversation).where(
                    TriggerConversation.source == "slack",
                    TriggerConversation.source_thread_id == source_thread_id,
                    TriggerConversation.user_id == user_id,
                )
            )
            tc = tc_result.scalar_one_or_none()

            if tc:
                conversation_id = str(tc.conversation_id)
            else:
                try:
                    async with session.begin_nested():
                        conv = Conversation(
                            user_id=user_id,
                            title=f"Slack: {instruction[:80]}",
                        )
                        session.add(conv)
                        await session.flush()
                        await session.refresh(conv)
                        conversation_id = str(conv.id)

                        tc = TriggerConversation(
                            user_id=user_id,
                            conversation_id=conv.id,
                            source="slack",
                            source_thread_id=source_thread_id,
                            source_channel_id=channel_id,
                            source_message_ts=thread_ts,
                        )
                        session.add(tc)
                    await session.commit()
                except IntegrityError:
                    # Concurrent insert hit the unique index -- re-query
                    await session.rollback()
                    tc_result = await session.execute(
                        select(TriggerConversation).where(
                            TriggerConversation.source == "slack",
                            TriggerConversation.source_thread_id == source_thread_id,
                            TriggerConversation.user_id == user_id,
                        )
                    )
                    tc = tc_result.scalar_one_or_none()
                    if not tc:
                        logger.error("Trigger conversation creation failed and re-query returned nothing", source_thread_id=source_thread_id)
                        return
                    conversation_id = str(tc.conversation_id)

            from services.events_service import track_trigger_received
            track_trigger_received(str(user_id), source="slack")

            # Fetch thread context with human-readable timestamps + names.
            # Previous shape was "<slack_user_id>: <text>" with no timestamp,
            # so the agent couldn't answer "how long did X take" without
            # hallucinating epoch seconds into the reply.
            # Skip for DMs — there's no thread to read; persistent context
            # already comes from the TriggerConversation message history.
            thread_context = ""
            if bot_token and not is_dm:
                try:
                    from datetime import datetime, timezone
                    from zoneinfo import ZoneInfo
                    from services.workspace_member_service import get_all_members
                    from db.models import User

                    client = get_slack_client(token=bot_token)
                    replies = await client.conversations_replies(channel=channel_id, ts=thread_ts, limit=20)
                    msgs = replies.get("messages", [])

                    # Resolve Slack user IDs to names from the cache
                    try:
                        slack_members = await get_all_members(session)
                        slack_id_to_name = {
                            m["id"]: (m.get("real_name") or m.get("name") or m.get("id"))
                            for m in slack_members if m.get("id")
                        }
                    except Exception:
                        slack_id_to_name = {}

                    # Pull user's timezone for display (falls back to UTC).
                    user_tz = ZoneInfo("UTC")
                    try:
                        u_res = await session.execute(select(User.timezone).where(User.id == user_id))
                        tz_row = u_res.first()
                        if tz_row and tz_row[0]:
                            user_tz = ZoneInfo(tz_row[0])
                    except Exception:
                        pass

                    context_lines = []
                    for m in msgs:
                        if m.get("ts") == message_ts:
                            continue  # Skip the trigger message itself
                        ts_raw = m.get("ts", "")
                        try:
                            ts_local = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc).astimezone(user_tz)
                            ts_str = ts_local.strftime("%Y-%m-%d %H:%M")
                        except (ValueError, TypeError):
                            ts_str = "unknown time"
                        slack_uid = m.get("user", "")
                        author = slack_id_to_name.get(slack_uid, slack_uid or "unknown")
                        text = (m.get("text") or "")[:300]
                        context_lines.append(f"- [{ts_str}] {author}: {text}")
                    thread_context = "\n".join(context_lines) if context_lines else ""
                except Exception as e:
                    logger.warning("Failed to fetch Slack thread context", error=str(e))

            # Resolve the channel name so "this channel" / "everyone here"
            # has a referent in the trigger agent's system prompt. Without
            # this the agent has to ask the user which channel they meant
            # even though we already know — see the screenshot in the
            # 2026-05-01 session.
            channel_name = ""
            if bot_token:
                try:
                    _info_client = get_slack_client(token=bot_token)
                    info_resp = await _info_client.conversations_info(channel=channel_id)
                    channel_name = (info_resp.get("channel") or {}).get("name", "") or ""
                except Exception as e:
                    logger.warning("Failed to resolve Slack channel name", channel_id=channel_id, error=str(e))

            # Retrieve user's last-known auth token for permission-gated tools (e.g. ClickHouse)
            from services.user_cache_service import get_user_auth_token
            cached_auth_token = await get_user_auth_token(str(user_id))

            # Drive the native Slack "thinking" status shimmer while the agent
            # works. Slack lets an AI app set status on ANY thread it's active
            # in — the assistant pane AND channel/@mention threads — so we gate
            # only on having a thread_ts (legacy flat DMs have none). The agent
            # updates the text per tool round via status_cb; a successful reply
            # auto-clears it and the finally covers empty/errored runs. All
            # best-effort: no-ops if the assistant feature isn't enabled.
            from services.slack_assistant import clear_status, set_status

            status_cb = (
                functools.partial(set_status, bot_token, channel_id, thread_ts)
                if (bot_token and thread_ts)
                else None
            )

            # Run trigger agent
            from services.trigger_agent_service import run_trigger_agent
            try:
                response_text = await run_trigger_agent(
                    trigger_conv_id=str(tc.id),
                    user_id=str(user_id),
                    instruction=instruction,
                    source_context=thread_context,
                    source_info={
                        "source": "Slack",
                        "channel_id": channel_id,
                        "channel_name": channel_name,
                        "thread_ts": thread_ts,
                        "auth_token": cached_auth_token,
                    },
                    session=session,
                    status_cb=status_cb,
                )

                # Post reply to Slack thread. Convert markdown → Slack mrkdwn
                # before sending — Claude emits `**bold**` / `### heading` /
                # `[text](url)` which Slack renders as literal characters.
                if response_text and bot_token:
                    from services.slack_format import markdown_to_slack_mrkdwn
                    client = get_slack_client(token=bot_token)
                    await client.chat_postMessage(
                        channel=channel_id,
                        thread_ts=thread_ts,
                        text=markdown_to_slack_mrkdwn(response_text),
                    )
            finally:
                if bot_token and thread_ts:
                    await clear_status(bot_token, channel_id, thread_ts)

    except Exception as e:
        logger.error("Slack trigger processing failed", error=str(e))
        from services.events_service import track_error
        track_error("unknown", "slack_trigger_failed", detail=str(e))
