"""Shared request context-bounding — single source of truth.

Both the chat loop (`services/chat_service.py`) and the workflow executor
(`services/workflow_execution_service.py`) must keep a request under the
model's hard context window, or the provider 400s ("exceeded model token
limit" on Kimi, "prompt is too long" on Anthropic's 1M beta). They each carried
a private copy of the same token-budgeting + field-truncation + drop-oldest
logic, which risked drifting apart. This module is that logic, made public so
both import it (repo rules forbid importing a sibling module's `_`-prefixed
helpers cross-module).

Everything here is REQUEST-ONLY and never mutates its inputs: callers keep the
canonical in-memory history / DB rows / workflow checkpoint at full fidelity.

The two callers differ in three ways, all expressed as parameters so behavior
stays byte-identical to the pre-extraction copies:
  - **ceiling** — chat derives a per-provider ceiling; workflow uses its 1M-beta
    ceiling. Passed in by the caller.
  - **transform** — workflow strips orphan server-tool blocks from the surviving
    tail (`_strip_orphan_server_tools`); chat passes none.
  - **prefer_newest_turn** — chat, as a final safety net, keeps the newest turn
    (without the anchor) if it fits rather than collapsing to the bare anchor;
    workflow returns the bare anchor.
Each caller logs its own message from the returned `BoundOutcome` (no logging
happens in here — keeps the module dependency-free and the log text unchanged).
"""

import json
from typing import Callable, NamedTuple, Optional

# Shared truncation marker (both callers used this exact string).
CONTEXT_TRUNC_MARKER = "\n\n[... truncated by Gideon to fit the model context window ...]"
# When over the ceiling, truncate any single string content/text field larger
# than this (chars); halved repeatedly down to the floor until the request fits.
CONTEXT_FIELD_CAP_CHARS = 12_000
CONTEXT_FIELD_CAP_FLOOR_CHARS = 256


class BoundOutcome(NamedTuple):
    """Result of `bound_request`. `method` tells the caller which path ran so it
    can emit its own log line: "none" (already under ceiling — no-op, `messages`
    is the original object), "field_truncate" (oversized string fields capped),
    or "drop_oldest" (last-resort message drop after flooring didn't fit)."""
    messages: list
    method: str
    field_cap: Optional[int]
    before_tokens: int
    after_tokens: int
    kept_messages: int


def cap_tool_result(payload: str, max_chars: int, suffix: str) -> str:
    """Cap a serialized tool result at `max_chars` (appending `suffix` when cut).
    The full result is persisted before this runs; this bounds only the copy that
    goes into the model request, so one fat result (a big Notion page, a full-day
    channel read) can't dominate the context window and slow/hang the request.
    No-op when already under `max_chars`."""
    if len(payload) <= max_chars:
        return payload
    return payload[:max_chars] + suffix


def approx_request_tokens(messages: list, system_prompt: str, tools: list) -> int:
    """Rough token estimate for a full request (~4 chars/token). `default=str`
    so it never raises on Anthropic SDK block objects that can sit in `messages`
    after a tool round / escalation."""
    try:
        body = json.dumps(messages, default=str)
        tools_s = json.dumps(tools or [], default=str)
    except (TypeError, ValueError):
        body, tools_s = str(messages), str(tools)
    return (len(body) + len(system_prompt or "") + len(tools_s)) // 4


def truncate_message_fields(
    messages: list, cap: int, marker: str = CONTEXT_TRUNC_MARKER
) -> list:
    """Return a copy of `messages` with every string `content`/`text` field
    longer than `cap` truncated to `cap` chars (+ marker). Truncates from the
    ORIGINAL each call (re-running at a smaller cap doesn't stack markers).
    Leaves non-dict blocks (SDK objects) and short fields untouched — no message
    or block is ever dropped, so tool_use/tool_result pairing and thinking-block
    ordering are preserved."""
    out: list = []
    for m in messages:
        content = m.get("content") if isinstance(m, dict) else None
        if isinstance(content, str) and len(content) > cap:
            out.append({**m, "content": content[:cap] + marker})
            continue
        if isinstance(content, list):
            new_blocks: list = []
            changed = False
            for b in content:
                if isinstance(b, dict):
                    nb = dict(b)
                    for key in ("content", "text"):
                        v = nb.get(key)
                        if isinstance(v, str) and len(v) > cap:
                            nb[key] = v[:cap] + marker
                            changed = True
                    new_blocks.append(nb)
                else:
                    new_blocks.append(b)
            out.append({**m, "content": new_blocks} if changed else m)
            continue
        out.append(m)
    return out


