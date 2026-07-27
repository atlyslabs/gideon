"""
Meta Ads Tools — talk directly to the Meta Graph API.

Originally targeted Meta's hosted MCP server at https://mcp.facebook.com/ads,
but probing showed it's allowlist-gated to a small set of partners (Anthropic,
OpenAI, Cursor) — self-issued Meta-app tokens are rejected at the resource
layer with `401 "This resource is restricted to certain users"`. So we hit the
underlying Graph API directly.

We expose 23 read-only tools across the same five capability groups the MCP
server advertises. Six write tools (campaign create/update, ad set, ad,
catalog create, activate entity) are intentionally omitted until an approval
gate is added — see the commented block at the bottom of _TOOL_DEFS.

Key Graph API conventions:
- Auth: `Authorization: Bearer <user-access-token>` (long-lived ~60d).
- Versioning: pinned to v25.0 to match what Meta's OAuth metadata references
  (override via META_GRAPH_API_VERSION env if needed).
- Pagination: Graph returns `paging.cursors.{before,after}` and `paging.next`.
  Tools accept an `after` cursor and a `limit` (capped server-side at ~100).
- ID hygiene: ad accounts are returned as `act_<numeric>`. Always pass them as
  the full `act_…` string to write paths and metrics endpoints.
- Error shape: `{"error": {"message": ..., "code": ..., "type": ..., "fbtrace_id": ...}}`.
  We surface the message + code so Claude can decide whether to retry or give up.

Insights tool: Meta's `/insights` endpoint is the workhorse for performance
data — accepts `fields=spend,impressions,clicks,ctr,cpm,cpc,conversions,...`,
`time_range`, `level=campaign|ad_set|ad`, `breakdowns=...`, `time_increment=1`.
We expose that as `meta_ads_get_insights`. The five MCP "insights & benchmarks"
tools (advertiser_context, anomaly_signal, auction_ranking_benchmarks,
industry_benchmark, opportunity_score) are MCP-server-exclusive and not exposed
via Graph; we omit them with a note in the docstring.
"""

import os
from typing import Any
from urllib.parse import urlencode

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

GRAPH_API_VERSION = os.environ.get("META_GRAPH_API_VERSION", "v25.0")
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# Single shared async client for ALL Graph calls.
#
# httpx.AsyncClient already pools TCP connections by (scheme, host, port) — and
# every Graph call goes to graph.facebook.com:443 — so one client with a
# default-sized pool serves every user with full keep-alive. The earlier
# per-token cache was a misguided optimization: it grew unboundedly with token
# rotation (every ~60 days a token rotates → a new dict entry that never gets
# evicted), each entry held its own pool of 5–10 sockets, and none of that
# bought us connection reuse beyond what a single client already provides.
# Auth is per-request (Authorization header) so the client doesn't bind to a
# token. Lazy-init so we don't open a socket pool at import time.
_shared_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=30.0,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_client


async def _graph_get(token: str, path: str, params: dict | None = None) -> dict:
    """Make a GET request to the Graph API. Path is the resource path WITHOUT
    the version prefix or leading slash, e.g. 'me/adaccounts'."""
    if path.startswith("/"):
        path = path[1:]
    url = f"{GRAPH_BASE}/{path}"
    headers = {"Authorization": f"Bearer {token}"}
    client = _get_client()

    try:
        response = await client.get(url, headers=headers, params=params or {})
    except httpx.HTTPError as e:
        logger.error("Meta Graph network error", path=path, error=str(e))
        return {"error": f"Network error calling Meta Graph: {e}"}

    if response.status_code == 401:
        # Surface 401 verbatim so tool_executor's reactive refresh fires.
        raise Exception(f"401: Meta Graph: {response.text[:300]}")

    try:
        data = response.json()
    except Exception:
        return {"error": f"Meta Graph returned non-JSON ({response.status_code}): {response.text[:300]}"}

    if response.status_code != 200 or "error" in data:
        err = data.get("error", {}) if isinstance(data, dict) else {}
        msg = err.get("message") or response.text[:300]
        code = err.get("code")
        logger.error("Meta Graph error", path=path, status=response.status_code, code=code, message=msg[:200])
        return {"error": f"Meta Graph error ({code}): {msg}"}

    return data


