"""OpenAI-compatible adapter — used for ``provider=openai`` and
``provider=kimi``.

Kimi's API is wire-compatible with OpenAI's chat completions; the only
difference is `base_url` + `api_key`. Both go through this adapter; the
client factory picks the right (base_url, key) pair from env.

The job of this adapter is to:
  1. Convert neutral (anthropic-shaped) messages → OpenAI messages.
  2. Convert tools (BaseTool or anthropic-shaped dict) → OpenAI function
     tools.
  3. Convert OpenAI streaming chunks → neutral events.
  4. Convert OpenAI non-streaming responses → neutral LLMResponse.

Anthropic-only knobs (advisor, prompt caching, extended thinking blocks)
are silently dropped — they're features Anthropic offers and OpenAI/Kimi
don't.
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
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


def _extra_field(obj: Any, name: str) -> Any:
    """Read a provider-specific extra field off a pydantic model from the
    OpenAI SDK. Kimi sends `reasoning_content` (not in the SDK's typed
    delta), and pydantic v2 stores it on `model_extra` when extras are
    allowed. We try attribute access first, then model_extra dict."""
    val = getattr(obj, name, None)
    if val is not None:
        return val
    extra = getattr(obj, "model_extra", None)
    if isinstance(extra, dict):
        return extra.get(name)
    return None

_logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Client factory — one async client per (provider, base_url, key) tuple.
# Cached at module scope so we keep TCP/connection pools warm.
# ---------------------------------------------------------------------------

_clients: dict[str, Any] = {}


def _get_client(provider: str):
    if provider in _clients:
        return _clients[provider]
    try:
        from openai import AsyncOpenAI  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "openai package not installed. Run `poetry add openai`."
        ) from exc

    if provider == "openai":
        api_key = os.environ.get("OPENAI_API_KEY", "")
        base_url = os.environ.get("OPENAI_BASE_URL") or None
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    elif provider == "kimi":
        api_key = os.environ.get("KIMI_API_KEY", "")
        base_url = os.environ.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1")
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    else:
        raise ValueError(f"OpenAI-compatible adapter does not handle provider={provider!r}")

    _clients[provider] = client
    return client


# ---------------------------------------------------------------------------
# Conversion helpers.
# ---------------------------------------------------------------------------


def _normalize_content_to_openai(content: Any) -> Any:
    """Convert Anthropic content-block list → OpenAI message content.

    OpenAI accepts:
      - plain string
      - list of parts: {"type":"text","text":...} | {"type":"image_url",...}

    Tool-use/tool_result blocks live on DIFFERENT roles in OpenAI
    (`assistant.tool_calls` and `role="tool"`), so this helper only handles
    the text/image side. Tool-use/result blocks are unpacked one level up
    in `_normalize_messages`.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    parts: list[dict] = []
    for blk in content:
        btype = blk.get("type")
        if btype == "text":
            parts.append({"type": "text", "text": blk.get("text", "")})
        elif btype == "image":
            src = blk.get("source", {})
            if src.get("type") == "base64":
                media = src.get("media_type", "image/png")
                data = src.get("data", "")
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{data}"},
                    }
                )
            elif src.get("type") == "url":
                parts.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": src.get("url", "")},
                    }
                )
        # thinking blocks dropped — provider doesn't understand them.
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts or ""


