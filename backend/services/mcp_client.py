"""
Shared MCP client for streamable-HTTP / JSON-RPC servers.

Gideon already talks to several MCP servers through hand-rolled clients
inside individual tool files (granola, notion, posthog, semrush). They all
do the same three things — initialize a session, fire `notifications/
initialized`, then POST `tools/call` requests — and they all parse SSE
responses the same way. This module is the shared implementation those
copies eventually migrate to; today, it is what new MCP integrations are
built against.

Auth model is intentionally pluggable. Per-user OAuth integrations pass a
`token_provider` callable that yields a fresh bearer token (so token
refresh stays the caller's problem and tightly-coupled refresh helpers
don't bleed into this module). Shared/no-auth MCP servers (e.g. Chromatic-
hosted Storybook MCP, which is project-scoped, not per-user) pass `None`
and skip the Authorization header entirely.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

TokenProvider = Callable[[], Awaitable[Optional[str]]]


class MCPClient:
    """Minimal streamable-HTTP MCP client.

    One instance per (url, auth_identity) pair. Session ID and the
    underlying httpx.AsyncClient are cached on the instance so a chat
    turn that fires several tool calls in a row only pays the handshake
    cost once.
    """

    def __init__(
        self,
        url: str,
        *,
        token_provider: TokenProvider | None = None,
        client_name: str = "Gideon",
        client_version: str = "1.0.0",
        protocol_version: str = "2025-03-26",
        timeout: float = 30.0,
    ) -> None:
        self._url = url
        self._token_provider = token_provider
        self._client_name = client_name
        self._client_version = client_version
        self._protocol_version = protocol_version
        self._timeout = timeout
        self._session_id: str | None = None
        self._http: httpx.AsyncClient | None = None
        self._next_id = 1

    # -- Public surface -----------------------------------------------------

    async def call_tool(self, tool_name: str, arguments: dict[str, Any] | None = None) -> dict:
        """JSON-RPC `tools/call`. Returns the raw `result` payload or `{"error": ...}`."""
        return await self._call("tools/call", {"name": tool_name, "arguments": arguments or {}})

    async def list_tools(self) -> dict:
        """JSON-RPC `tools/list`. Returns the raw `result` payload or `{"error": ...}`."""
        return await self._call("tools/list", {})

    @staticmethod
    def extract_text(result: dict) -> str:
        """MCP tool results wrap a `content: [{type: text, text: ...}]` envelope.

        Most tools return one or more text blocks; this helper joins them so
        callers don't have to walk the structure every time.
        """
        content = result.get("content") or []
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)

    async def aclose(self) -> None:
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None
        self._session_id = None

    # -- Internals ----------------------------------------------------------

    async def _auth_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token_provider is not None:
            token = await self._token_provider()
            if token:
                headers["Authorization"] = f"Bearer {token}"
        return headers

    def _http_client(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=self._timeout)
        return self._http

    def _alloc_id(self) -> int:
        i = self._next_id
        self._next_id += 1
        return i

    async def _ensure_session(self) -> str:
        if self._session_id is not None:
            return self._session_id

        headers = await self._auth_headers()
        init_payload = {
            "jsonrpc": "2.0",
            "id": self._alloc_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": self._protocol_version,
                "capabilities": {},
                "clientInfo": {"name": self._client_name, "version": self._client_version},
            },
        }
        response = await self._http_client().post(self._url, headers=headers, json=init_payload)
        if response.status_code == 401:
            raise MCPAuthError("401 during MCP initialize")

        session_id = (
            response.headers.get("mcp-session-id")
            or response.headers.get("Mcp-Session-Id")
            or response.headers.get("x-mcp-session-id")
        )

        if not session_id:
            # Some servers return session id inside an SSE / JSON body.
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
                    body = response.json()
                    session_id = body.get("sessionId") or body.get("session_id")
            except Exception:
                pass

        self._session_id = session_id or ""

        # `notifications/initialized` is required by spec before any tools/call.
        notify_headers = {**headers}
        if self._session_id:
            notify_headers["Mcp-Session-Id"] = self._session_id
        try:
            await self._http_client().post(
                self._url,
                headers=notify_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
        except Exception as e:
            logger.warning("MCP initialized notification failed", url=self._url, error=str(e))

        return self._session_id

    async def _call(self, method: str, params: dict, _retry: bool = True) -> dict:
        session_id = await self._ensure_session()
        headers = await self._auth_headers()
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        payload = {"jsonrpc": "2.0", "id": self._alloc_id(), "method": method, "params": params}
        response = await self._http_client().post(self._url, headers=headers, json=payload)

        if response.status_code == 401:
            self._session_id = None
            raise MCPAuthError(f"401 during MCP {method}")

        # Stale session — drop our cached id and try once more.
        if response.status_code in (400, 404) and "session" in response.text.lower():
            self._session_id = None
            if _retry:
                return await self._call(method, params, _retry=False)
            return {"error": "MCP session expired"}

        if response.status_code not in (200, 202):
            logger.error(
                "MCP call failed",
                url=self._url,
                method=method,
                status=response.status_code,
                body=response.text[:500],
            )
            return {"error": f"MCP request failed: {response.status_code}"}

        content_type = response.headers.get("content-type", "")
        body = response.text

        if "text/event-stream" in content_type:
            return _parse_sse_response(body)

        if body.strip():
            try:
                data = response.json()
                if "error" in data:
                    return {"error": f"MCP error: {data['error']}"}
                return data.get("result", data)
            except Exception:
                return {"raw_response": body[:2000]}

        if response.status_code == 202:
            return {"status": "accepted"}
        return {"error": "Empty response from MCP"}


class MCPAuthError(Exception):
    """Raised when the MCP server rejects credentials. Caller decides whether to refresh or surface to user."""


def _parse_sse_response(body: str) -> dict:
    result: dict = {}
    for line in body.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data_str = line[5:].strip()
        if not data_str:
            continue
        try:
            parsed = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        if "result" in parsed:
            result = parsed["result"]
        elif "error" in parsed:
            return {"error": f"MCP error: {parsed['error']}"}
    return result
