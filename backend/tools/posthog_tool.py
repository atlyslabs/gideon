"""
PostHog Tools -- interact with PostHog analytics via PostHog MCP server.

Same JSON-RPC 2.0 pattern as Granola/Notion MCP.
MCP endpoint: https://mcp.posthog.com/mcp
"""

from typing import Any
import asyncio
import hashlib
import json

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

POSTHOG_MCP_URL = "https://mcp.posthog.com/mcp"

_session_cache: dict[str, str] = {}
_shared_client: httpx.AsyncClient | None = None


def _token_hash(access_token: str) -> str:
    return hashlib.sha256(access_token.encode()).hexdigest()[:32]


def _get_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(timeout=30.0)
    return _shared_client


async def _mcp_init_session(access_token: str) -> str:
    """Initialize MCP session: send initialize + notifications/initialized."""
    client = _get_client()
    base_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    init_payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "Gideon", "version": "1.0.0"},
        },
    }

    response = await client.post(POSTHOG_MCP_URL, headers=base_headers, json=init_payload)

    if response.status_code == 401:
        raise Exception("401: PostHog MCP token expired or invalid")

    # Retry once on transient server errors (Cloudflare 500/502/503)
    if response.status_code >= 500:
        logger.warning("PostHog MCP server error, retrying", status=response.status_code)
        await asyncio.sleep(2)
        response = await client.post(POSTHOG_MCP_URL, headers=base_headers, json=init_payload)
        if response.status_code >= 500:
            logger.error("PostHog MCP server error after retry", status=response.status_code, body=response.text[:200])
            raise Exception(f"PostHog MCP server error: {response.status_code}")

    session_id = (
        response.headers.get("mcp-session-id")
        or response.headers.get("Mcp-Session-Id")
        or response.headers.get("x-mcp-session-id")
    )

    # Try extracting from SSE body
    if not session_id:
        try:
            content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                for line in response.text.split("\n"):
                    line = line.strip()
                    if line.startswith("data:"):
                        data = json.loads(line[5:].strip())
                        session_id = data.get("sessionId") or data.get("session_id")
                        if session_id:
                            break
            else:
                data = response.json()
                session_id = data.get("sessionId") or data.get("session_id")
        except Exception:
            pass

    if session_id:
        logger.info("PostHog MCP session initialized", session_id=session_id[:16] + "...")
    else:
        logger.info("PostHog MCP session initialized without session ID")

    # Send initialized notification (some MCP servers return session ID here)
    notify_headers = {**base_headers}
    if session_id:
        notify_headers["Mcp-Session-Id"] = session_id

    try:
        notify_resp = await client.post(POSTHOG_MCP_URL, headers=notify_headers, json={
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        })
        if not session_id:
            session_id = (
                notify_resp.headers.get("mcp-session-id")
                or notify_resp.headers.get("Mcp-Session-Id")
                or notify_resp.headers.get("x-mcp-session-id")
            )
            if session_id:
                logger.info("PostHog MCP session ID from notification", session_id=session_id[:16] + "...")
    except Exception as e:
        logger.warning("PostHog MCP initialized notification failed", error=str(e))

    if not session_id:
        logger.error(
            "PostHog MCP: no session ID from init or notification",
            init_status=response.status_code,
            init_body=response.text[:500],
        )
        raise Exception("PostHog MCP init returned no session ID")

    return session_id


async def _get_session_id(access_token: str) -> str:
    cache_key = _token_hash(access_token)
    if cache_key in _session_cache:
        return _session_cache[cache_key]
    session_id = await _mcp_init_session(access_token)
    # Only cache non-empty session IDs (empty = init failed, should retry next call)
    if session_id:
        _session_cache[cache_key] = session_id
    return session_id


async def _mcp_call(access_token: str, method: str, params: dict | None = None, _retry: bool = True) -> dict:
    try:
        session_id = await _get_session_id(access_token)
    except Exception as e:
        return {"error": f"PostHog MCP session init failed: {e}"}

    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": method,
        "params": params or {},
    }

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": session_id,
    }

    client = _get_client()
    response = await client.post(POSTHOG_MCP_URL, headers=headers, json=payload)

    if response.status_code == 401:
        _session_cache.pop(_token_hash(access_token), None)
        raise Exception("401: PostHog MCP token expired or invalid")

    if response.status_code in (400, 404) and "session" in response.text.lower():
        _session_cache.pop(_token_hash(access_token), None)
        if _retry:
            return await _mcp_call(access_token, method, params, _retry=False)
        return {"error": "PostHog MCP session expired"}

    if response.status_code not in (200, 202):
        logger.error("PostHog MCP call failed", status=response.status_code, body=response.text[:500])
        return {"error": f"PostHog MCP request failed: {response.status_code}"}

    content_type = response.headers.get("content-type", "")
    body = response.text

    if "text/event-stream" in content_type:
        return _parse_sse_response(body)

    if body.strip():
        try:
            data = response.json()
            if "error" in data:
                return {"error": f"PostHog MCP error: {data['error']}"}
            return data.get("result", data)
        except Exception:
            return {"raw_response": body[:2000]}

    if response.status_code == 202:
        return {"status": "accepted"}

    return {"error": "Empty response from PostHog MCP"}