def _normalize_messages(messages: list[dict]) -> list[dict]:
    """Walk Anthropic-shaped messages and emit the OpenAI-shaped equivalent.

    Critical conversions:
      - assistant message with tool_use blocks → assistant with `tool_calls`
        (no content, OR text content + tool_calls)
      - user message with tool_result blocks → role="tool" messages
        (one per tool_result), with the rest of the user content emitted as
        a separate user message if any text/images remain.
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "assistant" and isinstance(content, list):
            text_parts: list[dict] = []
            tool_calls: list[dict] = []
            thinking_parts: list[str] = []
            for blk in content:
                btype = blk.get("type")
                if btype == "text":
                    text_parts.append({"type": "text", "text": blk.get("text", "")})
                elif btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": blk.get("id", ""),
                            "type": "function",
                            "function": {
                                "name": blk.get("name", ""),
                                "arguments": json.dumps(blk.get("input", {})),
                            },
                        }
                    )
                elif btype == "thinking":
                    thinking_parts.append(blk.get("thinking", ""))
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            if text_parts:
                assistant_msg["content"] = (
                    text_parts[0]["text"] if len(text_parts) == 1 else text_parts
                )
            else:
                assistant_msg["content"] = None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            if thinking_parts:
                # Kimi (and OpenAI's o-series via Responses API) preserve
                # reasoning content on the assistant message. Setting this
                # is required by Kimi-k2.6/k2-thinking when the conversation
                # is replayed back with prior tool calls — without it the
                # API 400s with "thinking is enabled but reasoning_content
                # is missing". Harmless on other models that ignore it.
                assistant_msg["reasoning_content"] = "".join(thinking_parts)
            out.append(assistant_msg)
            continue

        if role == "user" and isinstance(content, list):
            non_tool_blocks: list[dict] = []
            tool_result_blocks: list[dict] = []
            for blk in content:
                if blk.get("type") == "tool_result":
                    tool_result_blocks.append(blk)
                else:
                    non_tool_blocks.append(blk)
            if non_tool_blocks:
                out.append(
                    {
                        "role": "user",
                        "content": _normalize_content_to_openai(non_tool_blocks),
                    }
                )
            for tr in tool_result_blocks:
                tr_content = tr.get("content", "")
                if isinstance(tr_content, list):
                    # OpenAI tool message content is a plain string. Collapse
                    # any list of text blocks into one string. Images inside
                    # tool_result aren't allowed by OpenAI — drop them.
                    parts = []
                    for blk in tr_content:
                        if blk.get("type") == "text":
                            parts.append(blk.get("text", ""))
                    tr_content = "\n".join(parts)
                elif not isinstance(tr_content, str):
                    tr_content = json.dumps(tr_content)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": tr_content or "",
                    }
                )
            continue

        # Plain string content or system role — pass straight through.
        out.append(
            {
                "role": role,
                "content": _normalize_content_to_openai(content)
                if isinstance(content, list)
                else content,
            }
        )
    return out


def _normalize_tools(tools: list[Any]) -> list[dict]:
    """Convert BaseTool / anthropic-shaped tool dicts → OpenAI function tools."""
    out: list[dict] = []
    for t in tools:
        if isinstance(t, dict):
            # Already in some shape — check which.
            if t.get("type") == "function" and "function" in t:
                out.append(t)
                continue
            # Anthropic shape {"name":..., "description":..., "input_schema":...}
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.get("name", ""),
                        "description": t.get("description", ""),
                        "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
                    },
                }
            )
        else:
            out.append(
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
            )
    return out


def _model_supports_reasoning_effort(model: str) -> bool:
    """OpenAI's reasoning_effort param is currently only supported on the
    o-series and gpt-5-class reasoning models. Sending it to gpt-4o / 4.1
    returns a 400."""
    model = model.lower()
    return (
        model.startswith("o1")
        or model.startswith("o3")
        or model.startswith("o4")
        or model.startswith("gpt-5")
    )


# Reasoning models emit hidden reasoning tokens that count against max_tokens.
# A tiny cap (e.g. an 8-token YES/NO triage, fine on a non-reasoning model)
# gets fully consumed by reasoning, leaving EMPTY visible output — which
# silently broke email-draft triage on Kimi (every email → "NO"). We enforce
# a floor so the visible answer always has room after the reasoning. Applies
# to Kimi's K2 reasoning line and OpenAI's o-series / gpt-5 reasoning models;
# NOT to moonshot-v1 (non-reasoning) or gpt-4.x.
_REASONING_MIN_OUTPUT_TOKENS = 1024


def _is_reasoning_model(provider: str, model: str) -> bool:
    m = (model or "").lower()
    if provider == "kimi":
        return m.startswith("kimi-k2")  # k2.5 / k2.6 / k2-thinking reason; moonshot-v1 doesn't
    if provider == "openai":
        return _model_supports_reasoning_effort(m)
    return False


def _build_kwargs(
    spec: LLMSpec,
    messages: list[dict],
    system: Any,
    tools: Optional[list[Any]],
    force_tool: Optional[str] = None,
    temperature: Optional[float] = None,
) -> dict[str, Any]:
    norm_messages = _normalize_messages(messages)
    if system is not None:
        # System normalization: collapse anthropic-list-shape to a single
        # string. OpenAI doesn't have a separate cache_control concept here.
        if isinstance(system, list):
            parts = [
                b.get("text", "") for b in system if b.get("type") == "text"
            ]
            system_str = "".join(parts)
        else:
            system_str = str(system)
        norm_messages = [{"role": "system", "content": system_str}] + norm_messages

    # Whether the model will actually emit hidden reasoning on this call.
    # `disable_reasoning` only genuinely turns reasoning OFF on Kimi (sent below
    # as thinking:disabled) — there we keep the caller's small max_tokens (cheap,
    # fast classification). OpenAI reasoning models (o-series / gpt-5) ALWAYS
    # reason regardless of the flag, so the output-token floor must still apply
    # to them or the reasoning trace eats the whole budget and the answer
    # truncates. So only honor disable_reasoning for Kimi.
    reasoning_active = _is_reasoning_model(spec.provider, spec.model) and not (
        spec.provider == "kimi" and spec.disable_reasoning
    )
    effective_max_tokens = spec.max_tokens
    if reasoning_active and effective_max_tokens < _REASONING_MIN_OUTPUT_TOKENS:
        _logger.debug(
            "raising max_tokens floor for reasoning model",
            model=spec.model, requested=spec.max_tokens, floored_to=_REASONING_MIN_OUTPUT_TOKENS,
        ) if hasattr(_logger, "debug") else None
        effective_max_tokens = _REASONING_MIN_OUTPUT_TOKENS

    kwargs: dict[str, Any] = {
        "model": spec.model,
        "messages": norm_messages,
        "max_tokens": effective_max_tokens,
    }
    if temperature is not None:
        # OpenAI o-series and gpt-5-class reasoning models reject `temperature`
        # with a 400 ("'temperature' is not supported with this model"). Silently
        # skip it for those — the model already self-regulates via reasoning.
        skip_temperature = False
        if spec.provider == "openai" and _model_supports_reasoning_effort(spec.model):
            skip_temperature = True
        elif spec.provider == "kimi":
            # Kimi PINS temperature per-model: kimi-k2.5 accepts only `1`,
            # kimi-k2-thinking only `0.6`, etc., and 400s on anything else
            # ("invalid temperature: only N is allowed for this model"). We can't
            # know each model's allowed value, so omit temperature and let Kimi
            # use its model default. (This broke a Haiku-class classifier after the
            # Kimi cutover: temperature=0 -> 400 -> caught -> always OTHER ->
            # "I didn't catch that" on every edit instruction.)
            skip_temperature = True
        if skip_temperature:
            _logger.debug(
                "temperature_skipped",
                provider=spec.provider, model=spec.model, temperature=temperature,
            ) if hasattr(_logger, "debug") else None
        else:
            kwargs["temperature"] = temperature

    if tools:
        kwargs["tools"] = _normalize_tools(tools)
        # `auto` is the OpenAI default and matches Anthropic's behavior.
        if force_tool:
            kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": force_tool},
            }

    if spec.provider == "openai":
        effort = map_effort_to_openai(spec.effort)
        if effort and _model_supports_reasoning_effort(spec.model):
            kwargs["reasoning_effort"] = effort
            # Reasoning models on OpenAI use `max_completion_tokens` and
            # reject `max_tokens` on some endpoints — be defensive. Use the
            # floored value so reasoning doesn't consume the whole budget.
            kwargs.pop("max_tokens", None)
            kwargs["max_completion_tokens"] = effective_max_tokens
    elif spec.provider == "kimi":
        # Kimi doesn't expose reasoning_effort; pick the *-thinking model
        # for reasoning. Warn once if the spec set effort and we're on Kimi.
        if spec.effort:
            _logger.debug(
                "kimi_effort_ignored: spec.effort=%r will not be applied (Kimi has no reasoning_effort param)",
                spec.effort,
            )
        # Turn off Kimi's intrinsic reasoning for trivial classification jobs.
        # `thinking` isn't a standard OpenAI param, so it goes via extra_body,
        # which the SDK forwards into the request JSON. Applies to the K2 line
        # (k2.5/k2.6); harmless on moonshot-v1 (non-reasoning, ignores it).
        if spec.disable_reasoning:
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}

    return kwargs


def _to_usage(raw_usage: Any) -> Usage:
    if raw_usage is None:
        return Usage()
    cache_read = 0
    details = getattr(raw_usage, "prompt_tokens_details", None)
    if details is not None:
        cache_read = getattr(details, "cached_tokens", 0) or 0
    return Usage(
        input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
        output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
        cache_read_input_tokens=cache_read,
    )


def _normalize_finish_reason(reason: Optional[str]) -> str:
    """OpenAI: stop | length | tool_calls | content_filter.
    Map to neutral: end_turn | max_tokens | tool_use | refusal."""
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "refusal",
    }
    return mapping.get(reason or "", reason or "end_turn")


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
    timeout: float = 300.0,
) -> LLMResponse:
    client = _get_client(spec.provider)
    kwargs = _build_kwargs(spec, messages, system, tools, force_tool=force_tool, temperature=temperature)

    resp = await client.with_options(timeout=timeout).chat.completions.create(**kwargs)

    choice = resp.choices[0]
    msg = choice.message
    text = msg.content or ""
    thinking = _extra_field(msg, "reasoning_content") or ""

    tool_calls: list[ToolCall] = []
    for tc in msg.tool_calls or []:
        try:
            input_dict = json.loads(tc.function.arguments) if tc.function.arguments else {}
        except json.JSONDecodeError:
            input_dict = {}
        tool_calls.append(
            ToolCall(
                id=tc.id,
                name=tc.function.name,
                input=input_dict,
            )
        )

    return LLMResponse(
        text=text,
        thinking=thinking,
        tool_calls=tool_calls,
        stop_reason=_normalize_finish_reason(choice.finish_reason),
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
    timeout: float = 300.0,
) -> AsyncIterator[Event]:
    client = _get_client(spec.provider)
    kwargs = _build_kwargs(spec, messages, system, tools, force_tool=force_tool, temperature=temperature)
    kwargs["stream"] = True
    kwargs["stream_options"] = {"include_usage": True}

    # Track tool calls by their stream index — OpenAI emits incremental
    # `tool_calls[i]` deltas under each chunk, identified by index, not id.
    started: dict[int, str] = {}  # index -> tool_call_id
    tool_names: dict[int, str] = {}
    accumulated_args: dict[int, str] = {}
    final_usage = Usage()
    finish_seen: Optional[str] = None

    # Exceptions propagate to the caller (workflow_execution etc need to
    # catch RateLimitError by type for retry logic).
    stream_resp = await client.with_options(timeout=timeout).chat.completions.create(**kwargs)
    async for chunk in stream_resp:
        # The last chunk under stream_options.include_usage has usage
        # and an empty choices list.
        if getattr(chunk, "usage", None) is not None:
            final_usage = _to_usage(chunk.usage)

        for choice in chunk.choices or []:
            delta = choice.delta
            # Kimi (and some OpenAI-compatible providers) stream
            # extended-thinking content as a separate `reasoning_content`
            # field on the delta. Yield it as ThinkingDelta so callers
            # can render it and so it can be re-attached to assistant
            # messages on the next turn for providers that require it.
            reasoning = _extra_field(delta, "reasoning_content")
            if reasoning:
                yield ThinkingDelta(thinking=reasoning, index=0)
            # Text delta.
            if getattr(delta, "content", None):
                yield TextDelta(text=delta.content, index=0)

            # Tool-call deltas.
            tcs = getattr(delta, "tool_calls", None) or []
            for tc in tcs:
                idx = tc.index
                if idx not in started:
                    # Use an unambiguous sentinel for the not-yet-known id.
                    # NOTE: OpenAI's real tool-call ids look like "call_abc123",
                    # so a "call_{idx}" placeholder would collide and we could
                    # never tell placeholder from real id. The sentinel below
                    # cannot appear as a real id.
                    tc_id = tc.id or f"__pending_tc_{idx}__"
                    started[idx] = tc_id
                    tool_names[idx] = (tc.function.name if tc.function else "") or ""
                    accumulated_args[idx] = ""
                    yield ToolCallStart(
                        id=tc_id,
                        name=tool_names[idx],
                        index=idx,
                    )
                # OpenAI sometimes sends the real id/name only in a later delta.
                # Replace the sentinel as soon as a real id arrives. (Previously
                # the condition was inverted — `not startswith("call_")` — so the
                # placeholder was never replaced and leaked into ToolCallEnd +
                # replay.)
                if tc.id and started[idx].startswith("__pending_tc_"):
                    started[idx] = tc.id
                if tc.function and tc.function.name and not tool_names[idx]:
                    tool_names[idx] = tc.function.name
                if tc.function and tc.function.arguments:
                    accumulated_args[idx] += tc.function.arguments
                    yield ToolCallArgsDelta(
                        args_delta=tc.function.arguments,
                        index=idx,
                    )

            if choice.finish_reason and finish_seen is None:
                finish_seen = choice.finish_reason
                # Emit ToolCallEnd for every pending tool call (once). Do NOT
                # return here: OpenAI/Kimi send token usage in a SEPARATE final
                # chunk that arrives AFTER finish_reason (with empty `choices`),
                # so returning now would emit Usage with 0 tokens — which is why
                # streamed (chat) turns never logged usage. Keep draining the
                # stream so the trailing usage chunk populates `final_usage`,
                # then emit Usage + MessageStop once after the loop.
                for idx in sorted(started.keys()):
                    try:
                        parsed = (
                            json.loads(accumulated_args[idx])
                            if accumulated_args[idx]
                            else {}
                        )
                    except json.JSONDecodeError:
                        parsed = {}
                    yield ToolCallEnd(
                        id=started[idx],
                        name=tool_names[idx],
                        input=parsed,
                        index=idx,
                    )

    # Stream exhausted. `final_usage` is now populated from the trailing
    # usage-only chunk (empty choices, usage set), so these counts are accurate.
    yield Usage(
        input_tokens=final_usage.input_tokens,
        output_tokens=final_usage.output_tokens,
        cache_read_input_tokens=final_usage.cache_read_input_tokens,
        cache_creation_input_tokens=final_usage.cache_creation_input_tokens,
    )
    yield MessageStop(stop_reason=_normalize_finish_reason(finish_seen or "stop"))
