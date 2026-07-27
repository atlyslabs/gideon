"""OpenAI Responses API adapter.

This is a separate code path from openai_compatible_adapter (which targets
Chat Completions). The Responses API is item-based (`input` is a list of
items, `output` is a list of items) rather than message-based, and it's the
ONLY OpenAI surface that exposes:

  - server-side `code_interpreter` (hosted Python sandbox, container lifecycle)
  - `reasoning_summary_text.delta` streaming events (Anthropic-style thinking
    visible to the user, not just `reasoning_effort` as a knob)

So when an LLMSpec wants either feature on OpenAI, the wrapper dispatches
here instead of to chat.completions.

Wire shape diffs vs Chat Completions:
  - Tool param: flat `{type: "function", name, parameters, description}` (no
    nested `function:` wrapper).
  - Tool call result: `{type: "function_call_output", call_id, output}` as an
    input item (not a `role: "tool"` message).
  - Assistant tool call on history: `{type: "function_call", call_id, name,
    arguments}` (not nested under `tool_calls`).
  - Thinking on history: `{type: "reasoning", id, summary: [...]}` items
    must be replayed back to preserve reasoning continuity (similar to Kimi).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, AsyncIterator, Optional

from config.llm_registry import LLMSpec, map_effort_to_openai

from services.llm.types import (
    Event,
    LLMResponse,
    MessageStop,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

_logger = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI  # type: ignore
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        _client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    return _client


# ---------------------------------------------------------------------------
# Conversion: neutral (Anthropic-shaped) messages → Responses input items.
# ---------------------------------------------------------------------------


def _content_to_responses_parts(content: Any) -> Any:
    """Convert Anthropic-style content blocks to Responses-API content parts.
    Text and images supported; thinking blocks are emitted as separate
    `reasoning` input items by the caller, not inline."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[dict] = []
    for blk in content:
        btype = blk.get("type")
        if btype == "text":
            parts.append({"type": "input_text", "text": blk.get("text", "")})
        elif btype == "image":
            src = blk.get("source", {})
            if src.get("type") == "base64":
                media = src.get("media_type", "image/png")
                data = src.get("data", "")
                parts.append(
                    {
                        "type": "input_image",
                        "image_url": f"data:{media};base64,{data}",
                    }
                )
            elif src.get("type") == "url":
                parts.append({"type": "input_image", "image_url": src.get("url", "")})
        # thinking blocks handled at message level
    if len(parts) == 1 and parts[0]["type"] == "input_text":
        return parts[0]["text"]
    return parts or ""


