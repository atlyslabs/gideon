"""
Usage Service — tracks Claude API token usage per user per request.
Fire-and-forget: never blocks the request, failures are logged and swallowed.
"""

import asyncio
from uuid import UUID

import structlog

from db.models import UsageLog

logger = structlog.get_logger()

# Module-level strong-reference set so background usage-logging tasks aren't
# GC'd before they hit the DB. asyncio only holds weak refs internally — any
# task whose returned Task object is discarded can disappear mid-execution
# with no exception, silently dropping the row. Symptom that flagged it:
# Anthropic invoice was ~2.5× what `usage_log` summed to, i.e. roughly half
# of all logged turns never made it to disk. The `discard` done-callback
# keeps the set bounded as tasks complete.
# Ref: https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
# and CLAUDE.md "ASYNC300" rule under Python Async/Sync Discipline.
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def log_usage(
    user_id: UUID,
    model: str,
    use_case: str,
    usage,  # anthropic Usage object or dict
) -> None:
    """Fire-and-forget usage logging. Safe to call from anywhere with an event loop."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # No event loop — skip
    task = loop.create_task(_save_usage(user_id, model, use_case, usage))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def _extract_token_counts(usage) -> tuple[int, int, int, int]:
    """Normalize across provider usage shapes. Returns
    (input_tokens, output_tokens, cache_read, cache_creation).

    Recognized shapes:
      - Anthropic SDK Usage  → input_tokens / output_tokens / cache_read_input_tokens / cache_creation_input_tokens
      - Neutral services.llm Usage → same field names as Anthropic
      - OpenAI Responses API Usage → input_tokens / output_tokens; cache on
        input_tokens_details.cached_tokens
      - OpenAI Chat Completions Usage → prompt_tokens / completion_tokens;
        cache on prompt_tokens_details.cached_tokens
      - Kimi via OpenAI-compatible chat → same as OpenAI Chat Completions
      - dict (any of the above, but with key names)
    """
    if usage is None:
        return 0, 0, 0, 0

    # First branch: Anthropic / neutral / Responses API (all use input_tokens).
    input_tokens = getattr(usage, "input_tokens", None)
    if input_tokens is not None:
        output_tokens = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_creation = getattr(usage, "cache_creation_input_tokens", 0) or 0
        # Responses API hides cache info under input_tokens_details.cached_tokens
        if not cache_read:
            details = getattr(usage, "input_tokens_details", None)
            if details is not None:
                cache_read = getattr(details, "cached_tokens", 0) or 0
        return input_tokens or 0, output_tokens, cache_read, cache_creation

    # Second branch: OpenAI Chat Completions (and Kimi via OpenAI-compatible).
    prompt_tokens = getattr(usage, "prompt_tokens", None)
    if prompt_tokens is not None:
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0
        cache_read = 0
        details = getattr(usage, "prompt_tokens_details", None)
        if details is not None:
            cache_read = getattr(details, "cached_tokens", 0) or 0
        return prompt_tokens or 0, completion_tokens, cache_read, 0

    # Third branch: dict (fallback for serialized usage rows).
    if isinstance(usage, dict):
        in_t = usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
        out_t = usage.get("output_tokens", usage.get("completion_tokens", 0)) or 0
        c_read = usage.get("cache_read_input_tokens", 0) or 0
        if not c_read:
            ptd = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            c_read = (ptd.get("cached_tokens") if isinstance(ptd, dict) else 0) or 0
        c_creation = usage.get("cache_creation_input_tokens", 0) or 0
        return in_t, out_t, c_read, c_creation

    return 0, 0, 0, 0


async def _save_usage(user_id: UUID, model: str, use_case: str, usage) -> None:
    """Store usage record in its own DB session."""
    try:
        from db.database import AsyncPostgresSessionGideon

        input_tokens, output_tokens, cache_read, cache_creation = _extract_token_counts(usage)
        # Drop ONLY genuinely-empty usage objects. A fully cache-hit turn can
        # have input_tokens=0 / output_tokens=0 (in this codebase input_tokens
        # is cache-MISS-only) while cache_read / cache_creation are non-zero —
        # those still cost money and must be recorded. Guarding on all four
        # avoids silently dropping cache-only rows.
        if not (input_tokens or output_tokens or cache_read or cache_creation):
            # Nothing to record — usage object was empty or unrecognized.
            return

        # Web search requests
        web_searches = 0
        server_tool_use = getattr(usage, "server_tool_use", None)
        if server_tool_use:
            web_searches = getattr(server_tool_use, "web_search_requests", 0) or 0
        elif isinstance(usage, dict):
            web_searches = usage.get("server_tool_use", {}).get("web_search_requests", 0)

        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            # Log executor usage
            session.add(UsageLog(
                user_id=user_id,
                model=model,
                use_case=use_case,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cache_read_tokens=cache_read,
                cache_creation_tokens=cache_creation,
                web_search_requests=web_searches,
            ))

            # Log advisor usage separately (billed at advisor model rates)
            if isinstance(usage, dict):
                iterations = usage.get("iterations")
            else:
                try:
                    iterations = usage.iterations
                except AttributeError:
                    iterations = None

            if iterations:
                for it in iterations:
                    if isinstance(it, dict):
                        it_type = it.get("type")
                        _get = it.get
                    else:
                        try:
                            it_type = it.type
                        except AttributeError:
                            continue
                        _get = lambda key, default=None: it.__dict__.get(key, default) if isinstance(it.__dict__, dict) else default

                    if it_type == "advisor_message":
                        if isinstance(it, dict):
                            adv_model = it.get("model", "advisor")
                            adv_input = it.get("input_tokens", 0)
                            adv_output = it.get("output_tokens", 0)
                            adv_cache_read = it.get("cache_read_input_tokens", 0)
                            adv_cache_creation = it.get("cache_creation_input_tokens", 0)
                        else:
                            adv_model = it.model if it.model else "advisor"
                            adv_input = it.input_tokens or 0
                            adv_output = it.output_tokens or 0
                            adv_cache_read = it.cache_read_input_tokens or 0
                            adv_cache_creation = it.cache_creation_input_tokens or 0
                        session.add(UsageLog(
                            user_id=user_id,
                            model=adv_model,
                            use_case=f"{use_case}_advisor",
                            input_tokens=adv_input,
                            output_tokens=adv_output,
                            cache_read_tokens=adv_cache_read,
                            cache_creation_tokens=adv_cache_creation,
                        ))

    except Exception as e:
        logger.debug("Usage logging failed", error=str(e))
