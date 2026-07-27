"""Anthropic adapter — converts neutral LLM requests into Anthropic SDK calls
and normalizes responses/streams back to neutral events.

Keeps every Anthropic-specific feature (prompt caching, thinking budget,
advisor tool) usable through the spec. The OpenAI/Kimi adapter ignores all
of those — the neutral API is intentionally a superset, with feature flags
that no-op for providers that don't support them.
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Optional

from pydantic import BaseModel

from config import ADVISOR_BETA, ADVISOR_TOOL, CODE_EXECUTION_BETA, CONTEXT_1M_BETA, get_anthropic_client
from config.llm_registry import LLMSpec

from services.llm.types import (
    Event,
    KeepAlive,
    LLMResponse,
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)

_logger = logging.getLogger(__name__)


# Anthropic response content blocks carry OUTPUT-only fields that the *input*
# schema rejects when replayed verbatim ("messages.N.content.K.<type>.<field>:
# Extra inputs are not permitted"). `parsed_output` appears on essentially every
# block type. Tool-RESULT blocks (`advisor_tool_result` and the other server
# `*_tool_result` types) additionally carry `citations` and `text` on output —
# their real payload lives in `content`, and the valid input shape is just
# {type, tool_use_id, content} (exactly what the chat replay rebuilds). Workflows
# and chat replay raw response content, so these are stripped before sending.
# Add new output-only fields here as Anthropic introduces them.
_ALWAYS_INVALID_BLOCK_KEYS = ("parsed_output",)
_TOOL_RESULT_INVALID_BLOCK_KEYS = ("parsed_output", "citations", "text")


def _invalid_keys_for_block(block_type: Any) -> tuple:
    """Output-only keys to strip for a block of this type. Tool-result blocks
    reject more than plain blocks (their content lives in `content`)."""
    if isinstance(block_type, str) and block_type.endswith("tool_result"):
        return _TOOL_RESULT_INVALID_BLOCK_KEYS
    return _ALWAYS_INVALID_BLOCK_KEYS


def _clean_input_block(block: Any) -> Any:
    """Return `block` with input-invalid output-only keys removed, or the ORIGINAL
    object unchanged (identity preserved) when it has none — so verbatim SDK
    blocks that need no cleaning (including server-tool blocks required for
    pause_turn replay) pass through untouched. A block that DOES need cleaning is
    returned as a stripped dict (Anthropic accepts dict content blocks on input)."""
    if isinstance(block, dict):
        d = block
    elif isinstance(block, BaseModel):
        d = block.model_dump(mode="json")
    else:
        return block
    keys = _invalid_keys_for_block(d.get("type"))
    if not any(k in d for k in keys):
        return block
    return {k: v for k, v in d.items() if k not in keys}


def _sanitize_messages_for_input(messages: list[dict]) -> list[dict]:
    """Strip output-only block fields (`parsed_output` everywhere; `citations` /
    `text` on tool-result blocks) that the Anthropic input schema rejects. Only
    messages with an offending block are rewritten; every other message and every
    clean block passes through verbatim (server-side blocks needed for pause_turn
    replay are preserved untouched)."""
    cleaned: list[dict] = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if not isinstance(content, list):
            cleaned.append(m)
            continue
        new_blocks = [_clean_input_block(b) for b in content]
        if any(nb is not ob for nb, ob in zip(new_blocks, content)):
            new_msg = dict(m)
            new_msg["content"] = new_blocks
            cleaned.append(new_msg)
        else:
            cleaned.append(m)
    return cleaned


# Anthropic's adaptive-thinking efforts. Each maps to a `thinking` block on
# the request. Opus 4.7 supports "xhigh"; other models 400 on it.
_VALID_EFFORTS = {"max", "xhigh", "high", "medium", "low"}


def _build_thinking_param(spec: LLMSpec) -> Optional[dict]:
    """Returns the `thinking` request param when adaptive-thinking is on.
    Anthropic's API wants `{type:"adaptive", display:"summarized"}` and the
    effort knob goes in a separate top-level `output_config` field — see
    `_build_output_config`. `{type:"enabled", effort:...}` is not accepted
    by the API (returns "thinking.enabled.budget_tokens: Field required").
    """
    if not spec.effort or spec.effort not in _VALID_EFFORTS:
        return None
    # Opus 4.7 defaults display to "omitted" which hides thinking from the
    # stream; summarized exposes a summary so UIs can render thinking blocks.
    return {"type": "adaptive", "display": "summarized"}


def _build_output_config(spec: LLMSpec) -> Optional[dict]:
    """Effort knob for adaptive thinking lives on a separate top-level
    `output_config` field (alongside `thinking`)."""
    if not spec.effort or spec.effort not in _VALID_EFFORTS:
        return None
    return {"effort": spec.effort}


def _build_tools_param(spec: LLMSpec, tools: list[Any]) -> list[dict]:
    """`tools` is a list of BaseTool instances (or dicts already in Anthropic
    shape). We call `to_claude_schema()` if available and add cache_control
    on the final tool to enable prompt caching for the tool definitions."""
    out: list[dict] = []
    for t in tools:
        if isinstance(t, dict):
            out.append(t)
        else:
            out.append(t.to_claude_schema())

    # Place the cache breakpoint on the last REGULAR tool. We deliberately do
    # NOT mark the advisor server-tool: it carries its own `caching` field
    # (advisor-side caching, a different mechanism) and is a beta server tool
    # whose schema may reject a `cache_control` key. Anthropic caches the
    # prefix up to and including the breakpoint, so marking the last regular
    # tool caches the entire (large) user-tool array; the advisor tool that
    # follows is tiny and is covered by the system-prompt breakpoint's prefix
    # anyway. (Earlier code placed the breakpoint, THEN appended advisor —
    # functionally the same prefix, but we make the ordering explicit here so
    # the breakpoint always lands on a plain tool.)
    last_regular_idx = len(out) - 1  # last index before any server tool append

    if spec.use_advisor:
        out.append(ADVISOR_TOOL)

    if spec.use_cache and last_regular_idx >= 0:
        out[last_regular_idx] = {
            **out[last_regular_idx],
            "cache_control": {"type": "ephemeral"},
        }
    return out


def _normalize_system(system: Any, use_cache: bool) -> Optional[Any]:
    """Anthropic accepts `system` as a string OR a list of content blocks.
    For caching we need the list form with cache_control on the last block.
    """
    if system is None:
        return None
    if isinstance(system, str):
        if not use_cache:
            return system
        return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if isinstance(system, list) and use_cache:
        # Add cache_control to the last text block if it's not already set.
        out = [dict(b) for b in system]
        for blk in reversed(out):
            if blk.get("type") == "text" and "cache_control" not in blk:
                blk["cache_control"] = {"type": "ephemeral"}
                break
        return out
    return system


def _tools_have_code_execution(tools: Optional[list[Any]]) -> bool:
    """Detect an Anthropic server-side code_execution tool in the tools list.
    Its `type` starts with `code_execution_` (e.g. code_execution_20250825)."""
    if not tools:
        return False
    for t in tools:
        ttype = t.get("type", "") if isinstance(t, dict) else ""
        if ttype.startswith("code_execution"):
            return True
    return False


def _build_betas(spec: LLMSpec, tools: Optional[list[Any]] = None) -> list[str]:
    """Beta headers required by the request. Advisor needs ADVISOR_BETA; a
    code_execution server tool needs CODE_EXECUTION_BETA — without the latter
    Anthropic 400s when the tool is present (this bit workflow execution,
    which passes CODE_EXECUTION_TOOL through the wrapper)."""
    betas: list[str] = []
    if spec.use_advisor:
        betas.append(ADVISOR_BETA)
    if _tools_have_code_execution(tools):
        betas.append(CODE_EXECUTION_BETA)
    if spec.use_1m_context:
        betas.append(CONTEXT_1M_BETA)
    return betas


def _to_usage(raw_usage: Any) -> Usage:
    if raw_usage is None:
        return Usage()
    return Usage(
        input_tokens=getattr(raw_usage, "input_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "output_tokens", 0) or 0,
        cache_creation_input_tokens=getattr(raw_usage, "cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=getattr(raw_usage, "cache_read_input_tokens", 0) or 0,
    )


def _normalize_stop_reason(stop_reason: Optional[str]) -> str:
    if not stop_reason:
        return "end_turn"
    return stop_reason


# ---------------------------------------------------------------------------
# Non-streaming completion.
# ---------------------------------------------------------------------------


async def complete(
    spec: LLMSpec,
    messages: list[dict],
    *,
    system: Any = None,
    tools: Optional[list[Any]] = None,
    force_tool: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout: float = 300.0,
) -> LLMResponse:
    # The shared Anthropic client is module-cached and only honors the
    # timeout passed to its first constructor call. Use with_options() so
    # this per-call timeout actually applies (otherwise short triage calls
    # poison long-running workflow calls and vice versa).
    base_client = get_anthropic_client(timeout=timeout)
    client = base_client.with_options(timeout=timeout)
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        "messages": _sanitize_messages_for_input(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    sys_norm = _normalize_system(system, spec.use_cache)
    if sys_norm is not None:
        kwargs["system"] = sys_norm
    thinking = _build_thinking_param(spec)
    if thinking is not None:
        kwargs["thinking"] = thinking
    output_config = _build_output_config(spec)
    if output_config is not None:
        kwargs["output_config"] = output_config
    if tools:
        kwargs["tools"] = _build_tools_param(spec, tools)
    if force_tool:
        kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

    betas = _build_betas(spec, tools)
    if betas:
        resp = await client.beta.messages.create(betas=betas, **kwargs)
    else:
        resp = await client.messages.create(**kwargs)

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in resp.content:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=getattr(block, "id", ""),
                    name=getattr(block, "name", ""),
                    input=getattr(block, "input", {}) or {},
                )
            )

    return LLMResponse(
        text="".join(text_parts),
        thinking="".join(thinking_parts),
        tool_calls=tool_calls,
        stop_reason=_normalize_stop_reason(getattr(resp, "stop_reason", None)),
        usage=_to_usage(getattr(resp, "usage", None)),
        raw=resp,
    )


# ---------------------------------------------------------------------------
# Stream-collect: streaming under the hood (long-request safe) + return one
# LLMResponse with the full SDK final_message attached as .raw. Use this
# when the caller needs access to Anthropic-specific server-side blocks
# (advisor_tool_result, code_execution_tool_result, server_tool_use) for
# replay — generic event-driven stream_collect drops those because they
# don't map to neutral events.
# ---------------------------------------------------------------------------


async def stream_collect(
    spec: LLMSpec,
    messages: list[dict],
    *,
    system: Any = None,
    tools: Optional[list[Any]] = None,
    force_tool: Optional[str] = None,
    temperature: Optional[float] = None,
    timeout: float = 300.0,
) -> LLMResponse:
    base_client = get_anthropic_client(timeout=timeout)
    client = base_client.with_options(timeout=timeout)
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        "messages": _sanitize_messages_for_input(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    sys_norm = _normalize_system(system, spec.use_cache)
    if sys_norm is not None:
        kwargs["system"] = sys_norm
    thinking = _build_thinking_param(spec)
    if thinking is not None:
        kwargs["thinking"] = thinking
    output_config = _build_output_config(spec)
    if output_config is not None:
        kwargs["output_config"] = output_config
    if tools:
        kwargs["tools"] = _build_tools_param(spec, tools)
    if force_tool:
        kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

    betas = _build_betas(spec, tools)
    stream_ctx = (
        client.beta.messages.stream(betas=betas, **kwargs) if betas
        else client.messages.stream(**kwargs)
    )
    async with stream_ctx as s:
        # Drain the event stream — required for the SDK to assemble the
        # final message. We don't yield anything from this loop; consumers
        # who want incremental events should use stream() directly.
        async for _ in s:
            pass
        final = await s.get_final_message()

    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for block in final.content or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            text_parts.append(getattr(block, "text", "") or "")
        elif btype == "thinking":
            thinking_parts.append(getattr(block, "thinking", "") or "")
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(
                    id=getattr(block, "id", ""),
                    name=getattr(block, "name", ""),
                    input=getattr(block, "input", {}) or {},
                )
            )

    return LLMResponse(
        text="".join(text_parts),
        thinking="".join(thinking_parts),
        tool_calls=tool_calls,
        stop_reason=_normalize_stop_reason(getattr(final, "stop_reason", None)),
        usage=_to_usage(getattr(final, "usage", None)),
        raw=final,  # full SDK Message with all blocks — including server_tool_use,
                    # advisor_tool_result, code_execution_tool_result, etc.
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
    timeout: float = 300.0,
) -> AsyncIterator[Event]:
    # See `complete()` above — the cached client only respects the timeout
    # from its first call, so per-stream timeout must use with_options().
    base_client = get_anthropic_client(timeout=timeout)
    client = base_client.with_options(timeout=timeout)
    kwargs: dict[str, Any] = {
        "model": spec.model,
        "max_tokens": spec.max_tokens,
        "messages": _sanitize_messages_for_input(messages),
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    sys_norm = _normalize_system(system, spec.use_cache)
    if sys_norm is not None:
        kwargs["system"] = sys_norm
    thinking = _build_thinking_param(spec)
    if thinking is not None:
        kwargs["thinking"] = thinking
    output_config = _build_output_config(spec)
    if output_config is not None:
        kwargs["output_config"] = output_config
    if tools:
        kwargs["tools"] = _build_tools_param(spec, tools)
    if force_tool:
        kwargs["tool_choice"] = {"type": "tool", "name": force_tool}

    betas = _build_betas(spec, tools)

    # Track partial tool calls by content-block index so we can emit
    # ToolCallEnd with the parsed JSON once input_json_delta finishes.
    pending_tool: dict[int, dict[str, Any]] = {}
    final_usage = Usage()
    emitted_stop = False  # ensures exactly one MessageStop regardless of which
                          # terminal event the SDK sends (message_delta vs message_stop)

    stream_ctx = (
        client.beta.messages.stream(betas=betas, **kwargs)
        if betas
        else client.messages.stream(**kwargs)
    )
    # Note: exceptions (RateLimitError, APIError, etc) propagate to the
    # caller. The wrapper used to convert them into StreamError events,
    # but that swallowed the type and made retry-by-exception-class
    # impossible at the caller (workflow_execution_service).
    async with stream_ctx as s:
        async for event in s:
            etype = getattr(event, "type", "")
            if etype == "content_block_start":
                block = event.content_block
                btype = getattr(block, "type", "")
                idx = event.index
                if btype == "tool_use":
                    pending_tool[idx] = {
                        "id": getattr(block, "id", ""),
                        "name": getattr(block, "name", ""),
                        "args": "",
                    }
                    yield ToolCallStart(
                        id=pending_tool[idx]["id"],
                        name=pending_tool[idx]["name"],
                        index=idx,
                    )
            elif etype == "content_block_delta":
                delta = event.delta
                dtype = getattr(delta, "type", "")
                idx = event.index
                if dtype == "text_delta":
                    yield TextDelta(text=getattr(delta, "text", "") or "", index=idx)
                elif dtype == "thinking_delta":
                    yield ThinkingDelta(
                        thinking=getattr(delta, "thinking", "") or "",
                        index=idx,
                    )
                elif dtype == "input_json_delta":
                    chunk = getattr(delta, "partial_json", "") or ""
                    if idx in pending_tool:
                        pending_tool[idx]["args"] += chunk
                    yield ToolCallArgsDelta(args_delta=chunk, index=idx)
            elif etype == "content_block_stop":
                idx = event.index
                if idx in pending_tool:
                    pt = pending_tool.pop(idx)
                    try:
                        parsed = json.loads(pt["args"]) if pt["args"] else {}
                    except json.JSONDecodeError as exc:
                        # Surface the parse failure with tool context. Returning
                        # `{}` silently lets downstream tool execution proceed
                        # with empty input — usually fails the call, but the
                        # root cause (truncated/malformed args stream) is
                        # invisible without this log.
                        #
                        # NOTE: `_logger` is a stdlib logging.Logger, NOT
                        # structlog — passing kwargs directly to .warning()
                        # raises TypeError and would convert this benign
                        # parse failure into a stream-killing exception. Use
                        # %-formatting in the message instead.
                        _logger.warning(
                            "tool_call_args_json_decode_failed tool_id=%s tool_name=%s "
                            "args_len=%d args_preview=%r error=%s",
                            pt["id"],
                            pt["name"],
                            len(pt["args"]),
                            pt["args"][:200],
                            str(exc),
                        )
                        parsed = {}
                    yield ToolCallEnd(
                        id=pt["id"],
                        name=pt["name"],
                        input=parsed,
                        index=idx,
                    )
            elif etype == "message_delta":
                # Usage updates arrive on message_delta in the stream API.
                usage_obj = getattr(event, "usage", None)
                if usage_obj is not None:
                    final_usage = _to_usage(usage_obj)
                stop = getattr(event.delta, "stop_reason", None)
                if stop and not emitted_stop:
                    yield Usage(
                        input_tokens=final_usage.input_tokens,
                        output_tokens=final_usage.output_tokens,
                        cache_creation_input_tokens=final_usage.cache_creation_input_tokens,
                        cache_read_input_tokens=final_usage.cache_read_input_tokens,
                    )
                    yield MessageStop(stop_reason=_normalize_stop_reason(stop))
                    emitted_stop = True
            elif etype == "message_stop":
                # Some SDK variants emit message_stop without a preceding
                # message_delta carrying stop_reason. ALWAYS emit MessageStop
                # here if we haven't yet — consumers block forever waiting for
                # a terminal event otherwise. The Usage block is best-effort
                # (may be empty if the SDK never surfaced usage); the terminal
                # MessageStop is non-negotiable.
                if not emitted_stop:
                    yield Usage(
                        input_tokens=final_usage.input_tokens,
                        output_tokens=final_usage.output_tokens,
                        cache_creation_input_tokens=final_usage.cache_creation_input_tokens,
                        cache_read_input_tokens=final_usage.cache_read_input_tokens,
                    )
                    yield MessageStop(stop_reason="end_turn")
                    emitted_stop = True
