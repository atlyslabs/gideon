"""
Notion Tools — interact with Notion via Notion MCP server.

The MCP token (from mcp.notion.com OAuth) is used to call the MCP server,
which provides JSON-RPC tools for Notion operations.
"""

from typing import Any, Optional
import json

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

NOTION_MCP_URL = "https://mcp.notion.com/mcp"
NOTION_API = "https://api.notion.com/v1"


def _notion_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }


# Cache session IDs per token to avoid re-initializing every call.
# A cached value of None means "this server is stateless" — see _mcp_init_session.
_session_cache: dict[str, Optional[str]] = {}


async def _mcp_init_session(access_token: str) -> Optional[str]:
    """Initialize an MCP session and return the session ID, or None if the
    server runs statelessly.

    As of June 2026 Notion's hosted MCP server (serverInfo 1.2.0) negotiates
    `initialize` successfully over SSE but no longer returns an `Mcp-Session-Id`
    header — it operates statelessly, and subsequent tool calls succeed with no
    session header. Treating the missing header as fatal (the previous behavior)
    broke every Notion call for every user. A missing session id is therefore a
    normal, supported outcome now; we only fail on a real auth error or a
    JSON-RPC error in the init response.
    """
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "Gideon", "version": "1.0.0"},
        },
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(
            NOTION_MCP_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json=payload,
        )

    if response.status_code == 401:
        raise Exception("401: Notion MCP token expired or invalid")

    if response.status_code not in (200, 202):
        logger.error(
            "Notion MCP init failed",
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body_len=len(response.text or ""),
        )
        raise Exception(
            f"Failed to initialize Notion MCP session (HTTP {response.status_code})"
        )

    # Parse the init body uniformly into the JSON-RPC `result` dict (success),
    # {"error": ...} (server reported an error), or {} (empty/malformed).
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # _parse_sse_response returns the inner result dict, {"error": ...}, or {}.
        parsed = _parse_sse_response(response.text)
    else:
        try:
            body_json = response.json()
            if isinstance(body_json, dict) and "error" in body_json:
                parsed = {"error": body_json["error"]}
            elif isinstance(body_json, dict) and "result" in body_json:
                parsed = body_json["result"]
            else:
                parsed = {}
        except Exception:
            parsed = {}

    # Surface a genuine JSON-RPC error in the init body (e.g. unsupported
    # protocol version) instead of silently proceeding to tool calls.
    if isinstance(parsed, dict) and parsed.get("error"):
        logger.error(
            "Notion MCP init returned error", error=str(parsed["error"])[:200]
        )
        raise Exception("Failed to initialize Notion MCP session")

    session_id = response.headers.get("mcp-session-id")
    if session_id:
        logger.info("Notion MCP session initialized", session_id=session_id[:16] + "...")
        return session_id

    # No session id. Only treat this as a (valid) stateless server if the body
    # actually carried a successful `initialize` result. A malformed/empty body
    # must fail fast here — caching it as "stateless" would defer the failure to
    # every later tool call with a far more confusing error.
    init_ok = isinstance(parsed, dict) and (
        "capabilities" in parsed or "protocolVersion" in parsed
    )
    if init_ok:
        # Stateless server (Notion MCP 1.2.0+) — no session id, none needed.
        logger.info("Notion MCP initialized (stateless, no session id)")
        return None

    logger.error(
        "Notion MCP init returned no session id and no valid result",
        status=response.status_code,
        content_type=content_type,
        body_len=len(response.text or ""),
    )
    raise Exception(
        "Failed to initialize Notion MCP session (unparseable init response)"
    )


async def _get_session_id(access_token: str) -> Optional[str]:
    """Get or create an MCP session ID for this token (None if stateless)."""
    # Use first 32 chars of token as cache key
    cache_key = access_token[:32]
    if cache_key in _session_cache:
        return _session_cache[cache_key]

    session_id = await _mcp_init_session(access_token)
    _session_cache[cache_key] = session_id
    return session_id


