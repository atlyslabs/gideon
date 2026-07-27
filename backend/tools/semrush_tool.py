"""
Semrush tools — SEO + marketing intelligence via Semrush's standard REST API.

Why REST and not the hosted MCP:
  The hosted Semrush MCP at mcp.semrush.com is Anthropic-partner-gated; the
  standard Semrush API key (the long token from account → API Units page)
  returns 401 against the MCP. The Analytics REST API at api.semrush.com
  accepts the same key as `?key=<key>` and exposes every report we need.
  Same trade-off as Meta Ads (see `meta_ads_tool.py`).

Auth: single shared org key in `SEMRUSH_API_KEY` env. No per-user OAuth.
Routed via InternalTool so tool_executor.py skips the Integration lookup.

Response format: CSV with `;` separator, first line is a header row. We
parse to JSON. Errors come back as plain text like "ERROR 50 :: NOTHING FOUND"
which we detect and return as `{error: ...}`.
"""

from typing import Any
from uuid import UUID

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from config import SEMRUSH_API_KEY, SEMRUSH_API_URL
from tools.internal_tools import InternalTool

logger = structlog.get_logger()

# Default country DB. US is the largest single
# market and the most-stocked Semrush DB, so it's the safe default. Callers can
# override per-call via the `database` arg.
_DEFAULT_DB = "us"

# Reasonable upper bound. Semrush itself caps at 100000 per request; we cap
# lower so a single tool call doesn't return a 5 MB CSV to Claude.
_MAX_LIMIT = 100

_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=30.0)
    return _shared_client


def _parse_csv(body: str) -> list[dict[str, str]]:
    """Parse Semrush's semicolon-delimited CSV into a list of dicts. Semrush
    doesn't quote values and doesn't embed semicolons in fields, so a plain
    split is safe and avoids pulling in the `csv` module's quoting baggage."""
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if not lines:
        return []
    header = [h.strip() for h in lines[0].split(";")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        parts = line.split(";")
        rows.append({header[i]: (parts[i] if i < len(parts) else "") for i in range(len(header))})
    return rows


async def _semrush_get(params: dict[str, Any]) -> dict[str, Any]:
    """Hit api.semrush.com with the shared key. Returns either `{rows: [...]}`
    on success or `{error: ...}` on Semrush's plaintext error responses /
    HTTP failures."""
    if not SEMRUSH_API_KEY:
        return {"error": "SEMRUSH_API_KEY is not configured."}

    query = {"key": SEMRUSH_API_KEY, **{k: v for k, v in params.items() if v is not None}}

    try:
        resp = await _get_client().get(SEMRUSH_API_URL, params=query)
    except httpx.HTTPError as e:
        logger.error("Semrush request failed", err=str(e))
        return {"error": f"Semrush request failed: {e}"}

    body = (resp.text or "").strip()
    # Semrush returns 200 even for "no data" / "bad key" — the body carries
    # the actual status as a plaintext "ERROR XX :: MSG" string.
    if body.startswith("ERROR"):
        # Don't leak the key back through the error message.
        sanitized = body.replace(SEMRUSH_API_KEY, "[redacted]")
        return {"error": f"Semrush: {sanitized}"}

    if resp.status_code != 200:
        return {"error": f"Semrush HTTP {resp.status_code}: {body[:300]}"}

    rows = _parse_csv(body)
    if not rows:
        return {"rows": [], "count": 0, "note": "No rows returned for these params."}
    return {"rows": rows, "count": len(rows)}


def _clamp_limit(value: Any) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 10
    return max(1, min(_MAX_LIMIT, n))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class SemrushDomainOverview(InternalTool):
    name = "semrush_domain_overview"
    description = (
        "High-level Semrush metrics for a domain in one country DB: organic "
        "keyword count, organic traffic, organic traffic cost, paid keyword "
        "count, paid traffic, paid traffic cost. Use for 'how is <domain> "
        "doing on SEO/Ads' questions. For multi-country, call repeatedly."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Root domain, e.g. 'example.com'."},
            "database": {
                "type": "string",
                "description": "Country DB code: 'us', 'gb', 'in', 'au', 'de', etc. Default 'us'.",
            },
        },
        "required": ["domain"],
    }

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        domain = (params.get("domain") or "").strip()
        if not domain:
            return {"error": "`domain` is required."}
        return await _semrush_get({
            "type": "domain_ranks",
            "domain": domain,
            "database": params.get("database") or _DEFAULT_DB,
            "export_columns": "Db,Dn,Rk,Or,Ot,Oc,Ad,At,Ac",
        })


