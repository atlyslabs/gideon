"""
Freshchat Tools -- read-only conversation lookup via Freshchat API v2.

Uses a shared API key (Bearer token). Always available, no per-user integration needed.
Base URL from FRESHCHAT_INSTANCE_URL env var.

Available endpoints:
- GET /conversations/{id} (by UUID or internal ID) -> conversation details
- GET /conversations/{uuid}/messages -> messages (requires UUID, not internal ID)
- GET /users/{id} -> user details (name, email, phone, properties incl. Firebase UID)
- GET /users/{id}/conversations -> all conversations for a user
- GET /agents -> agent list
- GET /groups -> group list
"""

import os
from typing import Any

import httpx
import structlog

from tools.internal_tools import InternalTool

logger = structlog.get_logger()

FRESHCHAT_INSTANCE_URL = os.environ.get("FRESHCHAT_INSTANCE_URL", "")
FRESHCHAT_API_KEY = os.environ.get("FRESHCHAT_API_KEY", "")


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {FRESHCHAT_API_KEY}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


async def _freshchat_get(path: str, params: dict | None = None) -> dict | list | None:
    if not FRESHCHAT_API_KEY or not FRESHCHAT_INSTANCE_URL:
        return {"error": "Freshchat API key or instance URL not configured."}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{FRESHCHAT_INSTANCE_URL}/v2{path}",
            headers=_headers(),
            params=params,
        )

    if response.status_code == 404:
        return {"error": "Not found."}
    if response.status_code == 403:
        return {"error": "Freshchat access denied for this endpoint."}
    if response.status_code != 200:
        logger.error("Freshchat API error", status=response.status_code, path=path)
        return {"error": f"Freshchat API error: {response.status_code}"}

    return response.json()


def _format_message(m: dict) -> dict:
    parts = m.get("message_parts") or []
    text = ""
    for part in parts:
        if part.get("text"):
            text_data = part["text"]
            text += text_data.get("content", "") if isinstance(text_data, dict) else str(text_data)

    return {
        "actor_type": m.get("actor_type"),
        "actor_id": m.get("actor_id"),
        "text": text[:1000],
        "created_time": m.get("created_time"),
        "message_type": m.get("message_type"),
    }


class GetFreshchatConversation(InternalTool):
    @property
    def name(self) -> str:
        return "get_freshchat_conversation"

    @property
    def description(self) -> str:
        return (
            "Get a Freshchat conversation with its messages by conversation ID "
            "(internal numeric ID or UUID). Returns conversation details and up to 50 messages."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "conversation_id": {
                    "type": "string",
                    "description": "Freshchat conversation ID (numeric internal ID or UUID)",
                },
            },
            "required": ["conversation_id"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        conversation_id = (params.get("conversation_id") or "").strip()
        if not conversation_id:
            return {"error": "conversation_id is required."}

        # Get conversation details
        conv = await _freshchat_get(f"/conversations/{conversation_id}")
        if isinstance(conv, dict) and "error" in conv:
            return conv

        # For messages, we need the UUID format
        conv_uuid = conv.get("conversation_id", conversation_id)

        # Get messages using UUID
        messages_data = await _freshchat_get(f"/conversations/{conv_uuid}/messages")
        messages = []
        messages_error = None
        if isinstance(messages_data, dict) and "error" in messages_data:
            messages_error = messages_data["error"]
        elif isinstance(messages_data, dict) and "messages" in messages_data:
            messages = messages_data["messages"]

        result = {
            "conversation_id": conv.get("conversation_id"),
            "internal_id": conv.get("conversation_internal_id"),
            "status": conv.get("status"),
            "created_time": conv.get("created_time"),
            "updated_time": conv.get("updated_time"),
            "assigned_agent_id": conv.get("assigned_agent_id"),
            "url": conv.get("url"),
            "user_id": conv.get("user_id"),
            "messages": [_format_message(m) for m in messages[:50]],
            "message_count": len(messages),
        }
        if messages_error:
            result["messages_error"] = messages_error
        return result


class SearchFreshchatConversations(InternalTool):
    @property
    def name(self) -> str:
        return "search_freshchat_conversations"

    @property
    def description(self) -> str:
        return (
            "Find Freshchat conversations for a customer. Provide a Freshchat user_id (UUID) "
            "to get all their conversations. The Freshchat user_id can be found in the "
            "get_freshchat_conversation response."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "freshchat_user_id": {
                    "type": "string",
                    "description": "Freshchat user UUID (get it from get_freshchat_user)",
                },
            },
            "required": ["freshchat_user_id"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        fc_user_id = (params.get("freshchat_user_id") or "").strip()
        if not fc_user_id:
            return {"error": "freshchat_user_id is required."}

        data = await _freshchat_get(f"/users/{fc_user_id}/conversations")
        if isinstance(data, dict) and "error" in data:
            return data

        conversations = data.get("conversations", []) if isinstance(data, dict) else []
        results = []
        for c in conversations[:20]:
            results.append({
                "conversation_id": c.get("conversation_id"),
                "internal_id": c.get("conversation_internal_id"),
                "status": c.get("status"),
                "created_time": c.get("created_time"),
                "updated_time": c.get("updated_time"),
                "assigned_agent_id": c.get("assigned_agent_id"),
            })

        return {
            "conversations": results,
            "count": len(conversations),
        }


class GetFreshchatUser(InternalTool):
    @property
    def name(self) -> str:
        return "get_freshchat_user"

    @property
    def description(self) -> str:
        return (
            "Look up a Freshchat user by their Freshchat user UUID. Returns name, email, phone, "
            "and properties including Firebase UID (cf_userid). "
            "The Freshchat user UUID can be found in get_freshchat_conversation responses."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "freshchat_user_id": {
                    "type": "string",
                    "description": "Freshchat user UUID",
                },
            },
            "required": ["freshchat_user_id"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        fc_user_id = (params.get("freshchat_user_id") or "").strip()
        if not fc_user_id:
            return {"error": "freshchat_user_id is required."}

        data = await _freshchat_get(f"/users/{fc_user_id}")
        if isinstance(data, dict) and "error" in data:
            return data

        props = {}
        for p in data.get("properties", []):
            props[p.get("name", "")] = p.get("value", "")

        return {
            "freshchat_user_id": data.get("id"),
            "first_name": data.get("first_name"),
            "last_name": data.get("last_name"),
            "email": data.get("email"),
            "phone": data.get("phone"),
            "firebase_uid": props.get("cf_userid"),
            "residence": props.get("cf_residence"),
            "repeat_user": props.get("cf_repeat_user"),
            "locale": props.get("locale"),
            "created_time": data.get("created_time"),
        }