def _normalize_messages_to_input_items(messages: list[dict]) -> list[dict]:
    """Walk the neutral message list and emit a flat list of Responses input
    items. Each Anthropic message can expand into multiple items because tool
    use/result/reasoning all become standalone items in the Responses world.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            text_parts: list[dict] = []
            for blk in content:
                btype = blk.get("type")
                if btype == "thinking":
                    # Preserve reasoning across turns — without this, models
                    # like o3 that previously emitted reasoning_summary fail
                    # on round 2 with "missing reasoning item".
                    #
                    # If the thinking block carries the original Responses
                    # `_reasoning_id` (captured during streaming via output_item.added),
                    # include it so OpenAI can link the prior reasoning state.
                    # Without `id`, the reasoning item is treated as a fresh
                    # one and the model cannot resume its chain of thought.
                    summary_text = blk.get("thinking", "")
                    reasoning_id = blk.get("_reasoning_id") or blk.get("id")
                    if summary_text or reasoning_id:
                        reasoning_item: dict[str, Any] = {"type": "reasoning"}
                        if reasoning_id:
                            reasoning_item["id"] = reasoning_id
                        if summary_text:
                            reasoning_item["summary"] = [
                                {"type": "summary_text", "text": summary_text}
                            ]
                        out.append(reasoning_item)
                elif btype == "text":
                    text_parts.append(
                        {"type": "output_text", "text": blk.get("text", "")}
                    )
                elif btype == "tool_use":
                    out.append(
                        {
                            "type": "function_call",
                            "call_id": blk.get("id", ""),
                            "name": blk.get("name", ""),
                            "arguments": json.dumps(blk.get("input", {})),
                        }
                    )
            if text_parts:
                out.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": text_parts,
                    }
                )
            continue

        if role == "user" and isinstance(content, list):
            non_tool: list[dict] = []
            for blk in content:
                if blk.get("type") == "tool_result":
                    tr_content = blk.get("content", "")
                    if isinstance(tr_content, list):
                        parts = [
                            b.get("text", "")
                            for b in tr_content
                            if b.get("type") == "text"
                        ]
                        tr_content = "\n".join(parts)
                    elif not isinstance(tr_content, str):
                        tr_content = json.dumps(tr_content)
                    out.append(
                        {
                            "type": "function_call_output",
                            "call_id": blk.get("tool_use_id", ""),
                            "output": tr_content or "",
                        }
                    )
                else:
                    non_tool.append(blk)
            if non_tool:
                out.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": _content_to_responses_parts(non_tool),
                    }
                )
            continue

        # Plain user/assistant string, or system role → message item.
        if role == "system":
            # system maps to `instructions` at the top level — handled by
            # _build_kwargs. Skip here.
            continue
        out.append(
            {
                "role": role,
                "content": _content_to_responses_parts(content)
                if isinstance(content, list)
                else content,
            }
        )
    return out


def _normalize_tools_to_responses(tools: list[Any]) -> list[dict]:
    """Convert BaseTool / anthropic-shaped tools → Responses API function
    tools (flat shape, not nested under `function:`)."""
    out: list[dict] = []
    for t in tools:
        if isinstance(t, dict):
            # Check shape
            if t.get("type") == "function" and "name" in t:
                # Already flat Responses shape
                out.append(t)
                continue
            if t.get("type") == "function" and "function" in t:
                # Chat Completions shape — flatten
                f = t["function"]
                out.append(
                    {
                        "type": "function",
                        "name": f.get("name", ""),
                        "description": f.get("description", ""),
                        "parameters": f.get("parameters", {"type": "object"}),
                        "strict": False,
                    }
                )
                continue
            if t.get("type") == "code_interpreter":
                # Server-side built-in. Pass through.
                out.append(t)
                continue
            # Anthropic shape {name, description, input_schema}
            out.append(
                {
                    "type": "function",
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "parameters": t.get(
                        "input_schema", {"type": "object", "properties": {}}
                    ),
                    "strict": False,
                }
            )
        else:
            out.append(
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                    "strict": False,
                }
            )
    return out


# ---------------------------------------------------------------------------
# Request kwargs builder.
# ---------------------------------------------------------------------------


def _extract_system(system: Any) -> Optional[str]:
    if system is None:
        return None
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = [b.get("text", "") for b in system if b.get("type") == "text"]
        return "".join(parts) or None
    return str(system)


def _model_supports_reasoning(model: str) -> bool:
    m = model.lower()
    return (
        m.startswith("o1")
        or m.startswith("o3")
        or m.startswith("o4")
        or m.startswith("gpt-5")
    )


# Reasoning models burn hidden reasoning tokens against the output budget; a
# tiny max_output_tokens leaves no room for the visible answer. Floor it so
# the answer survives. (Mirrors openai_compatible_adapter; this path is always
# a reasoning-capable OpenAI model since the Responses route is only chosen for
# code_interpreter / reasoning_summary.)
_REASONING_MIN_OUTPUT_TOKENS = 1024


def _build_kwargs(
    spec: LLMSpec,
    messages: list[dict],
    system: Any,
    tools: Optional[list[Any]],
    force_tool: Optional[str],
    temperature: Optional[float],
    *,
    enable_code_interpreter: bool = False,
    code_interpreter_container: Optional[str] = None,
) -> dict[str, Any]:
    input_items = _normalize_messages_to_input_items(messages)
    instructions = _extract_system(system)

    _max_out = spec.max_tokens
    if _model_supports_reasoning(spec.model) and _max_out < _REASONING_MIN_OUTPUT_TOKENS:
        _max_out = _REASONING_MIN_OUTPUT_TOKENS

    kwargs: dict[str, Any] = {
        "model": spec.model,
        "input": input_items,
        "max_output_tokens": _max_out,
    }
    if instructions:
        kwargs["instructions"] = instructions
    if temperature is not None and not _model_supports_reasoning(spec.model):
        # Reasoning models reject `temperature`.
        kwargs["temperature"] = temperature

    response_tools: list[dict] = []
    if tools:
        response_tools.extend(_normalize_tools_to_responses(tools))
    if enable_code_interpreter:
        # `code_interpreter_container` lets a caller resume a prior sandbox so
        # files generated in an earlier turn survive. The param is plumbed
        # through complete()/stream(); callers that want cross-turn file
        # persistence must (1) read the container id off the
        # code_interpreter_call item on the response, (2) persist it, and
        # (3) pass it back here next turn. The wrapper-chat path does NOT do
        # this yet — OpenAI chat with code_interpreter currently starts a
        # fresh sandbox each turn (`{"type": "auto"}`). Acceptable for one-shot
        # analysis; tracked as a follow-up for multi-turn file workflows.
        container = (
            code_interpreter_container
            if code_interpreter_container
            else {"type": "auto"}
        )
        response_tools.append(
            {
                "type": "code_interpreter",
                "container": container,
            }
        )
    if response_tools:
        kwargs["tools"] = response_tools
        if force_tool:
            kwargs["tool_choice"] = {"type": "function", "name": force_tool}

    # Reasoning. Only set on models that support it; emitting on gpt-4.1 etc
    # returns a 400.
    if _model_supports_reasoning(spec.model):
        effort = map_effort_to_openai(spec.effort) or "medium"
        kwargs["reasoning"] = {"effort": effort, "summary": "auto"}

    return kwargs


def _to_usage(raw_usage: Any) -> Usage:
    if raw_usage is None:
        return Usage()
    cache_read = 0
    details = getattr(raw_usage, "input_tokens_details", None)
    if details is not None:
        cache_read = getattr(details, "cached_tokens", 0) or 0
    return Usage(
        input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
        cache_read_input_tokens=cache_read,
    )


# ---------------------------------------------------------------------------
# Non-streaming.
# ---------------------------------------------------------------------------


async def complete(
    spec: LLMSpec,
    messages: list[dict],
    *,
    system: Any = None,
    tools: Optional[list[Any]] = None,
    force_tool: Optional[str] = None,
    temperature: Optional[float] = None,
    enable_code_interpreter: bool = False,
    code_interpreter_container: Optional[str] = None,
    timeout: float = 300.0,
) -> LLMResponse:
    client = _get_client()
    kwargs = _build_kwargs(
        spec,
        messages,
        system,
        tools,
        force_tool,
        temperature,
        enable_code_interpreter=enable_code_interpreter,
        code_interpreter_container=code_interpreter_container,
    )
    resp = await client.with_options(timeout=timeout).responses.create(**kwargs)

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []

    for item in resp.output or []:
        itype = getattr(item, "type", "")
        if itype == "message":
            for part in getattr(item, "content", None) or []:
                ptype = getattr(part, "type", "")
                if ptype == "output_text":
                    text_parts.append(getattr(part, "text", "") or "")
        elif itype == "reasoning":
            for s in getattr(item, "summary", None) or []:
                if getattr(s, "type", "") == "summary_text":
                    thinking_parts.append(getattr(s, "text", "") or "")
        elif itype == "function_call":
            try:
                args = json.loads(getattr(item, "arguments", "") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=getattr(item, "call_id", ""),
                    name=getattr(item, "name", ""),
                    input=args,
                )
            )
        elif itype == "code_interpreter_call":
            # Code interpreter ran server-side. Record as a tool_call so
            # downstream consumers see "Claude ran code". For chat_service
            # this surfaces as `code_execution` semantics.
            tool_calls.append(
                ToolCall(
                    id=getattr(item, "id", ""),
                    name="_openai_code_interpreter",
                    input={
                        "code": getattr(item, "code", "") or "",
                        "container_id": getattr(item, "container_id", "") or "",
                    },
                )
            )

    stop_reason = "end_turn"
    if getattr(resp, "status", "") == "incomplete":
        stop_reason = "max_tokens"
    elif tool_calls and not text_parts:
        stop_reason = "tool_use"

    return LLMResponse(
        text="".join(text_parts),
        thinking="".join(thinking_parts),
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        usage=_to_usage(getattr(resp, "usage", None)),
        raw=resp,
    )


# ---------------------------------------------------------------------------
# Streaming.
# ---------------------------------------------------------------------------


async def stream(
    spec: LLMSpec,
    messages: list[dict],
    *,
    system: Any = None,
    tools: Optional[list[Any]] = None,
    force_tool: Optional[str] = None,
    temperature: Optional[float] = None,
    enable_code_interpreter: bool = False,
    code_interpreter_container: Optional[str] = None,
    timeout: float = 300.0,
) -> AsyncIterator[Event]:
    client = _get_client()
    kwargs = _build_kwargs(
        spec,
        messages,
        system,
        tools,
        force_tool,
        temperature,
        enable_code_interpreter=enable_code_interpreter,
        code_interpreter_container=code_interpreter_container,
    )

    # Track function_call items by output_index. Responses streams a function
    # call as:
    #   output_item.added (with the item shell including call_id + name)
    #   function_call_arguments.delta (multiple, args streaming)
    #   function_call_arguments.done
    pending_calls: dict[int, dict[str, Any]] = {}
    # code_interpreter items: track id + accumulated code for the ToolCallEnd
    pending_code: dict[int, dict[str, Any]] = {}
    # reasoning items: track id-per-output-index so reasoning_summary deltas
    # can attach the id for downstream round-tripping.
    reasoning_ids: dict[int, str] = {}
    final_usage = Usage()

    # Exceptions propagate to caller (workflow-style retry-by-type works).
    stream_ctx = client.with_options(timeout=timeout).responses.stream(**kwargs)
    async with stream_ctx as s:
        async for event in s:
            etype = getattr(event, "type", "")

            if etype == "response.output_item.added":
                item = getattr(event, "item", None)
                output_index = getattr(event, "output_index", 0)
                itype = getattr(item, "type", "")
                if itype == "function_call":
                    pending_calls[output_index] = {
                        "id": getattr(item, "call_id", "") or getattr(item, "id", ""),
                        "name": getattr(item, "name", ""),
                        "args": "",
                    }
                    yield ToolCallStart(
                        id=pending_calls[output_index]["id"],
                        name=pending_calls[output_index]["name"],
                        index=output_index,
                    )
                elif itype == "code_interpreter_call":
                    pending_code[output_index] = {
                        "id": getattr(item, "id", ""),
                        "code": "",
                    }
                    yield ToolCallStart(
                        id=pending_code[output_index]["id"],
                        name="_openai_code_interpreter",
                        index=output_index,
                    )
                elif itype == "reasoning":
                    # Capture the reasoning item id so subsequent
                    # reasoning_summary_text.delta events can carry it on
                    # ThinkingDelta. Required for round-tripping reasoning
                    # state on the next turn (otherwise the model has to
                    # re-derive its chain of thought from scratch).
                    reasoning_ids[output_index] = getattr(item, "id", "") or ""

            elif etype == "response.output_text.delta":
                yield TextDelta(
                    text=getattr(event, "delta", "") or "",
                    index=getattr(event, "output_index", 0),
                )

            elif etype == "response.reasoning_summary_text.delta":
                idx = getattr(event, "output_index", 0)
                yield ThinkingDelta(
                    thinking=getattr(event, "delta", "") or "",
                    index=idx,
                    reasoning_id=reasoning_ids.get(idx),
                )

            elif etype == "response.function_call_arguments.delta":
                output_index = getattr(event, "output_index", 0)
                delta = getattr(event, "delta", "") or ""
                if output_index in pending_calls:
                    pending_calls[output_index]["args"] += delta
                yield ToolCallArgsDelta(args_delta=delta, index=output_index)

            elif etype == "response.function_call_arguments.done":
                output_index = getattr(event, "output_index", 0)
                if output_index in pending_calls:
                    pc = pending_calls.pop(output_index)
                    final_args = getattr(event, "arguments", None) or pc["args"]
                    try:
                        parsed = json.loads(final_args) if final_args else {}
                    except json.JSONDecodeError:
                        parsed = {}
                    yield ToolCallEnd(
                        id=pc["id"],
                        name=pc["name"],
                        input=parsed,
                        index=output_index,
                    )

            elif etype == "response.code_interpreter_call_code.delta":
                output_index = getattr(event, "output_index", 0)
                delta = getattr(event, "delta", "") or ""
                if output_index in pending_code:
                    pending_code[output_index]["code"] += delta
                # Emit as args delta so consumers stream the code body.
                yield ToolCallArgsDelta(args_delta=delta, index=output_index)

            elif etype == "response.code_interpreter_call.completed":
                output_index = getattr(event, "output_index", 0)
                if output_index in pending_code:
                    pc = pending_code.pop(output_index)
                    yield ToolCallEnd(
                        id=pc["id"],
                        name="_openai_code_interpreter",
                        input={"code": pc["code"]},
                        index=output_index,
                    )

            elif etype == "response.completed":
                # Final usage + stop reason.
                resp = getattr(event, "response", None)
                if resp is not None:
                    final_usage = _to_usage(getattr(resp, "usage", None))
                yield Usage(
                    input_tokens=final_usage.input_tokens,
                    output_tokens=final_usage.output_tokens,
                    cache_read_input_tokens=final_usage.cache_read_input_tokens,
                    cache_creation_input_tokens=final_usage.cache_creation_input_tokens,
                )
                yield MessageStop(stop_reason="end_turn")
                return

            elif etype == "response.incomplete":
                yield Usage(
                    input_tokens=final_usage.input_tokens,
                    output_tokens=final_usage.output_tokens,
                    cache_read_input_tokens=final_usage.cache_read_input_tokens,
                    cache_creation_input_tokens=final_usage.cache_creation_input_tokens,
                )
                yield MessageStop(stop_reason="max_tokens")
                return

            elif etype == "error":
                msg = getattr(event, "message", "") or getattr(event, "code", "")
                raise RuntimeError(f"Responses API error: {msg}")
