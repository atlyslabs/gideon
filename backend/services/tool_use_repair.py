"""Tool-use protocol sanitizers — single source of truth.

Both Anthropic and the OpenAI-compatible (Kimi/OpenAI) providers reject a
request when the tool_use ↔ tool_result pairing in the replayed message history
is broken. A single interrupted turn (crash / LLM error / tool raise /
idle-in-transaction poisoning between persisting the assistant tool_calls and
its tool rows) can leave a permanent orphan that 400s every subsequent turn,
wedging the conversation.

These three passes, run in order, keep any replayed history valid:

1. `repair_orphan_tool_uses`   — every assistant `tool_use` gets a following
                                  `tool_result` (synthesizes an error result for
                                  any that is missing).
2. `strip_orphan_tool_results` — drop `tool_result`s whose originating
                                  `tool_use` was lost.
3. `drop_unfulfilled_tool_uses`— last-resort: drop any `tool_use` still
                                  unanswered after the repair pass.

Used by both the chat loop (`chat_service._load_history`) and the trigger/
WhatsApp agent (`trigger_agent_service.run_trigger_agent`). The message shape is
the Anthropic content-block list; the OpenAI-compatible adapter converts the
repaired list into `role="tool"` messages, so repairing here fixes both paths.
"""

import json

import structlog

logger = structlog.get_logger()


def strip_orphan_tool_results(messages: list[dict]) -> list[dict]:
    """Drop `tool_result` blocks in a user message whose `tool_use_id` does not
    match a `tool_use` in the immediately-preceding assistant message.

    Anthropic rejects the whole request with "unexpected tool_use_id found in
    tool_result blocks" if even one orphan slips through. This happens when a
    conversation replay loses the originating assistant turn (e.g. regenerate,
    manual edit, history pruning).
    """
    for i, msg in enumerate(messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        if not any(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
            continue

        prev = messages[i - 1] if i > 0 else None
        valid_ids: set[str] = set()
        if prev and prev.get("role") == "assistant" and isinstance(prev.get("content"), list):
            valid_ids = {
                b["id"] for b in prev["content"]
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
            }

        cleaned = [
            b for b in content
            if not (isinstance(b, dict) and b.get("type") == "tool_result"
                    and b.get("tool_use_id") not in valid_ids)
        ]
        if len(cleaned) != len(content):
            dropped = len(content) - len(cleaned)
            if cleaned:
                msg["content"] = cleaned
            else:
                msg["content"] = [{"type": "text", "text": "(previous tool results were dropped because the originating turn was lost)"}]
            logger.warning("Stripped orphan tool_result blocks from history", dropped=dropped, index=i)

    return messages


def repair_orphan_tool_uses(messages: list[dict]) -> list[dict]:
    """Ensure every `tool_use` block in an assistant message has a matching
    `tool_result` in the immediately-following user message. Injects synthetic
    error tool_results for any orphans so the provider accepts the payload.

    Only applies to client-side `tool_use` blocks. Server-side blocks
    (`server_tool_use` / `advisor_tool_result`) are paired inside the same
    assistant message by the Advisor protocol.
    """
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") != "assistant":
            i += 1
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            i += 1
            continue

        # Dedupe (preserving order): a malformed assistant message with two
        # tool_use blocks sharing an id must not get two synthetic results for the
        # same id spliced in — later passes wouldn't remove the duplicate.
        tool_use_ids = list(dict.fromkeys(
            b["id"] for b in content
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id")
        ))
        if not tool_use_ids:
            i += 1
            continue

        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        next_is_user_list = (
            next_msg is not None
            and next_msg.get("role") == "user"
            and isinstance(next_msg.get("content"), list)
        )

        existing_result_ids: set[str] = set()
        if next_is_user_list:
            for b in next_msg["content"]:
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                    existing_result_ids.add(b["tool_use_id"])

        missing = [tid for tid in tool_use_ids if tid not in existing_result_ids]
        if missing:
            synthetic = [
                {
                    "type": "tool_result",
                    "tool_use_id": tid,
                    "content": json.dumps({
                        "error": "Tool execution was interrupted — no result was persisted. Tell the user the previous step did not complete and ask if they'd like to retry.",
                        "interrupted": True,
                    }),
                    "is_error": True,
                }
                for tid in missing
            ]
            if next_is_user_list:
                messages[i + 1]["content"] = synthetic + list(next_msg["content"])
            else:
                messages.insert(i + 1, {"role": "user", "content": synthetic})
                i += 1  # skip over the inserted message
            logger.warning(
                "Repaired orphan tool_use blocks in history",
                count=len(missing),
                orphan_ids=missing,
            )
        i += 1

    return messages


def drop_unfulfilled_tool_uses(messages: list[dict]) -> list[dict]:
    """Last-line-of-defense sanitizer: drop any client-side `tool_use` block
    whose id is not satisfied by a `tool_result` in the very next message.

    `repair_orphan_tool_uses` should normally fabricate a synthetic result for
    every orphan, but edge cases (duplicate tool_use ids in a single assistant
    message, type mismatches, stream replay glitches) can still leave the
    provider rejecting the payload with `tool_use ids were found without
    tool_result blocks immediately after`. Once we get here, we know the rich
    repair failed — drop the offending blocks so the chat stays usable. Worst
    case Claude loses memory of one tool call.
    """
    dropped_total = 0
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        fulfilled_ids: set[str] = set()
        if next_msg and next_msg.get("role") == "user" and isinstance(next_msg.get("content"), list):
            fulfilled_ids = {
                b.get("tool_use_id") for b in next_msg["content"]
                if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id")
            }
        cleaned = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") not in fulfilled_ids:
                dropped_total += 1
                continue
            cleaned.append(b)
        if len(cleaned) != len(content):
            # If the assistant message becomes empty, leave a placeholder so
            # role alternation isn't broken.
            if not cleaned:
                cleaned = [{"type": "text", "text": "(previous tool call was dropped because its result was lost)"}]
            msg["content"] = cleaned
    if dropped_total:
        logger.warning("Dropped unfulfilled tool_use blocks as last resort", count=dropped_total)
    return messages