def drop_oldest_until_fit(
    messages: list,
    system_prompt: str,
    tools: list,
    ceiling: int,
    *,
    transform: Optional[Callable[[list], list]] = None,
    prefer_newest_turn: bool = False,
) -> list:
    """Last-resort context bound: keep the first message (the anchor — task /
    first user message / rolling summary) + the most recent tail that fits under
    `ceiling`, dropping oldest middle messages. The kept tail always STARTS at an
    ASSISTANT turn so tool_use/tool_result pairing survives (client tool_use and
    its result live in adjacent messages; server-side blocks are paired inside a
    single assistant message, so whole-message drops can't orphan them).

    `transform`: applied to the surviving list before returning (workflow passes
    `_strip_orphan_server_tools`; chat passes none).
    `prefer_newest_turn`: when nothing fits WITH the anchor, try the newest turn
    WITHOUT the anchor if that fits (keeps what the user just asked instead of
    collapsing to stale anchor-only) before finally falling back to the anchor.
    Only reached when even flooring every field is over the ceiling."""
    if len(messages) <= 2:
        # No head + assistant-tail to rebuild for a 1- or 2-message request, but
        # still avoid returning an over-ceiling set: if the pair is over ceiling
        # and the anchor (message 0 — conventionally the user turn, so dropping
        # message 1 can't orphan a tool_use/tool_result pair) fits alone, drop to
        # it. A lone message that itself exceeds the ceiling is beyond what
        # message-dropping can fix (see module docstring) — return unchanged.
        if (
            len(messages) == 2
            and approx_request_tokens(messages, system_prompt, tools) > ceiling
            and approx_request_tokens(messages[:1], system_prompt, tools) <= ceiling
        ):
            return messages[:1]
        return messages
    head = messages[:1]
    newest_assistant_k = None
    for k in range(1, len(messages)):
        mk = messages[k]
        if not (isinstance(mk, dict) and mk.get("role") == "assistant"):
            continue
        newest_assistant_k = k  # track the most-recent turn boundary
        candidate = head + messages[k:]
        if approx_request_tokens(candidate, system_prompt, tools) <= ceiling:
            return transform(candidate) if transform else candidate
    if prefer_newest_turn and newest_assistant_k is not None:
        newest_only = messages[newest_assistant_k:]
        if approx_request_tokens(newest_only, system_prompt, tools) <= ceiling:
            return transform(newest_only) if transform else newest_only
    return head


def bound_request(
    messages: list,
    system_prompt: str,
    tools: list,
    ceiling: int,
    *,
    transform: Optional[Callable[[list], list]] = None,
    prefer_newest_turn: bool = False,
) -> BoundOutcome:
    """Return a request-safe copy of `messages` guaranteed under `ceiling`
    (except the pathological case where a single surviving message alone exceeds
    it), plus a `BoundOutcome` describing which path ran so the caller can log.

    Strategy: no-op if already under. Else truncate oversized STRING fields with
    the field cap halved repeatedly down to a floor (never drops messages, so
    pairing + thinking-block ordering stay intact). Only if flooring every field
    still doesn't fit does it drop oldest messages. The caller keeps the original
    `messages` untouched."""
    before = approx_request_tokens(messages, system_prompt, tools)
    if before <= ceiling:
        return BoundOutcome(messages, "none", None, before, before, len(messages))
    cap = CONTEXT_FIELD_CAP_CHARS
    out = messages
    while True:
        out = truncate_message_fields(messages, cap)
        after = approx_request_tokens(out, system_prompt, tools)
        if after <= ceiling:
            return BoundOutcome(out, "field_truncate", cap, before, after, len(out))
        if cap <= CONTEXT_FIELD_CAP_FLOOR_CHARS:
            break
        # Always try the floor as the final step before giving up (plain
        # `cap //= 2` can jump past it, e.g. 375 → 187 exits without a 256 pass).
        cap = max(cap // 2, CONTEXT_FIELD_CAP_FLOOR_CHARS)
    out = drop_oldest_until_fit(
        out, system_prompt, tools, ceiling,
        transform=transform, prefer_newest_turn=prefer_newest_turn,
    )
    after = approx_request_tokens(out, system_prompt, tools)
    return BoundOutcome(out, "drop_oldest", cap, before, after, len(out))
