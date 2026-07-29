"""
Trigger Agent Service -- runs an autonomous agent loop when Gideon is invoked
from Slack (@Gideon) or the Gideon mailbox (GIDEON_INBOX_EMAIL).
"""

import json
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional
from uuid import UUID

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import CODE_EXECUTION_TOOL
from services.llm import get_spec, llm
from services.llm.code_execution_bridge import EXECUTE_PYTHON_TOOL_SCHEMA
from services.llm.types import ToolCall
from db.models import Conversation, Message, Skill, TriggerConversation, User
from services.prompt_service import build_system_prompt, get_user_slack_id
from services.events_service import track_error
from services.tool_use_repair import (
    drop_unfulfilled_tool_uses,
    repair_orphan_tool_uses,
    strip_orphan_tool_results,
)
from tools.tool_executor import execute_tool_call
from tools.tool_registry import get_tools_for_user

logger = structlog.get_logger()

MAX_ROUNDS = 50


def _tool_status_phrase(tool_name: str) -> str:
    """Human-friendly present-progressive phrase for the Slack assistant status
    shimmer, derived from the tool about to run (e.g. 'Searching Slack…')."""
    n = (tool_name or "").lower()
    if "slack" in n:
        return "Searching Slack…"
    if "grafana" in n or "analytics" in n or "clickhouse" in n:
        return "Querying the data…"
    if "exa" in n or "web_search" in n or "web_answer" in n or "search_web" in n:
        return "Searching the web…"
    if "notion" in n:
        return "Looking through Notion…"
    if "gmail" in n or "email" in n:
        return "Checking email…"
    if "calendar" in n or "meeting" in n:
        return "Checking the calendar…"
    if "linear" in n:
        return "Checking Linear…"
    if "github" in n:
        return "Checking GitHub…"
    if "todo" in n:
        return "Checking your todos…"
    if "memory" in n or "memories" in n:
        return "Recalling what I know…"
    if "code" in n or "python" in n:
        return "Crunching numbers…"
    return "Working on it…"

# Cap how many of the most recent messages we replay into a trigger round. The
# trigger agent has no rolling summary (unlike chat), so older context is simply
# dropped. Without a cap, a long-lived lane (e.g. a long-running WhatsApp thread) reloads
# its ENTIRE history every round — slower + more expensive each turn, and the main
# reason WhatsApp rounds were taking 60s+. 40 keeps plenty of recent context for a
# quick chat/Slack thread while bounding per-round cost.
TRIGGER_MAX_CONTEXT_MESSAGES = 40

TRIGGER_SYSTEM_ADDON = """
## Trigger Mode -- Inline Conversation

You were invoked by the user via {source}. They said:

> {instruction}

### Where this is happening
{source_location}

### Thread Context
Each message is prefixed with `[YYYY-MM-DD HH:MM]` in the user's local timezone.
{thread_context}

### Voice
You are Gideon. Friendly AI sidekick energy: warm, a little playful, confident, not corporate. First person as "I". Refer to yourself as "it" if ever in third person, never "he/him". No em dashes (use commas or periods). No hedging filler ("unfortunately", "it looks like", "possibly"). No upsells after answering ("want me to dig in further?"). Keep it Slack-sized: one or two short paragraphs, maybe a bullet or two.

### Rules
- Be concise. This is an inline conversation in {source}, not a report.
- If you need clarification, ask directly. The user will see your reply in {source}.
- When you take an action (create Linear issue, send Slack message, etc.), confirm what you did with links/IDs.
- You have access to all the user's connected tools. Use them as needed.
- For email: you may send replies directly (no draft needed). The user explicitly invoked you.
- For Slack: your reply will appear in the same thread.
- Do NOT perform destructive actions (delete issues, overwrite documents) unless explicitly asked.

### Timestamps & durations
- The thread context above has human-readable timestamps per message in the user's timezone. Use those directly.
- NEVER print raw Unix epoch timestamps (e.g. 1776943504) in your reply. No one reads those.
- When asked "how fast / how long / when did", COMPUTE the answer from the timestamps and state it plainly: "2h 4m", "under 10 minutes", "the same afternoon". No vague "a few hours (possibly less)" hedging.
- If a key timestamp is genuinely missing, say so in one line and stop. Don't offer to "dig in further" — the user can ask if they want more.
"""


