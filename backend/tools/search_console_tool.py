"""
Google Search Console Tools — search analytics, sites, sitemaps, URL inspection.
Uses the Google Search Console API (webmasters v3 + searchconsole v1).
"""

from typing import Any
from urllib.parse import quote

import httpx

from tools.base import BaseTool


GSC_BASE = "https://www.googleapis.com/webmasters/v3"
INSPECTION_BASE = "https://searchconsole.googleapis.com/v1"


def _headers(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}", "Accept": "application/json"}


class ListSearchConsoleSites(BaseTool):
    @property
    def name(self) -> str:
        return "list_search_console_sites"

    @property
    def description(self) -> str:
        return (
            "List all Google Search Console properties (sites) the user has access to. "
            "Returns site URLs and permission levels. Call this first to discover available sites "
            "before querying search analytics."
        )

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{GSC_BASE}/sites", headers=_headers(access_token))
        if resp.status_code != 200:
            return {"error": f"Failed to list sites: {resp.status_code} {resp.text[:1000]}"}
        data = resp.json()
        sites = [
            {
                "site_url": s.get("siteUrl", ""),
                "permission_level": s.get("permissionLevel", ""),
            }
            for s in data.get("siteEntry", [])
        ]
        return {"sites": sites, "count": len(sites)}


class QuerySearchAnalytics(BaseTool):
    @property
    def name(self) -> str:
        return "query_search_analytics"

    @property
    def description(self) -> str:
        return (
            "Query Google Search Console search analytics data for a site. "
            "Returns clicks, impressions, CTR, and position data. "
            "Supports filtering by query, page, country, device, and date dimensions. "
            "Use list_search_console_sites first to get the site URL."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "site_url": {
                    "type": "string",
                    "description": "The site URL exactly as shown in Search Console (e.g. 'https://www.example.com/' or 'sc-domain:example.com').",
                },
                "start_date": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format.",
                },
                "end_date": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format.",
                },
                "dimensions": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["query", "page", "country", "device", "date", "searchAppearance"]},
                    "description": "Dimensions to group by. Common: ['query'], ['page'], ['query', 'page'], ['date', 'query'].",
                },
                "search_type": {
                    "type": "string",
                    "enum": ["web", "image", "video", "news", "discover", "googleNews"],
                    "description": "Type of search results. Default: 'web'.",
                },
                "filters": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "dimension": {"type": "string", "enum": ["query", "page", "country", "device"]},
                            "operator": {"type": "string", "enum": ["contains", "equals", "notContains", "notEquals", "includingRegex", "excludingRegex"]},
                            "expression": {"type": "string"},
                        },
                        "required": ["dimension", "operator", "expression"],
                    },
                    "description": "Filters to apply (e.g. query contains 'pricing', page contains '/blog/').",
                },
                "row_limit": {
                    "type": "integer",
                    "description": "Max rows to return (default 100, max 25000).",
                },
            },
            "required": ["site_url", "start_date", "end_date"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        site_url = params["site_url"]
        encoded_site = quote(site_url, safe="")

        body: dict[str, Any] = {
            "startDate": params["start_date"],
            "endDate": params["end_date"],
            "rowLimit": min(params.get("row_limit", 100), 25000),
        }
        if params.get("dimensions"):
            body["dimensions"] = params["dimensions"]
        if params.get("search_type"):
            body["type"] = params["search_type"]
        if params.get("filters"):
            body["dimensionFilterGroups"] = [{"filters": params["filters"]}]

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{GSC_BASE}/sites/{encoded_site}/searchAnalytics/query",
                headers={**_headers(access_token), "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            return {"error": f"Search analytics query failed: {resp.status_code} {resp.text[:1000]}"}

        data = resp.json()
        rows = []
        dimensions = params.get("dimensions", [])
        for row in data.get("rows", []):
            entry = {
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": round(row.get("ctr", 0), 4),
                "position": round(row.get("position", 0), 1),
            }
            for i, dim in enumerate(dimensions):
                keys = row.get("keys", [])
                if i < len(keys):
                    entry[dim] = keys[i]
            rows.append(entry)

        return {"rows": rows, "count": len(rows)}


class ListSitemaps(BaseTool):
    @property
    def name(self) -> str:
        return "list_sitemaps"

    @property
    def description(self) -> str:
        return (
            "List sitemaps submitted to Google Search Console for a site. "
            "Shows sitemap URLs, types, last download time, and error counts."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "site_url": {
                    "type": "string",
                    "description": "The site URL exactly as shown in Search Console.",
                },
            },
            "required": ["site_url"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        encoded_site = quote(params["site_url"], safe="")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{GSC_BASE}/sites/{encoded_site}/sitemaps",
                headers=_headers(access_token),
            )
        if resp.status_code != 200:
            return {"error": f"Failed to list sitemaps: {resp.status_code} {resp.text[:1000]}"}

        data = resp.json()
        sitemaps = [
            {
                "path": s.get("path", ""),
                "type": s.get("type", ""),
                "last_submitted": s.get("lastSubmitted"),
                "last_downloaded": s.get("lastDownloaded"),
                "is_pending": s.get("isPending", False),
                "warnings": s.get("warnings", 0),
                "errors": s.get("errors", 0),
            }
            for s in data.get("sitemap", [])
        ]
        return {"sitemaps": sitemaps, "count": len(sitemaps)}


class InspectUrl(BaseTool):
    @property
    def name(self) -> str:
        return "inspect_url"

    @property
    def description(self) -> str:
        return (
            "Inspect a URL's indexing status in Google Search Console. "
            "Shows whether the page is indexed, crawl info, mobile usability, and rich results. "
            "Useful for debugging why a page isn't appearing in search results."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "inspection_url": {
                    "type": "string",
                    "description": "The fully qualified URL to inspect (e.g. 'https://www.example.com/blog/getting-started').",
                },
                "site_url": {
                    "type": "string",
                    "description": "The Search Console property URL that owns this page.",
                },
            },
            "required": ["inspection_url", "site_url"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        body = {
            "inspectionUrl": params["inspection_url"],
            "siteUrl": params["site_url"],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{INSPECTION_BASE}/urlInspection/index:inspect",
                headers={**_headers(access_token), "Content-Type": "application/json"},
                json=body,
            )
        if resp.status_code != 200:
            return {"error": f"URL inspection failed: {resp.status_code} {resp.text[:1000]}"}

        data = resp.json()
        result = data.get("inspectionResult", {})
        index_status = result.get("indexStatusResult", {})
        mobile = result.get("mobileUsabilityResult", {})
        rich = result.get("richResultsResult", {})

        return {
            "inspection_url": result.get("inspectionResultLink", ""),
            "index_status": {
                "verdict": index_status.get("verdict", ""),
                "coverage_state": index_status.get("coverageState", ""),
                "crawled_as": index_status.get("crawledAs", ""),
                "robots_txt_state": index_status.get("robotsTxtState", ""),
                "indexing_state": index_status.get("indexingState", ""),
                "last_crawl_time": index_status.get("lastCrawlTime"),
                "page_fetch_state": index_status.get("pageFetchState", ""),
                "referring_urls": index_status.get("referringUrls", []),
            },
            "mobile_usability": {
                "verdict": mobile.get("verdict", ""),
                "issues": [i.get("message", "") for i in mobile.get("issues", [])],
            },
            "rich_results": {
                "verdict": rich.get("verdict", ""),
                "detected_items": [
                    {"type": item.get("richResultType", ""), "issues": [i.get("message", "") for i in item.get("issues", [])]}
                    for item in rich.get("detectedItems", [])
                ],
            },
        }
