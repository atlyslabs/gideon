"""Live smoke test for the provider-neutral LLM layer.

Exercises every adapter (anthropic, openai, kimi) on:
  1. complete() — non-streaming, plain prompt → text.
  2. stream() — streaming, collects text deltas.
  3. tool round-trip — sends one tool, receives tool_use, replays tool_result,
     gets final answer.

Requires real keys in env or .env:
  ANTHROPIC_API_KEY, OPENAI_API_KEY, KIMI_API_KEY (+ optional KIMI_BASE_URL).

Run:
    poetry run python scripts/smoke_llm_providers.py
    poetry run python scripts/smoke_llm_providers.py --only anthropic
    poetry run python scripts/smoke_llm_providers.py --only openai,kimi
    poetry run python scripts/smoke_llm_providers.py --skip-tools  # text-only

Exit code 0 if every selected case passes; 1 if any failed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Ensure we import the gideon service from a sibling poetry env.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from config.llm_registry import LLMSpec  # noqa: E402

from services.llm import llm  # noqa: E402
from services.llm.types import (  # noqa: E402
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCallArgsDelta,
    ToolCallEnd,
    ToolCallStart,
    Usage,
)


# --------------------------------------------------------------------------
# Per-provider spec recipes for the smoke test. These intentionally bypass
# the registry so the test isn't coupled to LLM_SPECS env state — we test
# the adapter shape directly.
# --------------------------------------------------------------------------

PROVIDER_MODELS = {
    "anthropic": {
        "chat": "claude-sonnet-4-6",
        "haiku": "claude-haiku-4-5-20251001",
    },
    "openai": {
        # gpt-5 might not be available in every org — fall back to gpt-4.1
        # if the user sets SMOKE_OPENAI_MODEL.
        "chat": os.environ.get("SMOKE_OPENAI_MODEL", "gpt-4.1"),
        "haiku": "gpt-4o-mini",
    },
    "kimi": {
        "chat": "kimi-k2.6",
        "haiku": "moonshot-v1-32k",
    },
}


def _make_spec(provider: str, tier: str = "chat") -> LLMSpec:
    return LLMSpec(
        provider=provider,  # type: ignore[arg-type]
        model=PROVIDER_MODELS[provider][tier],
        max_tokens=512,
        use_advisor=False,
        use_cache=False,
    )


# --------------------------------------------------------------------------
# Tiny tool used for the round-trip case. Mimics BaseTool just enough that
# the adapters can convert it (anthropic via to_claude_schema, openai/kimi
# via attribute access on .name/.description/.input_schema).
# --------------------------------------------------------------------------


class _FakeWeatherTool:
    name = "get_weather"
    description = "Get the current weather for a city. Returns a short string."
    input_schema = {
        "type": "object",
        "properties": {
            "city": {
                "type": "string",
                "description": "The city to get weather for, e.g. 'Bangalore'.",
            }
        },
        "required": ["city"],
        "additionalProperties": False,
    }

    def to_claude_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


# --------------------------------------------------------------------------
# Result tracking.
# --------------------------------------------------------------------------


@dataclass
class Result:
    provider: str
    case: str
    passed: bool
    detail: str = ""
    duration_ms: int = 0


RESULTS: list[Result] = []


def _record(provider: str, case: str, passed: bool, detail: str, start: float) -> None:
    RESULTS.append(
        Result(
            provider=provider,
            case=case,
            passed=passed,
            detail=detail,
            duration_ms=int((time.time() - start) * 1000),
        )
    )
    badge = "OK " if passed else "FAIL"
    print(f"  [{badge}] {case:<22} {detail}")


# --------------------------------------------------------------------------
# Cases.
# --------------------------------------------------------------------------


async def case_complete(provider: str) -> None:
    """One-shot non-streaming call."""
    case = "complete()"
    start = time.time()
    try:
        spec = _make_spec(provider)
        resp = await llm.complete(
            spec,
            messages=[{"role": "user", "content": "Reply with exactly: PONG"}],
            system="You are a smoke test. Reply with one word only.",
        )
        text = resp.text.strip()
        ok = "PONG" in text.upper()
        _record(
            provider,
            case,
            ok,
            f"text={text!r:.80} in_toks={resp.usage.input_tokens} out_toks={resp.usage.output_tokens}",
            start,
        )
    except Exception as exc:  # noqa: BLE001
        _record(provider, case, False, f"exception: {exc!r}", start)


async def case_stream(provider: str) -> None:
    """Streaming text-only call. Verifies TextDelta + MessageStop arrive."""
    case = "stream()"
    start = time.time()
    try:
        spec = _make_spec(provider)
        text_parts: list[str] = []
        saw_stop = False
        saw_usage = False
        stream_error: str | None = None
        async for ev in llm.stream(
            spec,
            messages=[{"role": "user", "content": "Count: 1, 2, 3. No other text."}],
            system="You are a smoke test. Be terse.",
        ):
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
            elif isinstance(ev, Usage):
                saw_usage = True
            elif isinstance(ev, MessageStop):
                saw_stop = True
            elif isinstance(ev, StreamError):
                stream_error = ev.message
                break
        text = "".join(text_parts).strip()
        ok = (
            stream_error is None
            and saw_stop
            and ("1" in text and "2" in text and "3" in text)
        )
        detail = f"text={text!r:.60} stop={saw_stop} usage={saw_usage}"
        if stream_error:
            detail += f" ERR={stream_error[:80]}"
        _record(provider, case, ok, detail, start)
    except Exception as exc:  # noqa: BLE001
        _record(provider, case, False, f"exception: {exc!r}", start)


async def case_tool_roundtrip(provider: str) -> None:
    """Round-trip: ask model to use tool, intercept ToolCallEnd, send
    tool_result, get the final answer.

    Validates: tool schema conversion, tool_call streaming, then the message
    shape conversion for the tool_use/tool_result follow-up.
    """
    case = "tool round-trip"
    start = time.time()
    try:
        spec = _make_spec(provider)
        tool = _FakeWeatherTool()

        # Round 1: ask for weather.
        first_messages: list[dict] = [
            {"role": "user", "content": "What is the weather in Bangalore? Use the get_weather tool."}
        ]
        captured_tool_calls: list[ToolCallEnd] = []
        first_text_parts: list[str] = []
        first_thinking_parts: list[str] = []
        first_stop_reason: str | None = None
        first_stream_error: str | None = None

        async for ev in llm.stream(
            spec,
            messages=first_messages,
            system="You have a get_weather tool. Use it to answer questions about weather.",
            tools=[tool],
        ):
            if isinstance(ev, TextDelta):
                first_text_parts.append(ev.text)
            elif isinstance(ev, ThinkingDelta):
                first_thinking_parts.append(ev.thinking)
            elif isinstance(ev, ToolCallStart):
                pass
            elif isinstance(ev, ToolCallArgsDelta):
                pass
            elif isinstance(ev, ToolCallEnd):
                captured_tool_calls.append(ev)
            elif isinstance(ev, MessageStop):
                first_stop_reason = ev.stop_reason
            elif isinstance(ev, StreamError):
                first_stream_error = ev.message
                break

        if first_stream_error:
            _record(provider, case, False, f"round1 error: {first_stream_error[:80]}", start)
            return

        if not captured_tool_calls:
            _record(
                provider,
                case,
                False,
                f"no tool_call emitted (stop_reason={first_stop_reason}, text={''.join(first_text_parts)[:80]!r})",
                start,
            )
            return

        tc = captured_tool_calls[0]
        if tc.name != "get_weather" or "city" not in tc.input:
            _record(
                provider,
                case,
                False,
                f"unexpected tool_call: name={tc.name!r} input={tc.input!r}",
                start,
            )
            return

        # Round 2: replay tool_use back to the model along with a tool_result.
        # Using anthropic-shaped content blocks; the OpenAI adapter converts.
        assistant_blocks: list[dict] = []
        # Some providers (Kimi-k2.6 with built-in thinking) require the
        # original reasoning to be preserved on the replayed assistant
        # message — otherwise round 2 400s with "reasoning_content missing".
        if first_thinking_parts:
            assistant_blocks.append(
                {"type": "thinking", "thinking": "".join(first_thinking_parts)}
            )
        if first_text_parts:
            joined = "".join(first_text_parts).strip()
            if joined:
                assistant_blocks.append({"type": "text", "text": joined})
        assistant_blocks.append(
            {
                "type": "tool_use",
                "id": tc.id,
                "name": tc.name,
                "input": tc.input,
            }
        )

        # Use an unguessable sentinel temperature so the final-answer assertion
        # proves the model actually consumed the tool RESULT, not just the
        # original "Bangalore" prompt. 17 is arbitrary and not derivable.
        followup_messages = first_messages + [
            {"role": "assistant", "content": assistant_blocks},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "content": "It is 17 degrees Celsius and hailing in Bangalore.",
                    }
                ],
            },
        ]

        final_text_parts: list[str] = []
        final_stop: str | None = None
        final_error: str | None = None
        async for ev in llm.stream(
            spec,
            messages=followup_messages,
            system="You have a get_weather tool. Summarize the result back to the user.",
            tools=[tool],
        ):
            if isinstance(ev, TextDelta):
                final_text_parts.append(ev.text)
            elif isinstance(ev, MessageStop):
                final_stop = ev.stop_reason
            elif isinstance(ev, StreamError):
                final_error = ev.message
                break

        final = "".join(final_text_parts).strip()
        if final_error:
            _record(provider, case, False, f"round2 error: {final_error[:80]}", start)
            return

        # The answer must reflect data that could ONLY have come from the tool
        # result (17° / hailing) — NOT the original prompt (Bangalore). Checking
        # for "bangalore"/"sunny" would pass even if the model ignored the tool
        # output entirely. This proves the tool_result round-tripped correctly.
        used_tool_result = ("17" in final) or ("hail" in final.lower())
        _record(
            provider,
            case,
            used_tool_result and final_stop is not None,
            f"used_tool_result={used_tool_result} final={final!r:.100} stop={final_stop}",
            start,
        )

    except Exception as exc:  # noqa: BLE001
        _record(provider, case, False, f"exception: {exc!r}", start)


# --------------------------------------------------------------------------
# Runner.
# --------------------------------------------------------------------------


async def case_openai_responses_text(provider: str) -> None:
    """OpenAI Responses API: plain text via reasoning model. Verifies the
    Responses code path emits TextDelta + MessageStop correctly."""
    case = "responses_text()"
    start = time.time()
    if provider != "openai":
        _record(provider, case, True, "skipped (only relevant for openai)", start)
        return
    try:
        spec = LLMSpec(
            provider="openai",
            model=os.environ.get("SMOKE_OPENAI_REASONING_MODEL", "o3"),
            max_tokens=512,
            use_reasoning_summary=True,  # routes to Responses adapter
        )
        text_parts: list[str] = []
        thinking_parts: list[str] = []
        saw_stop = False
        async for ev in llm.stream(
            spec,
            messages=[{"role": "user", "content": "What is 2+2? Reply with just the number."}],
            system="You are a smoke test. Be terse.",
        ):
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
            elif isinstance(ev, ThinkingDelta):
                thinking_parts.append(ev.thinking)
            elif isinstance(ev, MessageStop):
                saw_stop = True
        text = "".join(text_parts).strip()
        ok = saw_stop and "4" in text
        _record(
            provider,
            case,
            ok,
            f"text={text!r:.40} thinking_chars={len(''.join(thinking_parts))} stop={saw_stop}",
            start,
        )
    except Exception as exc:  # noqa: BLE001
        _record(provider, case, False, f"exception: {exc!r}"[:200], start)


async def case_openai_responses_code(provider: str) -> None:
    """OpenAI Responses API with code_interpreter enabled. Verifies the
    server-side sandbox runs and we see the code event stream."""
    case = "responses_code_exec"
    start = time.time()
    if provider != "openai":
        _record(provider, case, True, "skipped (only relevant for openai)", start)
        return
    try:
        spec = LLMSpec(
            provider="openai",
            model=os.environ.get("SMOKE_OPENAI_REASONING_MODEL", "o3"),
            max_tokens=1024,
            use_code_interpreter=True,
        )
        text_parts: list[str] = []
        code_started = False
        code_completed = False
        async for ev in llm.stream(
            spec,
            messages=[{"role": "user", "content": "Use the code interpreter to compute 7 factorial. Show the code and the result."}],
        ):
            if isinstance(ev, TextDelta):
                text_parts.append(ev.text)
            elif isinstance(ev, ToolCallStart) and ev.name == "_openai_code_interpreter":
                code_started = True
            elif isinstance(ev, ToolCallEnd) and ev.name == "_openai_code_interpreter":
                code_completed = True
            elif isinstance(ev, MessageStop):
                pass
        text = "".join(text_parts).strip()
        # Adapter correctness = code sandbox ran (start + completed events fired).
        # Whether the model echoes "5040" in the followup text is a model behavior
        # quirk, not an adapter concern — o3 sometimes summarizes without restating.
        ok = code_started and code_completed
        _record(
            provider,
            case,
            ok,
            f"code_started={code_started} code_completed={code_completed} answer_contains_5040={'5040' in text} answer_len={len(text)}",
            start,
        )
    except Exception as exc:  # noqa: BLE001
        _record(provider, case, False, f"exception: {exc!r}"[:200], start)


async def run_provider(provider: str, skip_tools: bool) -> None:
    print(f"\n=== {provider.upper()} ({PROVIDER_MODELS[provider]['chat']}) ===")
    await case_complete(provider)
    await case_stream(provider)
    if not skip_tools:
        await case_tool_roundtrip(provider)
    if provider == "openai":
        await case_openai_responses_text(provider)
        if not skip_tools:
            await case_openai_responses_code(provider)


def _check_keys(providers: list[str]) -> list[str]:
    """Drop any provider whose key is missing, with a warning."""
    out = []
    for p in providers:
        key_name = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "kimi": "KIMI_API_KEY",
        }[p]
        if os.environ.get(key_name):
            out.append(p)
        else:
            print(f"  [SKIP] {p}: {key_name} not set")
    return out


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--only",
        type=str,
        default="anthropic,openai,kimi",
        help="Comma-separated provider list (anthropic,openai,kimi).",
    )
    parser.add_argument("--skip-tools", action="store_true")
    args = parser.parse_args()

    requested = [p.strip() for p in args.only.split(",") if p.strip()]
    # Validate provider names BEFORE _check_keys (which indexes a dict by name
    # and would KeyError on a typo like `--only opena`). Fail loudly with the
    # valid set instead of a stack trace.
    valid_providers = set(PROVIDER_MODELS.keys())
    unknown = [p for p in requested if p not in valid_providers]
    if unknown:
        print(f"Unknown provider(s) {unknown}; valid: {sorted(valid_providers)}")
        return 2
    if not requested:
        print("Empty --only; nothing to run.")
        return 2

    providers = _check_keys(requested)
    if not providers:
        print("No providers with valid keys; exiting.")
        return 1

    for p in providers:
        await run_provider(p, args.skip_tools)

    # Summary table.
    print("\n" + "=" * 70)
    failed = [r for r in RESULTS if not r.passed]
    for r in RESULTS:
        badge = "OK " if r.passed else "FAIL"
        print(f"  [{badge}] {r.provider:<10} {r.case:<22} {r.duration_ms:>6} ms")
    print("=" * 70)
    print(f"Total: {len(RESULTS)}, passed: {len(RESULTS) - len(failed)}, failed: {len(failed)}")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