async def _mcp_call(access_token: str, method: str, params: dict | None = None, *, _retried_session: bool = False) -> dict:
    """Make a JSON-RPC call to the Notion MCP server with session."""
    session_id = await _get_session_id(access_token)

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
    }
    # Only sent when the server issued one. Notion MCP 1.2.0+ is stateless and
    # rejects nothing for its absence; older/session-based servers still get it.
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            NOTION_MCP_URL,
            headers=headers,
            json=payload,
        )

    if response.status_code == 401:
        _session_cache.pop(access_token[:32], None)
        raise Exception("401: Notion MCP token expired or invalid")

    if response.status_code == 400 and "Session-Id" in response.text:
        _session_cache.pop(access_token[:32], None)
        # Re-init the session ONCE. Without this guard a persistently-rejected
        # session would recurse forever.
        if _retried_session:
            return {"error": "Notion MCP session could not be established (repeated Session-Id errors)"}
        return await _mcp_call(access_token, method, params, _retried_session=True)

    if response.status_code not in (200, 202):
        logger.error(
            "Notion MCP call failed",
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            body_len=len(response.text or ""),
        )
        return {"error": f"Notion MCP request failed: {response.status_code}"}

    content_type = response.headers.get("content-type", "")
    body = response.text

    # Handle SSE (text/event-stream) responses
    if "text/event-stream" in content_type:
        return _parse_sse_response(body)

    # Handle JSON responses
    if body.strip():
        try:
            data = response.json()
            if "error" in data:
                return {"error": f"Notion MCP error: {data['error']}"}
            return data.get("result", data)
        except Exception:
            logger.warning(
                "Notion MCP non-JSON response",
                content_type=content_type,
                body_len=len(body or ""),
            )
            return {"raw_response": body[:2000]}

    # Empty 202 response — accepted but async, return acknowledgement
    if response.status_code == 202:
        return {"status": "accepted", "message": "Request accepted by Notion MCP"}

    return {"error": "Empty response from Notion MCP"}


async def _mcp_list_tools(access_token: str) -> dict:
    """List available tools on the Notion MCP server (debug helper)."""
    return await _mcp_call(access_token, "tools/list", {})


async def _mcp_tool_call(access_token: str, tool_name: str, arguments: dict) -> dict:
    """Call a specific tool on the Notion MCP server."""
    result = await _mcp_call(
        access_token,
        "tools/call",
        {
            "name": tool_name,
            "arguments": arguments,
        },
    )
    if "error" in result:
        logger.error(
            "Notion MCP tool error", tool=tool_name, error=str(result["error"])[:500]
        )
    return result


def _parse_sse_response(body: str) -> dict:
    """Parse a text/event-stream response and extract the last JSON-RPC result."""
    result = {}
    for line in body.split("\n"):
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str:
                try:
                    parsed = json.loads(data_str)
                    # Keep the last result (tool call response)
                    if "result" in parsed:
                        result = parsed["result"]
                    elif "error" in parsed:
                        return {"error": f"Notion MCP error: {parsed['error']}"}
                except json.JSONDecodeError:
                    continue
    return result


def _extract_text_content(result: dict) -> str:
    """Extract text from MCP tool call result."""
    content = result.get("content", [])
    parts = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            parts.append(item.get("text", ""))
    return "\n".join(parts)


