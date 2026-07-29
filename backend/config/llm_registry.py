"""Per-feature LLM specs.

One ``LLMSpec`` per AI feature in Gideon. Every field is overridable via env
var so ops can flip providers / models / effort per feature without a code
change. The 15 features map 1:1 to the table in CLAUDE.md "AI features".

Env var conventions per feature key (e.g. "chat"):
    PROVIDER_CHAT       — "anthropic" | "openai" | "kimi"
    MODEL_CHAT          — provider-native model id
    EFFORT_CHAT         — "max"/"xhigh"/"high"/"medium"/"low" (anthropic) or
                          "high"/"medium"/"low"/"minimal" (openai). Ignored on kimi.
    MAX_TOKENS_CHAT     — int
    USE_ADVISOR_CHAT    — bool (anthropic only — auto-skipped on openai/kimi)
    USE_CACHE_CHAT      — bool (anthropic only)

For backward compatibility the legacy env vars (``MODEL_CHAT``, ``CHAT_EFFORT``,
etc. as defined in ``config/__init__.py``) continue to drive the same fields,
so flipping ``PROVIDER_CHAT=openai`` is the minimal change to route chat off
Anthropic. The OpenAI/Kimi default models are picked tier-for-tier with the
Anthropic defaults.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Literal, Optional

_logger = logging.getLogger(__name__)

Provider = Literal["anthropic", "openai", "kimi"]

# OpenAI defaults per tier. `gpt-5` is the user-requested top tier — if the
# org doesn't have access yet the wrapper logs a warning at startup and the
# call will 404 at runtime (caller falls back to anthropic by changing the
# env var). We don't auto-fallback inside the registry.
_OPENAI_CHAT_TIER = "gpt-5"
_OPENAI_REASONING_TIER = "o3"
_OPENAI_HAIKU_TIER = "gpt-4o-mini"

# Kimi defaults. `kimi-k2.6` is the current top model (256k context, agentic,
# multimodal). The reasoning tier ALSO uses kimi-k2.6 — the old
# `kimi-k2-thinking` variant was EOL'd 2026-05-25, and k2.6 has reasoning
# built in (emits reasoning_content natively).
#
# Haiku tier uses `kimi-k2.5`, NOT a moonshot-v1 model. Rationale: the
# moonshot-v1 line caps at 8K-128K context and is a previous generation —
# moonshot-v1-8k (8K) can't hold the `suggestions` input (up to 20 emails +
# 200 Slack messages), and the older models are less reliable at the strict
# JSON output these features parse. kimi-k2.5 is $0.60/$3.00/M (cache $0.10) —
# ~40% cheaper than Haiku 4.5 on both axes, same cache rate, 256K context, and
# frontier-class JSON reliability. It's a big model priced below Haiku rather
# than a true small/fast model, but it's the safest cheap Kimi option here.
_KIMI_CHAT_TIER = "kimi-k2.6"
_KIMI_REASONING_TIER = "kimi-k2.6"
_KIMI_HAIKU_TIER = "kimi-k2.5"


@dataclass
class LLMSpec:
    """Configuration for a single AI feature.

    `provider` decides which adapter handles the call. `model` is the
    provider-native id. `effort` maps to either Anthropic's adaptive-thinking
    enum or OpenAI's `reasoning_effort` (see config/llm_registry effort map).
    Kimi ignores `effort` — pick a `*-thinking` model id to get reasoning.

    `use_code_interpreter` and `use_reasoning_summary` route the OpenAI provider
    through the Responses API (vs Chat Completions). Code interpreter on
    Anthropic is the native server-side tool; on Kimi it goes through the
    Anthropic-sandbox bridge function tool. On OpenAI these flags pick the
    server-side Responses API path.
    """

    provider: Provider
    model: str
    effort: Optional[str] = None
    max_tokens: int = 8192
    use_advisor: bool = False
    use_cache: bool = True  # anthropic prompt caching; ignored elsewhere
    use_code_interpreter: bool = False
    use_reasoning_summary: bool = False
    # Anthropic-only: request the 1M-token context window (beta). Heavy workflows
    # set this so large accumulated tool data fits instead of overflowing 256K.
    use_1m_context: bool = False
    # Kimi-only: turn off the model's intrinsic reasoning_content for this
    # feature (sends `thinking: {type: disabled}`). Set True for trivial
    # classification/formatting jobs (titles, YES/NO triage)
    # where reasoning is pure latency + cost + a tokens-budget hazard. No-op on
    # Anthropic (thinking is opt-in there) and on OpenAI (o-series can't be
    # fully disabled; gpt-4o-mini doesn't reason anyway).
    disable_reasoning: bool = False


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        _logger.warning("Invalid int for %s, falling back to %s", name, default)
        return default


def _provider_env(name: str, default: Provider) -> Provider:
    raw = (os.environ.get(name) or default).strip().lower()
    if raw not in ("anthropic", "openai", "kimi"):
        _logger.warning("Invalid provider %r for %s, using %s", raw, name, default)
        return default
    return raw  # type: ignore[return-value]


# Last-resort Anthropic model per tier. Used only when both the MODEL_* env
# var AND the config-derived default resolve empty (e.g. someone set
# `MODEL_CHAT=` in .env, which makes config.MODEL_CHAT itself empty since
# os.environ.get returns "" for a set-but-empty key). Without this, an empty
# model id would reach the SDK and 400 at runtime.
#
# The chat/executor tier is computed dynamically (not hardcoded) because the
# configured executor default depends on ADVISOR_ENABLED: with advisor OFF,
# chat/agent/workflow run on Opus, not Sonnet. Hardcoding Sonnet here would
# silently downgrade those flows to Sonnet whenever MODEL_CHAT was set-empty.


def _anthropic_tier_fallback(tier: str) -> str:
    if tier == "reasoning":
        return "claude-opus-4-7"
    if tier == "haiku":
        return "claude-haiku-4-5-20251001"
    # chat/executor tier — mirror config's `_default_executor` so we respect
    # ADVISOR_ENABLED (Sonnet when advisor on, Opus when off).
    from config import ADVISOR_ENABLED, Models
    return Models.SONNET if ADVISOR_ENABLED else Models.OPUS


def _default_model(provider: Provider, tier: str) -> str:
    """Pick a sensible default model when MODEL_* env var is unset for the
    chosen provider. `tier` is one of "chat", "reasoning", "haiku"."""
    if provider == "anthropic":
        # Anthropic defaults normally come from config.* (caller passes them
        # in). This is the last-resort fallback when that path also yields
        # empty. The chat tier respects ADVISOR_ENABLED (see above).
        return _anthropic_tier_fallback(tier)
    if provider == "openai":
        return {
            "chat": _OPENAI_CHAT_TIER,
            "reasoning": _OPENAI_REASONING_TIER,
            "haiku": _OPENAI_HAIKU_TIER,
        }[tier]
    if provider == "kimi":
        return {
            "chat": _KIMI_CHAT_TIER,
            "reasoning": _KIMI_REASONING_TIER,
            "haiku": _KIMI_HAIKU_TIER,
        }[tier]
    raise ValueError(f"Unknown provider {provider!r}")


def _spec(
    feature: str,
    tier: str,
    *,
    anthropic_default_model: str,
    default_provider: Provider = "anthropic",
    default_effort: Optional[str] = None,
    default_max_tokens: int = 8192,
    default_use_advisor: bool = False,
    default_use_cache: bool = True,
    default_use_code_interpreter: bool = False,
    default_use_reasoning_summary: bool = False,
    default_disable_reasoning: bool = False,
) -> LLMSpec:
    """Build an LLMSpec for `feature`, reading per-feature env overrides."""
    key = feature.upper()
    provider = _provider_env(f"PROVIDER_{key}", default_provider)

    # Treat empty `MODEL_*=` (i.e. unset-but-set-to-empty in .env) as unset.
    # Without this guard, `MODEL_CHAT=` would resolve to model="" and the
    # request would 400 at provider call time with "model is required".
    # `"" or X` evaluates to X because the empty string is falsy, so this
    # holds for both the anthropic and provider-switch branches below.
    raw_model = os.environ.get(f"MODEL_{key}")
    if raw_model is not None:
        raw_model = raw_model.strip()
    if provider == "anthropic":
        # raw_model first, then config-derived default, then hardcoded tier
        # fallback — the last guard covers `MODEL_CHAT=` (set-but-empty), which
        # makes config.MODEL_CHAT itself empty.
        model = raw_model or anthropic_default_model or _default_model("anthropic", tier)
    else:
        model = raw_model or _default_model(provider, tier)

    effort = os.environ.get(f"EFFORT_{key}", default_effort or "")
    effort = effort.strip().lower() or None

    return LLMSpec(
        provider=provider,
        model=model,
        effort=effort,
        max_tokens=_int_env(f"MAX_TOKENS_{key}", default_max_tokens),
        use_advisor=_bool_env(f"USE_ADVISOR_{key}", default_use_advisor),
        use_cache=_bool_env(f"USE_CACHE_{key}", default_use_cache),
        # These two route OpenAI through the Responses API (code_interpreter +
        # reasoning_summary). Previously hard-coded to dataclass defaults, so
        # USE_CODE_INTERPRETER_* / USE_REASONING_SUMMARY_* env overrides were
        # silently ignored. The wrapper-chat path sets them explicitly per turn
        # for OpenAI regardless, but feature-level overrides now work too.
        use_code_interpreter=_bool_env(
            f"USE_CODE_INTERPRETER_{key}", default_use_code_interpreter
        ),
        use_reasoning_summary=_bool_env(
            f"USE_REASONING_SUMMARY_{key}", default_use_reasoning_summary
        ),
        disable_reasoning=_bool_env(
            f"DISABLE_REASONING_{key}", default_disable_reasoning
        ),
    )


def _build_specs() -> dict[str, LLMSpec]:
    # Late-import the Anthropic config so this module stays import-cheap.
    from config import (
        ADVISOR_ENABLED,
        AGENT_EFFORT,
        CHAT_EFFORT,
        MODEL_AGENT,
        MODEL_CHAT,
        MODEL_EMAIL_DRAFT,
        MODEL_MEETING_PREP,
        MODEL_SKILL_LEARNING,
        MODEL_SMART_ONBOARDING,
        MODEL_SUGGESTIONS,
        MODEL_TITLE_GENERATION,
        MODEL_TRIGGER_AGENT,
        MODEL_WORKFLOW_EXECUTION,
        MODEL_WORKFLOW_SUGGESTIONS,
        Models,
        WORKFLOW_EFFORT,
    )

    return {
        "chat": _spec(
            "chat",
            "chat",
            anthropic_default_model=MODEL_CHAT,
            default_effort=CHAT_EFFORT,
            # 32K matches services/chat_service.py:CHAT_MAX_OUTPUT_TOKENS so
            # the wrapper-chat path (PROVIDER_CHAT != anthropic) doesn't
            # silently cap responses at 8K and truncate long turns.
            default_max_tokens=32768,
            default_use_advisor=ADVISOR_ENABLED,
        ),
        "agent": _spec(
            "agent",
            "chat",
            anthropic_default_model=MODEL_AGENT,
            default_effort=AGENT_EFFORT,
            # >=16K: Kimi reasoning models emit ~5K+ tokens of reasoning_content
            # that counts against max_tokens. Below 16K the trace can swallow the
            # whole budget and the answer comes back empty/truncated (measured:
            # 18K reasoning chars vs a 4K cap -> 0-char answer). No-op on Anthropic.
            default_max_tokens=16384,
            default_use_advisor=ADVISOR_ENABLED,
        ),
        "workflow": _spec(
            "workflow",
            "chat",
            anthropic_default_model=MODEL_WORKFLOW_EXECUTION,
            default_effort=WORKFLOW_EFFORT,
            default_max_tokens=32768,
            default_use_advisor=ADVISOR_ENABLED,
        ),
        "advisor": _spec(
            "advisor",
            "reasoning",
            anthropic_default_model=Models.OPUS_4_7,
            default_max_tokens=2048,
        ),
        "trigger_agent": _spec(
            "trigger_agent",
            "chat",
            anthropic_default_model=MODEL_TRIGGER_AGENT,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "meeting_prep": _spec(
            "meeting_prep",
            "chat",
            anthropic_default_model=MODEL_MEETING_PREP,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "email_draft": _spec(
            "email_draft",
            "chat",
            anthropic_default_model=MODEL_EMAIL_DRAFT,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "survey_analysis": _spec(
            "survey_analysis",
            "chat",
            anthropic_default_model=Models.SONNET,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "smart_onboarding": _spec(
            "smart_onboarding",
            "chat",
            anthropic_default_model=MODEL_SMART_ONBOARDING,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "skill_learning": _spec(
            "skill_learning",
            "chat",
            anthropic_default_model=MODEL_SKILL_LEARNING,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        # Haiku-tier jobs default to disable_reasoning=True: on Kimi these are
        # trivial classification/formatting tasks (preview, title, JSON parse,
        # YES/NO triage, intent) where the model's intrinsic reasoning is pure
        # latency + cost + a token-budget hazard. No-op on Anthropic/OpenAI.
        "workflow_preview": _spec(
            "workflow_preview",
            "haiku",
            anthropic_default_model=Models.HAIKU,
            default_max_tokens=2048,
            default_disable_reasoning=True,
        ),
        "workflow_suggestions": _spec(
            "workflow_suggestions",
            "chat",
            anthropic_default_model=MODEL_WORKFLOW_SUGGESTIONS,
            default_max_tokens=16384,  # reasoning-budget floor; see "agent" above
        ),
        "title_gen": _spec(
            "title_gen",
            "haiku",
            anthropic_default_model=MODEL_TITLE_GENERATION,
            default_max_tokens=256,
            default_disable_reasoning=True,
        ),
        "suggestions": _spec(
            "suggestions",
            "haiku",
            anthropic_default_model=MODEL_SUGGESTIONS,
            default_max_tokens=2048,
            default_disable_reasoning=True,
        ),
    }


_SPECS_CACHE: Optional[dict[str, LLMSpec]] = None


def get_spec(feature: str) -> LLMSpec:
    """Return the LLMSpec for `feature`. Specs are built lazily on first
    access and cached for the process lifetime — env vars are read once."""
    global _SPECS_CACHE
    if _SPECS_CACHE is None:
        _SPECS_CACHE = _build_specs()
    if feature not in _SPECS_CACHE:
        raise KeyError(
            f"Unknown LLM feature {feature!r}. Known: {sorted(_SPECS_CACHE)}"
        )
    return _SPECS_CACHE[feature]


def all_specs() -> dict[str, LLMSpec]:
    """Materialize every spec — used by startup logging + the smoke script."""
    global _SPECS_CACHE
    if _SPECS_CACHE is None:
        _SPECS_CACHE = _build_specs()
    return dict(_SPECS_CACHE)


# ---------------------------------------------------------------------------
# Effort mapping: anthropic → openai reasoning_effort. Conservative downshift
# per CLAUDE.md decision: max→high, xhigh→high, high→medium, medium→low,
# low→minimal. Kimi ignores effort entirely.
# ---------------------------------------------------------------------------

_ANTHROPIC_TO_OPENAI_EFFORT = {
    "max": "high",
    "xhigh": "high",
    "high": "medium",
    "medium": "low",
    "low": "minimal",
}


def map_effort_to_openai(effort: Optional[str]) -> Optional[str]:
    if not effort:
        return None
    return _ANTHROPIC_TO_OPENAI_EFFORT.get(effort.lower())
