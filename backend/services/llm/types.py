"""Provider-neutral LLM types.

The neutral shape mirrors Anthropic's content-block model because most of
Gideon's call sites already build messages in that shape. The OpenAI/Kimi
adapter converts at the SDK edge.

Content blocks supported by callers:
  {"type": "text", "text": str}
  {"type": "thinking", "thinking": str}             # anthropic-only on input
  {"type": "tool_use", "id": str, "name": str, "input": dict}
  {"type": "tool_result", "tool_use_id": str, "content": str | list, "is_error": bool}
  {"type": "image", "source": {"type": "base64", "media_type": str, "data": str}}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union


# ---------------------------------------------------------------------------
# Streaming events — emitted by LLMClient.stream(). Consumers pattern-match
# on the `type` field so dispatch doesn't depend on isinstance checks.
# ---------------------------------------------------------------------------


@dataclass
class TextDelta:
    """A chunk of assistant text. `index` identifies the content block."""

    text: str
    index: int = 0
    type: Literal["text_delta"] = "text_delta"


@dataclass
class ThinkingDelta:
    """A chunk of extended-thinking text. Emitted by:
      - Anthropic adapter: streamed `thinking_delta` events.
      - OpenAI Responses adapter: streamed `response.reasoning_summary_text.delta`
        events. `reasoning_id` carries the Responses-API reasoning item id so
        consumers can round-trip it on the next turn (without the id, OpenAI
        treats the reasoning state as fresh and continuity is lost).
      - OpenAI Chat Completions / Kimi adapter: `reasoning_content` field
        on the delta (Kimi-k2.6 emits this heavily). No id available there.
    """

    thinking: str
    index: int = 0
    reasoning_id: Optional[str] = None
    type: Literal["thinking_delta"] = "thinking_delta"


@dataclass
class ToolCallStart:
    """A tool call has begun. `input_so_far` is empty; args stream via
    ToolCallArgsDelta."""

    id: str
    name: str
    index: int = 0
    type: Literal["tool_call_start"] = "tool_call_start"


@dataclass
class ToolCallArgsDelta:
    """A chunk of the JSON arg string for the tool call at `index`."""

    args_delta: str
    index: int = 0
    type: Literal["tool_call_args_delta"] = "tool_call_args_delta"


@dataclass
class ToolCallEnd:
    """A tool call's arg stream is complete. `input` is the parsed dict."""

    id: str
    name: str
    input: dict
    index: int = 0
    type: Literal["tool_call_end"] = "tool_call_end"


@dataclass
class MessageStop:
    """Stream finished. `stop_reason` is normalized: end_turn, tool_use,
    max_tokens, stop_sequence, refusal, error."""

    stop_reason: str
    type: Literal["message_stop"] = "message_stop"


@dataclass
class Usage:
    """Token accounting. Cache fields are anthropic-only; openai/kimi leave
    them at 0."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    type: Literal["usage"] = "usage"


@dataclass
class KeepAlive:
    """Heartbeat event for long stream gaps. RESERVED — no adapter currently
    emits this. Defined here so a future adapter can introduce stream-level
    keepalives without expanding the public Event union. Consumers can
    safely ignore it.

    If you find yourself implementing keepalive logic, prefer emitting this
    type so the contract stays uniform across providers.
    """

    type: Literal["keepalive"] = "keepalive"


@dataclass
class StreamError:
    """Non-recoverable provider error wrapped as an event. RESERVED — no
    adapter currently emits this; both Anthropic and OpenAI/Kimi adapters
    let exceptions propagate so callers can retry by exception type (e.g.
    workflow_execution_service catches RateLimitError). Keep available as
    an escape hatch for future adapters that want to surface a failure
    without raising.
    """

    message: str
    provider: str
    type: Literal["error"] = "error"


# Event union — types ACTUALLY emitted by current adapters. KeepAlive and
# StreamError are defined above but deliberately excluded here so the public
# stream contract reflects what the adapters actually do.
Event = Union[
    TextDelta,
    ThinkingDelta,
    ToolCallStart,
    ToolCallArgsDelta,
    ToolCallEnd,
    MessageStop,
    Usage,
]


# ---------------------------------------------------------------------------
# Non-streaming response shape.
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    """Return value of LLMClient.complete(). `text` is the concatenation of
    text blocks; `tool_calls` is empty unless the model chose to call tools.
    `thinking` is anthropic-only and may be empty."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    thinking: str = ""
    # OpenAI Responses reasoning item id (when present) — callers that rebuild
    # an assistant `thinking` block for the next turn should attach this as
    # `_reasoning_id` so the model can resume its reasoning chain. None for
    # Anthropic/Kimi (no per-item reasoning id to round-trip).
    reasoning_id: Optional[str] = None
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    raw: Any = None  # provider-native response, for adapters that need it


# ---------------------------------------------------------------------------
# Caller-facing request shape. Kept loose (just dict) so existing call sites
# don't have to rewrite their message-building code.
# ---------------------------------------------------------------------------

Message = dict[str, Any]  # {"role": "user"|"assistant", "content": str | list[dict]}
