"""
Slack Canvas Tools — read, edit, and create canvases via slack_sdk AsyncWebClient.

Uses user token (canvases:read, canvases:write scopes).
Canvases use markdown content format.
"""

from typing import Any

import aiohttp
import structlog
from slack_sdk.web.async_client import AsyncWebClient
from services.slack_client import get_slack_client

from tools.base import BaseTool

logger = structlog.get_logger()

CANVAS_MAX_CHARS = 50_000


def _get_slack_client(token: str) -> AsyncWebClient:
    return get_slack_client(token=token)


class ListSlackCanvases(BaseTool):
    @property
    def name(self) -> str:
        return "list_slack_canvases"

    @property
    def description(self) -> str:
        return (
            "List Slack canvases accessible to the user. "
            "Returns canvas IDs, titles, and timestamps. Use this to find canvases before reading them."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max canvases to return (default 20)",
                    "default": 20,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        result = await client.api_call(
            "files.list",
            params={"types": "canvas", "count": min(params.get("limit", 20), 100)},
        )
        if not result.get("ok"):
            return {"error": result.get("error", "Failed to list canvases")}

        canvases = []
        for f in result.get("files", []):
            canvases.append({
                "canvas_id": f.get("id"),
                "title": f.get("title") or f.get("name"),
                "created": f.get("created"),
                "updated": f.get("updated"),
                "url": f.get("permalink"),
            })
        return {"canvases": canvases, "count": len(canvases)}


class ReadSlackCanvas(BaseTool):
    @property
    def name(self) -> str:
        return "read_slack_canvas"

    @property
    def description(self) -> str:
        return (
            "Read a Slack canvas. Returns the full markdown body plus section IDs for "
            "every header (pass any of these as `section_id` to edit_slack_canvas). "
            "Optionally filter both to entries matching a search string."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": "Canvas file ID (e.g. F1234ABCD). Get from list_slack_canvases.",
                },
                "search_text": {
                    "type": "string",
                    "description": "If provided, filters markdown lines and section IDs to those containing this text (case-insensitive).",
                },
            },
            "required": ["canvas_id"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        canvas_id = params["canvas_id"]

        info = await client.api_call("files.info", params={"file": canvas_id})
        if not info.get("ok"):
            return {"error": info.get("error", "Failed to read canvas")}

        file_data = info.get("file", {}) or {}
        title = file_data.get("title") or file_data.get("name")

        markdown: str | None = None
        canvas_data = file_data.get("canvas") or {}
        doc_content = canvas_data.get("document_content") or {}
        if doc_content.get("type") == "markdown":
            markdown = doc_content.get("markdown")

        if not markdown:
            url = file_data.get("url_private_download") or file_data.get("url_private")
            if url:
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=15)
                    ) as session:
                        async with session.get(
                            url, headers={"Authorization": f"Bearer {access_token}"}
                        ) as resp:
                            if resp.status == 200:
                                markdown = await resp.text()
                except aiohttp.ClientError as e:
                    return {"error": f"Failed to download canvas content: {e}"}

        if not markdown:
            return {
                "error": (
                    "Canvas content unavailable. The canvases:read scope may be "
                    "missing, or the canvas is empty. Reconnect Slack if the scope "
                    "was added recently."
                )
            }

        truncated = False
        if len(markdown) > CANVAS_MAX_CHARS:
            markdown = markdown[:CANVAS_MAX_CHARS]
            truncated = True

        # Section IDs come from a separate endpoint. edit_slack_canvas needs
        # these for insert_after / insert_before / replace / delete operations.
        search_text = params.get("search_text")
        criteria: dict[str, Any] = {"section_types": ["any_header"]}
        if search_text:
            criteria["contains_text"] = search_text
        sections: list = []
        try:
            lookup = await client.api_call(
                "canvases.sections.lookup",
                json={"canvas_id": canvas_id, "criteria": criteria},
            )
            if lookup.get("ok"):
                sections = lookup.get("sections", []) or []
            else:
                logger.warning(
                    "read_slack_canvas section lookup returned not-ok",
                    canvas_id=canvas_id,
                    slack_error=lookup.get("error"),
                )
        except Exception as e:
            # Section IDs are a nice-to-have for editing; don't fail the read
            # if only the lookup call errored. Log so operators can see the
            # degraded path instead of silently swallowing it.
            logger.warning(
                "read_slack_canvas section lookup failed",
                canvas_id=canvas_id,
                err=str(e),
            )

        if search_text:
            needle = search_text.lower()
            lines = [ln for ln in markdown.split("\n") if needle in ln.lower()]
            markdown = "\n".join(lines)

        return {
            "canvas_id": canvas_id,
            "title": title,
            "markdown": markdown,
            "sections": sections,
            "truncated": truncated,
        }


class EditSlackCanvas(BaseTool):
    @property
    def name(self) -> str:
        return "edit_slack_canvas"

    @property
    def description(self) -> str:
        return (
            "Edit a Slack canvas. Supports inserting, replacing, or deleting content. "
            "Content uses markdown format. One operation per call."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "canvas_id": {
                    "type": "string",
                    "description": "Canvas ID to edit",
                },
                "operation": {
                    "type": "string",
                    "description": "Edit operation type",
                    "enum": [
                        "insert_at_start",
                        "insert_at_end",
                        "insert_after",
                        "insert_before",
                        "replace",
                        "delete",
                    ],
                },
                "markdown": {
                    "type": "string",
                    "description": "Markdown content to insert or replace with. Not needed for delete.",
                },
                "section_id": {
                    "type": "string",
                    "description": "Section ID for insert_after, insert_before, replace, or delete. Get from read_slack_canvas.",
                },
            },
            "required": ["canvas_id", "operation"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        canvas_id = params["canvas_id"]
        operation = params["operation"]
        markdown = params.get("markdown", "")
        section_id = params.get("section_id")

        change: dict[str, Any] = {"operation": operation}

        if operation in ("insert_after", "insert_before", "delete") and not section_id:
            return {"error": f"section_id is required for {operation}"}

        if operation == "delete":
            change["section_id"] = section_id
        else:
            change["document_content"] = {"type": "markdown", "markdown": markdown}
            if section_id:
                change["section_id"] = section_id

        result = await client.api_call(
            "canvases.edit",
            json={"canvas_id": canvas_id, "changes": [change]},
        )
        if not result.get("ok"):
            return {"error": result.get("error", "Failed to edit canvas")}

        return {"success": True, "canvas_id": canvas_id, "operation": operation}


class CreateSlackCanvas(BaseTool):
    @property
    def name(self) -> str:
        return "create_slack_canvas"

    @property
    def description(self) -> str:
        return (
            "Create a new Slack canvas with markdown content. "
            "Optionally attach it to a channel."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Canvas title",
                },
                "markdown": {
                    "type": "string",
                    "description": "Initial canvas content in markdown",
                },
                "channel_id": {
                    "type": "string",
                    "description": "Optional channel ID to attach the canvas to",
                },
            },
            "required": ["title", "markdown"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        title = params["title"]
        markdown = params["markdown"]
        channel_id = params.get("channel_id")

        api_params: dict[str, Any] = {
            "title": title,
            "document_content": {"type": "markdown", "markdown": markdown},
        }
        if channel_id:
            api_params["channel_id"] = channel_id

        result = await client.api_call("canvases.create", json=api_params)
        if not result.get("ok"):
            return {"error": result.get("error", "Failed to create canvas")}

        return {
            "success": True,
            "canvas_id": result.get("canvas_id"),
            "title": title,
        }
