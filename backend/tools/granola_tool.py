"""
Granola Tools -- interact with Granola meeting notes via Granola MCP server.

The MCP token (from mcp-auth.granola.ai OAuth) is used to call the MCP server,
which provides JSON-RPC tools for Granola operations (meeting notes, transcripts, calendar).
"""

from typing import Any
import json

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

GRANOLA_MCP_URL = "https://mcp.granola.ai/mcp"

# Cache session IDs per token to avoid re-initializing every call
_session_cache: dict[str, str] = {}
# Reuse HTTP clients per token so the TCP connection persists across calls
_client_cache: dict[str, httpx.AsyncClient] = {}


def _get_client(access_token: str) -> httpx.AsyncClient:
    """Get or create a persistent HTTP client for this token."""
    cache_key = access_token[:32]
    client = _client_cache.get(cache_key)
    if client is None or client.is_closed:
        client = httpx.AsyncClient(timeout=30.0)
        _client_cache[cache_key] = client
    return client


async def _mcp_init_session(access_token: str) -> str:
    """Initialize an MCP session: send initialize + notifications/initialized."""
    client = _get_client(access_token)
    base_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    # Step 1: initialize
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

    response = await client.post(GRANOLA_MCP_URL, headers=base_headers, json=init_payload)

    if response.status_code == 401:
        raise Exception("401: Granola MCP token expired or invalid")

    # Extract session ID from headers
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
        logger.info("Granola MCP session initialized", session_id=session_id[:16] + "...")
    else:
        logger.info("Granola MCP session initialized without session ID")

    # Step 2: send initialized notification (required by MCP spec before tool calls)
    notify_headers = {**base_headers}
    if session_id:
        notify_headers["Mcp-Session-Id"] = session_id

    notify_payload = {
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }
    try:
        await client.post(GRANOLA_MCP_URL, headers=notify_headers, json=notify_payload)
    except Exception as e:
        logger.warning("Granola MCP initialized notification failed", error=str(e))

    return session_id or ""


async def _get_session_id(access_token: str) -> str:
    """Get or create an MCP session ID for this token."""
    cache_key = access_token[:32]
    if cache_key in _session_cache:
        return _session_cache[cache_key]

    session_id = await _mcp_init_session(access_token)
    _session_cache[cache_key] = session_id
    return session_id


async def _mcp_call(access_token: str, method: str, params: dict | None = None, _retry: bool = True) -> dict:
    """Make a JSON-RPC call to the Granola MCP server with session."""
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
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    client = _get_client(access_token)
    response = await client.post(GRANOLA_MCP_URL, headers=headers, json=payload)

    if response.status_code == 401:
        _session_cache.pop(access_token[:32], None)
        raise Exception("401: Granola MCP token expired or invalid")

    # Stale session -- re-initialize and retry once
    if response.status_code in (400, 404) and ("session" in response.text.lower() or "Session" in response.text):
        _session_cache.pop(access_token[:32], None)
        if _retry:
            return await _mcp_call(access_token, method, params, _retry=False)
        return {"error": "Granola MCP session expired"}

    if response.status_code not in (200, 202):
        logger.error("Granola MCP call failed", status=response.status_code, body=response.text[:500])
        return {"error": f"Granola MCP request failed: {response.status_code}"}

    content_type = response.headers.get("content-type", "")
    body = response.text

    # Handle SSE (text/event-stream) responses
    if "text/event-stream" in content_type:
        return _parse_sse_response(body)

    if body.strip():
        try:
            data = response.json()
            if "error" in data:
                return {"error": f"Granola MCP error: {data['error']}"}
            return data.get("result", data)
        except Exception:
            return {"raw_response": body[:2000]}

    if response.status_code == 202:
        return {"status": "accepted"}

    return {"error": "Empty response from Granola MCP"}


async def _mcp_tool_call(access_token: str, tool_name: str, arguments: dict) -> dict:
    """Call a specific tool on the Granola MCP server."""
    result = await _mcp_call(access_token, "tools/call", {
        "name": tool_name,
        "arguments": arguments,
    })
    if "error" in result:
        logger.error("Granola MCP tool error", tool=tool_name, error=str(result["error"])[:500])
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
                    if "result" in parsed:
                        result = parsed["result"]
                    elif "error" in parsed:
                        return {"error": f"Granola MCP error: {parsed['error']}"}
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


# Public aliases for cross-module use (avoids importing private names)
mcp_tool_call = _mcp_tool_call
extract_text_content = _extract_text_content


# ---------------------------------------------------------------------------
# Tool classes
# ---------------------------------------------------------------------------


class QueryGranolaMeetings(BaseTool):
    @property
    def name(self) -> str:
        return "query_granola_meetings"

    @property
    def description(self) -> str:
        return "Chat with Granola -- ask questions about your meeting notes in natural language. E.g. 'What did we discuss about pricing last week?' Returns AI-generated answers from your meeting history."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language question about meetings (topic, person, decision, action item, etc.)",
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "granola"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "query_granola_meetings", {
            "query": params["query"],
        })
        text = _extract_text_content(result)
        if text:
            return {"answer": text, "source": "granola_mcp"}
        return result


class ListGranolaMeetings(BaseTool):
    @property
    def name(self) -> str:
        return "list_granola_meetings"

    @property
    def description(self) -> str:
        return "List recent meetings from Granola. Returns meeting IDs, titles, dates, and attendees."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "granola"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "list_meetings", {})
        text = _extract_text_content(result)
        if text:
            return {"meetings": text, "source": "granola_mcp"}
        return result


class GetGranolaMeetings(BaseTool):
    @property
    def name(self) -> str:
        return "get_granola_meetings"

    @property
    def description(self) -> str:
        return "Search and retrieve Granola meeting content by keyword. Returns meeting IDs, titles, dates, attendees, and notes."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query for meeting content",
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "granola"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "get_meetings", {
            "query": params["query"],
        })
        text = _extract_text_content(result)
        if text:
            return {"content": text, "source": "granola_mcp"}
        return result


class GetGranolaMeetingTranscript(BaseTool):
    @property
    def name(self) -> str:
        return "get_granola_meeting_transcript"

    @property
    def description(self) -> str:
        return "Get the raw transcript of a specific meeting by its ID. Includes speaker attribution. Requires a paid Granola plan."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "meeting_id": {
                    "type": "string",
                    "description": "The Granola meeting ID",
                },
            },
            "required": ["meeting_id"],
        }

    @property
    def provider(self) -> str:
        return "granola"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        result = await _mcp_tool_call(access_token, "get_meeting_transcript", {
            "meeting_id": params["meeting_id"],
        })
        text = _extract_text_content(result)
        if text:
            return {"transcript": text, "source": "granola_mcp"}
        return result
