"""
Mention Router — search endpoint for @-mention autocomplete in chat.
Searches collaborators, Linear issues, Notion pages, and Google Drive files.
"""

import asyncio
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Query
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from services.slack_client import get_slack_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from db.models import Integration
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user
from services.workspace_member_service import search_workspace_members, _get_user_token

logger = structlog.get_logger()

router = APIRouter()

# MIME types for Google Workspace files
MIME_TYPES = {
    "google_docs": "application/vnd.google-apps.document",
    "google_sheets": "application/vnd.google-apps.spreadsheet",
    "google_slides": "application/vnd.google-apps.presentation",
}


@router.get("/search")
async def search_mentions(
    query: str = Query("", min_length=0),
    category: str = Query(...),
    limit: int = Query(10, le=500),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Search for mentionable items by category."""
    user = await get_or_create_user(session, auth_user)

    if category == "collaborator":
        return await _search_collaborators(query, session)
    elif category == "tool":
        return await _search_tools(query, user.id, session)
    elif category == "linear":
        return await _search_linear(query, limit, user.id, session)
    elif category == "notion":
        return await _search_notion(query, limit, user.id, session)
    elif category == "gmail":
        return await _search_gmail(query, limit, user.id, session)
    elif category == "google_calendar":
        return await _search_calendar(query, limit, user.id, session)
    elif category == "slack":
        return await _search_slack(query, limit, user.id, session)
    elif category in ("google_drive", "google_docs", "google_sheets", "google_slides"):
        return await _search_google(query, limit, category, user.id, session)
    else:
        return {"items": [], "has_more": False}


async def _search_tools(query: str, user_id: UUID, session: AsyncSession) -> dict:
    """List tools available to the user, filtered by connected integrations and query."""
    from tools.tool_registry import ALL_TOOLS

    # Get connected providers
    result = await session.execute(
        select(Integration.provider).where(
            Integration.user_id == user_id,
            Integration.is_active == True,
        )
    )
    connected = {row[0] for row in result.all()}
    # Internal/grafana tools are always available
    connected.update({"internal", "grafana"})

    q = query.lower()
    items = []
    for tool in ALL_TOOLS:
        if tool.provider not in connected:
            continue
        name = tool.name
        desc = tool.description
        if q and q not in name.lower() and q not in desc.lower():
            continue
        # Map provider to icon
        icon = tool.provider
        if icon == "google":
            # Infer more specific icon from tool name
            if "email" in name or "gmail" in name or "draft" in name:
                icon = "gmail"
            elif "calendar" in name or "event" in name:
                icon = "google_calendar"
            elif "sheet" in name or "spreadsheet" in name:
                icon = "google_sheets"
            elif "doc" in name or "document" in name:
                icon = "google_docs"
            elif "drive" in name or "file" in name:
                icon = "google_drive"
        items.append({
            "id": name,
            "category": "tool",
            "label": name,
            "subtitle": desc[:80],
            "icon": icon,
        })

    return {"items": items[:20], "has_more": False}


async def _search_collaborators(query: str, session: AsyncSession) -> dict:
    """Search workspace members across Slack, Notion, Linear."""
    members = await search_workspace_members(query, session)
    items = [
        {
            "id": m["email"],
            "category": "collaborator",
            "label": m.get("name") or m["email"],
            "subtitle": m["email"],
            "icon": m.get("source", "slack"),
            "avatar_url": m.get("avatar_url"),
        }
        for m in members
    ]
    return {"items": items, "has_more": False}


async def _search_linear(query: str, limit: int, user_id: UUID, session: AsyncSession) -> dict:
    """Search Linear issues."""
    if not query:
        return {"items": [], "has_more": False}

    token = await _get_user_token(session, user_id, "linear")
    if not token:
        return {"items": [], "has_more": False}

    try:
        from tools.linear_tool import _linear_gql

        gql = """
        query SearchIssues($query: String!, $first: Int!) {
            searchIssues(term: $query, first: $first) {
                nodes {
                    id
                    identifier
                    title
                    url
                    state { name }
                    team { name key }
                    assignee { name }
                }
            }
        }
        """
        data = await _linear_gql(token, gql, {"query": query, "first": limit})
        nodes = (data.get("searchIssues") or {}).get("nodes", [])

        items = [
            {
                "id": n["id"],
                "category": "linear",
                "label": f"{n['identifier']}: {n['title']}",
                "subtitle": f"{(n.get('state') or {}).get('name', '')} · {(n.get('team') or {}).get('name', '')}",
                "icon": "linear",
                "url": n.get("url", ""),
            }
            for n in nodes
        ]
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Linear", error=str(e))
        return {"items": [], "has_more": False}


async def _search_notion(query: str, limit: int, user_id: UUID, session: AsyncSession) -> dict:
    """Search Notion pages."""
    if not query:
        return {"items": [], "has_more": False}

    token = await _get_user_token(session, user_id, "notion")
    if not token:
        return {"items": [], "has_more": False}

    try:
        from tools.notion_tool import notion_mcp_search

        pages = await notion_mcp_search(token, query=query, page_size=limit)
        items = []
        for p in pages:
            url = p.get("url", "")
            # Ensure full Notion URL using www.notion.so
            if url and not url.startswith("http"):
                url = f"https://www.notion.so/{url.replace('-', '')}"
            elif not url:
                url = f"https://www.notion.so/{p.get('id', '').replace('-', '')}" if p.get("id") else ""
            items.append({
                "id": p.get("id", url),
                "category": "notion",
                "label": p.get("title", "(Untitled)"),
                "subtitle": p.get("type", "page"),
                "icon": "notion",
                "url": url,
            })
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Notion", error=str(e))
        return {"items": [], "has_more": False}


async def _search_google(
    query: str, limit: int, category: str, user_id: UUID, session: AsyncSession
) -> dict:
    """Search Google Drive/Docs/Sheets/Slides files."""
    if not query:
        return {"items": [], "has_more": False}

    token = await _get_user_token(session, user_id, "google")
    if not token:
        return {"items": [], "has_more": False}

    try:
        def _search_sync():
            creds = Credentials(token=token)
            service = build("drive", "v3", credentials=creds, cache_discovery=False)

            # Escape single quotes and backslashes for Drive API query syntax
            safe_query = query.replace("\\", "\\\\").replace("'", "\\'")
            q_parts = [f"name contains '{safe_query}'", "trashed = false"]
            if category in MIME_TYPES:
                q_parts.append(f"mimeType = '{MIME_TYPES[category]}'")

            results = service.files().list(
                q=" and ".join(q_parts),
                pageSize=min(limit, 20),
                fields="files(id, name, mimeType, modifiedTime, webViewLink)",
                orderBy="modifiedTime desc",
            ).execute()
            return results.get("files", [])

        files = await asyncio.to_thread(_search_sync)

        # Map MIME type to icon
        mime_to_icon = {
            "application/vnd.google-apps.document": "google_docs",
            "application/vnd.google-apps.spreadsheet": "google_sheets",
            "application/vnd.google-apps.presentation": "google_slides",
            "application/vnd.google-apps.folder": "google_drive",
        }

        items = [
            {
                "id": f["id"],
                "category": category,
                "label": f.get("name", ""),
                "subtitle": f.get("modifiedTime", "")[:10] if f.get("modifiedTime") else "",
                "icon": mime_to_icon.get(f.get("mimeType", ""), "google_drive"),
                "url": f.get("webViewLink", ""),
            }
            for f in files
        ]
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Google", category=category, error=str(e))
        return {"items": [], "has_more": False}


async def _search_gmail(query: str, limit: int, user_id: UUID, session: AsyncSession) -> dict:
    """Search Gmail emails by subject/sender. Empty query returns latest inbox."""

    token = await _get_user_token(session, user_id, "google")
    if not token:
        return {"items": [], "has_more": False}

    try:
        def _search_sync():
            creds = Credentials(token=token)
            service = build("gmail", "v1", credentials=creds, cache_discovery=False)

            # Use threads.list for speed, or messages.list with batch get
            results = service.users().messages().list(
                userId="me", maxResults=min(limit, 8), q=f"in:inbox {query}".strip()
            ).execute()

            msg_ids = [ref["id"] for ref in results.get("messages", [])]
            if not msg_ids:
                return []

            # Batch get — single HTTP request for all messages
            batch_results = []
            batch = service.new_batch_http_request()
            for mid in msg_ids:
                batch.add(
                    service.users().messages().get(
                        userId="me", id=mid, format="metadata",
                        metadataHeaders=["From", "Subject"],
                    ),
                    callback=lambda req_id, resp, exc: batch_results.append(resp) if not exc else None,
                )
            batch.execute()

            emails = []
            for msg in batch_results:
                if not msg:
                    continue
                headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
                emails.append({
                    "id": msg["id"],
                    "subject": headers.get("subject", "(No subject)"),
                    "from": headers.get("from", ""),
                })
            return emails

        emails = await asyncio.to_thread(_search_sync)
        items = [
            {
                "id": e["id"],
                "category": "gmail",
                "label": e["subject"],
                "subtitle": e["from"][:50],
                "icon": "gmail",
            }
            for e in emails
        ]
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Gmail", error=str(e))
        return {"items": [], "has_more": False}


async def _search_calendar(query: str, limit: int, user_id: UUID, session: AsyncSession) -> dict:
    """Search upcoming calendar events. Empty query returns upcoming events."""

    token = await _get_user_token(session, user_id, "google")
    if not token:
        return {"items": [], "has_more": False}

    try:
        from datetime import datetime, timezone, timedelta

        def _search_sync():
            creds = Credentials(token=token)
            service = build("calendar", "v3", credentials=creds, cache_discovery=False)
            now = datetime.now(timezone.utc)
            results = service.events().list(
                calendarId="primary",
                timeMin=now.isoformat(),
                timeMax=(now + timedelta(days=30)).isoformat(),
                q=query or None,
                singleEvents=True,
                orderBy="startTime",
                maxResults=min(limit, 10),
            ).execute()
            return results.get("items", [])

        events = await asyncio.to_thread(_search_sync)
        items = [
            {
                "id": ev.get("id", ""),
                "category": "google_calendar",
                "label": ev.get("summary", "(No title)"),
                "subtitle": (ev.get("start", {}).get("dateTime") or ev.get("start", {}).get("date", ""))[:16],
                "icon": "google_calendar",
                "url": ev.get("htmlLink", ""),
            }
            for ev in events
        ]
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Calendar", error=str(e))
        return {"items": [], "has_more": False}


async def _search_slack(query: str, limit: int, user_id: UUID, session: AsyncSession) -> dict:
    """Search Slack messages."""
    if not query:
        return {"items": [], "has_more": False}

    token = await _get_user_token(session, user_id, "slack")
    if not token:
        return {"items": [], "has_more": False}

    try:
        client = get_slack_client(token=token)
        resp = await client.search_messages(query=query, count=min(limit, 10))
        matches = resp.get("messages", {}).get("matches", [])

        items = [
            {
                "id": m.get("permalink", m.get("ts", "")),
                "category": "slack",
                "label": (m.get("text", "")[:80] or "(No text)"),
                "subtitle": f"#{m.get('channel', {}).get('name', 'unknown')}",
                "icon": "slack",
                "url": m.get("permalink", ""),
            }
            for m in matches
        ]
        return {"items": items, "has_more": False}
    except Exception as e:
        logger.error("Mention search failed for Slack", error=str(e))
        return {"items": [], "has_more": False}