def _normalize_paging(data: dict) -> dict:
    """Pull the paging cursors up to the top level so Claude can see them
    without scraping Meta's nested envelope."""
    out: dict[str, Any] = {}
    if isinstance(data.get("data"), list):
        out["data"] = data["data"]
    else:
        out.update(data)
    paging = data.get("paging") or {}
    cursors = paging.get("cursors") or {}
    if cursors.get("after"):
        out["next_cursor"] = cursors["after"]
    if paging.get("next"):
        out["has_more"] = True
    return out


# ---------------------------------------------------------------------------
# Tool catalog. Each spec compiles to one BaseTool subclass.
# Six write tools (create_*, update_entity, activate_entity, catalog_create)
# remain commented out — re-enable per-spec when an approval gate is in place.
# ---------------------------------------------------------------------------


_TOOL_DEFS: list[dict] = [
    # ---- Accounts, pages & assets (3 read) ----
    {
        "name": "meta_ads_get_ad_accounts",
        "desc": "Meta Ads — List ad accounts the user has access to. Returns id (act_*), name, currency, account_status, business owner. Always start here when you need an ad_account_id.",
        "props": {
            "fields": {"type": "string", "description": "Comma-separated Graph fields. Default: id,name,account_id,currency,account_status,business_name,timezone_name."},
            "limit": {"type": "integer", "description": "Max results per page. Default 25, max 100."},
            "after": {"type": "string", "description": "Pagination cursor from previous call."},
        },
        "required": [],
        "method": "GET",
        "path": "me/adaccounts",
        "default_params": {
            "fields": "id,name,account_id,currency,account_status,business_name,timezone_name",
            "limit": "25",
        },
    },
    {
        "name": "meta_ads_get_ad_entities",
        "desc": "Meta Ads — List campaigns / ad sets / ads under an ad account. Filter by entity_type, status, time range. Returns paginated rows.",
        "props": {
            "ad_account_id": {"type": "string", "description": "Ad account id, e.g. act_1234567890."},
            "entity_type": {"type": "string", "description": "campaigns | adsets | ads. Default: campaigns."},
            "fields": {"type": "string", "description": "Comma-separated Graph fields. Sensible defaults applied per entity_type."},
            "effective_status": {"type": "string", "description": "JSON list filter, e.g. [\"ACTIVE\"] or [\"PAUSED\",\"ACTIVE\"]."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["ad_account_id"],
        "method": "ENTITIES",  # custom dispatch
    },
    {
        "name": "meta_ads_get_pages_for_business",
        "desc": "Meta Ads — List Facebook Pages connected to a Meta Business. Pages are the identity used to publish ads.",
        "props": {
            "business_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,category,fan_count,verification_status."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["business_id"],
        "method": "GET",
        "path": "{business_id}/owned_pages",
        "default_params": {
            "fields": "id,name,category,fan_count,verification_status",
            "limit": "50",
        },
    },

    # ---- Insights & performance (1 unified Graph endpoint covers what was 7 MCP tools) ----
    {
        "name": "meta_ads_get_insights",
        "desc": (
            "Meta Ads — Performance metrics for an ad account / campaign / ad set / ad. "
            "Wraps Graph's /insights endpoint. Use `level` to set granularity, "
            "`breakdowns` to slice by age / gender / device / placement / region, "
            "`time_range` or `date_preset` for the window, and `time_increment` for "
            "daily/weekly buckets. Returns spend, impressions, clicks, CTR, CPM, CPC, "
            "conversions, ROAS, etc. (controlled by `fields`)."
        ),
        "props": {
            "object_id": {"type": "string", "description": "act_<id> for an account, or a campaign/ad_set/ad id."},
            "level": {"type": "string", "description": "account | campaign | adset | ad. Default: campaign."},
            "fields": {"type": "string", "description": "Comma-separated metric fields. Default covers spend, impressions, clicks, ctr, cpm, cpc, reach, frequency, conversions, action_values."},
            "date_preset": {"type": "string", "description": "today | yesterday | last_7d | last_14d | last_28d | last_30d | last_90d | this_month | last_month | this_quarter | last_quarter | maximum. Mutually exclusive with time_range."},
            "time_range": {"type": "string", "description": "JSON: {\"since\":\"YYYY-MM-DD\",\"until\":\"YYYY-MM-DD\"}."},
            "time_increment": {"type": "string", "description": "1 (daily), 7 (weekly), monthly, all_days. Default omits — single aggregated row."},
            "breakdowns": {"type": "string", "description": "Comma-separated breakdowns: age, gender, country, region, dma, impression_device, publisher_platform, platform_position, device_platform."},
            "filtering": {"type": "string", "description": "JSON filter list, e.g. [{\"field\":\"campaign.effective_status\",\"operator\":\"IN\",\"value\":[\"ACTIVE\"]}]."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["object_id"],
        "method": "GET",
        "path": "{object_id}/insights",
        "default_params": {
            "level": "campaign",
            "fields": "spend,impressions,clicks,ctr,cpm,cpc,reach,frequency,conversions,action_values,date_start,date_stop",
            "limit": "100",
        },
    },

    # ---- Dataset (Pixel / CAPI) — 4 read tools ----
    {
        "name": "meta_ads_get_dataset_details",
        "desc": "Meta Ads — Pixel / Conversions API dataset details. Returns id, name, code, last_fired_time, owner_business.",
        "props": {
            "dataset_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,code,last_fired_time,creation_time,owner_business."},
        },
        "required": ["dataset_id"],
        "method": "GET",
        "path": "{dataset_id}",
        "default_params": {"fields": "id,name,code,last_fired_time,creation_time,owner_business"},
    },
    {
        "name": "meta_ads_get_dataset_quality",
        "desc": "Meta Ads — Event Match Quality (EMQ) score for a Pixel/CAPI dataset. Higher EMQ = better attribution; flags missing customer-info parameters.",
        "props": {
            "dataset_id": {"type": "string"},
            "event_name": {"type": "string", "description": "Optional — narrow to a specific event (e.g. Purchase, Lead)."},
        },
        "required": ["dataset_id"],
        "method": "GET",
        "path": "{dataset_id}/event_match_quality",
    },
    {
        "name": "meta_ads_get_dataset_stats",
        "desc": "Meta Ads — Event volumes for a Pixel/CAPI dataset, split by browser vs server and dedup ratio.",
        "props": {
            "dataset_id": {"type": "string"},
            "start_time": {"type": "string", "description": "ISO 8601 or YYYY-MM-DD."},
            "end_time": {"type": "string"},
            "aggregation": {"type": "string", "description": "daily | weekly. Default daily."},
        },
        "required": ["dataset_id"],
        "method": "GET",
        "path": "{dataset_id}/stats",
    },
    {
        "name": "meta_ads_get_dataset_errors",
        "desc": "Meta Ads — Recent errors / rejections for a Pixel/CAPI dataset.",
        "props": {"dataset_id": {"type": "string"}},
        "required": ["dataset_id"],
        "method": "GET",
        "path": "{dataset_id}/da_checks",
    },

    # ---- Product catalog (9 read) ----
    {
        "name": "meta_ads_catalog_get_catalogs",
        "desc": "Meta Ads — List product catalogs owned by a Meta Business. Use to discover catalog_id before catalog_get_* calls.",
        "props": {
            "business_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,vertical,product_count."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["business_id"],
        "method": "GET",
        "path": "{business_id}/owned_product_catalogs",
        "default_params": {"fields": "id,name,vertical,product_count", "limit": "25"},
    },
    {
        "name": "meta_ads_catalog_get_details",
        "desc": "Meta Ads — Single catalog details: name, vertical, product count, owner business.",
        "props": {
            "catalog_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,vertical,product_count,business,da_display_settings,store_catalog_settings."},
        },
        "required": ["catalog_id"],
        "method": "GET",
        "path": "{catalog_id}",
        "default_params": {"fields": "id,name,vertical,product_count,business"},
    },
    {
        "name": "meta_ads_catalog_get_diagnostics",
        "desc": "Meta Ads — Catalog issues: rejected products, missing fields, broken links, expired availability.",
        "props": {"catalog_id": {"type": "string"}},
        "required": ["catalog_id"],
        "method": "GET",
        "path": "{catalog_id}/diagnostics",
    },
    {
        "name": "meta_ads_catalog_get_feed_rules",
        "desc": "Meta Ads — Automatic-update rules attached to a product feed (e.g. price localization, mapping).",
        "props": {
            "catalog_id": {"type": "string"},
            "feed_id": {"type": "string", "description": "Optional — narrow to a single feed."},
        },
        "required": ["catalog_id"],
        "method": "FEED_RULES",
    },
    {
        "name": "meta_ads_catalog_get_product_details",
        "desc": "Meta Ads — Full product data: price, availability, image, link, custom labels, GTIN, etc.",
        "props": {
            "product_id": {"type": "string", "description": "Catalog product id (NOT retailer SKU)."},
            "fields": {"type": "string", "description": "Default covers retailer_id, name, price, availability, condition, image_url, link."},
        },
        "required": ["product_id"],
        "method": "GET",
        "path": "{product_id}",
        "default_params": {
            "fields": "id,retailer_id,name,price,availability,condition,image_url,link,brand,gtin,custom_label_0,custom_label_1,review_status",
        },
    },
    {
        "name": "meta_ads_catalog_get_product_feed_details",
        "desc": "Meta Ads — Single feed status: source URL, schedule, last upload errors.",
        "props": {
            "feed_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,file_name,product_count,latest_upload,schedule."},
        },
        "required": ["feed_id"],
        "method": "GET",
        "path": "{feed_id}",
        "default_params": {"fields": "id,name,file_name,product_count,latest_upload,schedule"},
    },
    {
        "name": "meta_ads_catalog_get_product_sets",
        "desc": "Meta Ads — Product sets defined under a catalog (filter rules used by dynamic ads).",
        "props": {
            "catalog_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,product_count,filter."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["catalog_id"],
        "method": "GET",
        "path": "{catalog_id}/product_sets",
        "default_params": {"fields": "id,name,product_count,filter", "limit": "50"},
    },
    {
        "name": "meta_ads_catalog_get_product_set_products",
        "desc": "Meta Ads — Products in a product set.",
        "props": {
            "product_set_id": {"type": "string"},
            "fields": {"type": "string"},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["product_set_id"],
        "method": "GET",
        "path": "{product_set_id}/products",
        "default_params": {
            "fields": "id,retailer_id,name,price,availability,image_url,link",
            "limit": "50",
        },
    },
    {
        "name": "meta_ads_catalog_get_products",
        "desc": "Meta Ads — Paginated products in a catalog. Use `filter` (JSON) for retailer_id / availability / price queries.",
        "props": {
            "catalog_id": {"type": "string"},
            "filter": {"type": "string", "description": "JSON filter, e.g. {\"availability\":{\"eq\":\"in stock\"}}."},
            "fields": {"type": "string"},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["catalog_id"],
        "method": "GET",
        "path": "{catalog_id}/products",
        "default_params": {
            "fields": "id,retailer_id,name,price,availability,condition,image_url,link,brand",
            "limit": "50",
        },
    },

    # ---- Help (Graph API can fetch help center articles via the search endpoint) ----
    {
        "name": "meta_ads_get_business_users",
        "desc": "Meta Ads — Users assigned to a Meta Business (helpful for ownership / RBAC questions).",
        "props": {
            "business_id": {"type": "string"},
            "fields": {"type": "string", "description": "Default: id,name,email,role."},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": ["business_id"],
        "method": "GET",
        "path": "{business_id}/business_users",
        "default_params": {"fields": "id,name,email,role", "limit": "100"},
    },
    {
        "name": "meta_ads_list_businesses",
        "desc": "Meta Ads — List Meta Businesses the user has access to. Use first to find a business_id.",
        "props": {
            "fields": {"type": "string"},
            "limit": {"type": "integer"},
            "after": {"type": "string"},
        },
        "required": [],
        "method": "GET",
        "path": "me/businesses",
        "default_params": {
            "fields": "id,name,verification_status,created_time,primary_page",
            "limit": "25",
        },
    },

    # ---- WRITE TOOLS — DISABLED (uncomment + add execute branch when ready) ----
    # The 6 write tools are intentionally not exposed:
    #   - meta_ads_create_campaign       POST {act_id}/campaigns
    #   - meta_ads_create_ad_set         POST {act_id}/adsets
    #   - meta_ads_create_ad             POST {act_id}/ads
    #   - meta_ads_update_entity         POST {entity_id}
    #   - meta_ads_activate_entity       POST {entity_id} status=ACTIVE
    #   - meta_ads_catalog_create        POST {business_id}/owned_product_catalogs
    # Re-enable per-spec when an approval gate (chat confirmation, RBAC, or
    # both) is in place — these mutate live ads / spend money.
]


_DEFAULT_FIELDS_BY_ENTITY = {
    "campaigns": "id,name,objective,status,effective_status,daily_budget,lifetime_budget,start_time,stop_time,created_time,updated_time",
    "adsets": "id,name,campaign_id,status,effective_status,daily_budget,lifetime_budget,billing_event,optimization_goal,start_time,end_time,targeting",
    "ads": "id,name,adset_id,campaign_id,status,effective_status,creative,created_time,updated_time",
}


def _make_tool_class(spec: dict) -> type:
    """Build a BaseTool subclass for one Graph API call."""

    class _MetaAdsTool(BaseTool):
        @property
        def name(self) -> str:
            return spec["name"]

        @property
        def description(self) -> str:
            return spec["desc"]

        @property
        def input_schema(self) -> dict:
            return {
                "type": "object",
                "properties": dict(spec["props"]),
                "required": list(spec.get("required", [])),
                "additionalProperties": True,
            }

        @property
        def provider(self) -> str:
            return "meta_ads"

        async def execute(self, params: dict[str, Any], access_token: str) -> dict:
            method = spec.get("method", "GET")

            if method == "GET":
                # Substitute placeholders in the path template (e.g. {ad_account_id}).
                path = spec["path"]
                consumed: set[str] = set()
                for key, value in (params or {}).items():
                    placeholder = "{" + key + "}"
                    if placeholder in path:
                        path = path.replace(placeholder, str(value))
                        consumed.add(key)

                # Build query params: defaults + caller overrides (minus path
                # placeholders we already consumed).
                query: dict[str, Any] = {}
                for k, v in (spec.get("default_params") or {}).items():
                    query[k] = v
                for k, v in (params or {}).items():
                    if k in consumed or v is None or v == "":
                        continue
                    query[k] = v

                data = await _graph_get(access_token, path, query)
                if "error" in data:
                    return data
                return _normalize_paging(data)

            if method == "ENTITIES":
                # Entity dispatch: campaigns / adsets / ads
                ad_account_id = (params or {}).get("ad_account_id", "")
                entity_type = (params or {}).get("entity_type") or "campaigns"
                if entity_type not in _DEFAULT_FIELDS_BY_ENTITY:
                    return {"error": f"entity_type must be one of: {', '.join(_DEFAULT_FIELDS_BY_ENTITY)}"}
                query = {
                    "fields": (params or {}).get("fields") or _DEFAULT_FIELDS_BY_ENTITY[entity_type],
                    "limit": (params or {}).get("limit") or 25,
                }
                if (params or {}).get("effective_status"):
                    query["effective_status"] = (params or {}).get("effective_status")
                if (params or {}).get("after"):
                    query["after"] = (params or {}).get("after")
                data = await _graph_get(access_token, f"{ad_account_id}/{entity_type}", query)
                if "error" in data:
                    return data
                return _normalize_paging(data)

            if method == "FEED_RULES":
                catalog_id = (params or {}).get("catalog_id", "")
                feed_id = (params or {}).get("feed_id")
                if feed_id:
                    return _normalize_paging(await _graph_get(access_token, f"{feed_id}/rules"))
                # Otherwise list feeds, then their rules — Graph doesn't expose a
                # single "all rules under a catalog" endpoint, so we fan out.
                feeds = await _graph_get(access_token, f"{catalog_id}/product_feeds", {"fields": "id,name", "limit": "50"})
                if "error" in feeds:
                    return feeds
                feed_list = feeds.get("data", [])
                rules = []
                for feed in feed_list:
                    fid = feed.get("id")
                    if not fid:
                        continue
                    feed_rules = await _graph_get(access_token, f"{fid}/rules")
                    rules.append({"feed_id": fid, "feed_name": feed.get("name"), "rules": feed_rules.get("data", [])})
                return {"feeds": rules}

            return {"error": f"Unknown tool dispatch method: {method}"}

    _MetaAdsTool.__name__ = "".join(p.capitalize() for p in spec["name"].split("_"))
    return _MetaAdsTool


_GENERATED: dict[str, type] = {}
for _spec in _TOOL_DEFS:
    _cls = _make_tool_class(_spec)
    globals()[_cls.__name__] = _cls
    _GENERATED[_spec["name"]] = _cls


META_ADS_TOOL_INSTANCES: list[BaseTool] = [cls() for cls in _GENERATED.values()]