class SemrushOrganicKeywords(InternalTool):
    name = "semrush_organic_keywords"
    description = (
        "Top organic search keywords a domain ranks for, with each keyword's "
        "position, search volume, CPC, and the share of the domain's organic "
        "traffic it drives. Use for 'what keywords does <domain> rank for' "
        "and competitor analysis."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Root domain, e.g. 'example.com'."},
            "database": {"type": "string", "description": "Country DB. Default 'us'."},
            "limit": {
                "type": "integer",
                "description": f"Max rows (1–{_MAX_LIMIT}). Default 10.",
            },
        },
        "required": ["domain"],
    }

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        domain = (params.get("domain") or "").strip()
        if not domain:
            return {"error": "`domain` is required."}
        return await _semrush_get({
            "type": "domain_organic",
            "domain": domain,
            "database": params.get("database") or _DEFAULT_DB,
            "display_limit": _clamp_limit(params.get("limit") or 10),
            "export_columns": "Ph,Po,Nq,Cp,Co,Tr,Tc,Ur",
        })


class SemrushKeywordOverview(InternalTool):
    name = "semrush_keyword_overview"
    description = (
        "Single keyword metrics: monthly search volume, CPC, competition, "
        "number of results, and SERP trend. Use for 'is <keyword> worth "
        "targeting' / 'what's the volume for <keyword>'."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Phrase to look up, e.g. 'project management software'."},
            "database": {"type": "string", "description": "Country DB. Default 'us'."},
        },
        "required": ["keyword"],
    }

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        keyword = (params.get("keyword") or "").strip()
        if not keyword:
            return {"error": "`keyword` is required."}
        return await _semrush_get({
            "type": "phrase_this",
            "phrase": keyword,
            "database": params.get("database") or _DEFAULT_DB,
            "export_columns": "Ph,Nq,Cp,Co,Nr,Td",
        })


class SemrushRelatedKeywords(InternalTool):
    name = "semrush_related_keywords"
    description = (
        "Related-keyword ideas (phrase variations) for a seed keyword, with "
        "volume + CPC + competition for each suggestion. Use for content "
        "brainstorming or expanding ad keyword lists."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "description": "Seed keyword."},
            "database": {"type": "string", "description": "Country DB. Default 'us'."},
            "limit": {
                "type": "integer",
                "description": f"Max suggestions (1–{_MAX_LIMIT}). Default 15.",
            },
        },
        "required": ["keyword"],
    }

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        keyword = (params.get("keyword") or "").strip()
        if not keyword:
            return {"error": "`keyword` is required."}
        return await _semrush_get({
            "type": "phrase_related",
            "phrase": keyword,
            "database": params.get("database") or _DEFAULT_DB,
            "display_limit": _clamp_limit(params.get("limit") or 15),
            "export_columns": "Ph,Nq,Cp,Co,Nr",
        })


class SemrushCompetitors(InternalTool):
    name = "semrush_competitors"
    description = (
        "Domains that compete with the given domain in organic search, "
        "ranked by competition level. Each row includes the competitor's "
        "organic keyword count, organic traffic, and the number of common "
        "keywords with the queried domain."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Root domain, e.g. 'example.com'."},
            "database": {"type": "string", "description": "Country DB. Default 'us'."},
            "limit": {
                "type": "integer",
                "description": f"Max competitors (1–{_MAX_LIMIT}). Default 10.",
            },
        },
        "required": ["domain"],
    }

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        domain = (params.get("domain") or "").strip()
        if not domain:
            return {"error": "`domain` is required."}
        return await _semrush_get({
            "type": "domain_organic_organic",
            "domain": domain,
            "database": params.get("database") or _DEFAULT_DB,
            "display_limit": _clamp_limit(params.get("limit") or 10),
            "export_columns": "Dn,Cr,Np,Or,Ot,Oc",
        })