async def _mcp_tool_call(access_token: str, tool_name: str, arguments: dict) -> dict:
    result = await _mcp_call(access_token, "tools/call", {
        "name": tool_name,
        "arguments": arguments,
    })
    if "error" in result:
        logger.error("PostHog MCP tool error", tool=tool_name, error=str(result["error"])[:500])
    return result


def _parse_sse_response(body: str) -> dict:
    result = {}
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str:
                try:
                    parsed = json.loads(data_str)
                    if "result" in parsed:
                        result = parsed["result"]
                    elif "error" in parsed:
                        return {"error": f"PostHog MCP error: {parsed['error']}"}
                except json.JSONDecodeError:
                    continue
    return result


def _extract_text_content(result: dict) -> str:
    content = result.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------


class RunPostHogQuery(BaseTool):
    @property
    def name(self) -> str:
        return "run_posthog_query"

    @property
    def description(self) -> str:
        return "Run a HogQL analytics query on PostHog data. Returns trends, funnels, retention, or custom SQL results. Use HogQL syntax (PostHog's SQL dialect)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "HogQL query string (e.g. 'SELECT count() FROM events WHERE event = \"$pageview\" GROUP BY properties.$current_url')",
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "query-run", {
            "query": params["query"],
        })
        text = _extract_text_content(result)
        if text:
            return {"results": text, "source": "posthog_mcp"}
        return result


class SearchPostHogInsights(BaseTool):
    @property
    def name(self) -> str:
        return "search_posthog_insights"

    @property
    def description(self) -> str:
        return "Search for PostHog entities (insights, dashboards, feature flags, experiments, surveys) by name or description."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for entity name or description",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["insight", "dashboard", "feature_flag", "experiment", "survey"]},
                    "description": "Entity types to search. Default: all types.",
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {"query": params["query"]}
        if params.get("entities"):
            args["entities"] = params["entities"]
        result = await _mcp_tool_call(access_token, "entity-search", args)
        text = _extract_text_content(result)
        if text:
            return {"insights": text, "source": "posthog_mcp"}
        return result


class GetPostHogFeatureFlags(BaseTool):
    @property
    def name(self) -> str:
        return "get_posthog_feature_flags"

    @property
    def description(self) -> str:
        return "List feature flags in PostHog with their status, rollout percentage, and variants."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional search query to filter flags by key. Leave empty to list all.",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {}
        if params.get("search"):
            args["search"] = params["search"]
        result = await _mcp_tool_call(access_token, "feature-flag-get-all", args)
        text = _extract_text_content(result)
        if text:
            return {"flags": text, "source": "posthog_mcp"}
        return result


class CreatePostHogFeatureFlag(BaseTool):
    @property
    def name(self) -> str:
        return "create_posthog_feature_flag"

    @property
    def description(self) -> str:
        return "Create a new feature flag in PostHog with a name, key, and optional filters."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Feature flag key (e.g. 'new-checkout-flow')",
                },
                "name": {
                    "type": "string",
                    "description": "Human-readable name for the flag",
                },
                "active": {
                    "type": "boolean",
                    "description": "Whether the flag is active. Default true.",
                },
            },
            "required": ["key", "name"],
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {
            "key": params["key"],
            "name": params["name"],
        }
        if "active" in params:
            args["active"] = params["active"]
        result = await _mcp_tool_call(access_token, "create-feature-flag", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "posthog_mcp"}
        return result


class GetPostHogErrors(BaseTool):
    @property
    def name(self) -> str:
        return "get_posthog_errors"

    @property
    def description(self) -> str:
        return "Get recent error tracking issues from PostHog. Shows error types, counts, status, and first/last seen times."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "search": {
                    "type": "string",
                    "description": "Optional search query to filter errors",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {}
        if params.get("search"):
            args["searchQuery"] = params["search"]
        result = await _mcp_tool_call(access_token, "query-error-tracking-issues", args)
        text = _extract_text_content(result)
        if text:
            return {"errors": text, "source": "posthog_mcp"}
        return result


class ListPostHogExperiments(BaseTool):
    @property
    def name(self) -> str:
        return "list_posthog_experiments"

    @property
    def description(self) -> str:
        return "List A/B test experiments in PostHog with their status and results."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "posthog"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "experiment-get-all", {})
        text = _extract_text_content(result)
        if text:
            return {"experiments": text, "source": "posthog_mcp"}
        return result
