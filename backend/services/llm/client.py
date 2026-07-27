"""LLMClient — top-level dispatcher across providers.

Usage:

    from services.llm import llm, get_spec

    spec = get_spec("chat")

    # non-streaming
    resp = await llm.complete(spec, messages=[...], system=..., tools=[...])
    print(resp.text)

    # streaming
    async for event in llm.stream(spec, messages=[...]):
        match event.type:
            case "text_delta": ...
            case "tool_call_end": ...
            case "message_stop": break
"""

from __future__ import annotations

import asyncio
import random
import time
from functools import partial
from typing import Any, AsyncIterator, Optional

import structlog

from config.llm_registry import LLMSpec, get_spec

from services.llm import (
    anthropic_adapter,
    openai_compatible_adapter,
    openai_responses_adapter,
)

_logger = structlog.get_logger()
from services.llm.types import (
    Event,
    LLMResponse,
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCall,
    ToolCallEnd,
    Usage,
)


def _use_openai_responses(spec: LLMSpec) -> bool:
    """OpenAI's Responses API is required for server-side code_interpreter
    and for streamed reasoning_summary. When neither is requested, the older
    Chat Completions path is cheaper and supports more models (gpt-4.1 etc).
    """
    return spec.provider == "openai" and (
        spec.use_code_interpreter or spec.use_reasoning_summary
    )


# ---------------------------------------------------------------------------
# Transient-error retry
# ---------------------------------------------------------------------------
# Providers throw transient failures under load: Kimi/Moonshot returns
# `429 engine_overloaded_error`, Anthropic `529 overloaded`, OpenAI 429/5xx,
# plus request timeouts. Without a retry these bubble up and silently degrade
# non-interactive features — e.g. a failed title gen leaves a NULL conversation
# title, so the chat renders nameless and looks "deleted". Retry with backoff
# wraps complete() and stream_collect(); it is NOT applied to stream() because
# retrying mid-stream would replay tokens already sent to the client.

_RETRY_MAX_ATTEMPTS = 4          # 1 initial attempt + 3 retries
_RETRY_BASE_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 30.0
# 520-524 are Cloudflare-origin 5xx (520 unknown, 521 web server down, 522
# connection timed out, 523 origin unreachable, 524 a timeout occurred). They
# front the Anthropic/Kimi API and surface as transient origin hiccups — a
# workflow run (e375b030) failed outright on a bare 521 because it wasn't here.
_RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524, 529}
_RETRYABLE_SUBSTRINGS = (
    "engine_overloaded", "overloaded", "rate_limit", "rate limit",
    "timed out", "timeout", "temporarily unavailable", "service unavailable",
    "internal server error", "try again later",
    # Transient stream-truncation / connection-drop errors. These surface mid- or
    # end-of-stream on long requests (esp. large Anthropic 1M-context workflow
    # rounds) when an intermediary closes the connection before the body is fully
    # delivered. They carry no retryable status code (the TransferEncodingError we
    # saw was tagged 400), so they must match by message. Safe to retry here:
    # _with_llm_retry only wraps complete() + stream_collect(), both of which
    # buffer the full response internally, so a re-run replays nothing to a client.
    "payload is not completed",          # aiohttp ClientPayloadError / the alert text
    "not enough data to satisfy",        # TransferEncodingError detail
    "transfer length",
    "transferencoding",
    "server disconnected",               # aiohttp ServerDisconnectedError
    "connection reset", "connection aborted", "connection closed",
    "incomplete read", "incomplete chunked read",
    "peer closed connection", "remoteprotocolerror",  # httpx RemoteProtocolError
)


def _is_retryable_llm_error(exc: Exception) -> bool:
    # SDK APIStatusError subclasses carry a numeric .status_code. Use try/except
    # (not getattr — banned in this repo) to read it safely.
    try:
        code = exc.status_code
    except AttributeError:
        code = None
    if isinstance(code, int) and code in _RETRYABLE_STATUS:
        return True
    msg = str(exc).lower()
    return any(s in msg for s in _RETRYABLE_SUBSTRINGS)


def is_transient_error(exc: Exception) -> bool:
    """Public predicate: True if `exc` is a transient provider failure (overload /
    429 / 5xx / timeout / stream-truncation) — the same classification the retry
    loop uses. Callers (e.g. the workflow executor) use it to decide whether to
    PAUSE-and-resume rather than hard-fail when the in-call retries are exhausted."""
    return _is_retryable_llm_error(exc)