async def notion_mcp_search(
    access_token: str, query: str = "", page_size: int = 10
) -> list[dict]:
    """
    Search Notion pages/databases via MCP. Returns a list of dicts with type, title, url, last_edited.
    Used by background services (smart_onboarding, workflow_suggestion, workflow_execution, hub_router).
    """
    result = await _mcp_tool_call(
        access_token,
        "notion-search",
        {
            "query": query,
            "limit": page_size,
        },
    )
    text = _extract_text_content(result)
    if not text:
        return []
    try:
        data = json.loads(text)
        pages = []
        results_list = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(results_list, dict):
            results_list = results_list.get("results", [])
        if not isinstance(results_list, list):
            results_list = []

        for item in results_list:
            if not isinstance(item, dict):
                continue
            obj_type = item.get("object", "page")

            # Try multiple ways to extract title
            title = ""
            # 1. Direct title field (databases)
            if obj_type == "database":
                title_parts = item.get("title", [])
                if isinstance(title_parts, list):
                    title = "".join(
                        t.get("plain_text", "")
                        for t in title_parts
                        if isinstance(t, dict)
                    )
                elif isinstance(title_parts, str):
                    title = title_parts

            # 2. Properties → title type
            if not title:
                props = item.get("properties", {})
                for prop in props.values():
                    if isinstance(prop, dict) and prop.get("type") == "title":
                        title_arr = prop.get("title", [])
                        if isinstance(title_arr, list):
                            title = "".join(
                                t.get("plain_text", "")
                                for t in title_arr
                                if isinstance(t, dict)
                            )
                        break

            # 3. MCP simplified format — "title" as string
            if not title and isinstance(item.get("title"), str):
                title = item["title"]

            # 4. "name" field
            if not title:
                title = item.get("name", "")

            url = item.get("url", "")
            if not url and item.get("id"):
                url = f"https://notion.so/{item['id'].replace('-', '')}"

            pages.append(
                {
                    "type": obj_type,
                    "title": title or "(Untitled)",
                    "url": url,
                    "last_edited": item.get("last_edited_time", ""),
                }
            )
        return pages
    except (json.JSONDecodeError, KeyError, TypeError):
        # If JSON parsing fails, try to parse as markdown text
        pages = []
        for line in text.split("\n"):
            line = line.strip()
            if line.startswith("- ") or line.startswith("* "):
                title = line[2:].strip()
                if title:
                    pages.append(
                        {"type": "page", "title": title, "url": "", "last_edited": ""}
                    )
        return pages


async def notion_mcp_fetch(access_token: str, url: str) -> str:
    """
    Fetch any Notion URL (page or database) via MCP. Returns raw text content.
    Used by hub_router for database queries.
    """
    result = await _mcp_tool_call(access_token, "notion-fetch", {"id": url})
    return _extract_text_content(result)


class SearchNotionPages(BaseTool):
    @property
    def name(self) -> str:
        return "search_notion_pages"

    @property
    def description(self) -> str:
        return "Search for pages and databases in Notion by title or content. Returns pages with clickable links. Present results as a numbered list with linked titles, not as a table."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query string",
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(
            access_token,
            "notion-search",
            {
                "query": params["query"],
                "limit": 10,
            },
        )
        # MCP returns JSON-as-text inside content — parse and format with proper URLs
        text = _extract_text_content(result)
        if text:
            try:
                data = json.loads(text)
                pages = []
                for item in data.get("results", []):
                    page_id = item.get("url", "") or item.get("id", "")
                    pages.append(
                        {
                            "title": item.get("title", ""),
                            "url": f"https://notion.so/{page_id}",
                            "type": item.get("type", "page"),
                            "last_edited": item.get("timestamp", ""),
                        }
                    )
                # Pre-format as markdown list for better rendering
                lines = []
                for i, p in enumerate(pages, 1):
                    date = p["last_edited"][:10] if p["last_edited"] else ""
                    lines.append(f"{i}. [{p['title']}]({p['url']}) — {date}")
                return {
                    "formatted": "\n".join(lines),
                    "pages": pages,
                    "count": len(pages),
                    "hint": "Present the 'formatted' field directly as markdown. Each line is a clickable link.",
                }
            except json.JSONDecodeError:
                return {"results": text}
        return result


