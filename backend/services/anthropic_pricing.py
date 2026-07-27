"""
Per-model LLM pricing (Anthropic + OpenAI + Kimi/Moonshot).

Single source of truth for converting a `usage_log` row (or any
(model, input/output/cache_read/cache_write) tuple) into a USD estimate.
Routers, admin endpoints, scripts, and the in-app `/usage` page all go
through `estimate_cost_usd` so a model rename or rate change only needs
one edit. Keep in sync with the `PRICING` table in
`frontend/src/app/components/UsagePage.tsx`.

Module name preserved for backward compatibility — historically Anthropic-only.

Rates are USD per million tokens, public list as of May 2026.

OpenAI + Kimi notes:
  - OpenAI prompt caching is automatic ≥1024 tokens (no explicit write cost);
    we leave cache_write = 0 since you can't pay to create a cache.
  - Kimi context caching is also automatic; cache_write = 0.
"""

from typing import Optional

# (input, output, cache_read, cache_write) per million tokens, USD.
_PRICING_PER_M: dict[str, tuple[float, float, float, float]] = {
    # ----- Anthropic (May 2026 list) -----
    "claude-sonnet-4-6":         (3.0, 15.0, 0.30, 3.75),
    # Sonnet 5 (workflow + chat fallback model). Standard list price = Sonnet
    # 4.6's exactly; the $2/$10 intro (through 2026-08-31) is NOT encoded, so
    # cost is exact post-intro and ~30% over during it — conservative, which is
    # the safe direction for cost reporting. Update to $2/$10/$0.20/$2.50 only
    # if accurate intro-period cost reporting is needed before Sep 1.
    "claude-sonnet-5":           (3.0, 15.0, 0.30, 3.75),
    # Opus 4.6 and 4.7 share the same list price; keep both so direct-Opus
    # rows (the ADVISOR_ENABLED=false path falls back to Opus 4.6) price as
    # Opus instead of via the fallback.
    "claude-opus-4-6":           (15.0, 75.0, 1.50, 18.75),
    "claude-opus-4-7":           (15.0, 75.0, 1.50, 18.75),
    "claude-haiku-4-5-20251001": (1.0, 5.0, 0.10, 1.25),

    # ----- OpenAI (verified May 2026 list price) -----
    # Cache write = 0: OpenAI prompt caching is automatic (no explicit write
    # cost). Cache read = 50% of input per OpenAI's published discount.
    "gpt-5.5":        (5.0, 30.0, 2.50, 0.0),        # current flagship, Apr 23 2026
    "gpt-5.5-pro":    (30.0, 180.0, 15.0, 0.0),      # highest-stakes reasoning
    "gpt-5.4":        (2.50, 15.0, 1.25, 0.0),       # Mar 5 2026 strong all-rounder
    "gpt-5.4-pro":    (30.0, 180.0, 15.0, 0.0),
    "gpt-5.4-mini":   (0.40, 1.60, 0.20, 0.0),       # Mar 17 2026 mid-tier
    "gpt-5.4-nano":   (0.20, 1.25, 0.10, 0.0),       # Mar 17 2026 cheapest gpt-5 family
    "gpt-5":          (15.0, 60.0, 7.50, 0.0),       # Aug 2025 (older, still available)
    "gpt-4.1":        (2.0, 8.0, 1.0, 0.0),
    "gpt-4.1-mini":   (0.40, 1.60, 0.20, 0.0),
    "gpt-4o":         (2.5, 10.0, 1.25, 0.0),
    "gpt-4o-mini":    (0.15, 0.60, 0.075, 0.0),
    "o3":             (2.0, 8.0, 1.0, 0.0),          # Apr 2025
    "o3-pro":         (60.0, 240.0, 30.0, 0.0),
    "o3-mini":        (1.10, 4.40, 0.55, 0.0),
    "o4-mini":        (1.10, 4.40, 0.55, 0.0),       # fast/cheap reasoning

    # ----- Kimi / Moonshot (verified May 2026 list per kimi.com) -----
    # Cache_read is the published cached-input rate (~80-85% off cache-miss).
    # K2 / K2-thinking series EOL'd 2026-05-25 — kept for historical rows.
    # Verified against platform.kimi.ai/docs/pricing/chat-v1 (input is
    # cache-MISS rate; 3rd value is cache-HIT / cache_read rate).
    "kimi-k2.6":      (0.95, 4.00, 0.16, 0.0),       # current flagship, Apr 20 2026
    "kimi-k2.5":      (0.60, 3.00, 0.10, 0.0),       # Jan 2026
    # Legacy (EOL May 25 2026):
    "kimi-k2-0905-preview": (0.60, 2.50, 0.15, 0.0),
    "kimi-k2-thinking":     (0.60, 2.50, 0.15, 0.0),
    "moonshot-v1-8k":  (0.20, 2.00, 0.05, 0.0),
    "moonshot-v1-32k": (1.00, 3.00, 0.10, 0.0),
    "moonshot-v1-128k":(2.00, 5.00, 0.40, 0.0),
}

