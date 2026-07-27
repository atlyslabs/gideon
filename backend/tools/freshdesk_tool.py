"""
Freshdesk Tools -- read-only ticket lookup via Freshdesk REST API v2.

Uses a shared API key (Basic auth). Always available, no per-user integration needed.
Base URL: set FRESHDESK_API_URL, e.g. https://<your-domain>.freshdesk.com/api/v2
"""

import os
from typing import Any

import httpx
import structlog

from tools.internal_tools import InternalTool

logger = structlog.get_logger()

FRESHDESK_API_URL = os.environ.get("FRESHDESK_API_URL", "")
FRESHDESK_API_KEY = os.environ.get("FRESHDESK_API_KEY", "")


def _auth() -> tuple[str, str]:
    return (FRESHDESK_API_KEY, "x")


async def _freshdesk_get(path: str, params: dict | None = None) -> dict | list:
    if not FRESHDESK_API_KEY:
        return {"error": "Freshdesk API key not configured."}

    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{FRESHDESK_API_URL}{path}",
            auth=_auth(),
            headers={"Accept": "application/json"},
            params=params,
        )

    if response.status_code == 404:
        return {"error": "Not found."}
    if response.status_code != 200:
        logger.error("Freshdesk API error", status=response.status_code, path=path)
        return {"error": f"Freshdesk API error: {response.status_code}"}

    return response.json()


def _format_ticket(t: dict) -> dict:
    status_map = {2: "Open", 3: "Pending", 4: "Resolved", 5: "Closed"}
    priority_map = {1: "Low", 2: "Medium", 3: "High", 4: "Urgent"}
    return {
        "id": t.get("id"),
        "subject": t.get("subject"),
        "status": status_map.get(t.get("status"), str(t.get("status"))),
        "priority": priority_map.get(t.get("priority"), str(t.get("priority"))),
        "type": t.get("type"),
        "requester_email": t.get("requester", {}).get("email") if isinstance(t.get("requester"), dict) else None,
        "created_at": t.get("created_at"),
        "updated_at": t.get("updated_at"),
        "tags": t.get("tags"),
        "group_name": t.get("group", {}).get("name") if isinstance(t.get("group"), dict) else None,
        "description_text": (t.get("description_text") or "")[:500],
        "custom_fields": t.get("custom_fields"),
    }


class SearchFreshdeskTickets(InternalTool):
    @property
    def name(self) -> str:
        return "search_freshdesk_tickets"

    @property
    def description(self) -> str:
        return (
            "Search Freshdesk tickets. Provide ONE of: firebase_uid (searches by custom field cf_uid), "
            "email (searches by requester email), or raw_query (Freshdesk query syntax like \"status:2\"). "
            "Returns matching tickets with status, priority, and subject."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "firebase_uid": {
                    "type": "string",
                    "description": "Firebase user ID to search by (searches custom_string fields including cf_uid)",
                },
                "email": {
                    "type": "string",
                    "description": "Requester email address",
                },
                "raw_query": {
                    "type": "string",
                    "description": "Raw Freshdesk search query (e.g. \"status:2\", \"priority:3 AND status:2\")",
                },
            },
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        firebase_uid = (params.get("firebase_uid") or "").strip()
        email = (params.get("email") or "").strip()
        raw_query = (params.get("raw_query") or "").strip()

        if firebase_uid:
            query = f'"custom_string:{firebase_uid}"'
        elif email:
            query = f'"requester_email:\'{email}\'"'
        elif raw_query:
            query = f'"{raw_query}"'
        else:
            return {"error": "Provide firebase_uid, email, or raw_query."}

        data = await _freshdesk_get("/search/tickets", params={"query": query})
        if isinstance(data, dict) and "error" in data:
            return data

        results = data.get("results", []) if isinstance(data, dict) else []
        return {
            "tickets": [_format_ticket(t) for t in results[:20]],
            "total": data.get("total", len(results)) if isinstance(data, dict) else len(results),
        }


class GetFreshdeskTicket(InternalTool):
    @property
    def name(self) -> str:
        return "get_freshdesk_ticket"

    @property
    def description(self) -> str:
        return "Get full details of a Freshdesk ticket by ID, including the conversation thread (all replies and notes)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "ticket_id": {
                    "type": "integer",
                    "description": "Freshdesk ticket ID",
                },
            },
            "required": ["ticket_id"],
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        ticket_id = params.get("ticket_id")
        if not ticket_id:
            return {"error": "ticket_id is required."}

        ticket = await _freshdesk_get(f"/tickets/{ticket_id}?include=requester,stats")
        if isinstance(ticket, dict) and "error" in ticket:
            return ticket

        conversations = await _freshdesk_get(f"/tickets/{ticket_id}/conversations")
        conv_list = []
        if isinstance(conversations, list):
            for c in conversations[:30]:
                conv_list.append({
                    "body_text": (c.get("body_text") or "")[:500],
                    "from_email": c.get("from_email"),
                    "to_emails": c.get("to_emails"),
                    "created_at": c.get("created_at"),
                    "incoming": c.get("incoming"),
                    "private": c.get("private"),
                    "source": c.get("source"),
                })

        formatted = _format_ticket(ticket)
        formatted["conversations"] = conv_list
        formatted["custom_fields"] = ticket.get("custom_fields")
        return formatted


class ListFreshdeskTickets(InternalTool):
    @property
    def name(self) -> str:
        return "list_freshdesk_tickets"

    @property
    def description(self) -> str:
        return "List recent Freshdesk tickets, optionally filtered by status or email. Returns up to 30 tickets sorted by most recently updated."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "email": {
                    "type": "string",
                    "description": "Filter by requester email address",
                },
                "status": {
                    "type": "integer",
                    "description": "Filter by status: 2=Open, 3=Pending, 4=Resolved, 5=Closed",
                    "enum": [2, 3, 4, 5],
                },
                "updated_since": {
                    "type": "string",
                    "description": "ISO 8601 date to filter tickets updated after (e.g. 2026-04-01T00:00:00Z)",
                },
            },
        }

    async def execute_internal(self, params, user_id, session, **kwargs) -> dict:
        query_params: dict[str, Any] = {"per_page": 30, "order_by": "updated_at", "order_type": "desc"}
        if params.get("email"):
            query_params["email"] = params["email"]
        if params.get("status"):
            query_params["filter"] = f"status:{params['status']}"
        if params.get("updated_since"):
            query_params["updated_since"] = params["updated_since"]

        data = await _freshdesk_get("/tickets", params=query_params)
        if isinstance(data, dict) and "error" in data:
            return data

        tickets = data if isinstance(data, list) else []
        return {
            "tickets": [_format_ticket(t) for t in tickets[:30]],
            "count": len(tickets),
        }