class ReadNotionPage(BaseTool):
    @property
    def name(self) -> str:
        return "read_notion_page"

    @property
    def description(self) -> str:
        return (
            "Read the content of a Notion page. Provide a page URL or page ID. "
            "NOTE: This integration uses OAuth — the user authorized Gideon for "
            "their whole workspace at connect time. There is NO per-page 'Share → "
            "Connect to Gideon' flow (that's the older internal-integration model "
            "which does not apply here). If a fetch returns not-found, the page "
            "is in a different workspace, was deleted, or the URL has the wrong "
            "ID — do NOT instruct the user to share the page with the integration."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_url": {
                    "type": "string",
                    "description": "The Notion page URL (e.g. https://notion.so/Page-Title-abc123)",
                },
            },
            "required": ["page_url"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        return await _fetch_notion_resource(
            access_token, params["page_url"], kind="page"
        )


class ReadNotionDatabase(BaseTool):
    @property
    def name(self) -> str:
        return "read_notion_database"

    @property
    def description(self) -> str:
        return (
            "Query a Notion database. Provide the database URL. "
            "OAuth-based connection — see read_notion_page for the same "
            "no-per-page-share caveat on not-found errors."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "database_url": {
                    "type": "string",
                    "description": "The Notion database URL",
                },
            },
            "required": ["database_url"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        return await _fetch_notion_resource(
            access_token, params["database_url"], kind="database"
        )


def _looks_like_not_found(payload: str) -> bool:
    """Heuristic: does the MCP error/response indicate the resource doesn't
    exist or isn't visible to this OAuth token? Notion returns several
    error shapes — match the ones we've seen."""
    if not payload:
        return False
    p = payload.lower()
    return (
        "object_not_found" in p
        or "could not find" in p
        or "not_found" in p
        or "no such block" in p
        or "404" in p
    )


async def _fetch_notion_resource(access_token: str, url_or_id: str, kind: str) -> dict:
    """Shared fetch path for read_notion_page / read_notion_database.

    Strips query strings (e.g. `?source=copy_link`) by extracting the bare
    32-char ID first, falling back to the raw input only if extraction fails
    (older Notion URLs / unusual shapes). On not-found, returns a structured
    error that tells Claude the OAuth-specific truth so it doesn't fabricate
    'Share → Connect to Gideon' steps from internal-integration docs.
    """
    notion_id = _extract_notion_id(url_or_id) or url_or_id
    result = await _mcp_tool_call(access_token, "notion-fetch", {"id": notion_id})

    # Detect not-found / no-access — MCP surfaces these as text in `content`
    # rather than a top-level error, so we sniff both places.
    err_payload = ""
    if isinstance(result.get("error"), (str, dict)):
        err_payload = str(result["error"])
    text = _extract_text_content(result)
    if not text:
        text = ""
    combined = f"{err_payload}\n{text}"

    # Two separate error paths — DO NOT lump them together. Treating every
    # `isError` as "not found" sends users with a rate-limit / server-error
    # /malformed-request down a Notion-reconnect rabbit hole that won't fix
    # the actual problem.
    is_error = bool(result.get("isError")) or bool(err_payload)
    if not _looks_like_real_content(text):
        if _looks_like_not_found(combined):
            # OAuth-specific anti-hallucination message — see _fetch_notion_resource
            # docstring for why we spell this out explicitly.
            return {
                "error": (
                    f"Notion {kind} not found or not accessible. "
                    f"Likely causes: the {kind} is in a different Notion workspace "
                    f"than the one connected to Gideon, the {kind} was deleted, or "
                    f"the URL doesn't contain a valid Notion ID. "
                    f"This integration uses OAuth — DO NOT tell the user to share "
                    f"the {kind} with an integration via 'Share → Connect to'; that "
                    f"flow only exists for Notion's internal integrations and does "
                    f"not apply to OAuth apps. If the user is on a different "
                    f"workspace, they need to reconnect Notion from Integrations "
                    f"and select that workspace."
                ),
                "id_used": notion_id,
                "raw": text[:500] if text else None,
            }
        if is_error:
            # Generic MCP error — surface the actual cause so Claude can
            # relay it accurately (rate limit / network / validation / etc.)
            # instead of fabricating a fix that won't help.
            detail = err_payload or text[:300] or "Unknown error"
            return {
                "error": f"Notion MCP error: {detail}",
                "id_used": notion_id,
                "raw": text[:500] if text else None,
            }

    meta: dict[str, Any] = {}
    if text:
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                for key in (
                    "id",
                    "url",
                    "created_time",
                    "last_edited_time",
                    "created_by",
                    "last_edited_by",
                    "parent",
                    "archived",
                ):
                    if key in data:
                        meta[key] = data[key]
        except (json.JSONDecodeError, TypeError):
            pass
        return {"content": text, "metadata": meta, "source": "notion_mcp"}
    return result


def _looks_like_real_content(text: str) -> bool:
    """Quick sanity check: did the MCP return an actual Notion page payload
    (JSON with `object`/`id` keys) vs an error masquerading as text? Used to
    avoid false-positive 'not found' on edge cases where the error sniffer
    matches incidental text inside valid content."""
    if not text:
        return False
    t = text.strip()
    if not t.startswith("{"):
        return False
    return '"object"' in t or '"id"' in t or '"properties"' in t


class CreateNotionPage(BaseTool):
    @property
    def name(self) -> str:
        return "create_notion_page"

    @property
    def description(self) -> str:
        return (
            "Create a new sub-page under an existing Notion PAGE (not a database). "
            "Provide title, parent_url (must be a page URL), and optional markdown content. "
            "To add a ROW to a database, use create_notion_database_row instead — passing a database URL here will fail."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Page title",
                },
                "parent_url": {
                    "type": "string",
                    "description": (
                        "URL of the parent PAGE (not a database). For database rows, "
                        "use create_notion_database_row instead."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "Page content in markdown format",
                },
            },
            "required": ["title"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        page: dict[str, Any] = {
            "properties": {"title": params["title"]},
        }
        if params.get("content"):
            page["content"] = params["content"]

        call_args: dict[str, Any] = {"pages": [page]}
        if params.get("parent_url"):
            pid = _extract_notion_id(params["parent_url"])
            if pid:
                call_args["parent"] = {"type": "page_id", "page_id": pid}

        logger.info("Creating Notion page", call_args=str(call_args)[:300])
        result = await _mcp_tool_call(access_token, "notion-create-pages", call_args)
        logger.info("Notion create page result", result=str(result)[:500])
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class CreateNotionDatabaseRow(BaseTool):
    @property
    def name(self) -> str:
        return "create_notion_database_row"

    @property
    def description(self) -> str:
        return (
            "Add a row to a Notion database. `data_source_url` is the database URL or data_source id. "
            "`properties` is a dict keyed by the database's actual column names — fetch the database "
            "first with read_notion_database to learn the schema. The title column accepts a string. "
            "Use this — NOT create_notion_page — whenever the parent is a database; create_notion_page "
            "with a database URL will fail because Notion requires parent.type='data_source_id' for rows."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data_source_url": {
                    "type": "string",
                    "description": "Notion database/data source URL or raw data_source id.",
                },
                "properties": {
                    "type": "object",
                    "description": "Property values keyed by the database's column names (from read_notion_database). "
                    'Example: {"Name": "Login bug", "Status": "Open", "Priority": "High"}.',
                },
                "content": {
                    "type": "string",
                    "description": "Optional markdown body for the new row's page.",
                },
            },
            "required": ["data_source_url", "properties"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        ds_id = (
            _extract_notion_id(params["data_source_url"]) or params["data_source_url"]
        )
        page: dict[str, Any] = {"properties": params["properties"]}
        if params.get("content"):
            page["content"] = params["content"]
        call_args: dict[str, Any] = {
            "pages": [page],
            "parent": {"type": "data_source_id", "data_source_id": ds_id},
        }
        result = await _mcp_tool_call(access_token, "notion-create-pages", call_args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


def _extract_notion_id(url_or_id: str) -> str | None:
    """Pull a 32-char hex ID out of a Notion URL or accept a raw ID.
    Notion URLs append the page ID at the very end of the path, either as a
    dashed UUID or a bare 32-char hex run. Titles in the URL can contain
    incidental hex letters (e.g. 'Banerjee'), so we must anchor on the end of
    the path, not just take the first hex run.
    """
    import re

    cleaned = url_or_id.split("?")[0].split("#")[0].rstrip("/")
    # Dashed UUID form first
    uuid_match = re.search(
        r"([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})", cleaned
    )
    if uuid_match:
        return uuid_match.group(1).replace("-", "")
    # Bare: last 32 hex chars at end of path
    no_hyphens = cleaned.replace("-", "")
    tail_match = re.search(r"([a-f0-9]{32})$", no_hyphens)
    return tail_match.group(1) if tail_match else None


class UpdateNotionPage(BaseTool):
    @property
    def name(self) -> str:
        return "update_notion_page"

    @property
    def description(self) -> str:
        return (
            "Update an existing Notion page. Pick exactly one command:\n"
            "- update_properties: change page properties (pass `properties` dict; for pages in a database "
            "use the SQLite schema from fetch; for standalone pages, only `title` is allowed).\n"
            "- replace_content: replace the entire page body (pass `new_str`).\n"
            "- update_content: search-and-replace within existing content (pass `content_updates` array of {old_str, new_str})."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_url": {"type": "string", "description": "Notion page URL or ID"},
                "command": {
                    "type": "string",
                    "enum": ["update_properties", "update_content", "replace_content"],
                    "description": "Which update operation to perform",
                },
                "properties": {
                    "type": "object",
                    "description": 'For update_properties. Example: {"Status": "Done"} or {"title": "New title"}',
                },
                "new_str": {
                    "type": "string",
                    "description": "For replace_content. The new full page body.",
                },
                "content_updates": {
                    "type": "array",
                    "description": "For update_content. List of {old_str, new_str} replacements.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "old_str": {"type": "string"},
                            "new_str": {"type": "string"},
                        },
                        "required": ["old_str", "new_str"],
                    },
                },
            },
            "required": ["page_url", "command"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        page_id = _extract_notion_id(params["page_url"]) or params["page_url"]
        args: dict[str, Any] = {"page_id": page_id, "command": params["command"]}
        if params["command"] == "update_properties":
            if not params.get("properties"):
                return {"error": "update_properties requires `properties`"}
            args["properties"] = params["properties"]
        elif params["command"] == "replace_content":
            if "new_str" not in params:
                return {"error": "replace_content requires `new_str`"}
            args["new_str"] = params["new_str"]
        elif params["command"] == "update_content":
            if not params.get("content_updates"):
                return {"error": "update_content requires `content_updates`"}
            args["content_updates"] = params["content_updates"]
        result = await _mcp_tool_call(access_token, "notion-update-page", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class DuplicateNotionPage(BaseTool):
    @property
    def name(self) -> str:
        return "duplicate_notion_page"

    @property
    def description(self) -> str:
        return "Duplicate an existing Notion page. Optionally specify a parent to place the copy in."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_url": {
                    "type": "string",
                    "description": "URL of the page to duplicate",
                },
                "parent_url": {
                    "type": "string",
                    "description": "Optional destination parent page URL. Defaults to same parent.",
                },
            },
            "required": ["page_url"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {
            "page_id": _extract_notion_id(params["page_url"]) or params["page_url"],
        }
        if params.get("parent_url"):
            pid = _extract_notion_id(params["parent_url"])
            if pid:
                args["parent"] = {"type": "page_id", "page_id": pid}
        result = await _mcp_tool_call(access_token, "notion-duplicate-page", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class MoveNotionPages(BaseTool):
    @property
    def name(self) -> str:
        return "move_notion_pages"

    @property
    def description(self) -> str:
        return "Move one or more Notion pages under a new parent page."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_urls": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "URLs or IDs of pages to move",
                },
                "target_parent_url": {
                    "type": "string",
                    "description": "URL or ID of the destination parent page",
                },
            },
            "required": ["page_urls", "target_parent_url"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        ids = [_extract_notion_id(u) or u for u in params["page_urls"]]
        target = (
            _extract_notion_id(params["target_parent_url"])
            or params["target_parent_url"]
        )
        result = await _mcp_tool_call(
            access_token,
            "notion-move-pages",
            {
                "page_or_database_ids": ids,
                "new_parent": {"type": "page_id", "page_id": target},
            },
        )
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class CreateNotionDatabase(BaseTool):
    @property
    def name(self) -> str:
        return "create_notion_database"

    @property
    def description(self) -> str:
        return (
            "Create a new Notion database under a parent page using SQL DDL syntax. "
            'Example schema: `CREATE TABLE "Tasks" ("Name" TITLE, "Status" SELECT(\'Todo\', \'Done\'), "Due" DATE)`. '
            "Column names must be double-quoted; type options use single quotes."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Database title"},
                "parent_url": {"type": "string", "description": "Parent page URL"},
                "schema": {
                    "type": "string",
                    "description": "SQL DDL CREATE TABLE statement defining the database schema.",
                },
                "description": {
                    "type": "string",
                    "description": "Optional database description",
                },
            },
            "required": ["title", "parent_url", "schema"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        pid = _extract_notion_id(params["parent_url"])
        if not pid:
            return {"error": "Could not extract parent page ID from URL"}
        args: dict[str, Any] = {
            "parent": {"type": "page_id", "page_id": pid},
            "title": params["title"],
            "schema": params["schema"],
        }
        if params.get("description"):
            args["description"] = params["description"]
        result = await _mcp_tool_call(access_token, "notion-create-database", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class UpdateNotionDataSource(BaseTool):
    @property
    def name(self) -> str:
        return "update_notion_data_source"

    @property
    def description(self) -> str:
        return (
            "Update a Notion database's data source — rename, re-describe, or alter schema via SQL DDL. "
            "Supports ADD COLUMN, DROP COLUMN, RENAME COLUMN, ALTER COLUMN SET."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "Data source ID, collection:// URI, or database URL",
                },
                "statements": {
                    "type": "string",
                    "description": "Semicolon-separated SQL DDL statements (e.g. `ALTER TABLE ADD COLUMN \"Priority\" SELECT('High', 'Low')`)",
                },
                "title": {
                    "type": "string",
                    "description": "New database/data source title",
                },
                "description": {"type": "string", "description": "New description"},
                "is_inline": {"type": "boolean"},
                "in_trash": {
                    "type": "boolean",
                    "description": "Set true to move to trash",
                },
            },
            "required": ["data_source_id"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        ds_id = _extract_notion_id(params["data_source_id"]) or params["data_source_id"]
        args: dict[str, Any] = {"data_source_id": ds_id}
        for key in ("statements", "title", "description", "is_inline", "in_trash"):
            if key in params:
                args[key] = params[key]
        if len(args) == 1:
            return {
                "error": "Provide at least one of: statements, title, description, is_inline, in_trash"
            }
        result = await _mcp_tool_call(access_token, "notion-update-data-source", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class CreateNotionView(BaseTool):
    @property
    def name(self) -> str:
        return "create_notion_view"

    @property
    def description(self) -> str:
        return (
            "Create a new view on a Notion database. Provide data_source_id (required) plus "
            "optionally database_id (adds a view tab to the database) or parent_page_id "
            "(creates an inline linked-view block). Exactly one of database_id/parent_page_id."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "data_source_id": {
                    "type": "string",
                    "description": "Data source (collection) ID, collection:// URI, or bare UUID",
                },
                "name": {"type": "string", "description": "View name"},
                "type": {
                    "type": "string",
                    "enum": [
                        "table",
                        "board",
                        "list",
                        "calendar",
                        "timeline",
                        "gallery",
                        "form",
                        "chart",
                        "map",
                        "dashboard",
                    ],
                },
                "database_id": {
                    "type": "string",
                    "description": "Database URL/UUID to add a view tab to. Mutually exclusive with parent_page_id.",
                },
                "parent_page_id": {
                    "type": "string",
                    "description": "Page URL/UUID to create an inline linked-view on. Mutually exclusive with database_id.",
                },
                "configure": {
                    "type": "string",
                    "description": "View configuration DSL (FILTER, SORT BY, GROUP BY, etc.)",
                },
            },
            "required": ["data_source_id", "name", "type"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Notion views attach to either an inline database (database_id) OR a standalone
        # page (parent_page_id), never both. Reject ambiguous input up front.
        if params.get("database_id") and params.get("parent_page_id"):
            return {"error": "Pass either database_id or parent_page_id, not both."}
        ds_id = _extract_notion_id(params["data_source_id"]) or params["data_source_id"]
        args: dict[str, Any] = {
            "data_source_id": ds_id,
            "name": params["name"],
            "type": params["type"],
        }
        if params.get("database_id"):
            args["database_id"] = (
                _extract_notion_id(params["database_id"]) or params["database_id"]
            )
        if params.get("parent_page_id"):
            args["parent_page_id"] = (
                _extract_notion_id(params["parent_page_id"]) or params["parent_page_id"]
            )
        if params.get("configure"):
            args["configure"] = params["configure"]
        result = await _mcp_tool_call(access_token, "notion-create-view", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class UpdateNotionView(BaseTool):
    @property
    def name(self) -> str:
        return "update_notion_view"

    @property
    def description(self) -> str:
        return "Update a Notion database view's name or configuration. Only include fields you want to change."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "view_id": {
                    "type": "string",
                    "description": "View ID, view:// URI, or Notion URL with ?v= param",
                },
                "name": {"type": "string", "description": "New view name"},
                "configure": {
                    "type": "string",
                    "description": "View configuration DSL (FILTER, SORT BY, GROUP BY, CLEAR, etc.)",
                },
            },
            "required": ["view_id"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {"view_id": params["view_id"]}
        if params.get("name"):
            args["name"] = params["name"]
        if params.get("configure"):
            args["configure"] = params["configure"]
        if len(args) == 1:
            return {"error": "Provide at least one of: name, configure"}
        result = await _mcp_tool_call(access_token, "notion-update-view", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class CreateNotionComment(BaseTool):
    @property
    def name(self) -> str:
        return "create_notion_comment"

    @property
    def description(self) -> str:
        return (
            "Add a comment to a Notion page, or reply to an existing discussion. "
            "Provide page_url (always required). Optionally provide discussion_id to reply to an existing thread."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_url": {
                    "type": "string",
                    "description": "Page URL or ID to comment on",
                },
                "discussion_id": {
                    "type": "string",
                    "description": "Optional existing discussion ID or URL to reply to",
                },
                "comment": {"type": "string", "description": "Comment text"},
            },
            "required": ["page_url", "comment"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        page_id = _extract_notion_id(params["page_url"])
        if not page_id:
            return {"error": "Could not extract page ID from page_url"}
        args: dict[str, Any] = {
            "page_id": page_id,
            "rich_text": [{"type": "text", "text": {"content": params["comment"]}}],
        }
        if params.get("discussion_id"):
            args["discussion_id"] = params["discussion_id"]
        result = await _mcp_tool_call(access_token, "notion-create-comment", args)
        text = _extract_text_content(result)
        if text:
            return {"result": text, "source": "notion_mcp"}
        return result


class GetNotionComments(BaseTool):
    @property
    def name(self) -> str:
        return "get_notion_comments"

    @property
    def description(self) -> str:
        return "Get comments and discussions on a Notion page."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "page_url": {"type": "string", "description": "Page URL or ID"},
                "include_resolved": {"type": "boolean"},
                "include_all_blocks": {"type": "boolean"},
                "discussion_id": {
                    "type": "string",
                    "description": "Optional: fetch a specific discussion",
                },
            },
            "required": ["page_url"],
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        page_id = _extract_notion_id(params["page_url"])
        if not page_id:
            return {"error": "Could not extract page ID from page_url"}
        args: dict[str, Any] = {"page_id": page_id}
        for key in ("include_resolved", "include_all_blocks", "discussion_id"):
            if key in params:
                args[key] = params[key]
        result = await _mcp_tool_call(access_token, "notion-get-comments", args)
        text = _extract_text_content(result)
        if text:
            return {"content": text, "source": "notion_mcp"}
        return result


class GetNotionUsers(BaseTool):
    @property
    def name(self) -> str:
        return "get_notion_users"

    @property
    def description(self) -> str:
        return "List all users in the Notion workspace (useful for resolving created_by / last_edited_by IDs to names)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max users (default 100)",
                    "default": 100,
                },
                "query": {
                    "type": "string",
                    "description": "Optional substring to filter users by name/email.",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        args: dict[str, Any] = {"page_size": min(params.get("limit", 100), 100)}
        if params.get("query"):
            args["query"] = params["query"]
        result = await _mcp_tool_call(access_token, "notion-get-users", args)
        text = _extract_text_content(result)
        if text:
            return {"content": text, "source": "notion_mcp"}
        return result


class GetNotionTeams(BaseTool):
    @property
    def name(self) -> str:
        return "get_notion_teams"

    @property
    def description(self) -> str:
        return "List teamspaces in the Notion workspace."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "notion"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "notion-get-teams", {})
        text = _extract_text_content(result)
        if text:
            return {"content": text, "source": "notion_mcp"}
        return result