def _retry_after_seconds(exc: Exception) -> float | None:
    """Honor a `Retry-After` header (seconds) when the provider supplies one
    (Anthropic does on 429 TPM-cap errors). try/except instead of getattr to
    comply with the repo's no-getattr rule. None → caller uses backoff."""
    try:
        response = exc.response
    except AttributeError:
        return None
    if response is None:
        return None
    try:
        headers = response.headers or {}
    except AttributeError:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


async def _with_llm_retry(
    call,
    *,
    label: str,
    max_attempts: int = _RETRY_MAX_ATTEMPTS,
    max_delay: float = _RETRY_MAX_DELAY_S,
    max_total_seconds: Optional[float] = None,
):
    """Await `call()` (a 0-arg callable — pass a functools.partial), retrying
    transient provider errors. This is the SINGLE owner of LLM transient-retry —
    callers must NOT wrap their own retry loop around llm.complete/stream_collect
    (double-stacking turns one 429 into an exponential fan-out and ignores
    Retry-After on the inner attempts). Honors Retry-After when present, else
    exponential backoff + jitter. Non-transient errors raise immediately.

    `max_attempts` / `max_delay` default to the chat-tuned budget (fast give-up so
    an interactive user isn't left hanging). Non-interactive callers (workflow /
    trigger-agent rounds) pass a larger budget to ride out short provider
    overloads in-call instead of bubbling up.

    `max_total_seconds` caps the WALL-CLOCK spent across all attempts (including
    the time burned inside each failed attempt). This bounds the worst case:
    without it, N attempts of a slow call that times out at the per-request
    timeout (e.g. 7 × 900s) could blow a caller's own runaway cap. With it, once
    elapsed + next backoff would exceed the budget we stop retrying — so a single
    long timeout effectively ends the retries while many instant 429s still get
    the full attempt count."""
    started = time.monotonic()
    for attempt in range(max_attempts):
        try:
            return await call()
        except Exception as e:
            if attempt == max_attempts - 1 or not _is_retryable_llm_error(e):
                raise
            delay = _retry_after_seconds(e)
            if delay is None:
                delay = min(_RETRY_BASE_DELAY_S * (2 ** attempt), max_delay) + random.uniform(0, 0.5)
            if max_total_seconds is not None and (time.monotonic() - started) + delay >= max_total_seconds:
                _logger.warning(
                    "llm transient error — retry time budget exhausted, giving up",
                    label=label, attempt=attempt + 1,
                    elapsed_s=round(time.monotonic() - started, 1), budget_s=max_total_seconds,
                )
                raise
            _logger.warning(
                "llm transient error — retrying",
                label=label, attempt=attempt + 1, max_attempts=max_attempts,
                error=str(e)[:180], delay_s=round(delay, 2),
            )
            await asyncio.sleep(delay)


async def _dispatch_complete(
    spec: LLMSpec, messages: list[dict], *, system, tools, force_tool, temperature, timeout,
) -> "LLMResponse":
    if spec.provider == "anthropic":
        return await anthropic_adapter.complete(
            spec, messages, system=system, tools=tools,
            force_tool=force_tool, temperature=temperature, timeout=timeout,
        )
    if _use_openai_responses(spec):
        return await openai_responses_adapter.complete(
            spec, messages, system=system, tools=tools,
            force_tool=force_tool, temperature=temperature,
            enable_code_interpreter=spec.use_code_interpreter, timeout=timeout,
        )
    if spec.provider in ("openai", "kimi"):
        return await openai_compatible_adapter.complete(
            spec, messages, system=system, tools=tools,
            force_tool=force_tool, temperature=temperature, timeout=timeout,
        )
    raise ValueError(f"Unsupported provider: {spec.provider!r}")