async def _store_trigger_message(
    session: AsyncSession,
    conversation_id: str,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_results: dict | None = None,
    model: str | None = None,
    tokens_used: int | None = None,
) -> Message:
    """Store a message and commit immediately."""
    msg = Message(
        conversation_id=UUID(conversation_id),
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_results=tool_results,
        model=model,
        tokens_used=tokens_used,
    )
    session.add(msg)
    await session.flush()
    await session.refresh(msg)
    await session.commit()
    return msg


async def run_trigger_agent(
    trigger_conv_id: str,
    user_id: str,
    instruction: str,
    source_context: str,
    source_info: dict,
    session: AsyncSession,
    status_cb: Optional[Callable[[str], Awaitable[None]]] = None,
    attachments: Optional[list[dict]] = None,
) -> str | None:
    """
    Run the trigger agent loop. Returns the final text response (for posting back to source).

    source_info: {
        "source": "slack" | "gmail",
        "channel_id": str (Slack),
        "thread_ts": str (Slack),
        "thread_id": str (Gmail),
    }

    attachments: optional content blocks for the CURRENT turn, merged into the
    last user message so the model sees them this round (used by multimodal
    sources, e.g. the WhatsApp lane). Provider-safety differs by block type:
    IMAGE blocks work everywhere — the OpenAI-compatible (Kimi) adapter
    translates them to `image_url` and Anthropic takes them natively. DOCUMENT
    (PDF) blocks are Anthropic-ONLY and are silently dropped on Kimi/OpenAI —
    and prod runs trigger_agent on Kimi — so callers must not depend on them
    there. (The WhatsApp lane text-extracts docs and renders scanned PDFs to
    image blocks rather than sending document blocks, so it stays safe on Kimi.)
    """
    uid = UUID(user_id)
    conversation_id = None

    try:
        # Look up existing trigger conversation or get the one just created
        tc_result = await session.execute(
            select(TriggerConversation).where(TriggerConversation.id == UUID(trigger_conv_id))
        )
        tc = tc_result.scalar_one_or_none()
        if not tc:
            logger.error("Trigger agent: trigger_conversation not found", trigger_conv_id=trigger_conv_id)
            return None
        conversation_id = str(tc.conversation_id)

        # Load user
        result = await session.execute(select(User).where(User.id == uid))
        user = result.scalar_one_or_none()
        if not user:
            logger.error("Trigger agent: user not found", user_id=user_id)
            return None

        # Load tools + server-side tools (web search, code execution)
        tools = await get_tools_for_user(uid, session)
        tool_schemas = [t.to_claude_schema() for t in tools]
        # Exa web search is registered as a regular tool in tool_registry

        # Load skill memories
        skill_memories = []
        try:
            skill_result = await session.execute(
                select(Skill).where(
                    Skill.user_id == uid,
                    Skill.status == "completed",
                    Skill.memory_content.isnot(None),
                )
            )
            for skill in skill_result.scalars().all():
                skill_memories.append({
                    "name": skill.name,
                    "memory_content": skill.memory_content,
                })
        except Exception as e:
            logger.warning("Trigger agent: failed to load skill memories", error=str(e))

        slack_user_id = await get_user_slack_id(session, user_id)

        # Build system prompt
        system_prompt = build_system_prompt(
            persona_text=user.persona_text,
            user_name=user.name,
            user_email=user.email,
            slack_user_id=slack_user_id,
            connected_tools=tools,
            skill_memories=skill_memories if skill_memories else None,
            background_info=user.background_info,
            user_timezone=user.timezone,
            preferred_communication=user.preferred_communication,
        )
        # Render the source location so the agent has a referent for "this
        # channel", "everyone here", etc. Slack: name + ID. Gmail: thread id.
        # Without this, "this channel" was unanchored and the agent had to
        # ask the user which channel they meant.
        source_kind = source_info.get("source", "unknown")
        if source_kind == "Slack":
            ch_name = (source_info.get("channel_name") or "").strip()
            ch_id = source_info.get("channel_id", "")
            if ch_name and ch_id:
                source_location = (
                    f"You are replying inside the Slack channel `#{ch_name}` "
                    f"(channel id `{ch_id}`). When the user says \"this channel\" "
                    f"or \"everyone here\", they mean `#{ch_name}`. To get the "
                    f"member roster (with names + emails), call "
                    f"`list_channel_members` with this channel id."
                )
            elif ch_id:
                source_location = (
                    f"You are replying inside the Slack channel id `{ch_id}`. "
                    f"When the user says \"this channel\" / \"everyone here\", "
                    f"they mean this channel. Call `list_channel_members` with "
                    f"this id to get the roster."
                )
            else:
                source_location = "You are replying inside a Slack thread. The channel context is not available."
        elif source_kind == "Gmail":
            thread_id = source_info.get("thread_id", "")
            source_location = (
                f"You are replying inside a Gmail thread (id `{thread_id}`). "
                "When the user says \"this thread\" / \"everyone on this email\", "
                "they mean the recipients of this thread."
            )
        else:
            source_location = f"Source: {source_kind}."

        system_prompt += "\n" + TRIGGER_SYSTEM_ADDON.format(
            source=source_kind,
            source_location=source_location,
            instruction=instruction,
            thread_context=source_context or "No prior thread context.",
        )

        # Store user message
        await _store_trigger_message(session, conversation_id, "user", content=instruction)

        # Load conversation history (for follow-ups). Take the most recent
        # TRIGGER_MAX_CONTEXT_MESSAGES, then reverse back to chronological order.
        # Capping from the most-recent side never separates a tool_use from its
        # (later) tool_results, so the only boundary risk is a leading tool_result
        # whose tool_use fell outside the window — guarded below via seen ids.
        history_result = await session.execute(
            select(Message)
            .where(Message.conversation_id == UUID(conversation_id))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(TRIGGER_MAX_CONTEXT_MESSAGES)
        )
        history_msgs = list(reversed(history_result.scalars().all()))
        # Anthropic requires the first message to use the "user" role, and a window
        # cut can land on an assistant/tool message. Advance the start to the first
        # real user message (role "user"; tool results are stored as role "tool").
        # This also drops any leading assistant turn whose tool_results would
        # otherwise be left dangling. The just-stored user instruction is always
        # present as the final message, so a user message always exists.
        _first_user = next((i for i, m in enumerate(history_msgs) if m.role == "user"), 0)
        history_msgs = history_msgs[_first_user:]

        messages = []
        seen_tool_use_ids: set[str] = set()
        for msg in history_msgs:
            if msg.role == "user":
                messages.append({"role": "user", "content": msg.content or ""})
            elif msg.role == "assistant":
                if msg.tool_calls:
                    content_blocks = []
                    if msg.content:
                        content_blocks.append({"type": "text", "text": msg.content})
                    for tc in msg.tool_calls:
                        content_blocks.append({
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["name"],
                            "input": tc["input"],
                        })
                        seen_tool_use_ids.add(tc["id"])
                    messages.append({"role": "assistant", "content": content_blocks})
                elif msg.content:
                    messages.append({"role": "assistant", "content": msg.content})
            elif msg.role == "tool" and msg.tool_results:
                tuid = msg.tool_results.get("tool_use_id")
                # Drop an orphan tool_result whose tool_use was trimmed out of the
                # window — Anthropic 400s on a tool_result with no matching tool_use.
                if tuid not in seen_tool_use_ids:
                    continue
                block = {
                    "type": "tool_result",
                    "tool_use_id": tuid,
                    "content": json.dumps(msg.tool_results.get("result", {})),
                }
                # Coalesce consecutive tool rows into ONE user message, so every
                # result for an assistant turn sits in a single immediately-
                # following message. Persisting them as separate user messages
                # made repair_orphan_tool_uses splice a synthetic result for the
                # later tool calls into the FIRST result message while the real
                # later result stayed behind as an orphan (provider 400) — and for
                # a healthy multi-tool turn that synthetic would wrongly stand in
                # for a real result. One combined message avoids both.
                prev = messages[-1] if messages else None
                if (
                    prev is not None
                    and prev.get("role") == "user"
                    and isinstance(prev.get("content"), list)
                    and prev["content"]
                    and all(
                        isinstance(b, dict) and b.get("type") == "tool_result"
                        for b in prev["content"]
                    )
                ):
                    prev["content"].append(block)
                else:
                    messages.append({"role": "user", "content": [block]})

        # Run the full tool_use ↔ tool_result sanitizer sequence (same as the chat
        # path) so an interrupted prior turn can't wedge replay. repair fabricates
        # a result for any orphan tool_use (assistant tool_calls persisted but the
        # tool_result never written — observed on a WhatsApp lane as `web_search:12`
        # orphaned, every retry re-failing at round 1); strip drops any orphan
        # tool_result; drop is the last-resort net for anything still unpaired.
        messages = repair_orphan_tool_uses(messages)
        messages = strip_orphan_tool_results(messages)
        messages = drop_unfulfilled_tool_uses(messages)

        # Multimodal sources (WhatsApp images/PDFs) attach content blocks to the
        # CURRENT turn. Merge them into the last user message (the just-stored
        # instruction) so the model sees them this round. Stored history stays
        # text-only (attachment bytes aren't persisted) — fine for follow-ups.
        if attachments:
            if messages and messages[-1].get("role") == "user":
                prior = messages[-1].get("content")
                if isinstance(prior, str):
                    text_blocks = [{"type": "text", "text": prior}] if prior.strip() else []
                    messages[-1]["content"] = text_blocks + list(attachments)
                elif isinstance(prior, list):
                    messages[-1]["content"] = list(prior) + list(attachments)
                else:
                    messages[-1]["content"] = list(attachments)
            else:
                messages.append({"role": "user", "content": list(attachments)})

        from dataclasses import replace as _replace
        trigger_spec = _replace(get_spec("trigger_agent"), max_tokens=16384)
        final_text = None

        # Code execution tool depends on provider:
        #  - anthropic: native server-side `code_execution` tool (adapter adds
        #    the required beta automatically now).
        #  - openai/kimi: the `execute_python` bridge function tool, which
        #    tool_executor routes to Anthropic's hosted sandbox. The native
        #    code_execution tool would be sent as an unimplemented function and
        #    the model's Python requests would silently fail.
        if trigger_spec.provider == "anthropic":
            _code_tool = CODE_EXECUTION_TOOL
        else:
            _code_tool = EXECUTE_PYTHON_TOOL_SCHEMA
        round_tools = (tool_schemas or []) + [_code_tool]

        for round_num in range(MAX_ROUNDS):
            # Drive the Slack assistant "thinking" status shimmer (no-op for
            # non-Slack callers / when status_cb is None). First round reads as
            # "thinking", later rounds are synthesizing prior tool output.
            if status_cb:
                await status_cb("is thinking…" if round_num == 0 else "Putting it all together…")

            # Close any open transaction BEFORE the LLM round. The history read
            # above (and each prior round's tool reads) leaves a transaction open;
            # a trigger round can take >30s, and Postgres
            # `idle_in_transaction_session_timeout` (30s) terminates a connection
            # left idle-in-transaction during the await — poisoning the session so
            # the next flush raises PendingRollbackError and the user just gets
            # "Sorry, I ran into an error" (this is exactly the WhatsApp failure on
            # slow model rounds). CLAUDE.md: no external calls inside an
            # open DB session. A commit with nothing pending is a safe no-op, and
            # the loop only uses plain dicts/values afterward, so commit-expiry of
            # ORM instances is harmless here.
            await session.commit()

            logger.info(
                "Trigger agent round",
                round=round_num + 1,
                trigger_conv_id=trigger_conv_id,
                source=source_info.get("source"),
            )

            try:
                # stream_collect avoids Anthropic's long-request timeout while
                # presenting a single-call API to the round loop.
                response = await llm.stream_collect(
                    trigger_spec,
                    messages=messages,
                    system=system_prompt,
                    tools=round_tools,
                    timeout=300.0,
                )
            except Exception as e:
                logger.error("Trigger agent: LLM API error", error=str(e), round=round_num + 1)
                track_error(user_id, "trigger_agent_claude_error", detail=f"round={round_num + 1}: {e}")
                error_msg = "I encountered an error while processing your request. Please try again."
                await _store_trigger_message(session, conversation_id, "assistant", content=error_msg)
                return error_msg

            # Log usage
            if response.usage:
                from services.usage_service import log_usage
                log_usage(uid, trigger_spec.model, "trigger_agent", response.usage)

            # Process response
            tool_use_blocks: list[ToolCall] = list(response.tool_calls)
            has_tool_use = bool(tool_use_blocks)
            stored_text = response.text or None
            stored_tool_calls = [
                {"id": tc.id, "name": tc.name, "input": tc.input}
                for tc in tool_use_blocks
            ] or None

            await _store_trigger_message(
                session, conversation_id, "assistant",
                content=stored_text,
                tool_calls=stored_tool_calls,
                model=trigger_spec.model,
                tokens_used=response.usage.output_tokens if response.usage else None,
            )

            # Build the assistant content blocks for next-round history.
            # On Anthropic, replay the raw SDK content VERBATIM — it carries
            # server-side blocks (server_tool_use, code_execution_tool_result)
            # that a `pause_turn` continuation needs on the next round. The
            # neutral rebuild (thinking/text/tool_use only) drops those and the
            # follow-up round 400s. (anthropic_adapter.stream_collect exposes
            # the SDK message as response.raw.) Non-anthropic providers have no
            # such server blocks, so the neutral rebuild is correct there.
            raw_content = getattr(response.raw, "content", None) if response.raw else None
            if raw_content:
                messages.append({"role": "assistant", "content": list(raw_content)})
            else:
                assistant_blocks: list[dict] = []
                if response.thinking:
                    thinking_block: dict = {"type": "thinking", "thinking": response.thinking}
                    # Preserve OpenAI Responses reasoning id for round-trip.
                    if response.reasoning_id:
                        thinking_block["_reasoning_id"] = response.reasoning_id
                    assistant_blocks.append(thinking_block)
                if stored_text:
                    assistant_blocks.append({"type": "text", "text": stored_text})
                for tc in tool_use_blocks:
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })
                if assistant_blocks:
                    messages.append({"role": "assistant", "content": assistant_blocks})

            # pause_turn = server-side tool (web search / code execution) executed
            if response.stop_reason == "pause_turn":
                continue

            # Done? Only exit if there are no pending tool calls.
            if not has_tool_use:
                final_text = stored_text
                logger.info(
                    "Trigger agent completed",
                    rounds=round_num + 1,
                    trigger_conv_id=trigger_conv_id,
                )
                break

            # Execute tools. Every tool_use MUST end with a stored tool_result,
            # even if the tool raises: otherwise the assistant tool_call persisted
            # above is left orphaned in the DB and every future turn 400s on
            # replay (see repair_orphan_tool_uses). A tool that raises (rather
            # than returning an {"error": ...} dict — e.g. an Exa/web_search
            # timeout) is converted to an error result so the invariant holds.
            tool_results = []
            for tc in tool_use_blocks:
                if status_cb:
                    await status_cb(_tool_status_phrase(tc.name))
                try:
                    result = await execute_tool_call(
                        tc.name, tc.input, uid, session,
                        auth_token=source_info.get("auth_token", ""),
                        conversation_id=conversation_id,
                    )
                except Exception as tool_err:
                    logger.error(
                        "Trigger agent: tool execution raised",
                        tool_name=tc.name,
                        error=str(tool_err),
                        round=round_num + 1,
                    )
                    # Surface only the exception TYPE to the model-visible/persisted
                    # result — the full message (which can carry paths, creds, stack
                    # detail) is logged above, not replayed. Matches chat_service.
                    result = {"error": f"Tool '{tc.name}' failed: {type(tool_err).__name__}"}
                if isinstance(result, dict) and result.get("error"):
                    track_error(
                        user_id, "tool_execution_error",
                        tool_name=tc.name,
                        detail=str(result["error"])[:500],
                    )
                await _store_trigger_message(
                    session, conversation_id, "tool",
                    tool_results={"tool_use_id": tc.id, "result": result},
                )
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})

        if final_text is None:
            final_text = "I reached the maximum number of steps. Please continue in the Gideon app if needed."
            await _store_trigger_message(session, conversation_id, "assistant", content=final_text)

        # Update trigger conversation
        await session.execute(
            update(TriggerConversation)
            .where(TriggerConversation.id == UUID(trigger_conv_id))
            .values(last_activity_at=datetime.now(timezone.utc), updated_at=datetime.now(timezone.utc))
        )
        await session.commit()

        return final_text

    except Exception as e:
        logger.error("Trigger agent failed", error=str(e), trigger_conv_id=trigger_conv_id)
        track_error(user_id, "trigger_agent_failed", detail=str(e))
        if conversation_id:
            try:
                await _store_trigger_message(
                    session, conversation_id, "assistant",
                    content="Sorry, I ran into an error while processing your request.",
                )
            except Exception:
                pass
        return f"Sorry, I ran into an error while processing your request."