# Placeholders / shorthand the rest of the codebase may pass in. Keep
# resolution explicit so a missing entry doesn't quietly mis-price.
# `services.usage_service._save_usage` writes the literal "advisor" when
# an advisor iteration block doesn't carry a model name; advisor calls
# always run on Opus (see services/gideon/CLAUDE.md "Models"), so route
# them to Opus pricing rather than letting them fall through to Sonnet.
_PRICING_ALIASES: dict[str, str] = {
    "advisor": "claude-opus-4-7",
}

_SONNET_FALLBACK: tuple[float, float, float, float] = _PRICING_PER_M["claude-sonnet-4-6"]
_GPT5_FALLBACK: tuple[float, float, float, float] = _PRICING_PER_M["gpt-5"]
_KIMI_FALLBACK: tuple[float, float, float, float] = _PRICING_PER_M["kimi-k2.6"]
# Moonshot v1 (moonshot-v1-*) is a cheaper, separate family from the Kimi K2
# line — pricing it as kimi-k2.6 would over-estimate ~2x. Use the 32k variant
# as the representative mid-tier rate for unknown moonshot-* ids.
_MOONSHOT_FALLBACK: tuple[float, float, float, float] = _PRICING_PER_M["moonshot-v1-32k"]


def _provider_aware_fallback(model: str) -> tuple[float, float, float, float]:
    """Best-effort fallback by model-name prefix when the exact id is unknown.
    Beats the old "everything is Sonnet" behavior which mis-priced every
    OpenAI/Kimi row by 2-10x."""
    m = model.lower()
    if m.startswith("claude-"):
        return _SONNET_FALLBACK
    if m.startswith("moonshot"):
        return _MOONSHOT_FALLBACK
    if m.startswith("kimi"):
        return _KIMI_FALLBACK
    # OpenAI: gpt-* (4.x, 5.x, ...) and the o-series (o1/o3/o4/o5/...).
    # The o-series is matched by single-letter prefix + digit to survive
    # future o5/o6/etc. without code changes.
    if m.startswith("gpt-") or (len(m) >= 2 and m[0] == "o" and m[1].isdigit()):
        return _GPT5_FALLBACK
    return _SONNET_FALLBACK


def get_rates(model: Optional[str]) -> tuple[float, float, float, float]:
    """Return (input, output, cache_read, cache_write) per-million USD for
    `model`. Unknown models route by provider prefix (claude-* / gpt-* /
    o-series / kimi-* / moonshot-*) so a newly released model in a known
    family still produces a usable estimate."""
    if not model:
        return _SONNET_FALLBACK
    canonical = _PRICING_ALIASES.get(model, model)
    return _PRICING_PER_M.get(canonical) or _provider_aware_fallback(canonical)


def estimate_cost_usd(
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float:
    """Estimate USD cost given a model name + token counts."""
    in_p, out_p, cr_p, cw_p = get_rates(model)
    return (
        input_tokens * in_p
        + output_tokens * out_p
        + cache_read_tokens * cr_p
        + cache_creation_tokens * cw_p
    ) / 1_000_000
