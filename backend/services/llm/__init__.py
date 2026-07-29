"""Provider-neutral LLM layer.

Public surface:
    services.llm.llm          — global LLMClient instance
    services.llm.get_spec     — re-export from config.llm_registry
    services.llm.types        — neutral types module

See services/llm/client.py for usage examples.
"""

from config.llm_registry import LLMSpec, get_spec

from services.llm.client import LLMClient, llm
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

__all__ = [
    "LLMClient",
    "LLMSpec",
    "llm",
    "get_spec",
    # event types
    "Event",
    "KeepAlive",
    "LLMResponse",
    "MessageStop",
    "StreamError",
    "TextDelta",
    "ThinkingDelta",
    "ToolCall",
    "ToolCallArgsDelta",
    "ToolCallEnd",
    "ToolCallStart",
    "Usage",
]