async def _stream_collect_impl(
    spec: LLMSpec, messages: list[dict], *, system, tools, force_tool, temperature, timeout,
) -> "LLMResponse":
    if spec.provider == "anthropic":
        return await anthropic_adapter.stream_collect(
            spec, messages, system=system, tools=tools,
            force_tool=force_tool, temperature=temperature, timeout=timeout,
        )
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    stop_reason = "end_turn"
    usage = Usage()
    error: Optional[str] = None
    reasoning_id: Optional[str] = None
    async for ev in llm.stream(
        spec, messages, system=system, tools=tools,
        force_tool=force_tool, temperature=temperature, timeout=timeout,
    ):
        if isinstance(ev, TextDelta):
            text_parts.append(ev.text)
        elif isinstance(ev, ThinkingDelta):
            thinking_parts.append(ev.thinking)
            if ev.reasoning_id:
                reasoning_id = ev.reasoning_id
        elif isinstance(ev, ToolCallEnd):
            tool_calls.append(ToolCall(id=ev.id, name=ev.name, input=ev.input))
        elif isinstance(ev, Usage):
            usage = ev
        elif isinstance(ev, MessageStop):
            stop_reason = ev.stop_reason
        elif isinstance(ev, StreamError):
            error = ev.message
            break
    if error is not None:
        # Surfaced as RuntimeError; its message carries the provider error text
        # so _is_retryable_llm_error can still classify it (e.g. "overloaded").
        raise RuntimeError(f"stream_collect failed: {error}")
    return LLMResponse(
        text="".join(text_parts),
        thinking="".join(thinking_parts),
        reasoning_id=reasoning_id,
        tool_calls=tool_calls,
        stop_reason=stop_reason,
        usage=usage,
    )


class LLMClient:
    async def complete(
        self,
        spec: LLMSpec,
        messages: list[dict],
        *,
        system: Any = None,
        tools: Optional[list[Any]] = None,
        force_tool: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: float = 300.0,
        retry_max_attempts: int = _RETRY_MAX_ATTEMPTS,
        retry_max_delay: float = _RETRY_MAX_DELAY_S,
        retry_max_total_seconds: Optional[float] = None,
    ) -> LLMResponse:
        return await _with_llm_retry(
            partial(
                _dispatch_complete, spec, messages,
                system=system, tools=tools, force_tool=force_tool,
                temperature=temperature, timeout=timeout,
            ),
            label=f"complete[{spec.provider}/{spec.model}]",
            max_attempts=retry_max_attempts,
            max_delay=retry_max_delay,
            max_total_seconds=retry_max_total_seconds,
        )

    async def stream(
        self,
        spec: LLMSpec,
        messages: list[dict],
        *,
        system: Any = None,
        tools: Optional[list[Any]] = None,
        force_tool: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: float = 300.0,
    ) -> AsyncIterator[Event]:
        if spec.provider == "anthropic":
            it = anthropic_adapter.stream(
                spec, messages, system=system, tools=tools,
                force_tool=force_tool, temperature=temperature, timeout=timeout,
            )
        elif _use_openai_responses(spec):
            it = openai_responses_adapter.stream(
                spec, messages, system=system, tools=tools,
                force_tool=force_tool, temperature=temperature,
                enable_code_interpreter=spec.use_code_interpreter,
                timeout=timeout,
            )
        elif spec.provider in ("openai", "kimi"):
            it = openai_compatible_adapter.stream(
                spec, messages, system=system, tools=tools,
                force_tool=force_tool, temperature=temperature, timeout=timeout,
            )
        else:
            raise ValueError(f"Unsupported provider: {spec.provider!r}")
        async for ev in it:
            yield ev


    async def stream_collect(
        self,
        spec: LLMSpec,
        messages: list[dict],
        *,
        system: Any = None,
        tools: Optional[list[Any]] = None,
        force_tool: Optional[str] = None,
        temperature: Optional[float] = None,
        timeout: float = 300.0,
        retry_max_attempts: int = _RETRY_MAX_ATTEMPTS,
        retry_max_delay: float = _RETRY_MAX_DELAY_S,
        retry_max_total_seconds: Optional[float] = None,
    ) -> LLMResponse:
        """Stream the response and collect into a single LLMResponse.

        Use this in non-interactive contexts where you don't need token-by-token
        updates but DO want to avoid Anthropic's long-request timeouts (workflow
        executions, trigger-agent rounds). The wire-level connection stays open
        via stream events even though the caller only sees the final result.

        On Anthropic, this dispatches to the adapter's native stream_collect
        which preserves the full SDK final_message (including server-side
        blocks: advisor_tool_result, code_execution_tool_result, etc) on
        `LLMResponse.raw`. Workflow execution + chat replay rely on those
        blocks to keep `pause_turn` continuations valid — without them the
        next round 400s.
        """
        return await _with_llm_retry(
            partial(
                _stream_collect_impl, spec, messages,
                system=system, tools=tools, force_tool=force_tool,
                temperature=temperature, timeout=timeout,
            ),
            label=f"stream_collect[{spec.provider}/{spec.model}]",
            max_attempts=retry_max_attempts,
            max_delay=retry_max_delay,
            max_total_seconds=retry_max_total_seconds,
        )


llm = LLMClient()

__all__ = ["LLMClient", "llm", "get_spec"]
