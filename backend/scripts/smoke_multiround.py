"""Multi-round tool-use smoke test.

Verifies stream-state correctness across multiple tool rounds — the failure
mode that's invisible to the single-round smoke test. Each provider gets
two tool calls in sequence: first a city lookup, then weather for that city.
The second call must use the *output* of the first as input.

This exercises:
  - Tool call args streaming + parsing across rounds
  - Assistant-message replay with tool_use blocks
  - User-message replay with tool_result blocks
  - Provider-specific quirks (Kimi reasoning_content round-tripping,
    OpenAI call_id vs tool_use_id naming, Anthropic tool_use ordering)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

from config.llm_registry import LLMSpec
from services.llm import llm
from services.llm.types import (
    MessageStop,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCallEnd,
    ToolCallStart,
)


PROVIDER_MODELS = {
    "anthropic": "claude-sonnet-4-6",
    "openai": os.environ.get("SMOKE_OPENAI_MODEL", "gpt-4.1"),
    "kimi": "kimi-k2.6",
}


class _LookupCityTool:
    name = "lookup_city_for_user"
    description = "Return the city the user is currently in. Call this first when asked about local weather."
    input_schema = {
        "type": "object",
        "properties": {
            "username": {"type": "string", "description": "The user's name."}
        },
        "required": ["username"],
        "additionalProperties": False,
    }

    def to_claude_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


class _GetWeatherTool:
    name = "get_weather"
    description = "Get the current weather for a city. Call this AFTER lookup_city_for_user."
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name returned by lookup_city_for_user."}
        },
        "required": ["city"],
        "additionalProperties": False,
    }

    def to_claude_schema(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.input_schema}


# Faked tool implementations.
# CITY is intentionally an obscure town the model would never guess from the
# the username alone (the previous check used "Bangalore" which any
# model could fabricate without actually calling lookup_city_for_user — that
# made the smoke test pass even when the chain was broken).
_LOOKUP_CITY = "Sandakphu"
_LOOKUP_TEMP_C = 12  # likewise unlikely to be guessed
_LOOKUP_WEATHER = "drizzly with low clouds"


def _run_fake_tool(name: str, args: dict) -> str:
    if name == "lookup_city_for_user":
        # Validate the required `username` arg actually arrived. A malformed
        # first-round tool call (empty/missing args) would otherwise still get
        # the city and let the whole chain "pass" despite broken arg streaming.
        username = (args.get("username") or "").strip()
        if not username:
            return (
                "ERROR: lookup_city_for_user requires a non-empty `username` "
                "argument. Received none — check the tool call arguments."
            )
        return _LOOKUP_CITY
    if name == "get_weather":
        city = (args.get("city") or "").strip()
        # Only return real weather if get_weather was called with the EXACT
        # city the lookup returned — proves the chain wired correctly.
        if city.lower() != _LOOKUP_CITY.lower():
            return (
                f"ERROR: no weather data for {city!r}. The lookup_city_for_user "
                f"tool must be called first and its return value used as the city argument."
            )
        return f"It is {_LOOKUP_TEMP_C} degrees Celsius and {_LOOKUP_WEATHER} in {city}."
    return f"Unknown tool {name}"


@dataclass
class StreamCollect:
    text: str = ""
    thinking: str = ""
    tool_calls: list[ToolCallEnd] = None
    stop_reason: str = "end_turn"
    error: str | None = None

    def __post_init__(self):
        self.tool_calls = []


async def _collect(spec: LLMSpec, messages: list[dict], tools: list, system: str) -> StreamCollect:
    out = StreamCollect()
    async for ev in llm.stream(spec, messages=messages, system=system, tools=tools):
        if isinstance(ev, TextDelta):
            out.text += ev.text
        elif isinstance(ev, ThinkingDelta):
            out.thinking += ev.thinking
        elif isinstance(ev, ToolCallEnd):
            out.tool_calls.append(ev)
        elif isinstance(ev, ToolCallStart):
            pass
        elif isinstance(ev, MessageStop):
            out.stop_reason = ev.stop_reason
        elif isinstance(ev, StreamError):
            out.error = ev.message
            break
    return out


async def run_provider(provider: str) -> tuple[bool, str]:
    print(f"\n=== {provider.upper()} ({PROVIDER_MODELS[provider]}) ===")
    spec = LLMSpec(
        provider=provider,  # type: ignore[arg-type]
        model=PROVIDER_MODELS[provider],
        max_tokens=512,
        use_advisor=False,
        use_cache=False,
    )
    tools = [_LookupCityTool(), _GetWeatherTool()]
    system = (
        "You answer questions using the available tools. ALWAYS call "
        "lookup_city_for_user FIRST, then get_weather. Do not guess the city."
    )

    messages: list[dict] = [
        {"role": "user", "content": "Hi I'm Alex. What's the weather where I am right now?"}
    ]

    rounds_taken = 0
    saw_lookup = False
    saw_weather = False
    _weather_args_used_lookup_value = False  # True when get_weather called with city=_LOOKUP_CITY
    lookup_round = 0   # round in which lookup_city_for_user was first called
    weather_round = 0  # round in which get_weather was first called

    while rounds_taken < 4:
        rounds_taken += 1
        r = await _collect(spec, messages, tools, system)
        if r.error:
            return False, f"round {rounds_taken} error: {r.error[:120]}"

        if not r.tool_calls:
            print(f"  Round {rounds_taken}: final text = {r.text[:80]!r}")
            # Validate chain end-to-end: the lookup tool must have been called,
            # get_weather must have been called with the lookup's exact return
            # value (Sandakphu — an obscure town the model can't guess), and
            # the final text must mention that city + the weather details
            # only get_weather can produce.
            final = r.text.lower()
            city_in_answer = _LOOKUP_CITY.lower() in final
            weather_details = (str(_LOOKUP_TEMP_C) in final) or ("drizzly" in final) or ("low clouds" in final)
            answer_correct = city_in_answer and weather_details
            both_called = saw_lookup and saw_weather
            weather_call_used_lookup = (
                _weather_args_used_lookup_value if saw_weather else False
            )
            # The whole point of a MULTI-round test: get_weather must happen in
            # a LATER round than lookup_city_for_user, because its `city` arg
            # depends on the lookup result. If a provider emitted both calls in
            # the same assistant turn, it guessed the city rather than chaining
            # the result — that doesn't exercise multi-round replay at all.
            distinct_rounds = (
                saw_lookup and saw_weather and weather_round > lookup_round
            )
            if both_called and answer_correct and weather_call_used_lookup and distinct_rounds:
                return True, (
                    f"completed in {rounds_taken} rounds "
                    f"(lookup r{lookup_round} -> weather r{weather_round}); final='{r.text[:50]}...'"
                )
            return False, (
                f"completed but chain incomplete: "
                f"lookup_called={saw_lookup}(r{lookup_round}), weather_called={saw_weather}(r{weather_round}), "
                f"distinct_rounds={distinct_rounds}, "
                f"weather_used_lookup_value={weather_call_used_lookup}, "
                f"city_in_answer={city_in_answer}, weather_details_in_answer={weather_details}; "
                f"final={r.text[:160]}"
            )

        # Build assistant message blocks (thinking + text + tool_uses)
        assistant_blocks = []
        if r.thinking:
            assistant_blocks.append({"type": "thinking", "thinking": r.thinking})
        if r.text:
            assistant_blocks.append({"type": "text", "text": r.text})
        for tc in r.tool_calls:
            assistant_blocks.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.input}
            )
        messages.append({"role": "assistant", "content": assistant_blocks})

        # Execute tool calls
        tool_result_blocks = []
        for tc in r.tool_calls:
            if tc.name == "lookup_city_for_user":
                saw_lookup = True
                if lookup_round == 0:
                    lookup_round = rounds_taken
            elif tc.name == "get_weather":
                saw_weather = True
                if weather_round == 0:
                    weather_round = rounds_taken
                if (tc.input.get("city") or "").strip().lower() == _LOOKUP_CITY.lower():
                    _weather_args_used_lookup_value = True
            output = _run_fake_tool(tc.name, tc.input)
            print(f"  Round {rounds_taken}: tool {tc.name}({tc.input}) -> {output[:60]!r}")
            tool_result_blocks.append(
                {"type": "tool_result", "tool_use_id": tc.id, "content": output}
            )
        messages.append({"role": "user", "content": tool_result_blocks})

    return False, f"exhausted {rounds_taken} rounds without final answer"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="anthropic,openai,kimi")
    args = parser.parse_args()

    _KEY_FOR = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "kimi": "KIMI_API_KEY"}
    providers = [p.strip() for p in args.only.split(",") if p.strip()]
    # Validate provider names up front so a typo (`--only opena`) fails loudly
    # instead of KeyError-ing mid-run.
    unknown = [p for p in providers if p not in _KEY_FOR]
    if unknown:
        print(f"Unknown provider(s) {unknown}; valid: {sorted(_KEY_FOR)}")
        return 2
    if not providers:
        print("Empty --only; nothing to run.")
        return 2

    results: list[tuple[str, bool, str]] = []
    for p in providers:
        key = _KEY_FOR[p]
        if not os.environ.get(key):
            print(f"\n[SKIP] {p}: {key} not set")
            continue
        start = time.time()
        ok, detail = await run_provider(p)
        elapsed = int((time.time() - start) * 1000)
        results.append((p, ok, f"{detail} ({elapsed}ms)"))

    print("\n" + "=" * 70)
    for p, ok, detail in results:
        badge = "OK " if ok else "FAIL"
        print(f"  [{badge}] {p:<10} {detail}")
    print("=" * 70)
    failed = [r for r in results if not r[1]]
    print(f"Total: {len(results)}, passed: {len(results) - len(failed)}, failed: {len(failed)}")

    # If every requested provider got skipped (missing keys), this is NOT a
    # passing smoke run — it's a no-op that would silently green CI even when
    # all our credentials are misconfigured. Exit non-zero so the regression
    # path is visible.
    if not results:
        print("FAIL: no providers ran (all requested providers were skipped — check API keys).")
        return 2
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
