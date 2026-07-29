"""
Activity Ingestion Service — polls connected integrations and creates activity log entries.

For each user with active integrations, fetches new items (emails, Slack messages,
calendar events, Linear issues, GitHub events) and logs them as ActivityLog rows.
Deduplication is via (user_id, source, reference_id).

Fetch-then-write pattern: each ingester fetches all external data first (no DB session),
then a short-lived session batch-writes activities. This avoids holding DB connections
open during external API calls.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID

import httpx
import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from slack_sdk.web.async_client import AsyncWebClient
from services.slack_client import get_slack_client
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import session_manager
from db.models import ActivityLog, Integration
from services.encryption_service import decrypt
from services.integration_health import clear_integration_errors, record_integration_error
from services.slack_mention_resolver import build_decode_map, decode_with_map
from services.topic_service import assign_topics_for_user

logger = structlog.get_logger()

# Default lookback window for first-time ingestion
DEFAULT_LOOKBACK_HOURS = 24


# ---------------------------------------------------------------------------
# Token resolution (needs a session for token refresh writes)
# ---------------------------------------------------------------------------

async def _resolve_token(integration: Integration, session: AsyncSession) -> str | None:
    """Resolve and refresh (if needed) the access token for an integration.
    Mirrors the logic in tool_executor.py."""

    # Slack: prefer user token, fall back to bot token
    if integration.provider == "slack":
        if integration.access_token:
            return decrypt(integration.access_token)
        if integration.bot_token:
            return decrypt(integration.bot_token)
        return None

    # Google: check expiry and refresh if needed
    if integration.provider == "google" and integration.token_expiry:
        if datetime.now(timezone.utc) >= integration.token_expiry:
            if integration.refresh_token:
                try:
                    from services.oauth.google_oauth import GoogleOAuthProvider
                    from services.encryption_service import encrypt

                    new_tokens = await GoogleOAuthProvider().refresh_access_token(
                        decrypt(integration.refresh_token)
                    )
                    integration.access_token = encrypt(new_tokens.access_token)
                    if new_tokens.refresh_token:
                        integration.refresh_token = encrypt(new_tokens.refresh_token)
                    integration.token_expiry = new_tokens.token_expiry
                    await session.flush()
                    return new_tokens.access_token
                except Exception as e:
                    logger.error("Google token refresh failed during ingestion", error=str(e))
                    await record_integration_error(session, integration.id, f"Token refresh failed: {e}")
                    return None
            await record_integration_error(session, integration.id, "Token expired, no refresh token available")
            return None

    if not integration.access_token:
        return None
    return decrypt(integration.access_token)


# ---------------------------------------------------------------------------
# Activity write helper (unchanged — used in the batch-write phase)
# ---------------------------------------------------------------------------

async def _create_activity(
    user_id: UUID,
    source: str,
    event_type: str,
    title: str,
    summary: str | None,
    reference_id: str,
    metadata: dict,
    session: AsyncSession,
) -> ActivityLog | None:
    """Create an ActivityLog entry atomically. Returns None if duplicate (unique constraint)."""
    from sqlalchemy.exc import IntegrityError

    activity = ActivityLog(
        user_id=user_id,
        source=source,
        event_type=event_type,
        title=title,
        summary=summary,
        reference_id=reference_id,
        extra_data=metadata,
    )
    try:
        async with session.begin_nested():
            session.add(activity)
            await session.flush()
        return activity
    except IntegrityError:
        # Savepoint rolled back, session still usable
        return None


# ---------------------------------------------------------------------------
# 429 retry helper
# ---------------------------------------------------------------------------

def _parse_retry_after(exc: Exception, default: float = 5.0) -> float:
    """Extract Retry-After from Slack SDK exception."""
    try:
        response = exc.response
        if not response:
            return default
        headers = response.headers
        return float(headers.get("Retry-After", default))
    except (AttributeError, ValueError, TypeError):
        return default


async def _fetch_slack_history_with_retry(
    client: AsyncWebClient,
    channel_id: str,
    oldest_ts: str,
    limit: int,
) -> dict | None:
    """Fetch conversations.history with retry-once on 429."""
    try:
        return await client.conversations_history(
            channel=channel_id, oldest=oldest_ts, limit=limit
        )
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "ratelimited" in error_str:
            retry_after = _parse_retry_after(e, default=5.0)
            logger.warning("Slack rate limited", channel=channel_id, retry_after=retry_after)
            await asyncio.sleep(retry_after)
            try:
                return await client.conversations_history(
                    channel=channel_id, oldest=oldest_ts, limit=limit
                )
            except Exception:
                return None
        logger.debug("Slack conversations_history failed", channel=channel_id, error=error_str)
        return None


# ---------------------------------------------------------------------------
# Gmail Ingester (fetch-then-write)
# ---------------------------------------------------------------------------

async def _ingest_gmail(
    user_id: UUID, access_token: str, since: datetime
) -> tuple[list[dict], int]:
    """Fetch recent emails. Returns (activity_dicts, return_code).
    return_code: 0 = success, -1 = auth failure."""

    # Short session: load existing reference_ids to skip already-ingested messages
    existing_ids: set[str] = set()
    async with session_manager.get_session() as session:
        result = await session.execute(
            select(ActivityLog.reference_id).where(
                ActivityLog.user_id == user_id,
                ActivityLog.source == "gmail",
            )
        )
        existing_ids = {row[0] for row in result.all()}
    # Session closed

    # Fetch from Gmail API (no DB session held)
    creds = Credentials(token=access_token)
    service = build("gmail", "v1", credentials=creds, cache_discovery=False)

    epoch = int(since.timestamp())
    results = await asyncio.get_running_loop().run_in_executor(
        None,
        lambda: service.users()
        .messages()
        .list(userId="me", maxResults=50, q=f"after:{epoch}")
        .execute(),
    )

    collected: list[dict] = []
    for msg_ref in results.get("messages", []):
        msg_id = msg_ref["id"]

        # Skip already-ingested messages (uses pre-loaded set, no DB call)
        if msg_id in existing_ids:
            continue

        try:
            msg = await asyncio.get_running_loop().run_in_executor(
                None,
                lambda mid=msg_id: service.users()
                .messages()
                .get(
                    userId="me",
                    id=mid,
                    format="metadata",
                    metadataHeaders=["From", "To", "Subject", "Date"],
                )
                .execute(),
            )
        except Exception:
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        labels = msg.get("labelIds", [])
        event_type = "email_sent" if "SENT" in labels else "email_received"
        subject = headers.get("subject", "(No subject)")
        snippet = msg.get("snippet", "")

        collected.append({
            "user_id": user_id,
            "source": "gmail",
            "event_type": event_type,
            "title": subject,
            "summary": snippet[:500] if snippet else None,
            "reference_id": msg_id,
            "metadata": {
                "from": headers.get("from", ""),
                "to": [addr.strip() for addr in headers.get("to", "").split(",") if addr.strip()],
                "labels": labels,
                "thread_id": msg.get("threadId", ""),
            },
        })

    return collected, 0


# ---------------------------------------------------------------------------
# Slack Ingester (fetch-then-write)
# ---------------------------------------------------------------------------

async def _ingest_slack(
    user_id: UUID, access_token: str, since: datetime
) -> tuple[list[dict], int]:
    """Fetch recent Slack activity relevant to the user.
    Returns (activity_dicts, return_code). -1 = auth failure."""
    client = get_slack_client(token=access_token)

    # Get user's own Slack ID to determine sent vs mentioned
    try:
        auth_resp = await client.auth_test()
        my_slack_id = auth_resp.get("user_id", "")
    except Exception:
        my_slack_id = ""

    if not my_slack_id:
        logger.warning("Slack auth_test failed, cannot determine user ID")
        return [], -1

    oldest_ts = str(since.timestamp())
    collected: list[dict] = []

    # Build the Slack id→name map once per ingestion so every persisted
    # title/summary stores readable `@Name` instead of opaque `<@U...>`
    # tokens. Downstream (suggestion service, chat history, todo content)
    # all read from activity_log; decoding here fixes them in one shot.
    sm = session_manager
    async with sm.get_session() as decode_session:
        slack_id_to_name = await build_decode_map(decode_session)

    # --- 1. DMs and group DMs (always relevant) ---
    try:
        dm_convos = await client.users_conversations(
            limit=20, types="im,mpim", exclude_archived=True,
        )
    except Exception as e:
        logger.warning("Slack DM conversations_list failed", error=str(e))
        dm_convos = {"channels": []}

    for channel in dm_convos.get("channels", [])[:15]:
        channel_id = channel["id"]
        channel_name = channel.get("name", "DM")

        history = await _fetch_slack_history_with_retry(client, channel_id, oldest_ts, 20)
        if history is None:
            continue
        await asyncio.sleep(0.3)

        for msg in history.get("messages", []):
            ts = msg.get("ts", "")
            if not ts:
                continue
            sender = msg.get("user", "")
            event_type = "message_sent" if sender == my_slack_id else "message_received"
            text = decode_with_map(msg.get("text", ""), slack_id_to_name)
            title = f"DM: {text[:100]}" if text else "DM: (no text)"

            collected.append({
                "user_id": user_id,
                "source": "slack",
                "event_type": event_type,
                "title": title,
                "summary": text[:500] if text else None,
                "reference_id": f"{channel_id}:{ts}",
                "metadata": {
                    "channel": channel_name,
                    "channel_id": channel_id,
                    "sender": sender,
                    "thread_ts": msg.get("thread_ts"),
                },
            })

    # --- 2. Channel messages: only where user sent or is mentioned ---
    try:
        ch_convos = await client.users_conversations(
            limit=30, types="public_channel,private_channel", exclude_archived=True,
        )
    except Exception as e:
        logger.warning("Slack channel conversations_list failed", error=str(e))
        ch_convos = {"channels": []}

    mention_tag = f"<@{my_slack_id}>"

    for channel in ch_convos.get("channels", [])[:20]:
        channel_id = channel["id"]
        channel_name = channel.get("name", channel_id)

        history = await _fetch_slack_history_with_retry(client, channel_id, oldest_ts, 30)
        if history is None:
            continue
        await asyncio.sleep(0.3)

        for msg in history.get("messages", []):
            ts = msg.get("ts", "")
            if not ts:
                continue

            sender = msg.get("user", "")
            raw_text = msg.get("text", "")
            is_from_me = sender == my_slack_id
            # Use raw text for the mention-presence check — `mention_tag` is
            # the bot's literal `<@U...>` token. After decoding it'd be the
            # bot's display name, which we can't pre-compute here.
            is_mention = mention_tag in raw_text

            # Skip messages not involving the user
            if not is_from_me and not is_mention:
                continue

            text = decode_with_map(raw_text, slack_id_to_name)
            event_type = "message_sent" if is_from_me else "mention_received"
            title = f"#{channel_name}: {text[:100]}" if text else f"#{channel_name}: (no text)"

            collected.append({
                "user_id": user_id,
                "source": "slack",
                "event_type": event_type,
                "title": title,
                "summary": text[:500] if text else None,
                "reference_id": f"{channel_id}:{ts}",
                "metadata": {
                    "channel": channel_name,
                    "channel_id": channel_id,
                    "sender": sender,
                    "thread_ts": msg.get("thread_ts"),
                    "is_mention": is_mention,
                },
            })

    return collected, 0


# ---------------------------------------------------------------------------
# Google Calendar Ingester (fetch-then-write)
# ---------------------------------------------------------------------------

async def _ingest_calendar(
    user_id: UUID, access_token: str, since: datetime
) -> tuple[list[dict], list[dict], int]:
    """Fetch meetings. Returns (activity_dicts, update_candidates, return_code).
    update_candidates are events that may need updating if already ingested."""
    creds = Credentials(token=access_token)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)

    now = datetime.now(timezone.utc)
    time_min = since.isoformat()
    end_of_day = (now + timedelta(hours=24)).replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = end_of_day.isoformat()

    try:
        results = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: service.events()
            .list(
                calendarId="primary",
                timeMin=time_min,
                timeMax=time_max,
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            )
            .execute(),
        )
    except Exception as e:
        logger.warning("Calendar events list failed", error=str(e))
        return [], [], 0

    collected: list[dict] = []
    update_candidates: list[dict] = []

    for event in results.get("items", []):
        event_id = event.get("id", "")
        if not event_id:
            continue

        # Skip all-day events
        start_raw = event.get("start", {})
        if not start_raw.get("dateTime"):
            continue

        # Parse end time
        end_raw = event.get("end", {})
        end_str = end_raw.get("dateTime") or end_raw.get("date", "")
        if not end_str:
            continue

        try:
            if "T" in end_str:
                end_dt = datetime.fromisoformat(end_str)
            else:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)

        summary = event.get("summary", "(No title)")
        attendees = [
            {
                "email": a.get("email", ""),
                "display_name": a.get("displayName"),
                "response_status": a.get("responseStatus", "needsAction"),
                "self": a.get("self", False),
            }
            for a in event.get("attendees", [])
            if a.get("email")
        ]

        start_str = start_raw.get("dateTime") or start_raw.get("date", "")

        # Calculate duration
        duration_min = None
        if start_str and "T" in start_str:
            try:
                start_dt = datetime.fromisoformat(start_str)
                duration_min = int((end_dt - start_dt).total_seconds() / 60)
            except (ValueError, TypeError):
                pass

        # Look for Google Meet link
        meet_link = ""
        conference_data = event.get("conferenceData", {})
        for entry_point in conference_data.get("entryPoints", []):
            if entry_point.get("entryPointType") == "video":
                meet_link = entry_point.get("uri", "")
                break

        event_type = "meeting_ended" if end_dt <= now else "meeting_upcoming"

        event_metadata = {
            "attendees": attendees[:20],
            "meet_link": meet_link,
            "duration_min": duration_min,
            "organizer": event.get("organizer", {}).get("email", ""),
            "start_time": start_str,
            "end_time": end_str,
            "location": event.get("location", ""),
            "updated": event.get("updated", ""),
            "recurring_event_id": event.get("recurringEventId"),
        }

        activity_data = {
            "user_id": user_id,
            "source": "calendar",
            "event_type": event_type,
            "title": summary,
            "summary": f"Meeting with {len(attendees)} attendees" if attendees else None,
            "reference_id": event_id,
            "metadata": event_metadata,
        }
        collected.append(activity_data)

        # Track events that may need updating if already ingested
        google_updated = event.get("updated", "")
        if google_updated:
            update_candidates.append({
                "reference_id": event_id,
                "updated": google_updated,
                "data": activity_data,
            })

    return collected, update_candidates, 0


# ---------------------------------------------------------------------------
# Linear Ingester (fetch-then-write)
# ---------------------------------------------------------------------------

LINEAR_API_URL = "https://api.linear.app/graphql"


async def _ingest_linear(
    user_id: UUID, access_token: str, since: datetime
) -> tuple[list[dict], int]:
    """Fetch recent Linear issue updates.
    Returns (activity_dicts, return_code). -1 = auth failure (try refresh)."""
    since_iso = since.isoformat()

    query = """
    query RecentIssues($since: DateTimeOrDuration!) {
        issues(
            filter: {
                updatedAt: { gte: $since }
                assignee: { isMe: { eq: true } }
            }
            first: 50
            orderBy: updatedAt
        ) {
            nodes {
                id
                identifier
                title
                state { name }
                priority
                assignee { name email }
                team { name key }
                project { name }
                createdAt
                updatedAt
            }
        }
    }
    """

    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.post(
                LINEAR_API_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "variables": {"since": since_iso}},
            )
        if resp.status_code == 401:
            logger.warning("Linear 401 during ingestion, attempting refresh", user_id=str(user_id))
            return [], -1
        if resp.status_code != 200:
            logger.warning("Linear API error during ingestion", status=resp.status_code, body=resp.text[:500])
            return [], 0

        data = resp.json()
        if "errors" in data:
            logger.warning("Linear GraphQL error during ingestion", errors=data["errors"])
            return [], 0
    except Exception as e:
        logger.warning("Linear ingestion request failed", error=str(e))
        return [], 0

    nodes = (data.get("data") or {}).get("issues", {}).get("nodes", [])
    collected: list[dict] = []

    for node in nodes:
        issue_id = node.get("id", "")
        if not issue_id:
            continue

        status = (node.get("state") or {}).get("name", "")
        created_at = node.get("createdAt", "")
        updated_at = node.get("updatedAt", "")

        if status.lower() in ("done", "completed", "cancelled", "canceled"):
            event_type = "issue_completed"
        elif created_at == updated_at:
            event_type = "issue_created"
        else:
            event_type = "issue_updated"

        reference_id = f"{node.get('identifier', issue_id)}:{status}"

        collected.append({
            "user_id": user_id,
            "source": "linear",
            "event_type": event_type,
            "title": node.get("title", ""),
            "summary": f"[{node.get('identifier', '')}] {status}",
            "reference_id": reference_id,
            "metadata": {
                "issue_id": issue_id,
                "identifier": node.get("identifier", ""),
                "assignee": node.get("assignee", {}).get("name", "") if node.get("assignee") else "",
                "status": status,
                "priority": node.get("priority"),
                "project": node.get("project", {}).get("name", "") if node.get("project") else "",
                "team": node.get("team", {}).get("name", "") if node.get("team") else "",
            },
        })

    return collected, 0


# ---------------------------------------------------------------------------
# GitHub Ingester (fetch-then-write)
# ---------------------------------------------------------------------------

GITHUB_API_URL = "https://api.github.com"


async def _ingest_github(
    user_id: UUID, access_token: str, since: datetime
) -> tuple[list[dict], int]:
    """Fetch recent GitHub activity.
    Returns (activity_dicts, return_code). -1 = auth failure."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            resp = await http_client.get(
                f"{GITHUB_API_URL}/user/events",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/vnd.github.v3+json",
                },
                params={"per_page": 50},
            )
        if resp.status_code == 401:
            logger.warning("GitHub auth failure", status=resp.status_code)
            return [], -1
        elif resp.status_code == 403:
            if resp.headers.get("x-ratelimit-remaining") == "0" or resp.headers.get("retry-after"):
                logger.warning("GitHub rate limited", status=resp.status_code)
                return [], 0
            logger.warning("GitHub permission denied", status=resp.status_code)
            return [], -1
        elif resp.status_code != 200:
            logger.warning("GitHub API transient error", status=resp.status_code)
            return [], 0

        events = resp.json()
    except Exception as e:
        logger.warning("GitHub ingestion request failed", error=str(e))
        return [], 0

    collected: list[dict] = []

    for event in events:
        event_id = event.get("id", "")
        if not event_id:
            continue

        created_str = event.get("created_at", "")
        if created_str:
            try:
                created_dt = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
                if created_dt < since:
                    continue
            except (ValueError, TypeError):
                pass

        event_type_raw = event.get("type", "")
        payload = event.get("payload", {})
        repo_name = event.get("repo", {}).get("name", "")
        actor = event.get("actor", {}).get("login", "")

        title = ""
        event_type = ""
        metadata = {"repo": repo_name, "author": actor}
        reference_id = ""

        if event_type_raw == "PullRequestEvent":
            action = payload.get("action", "")
            pr = payload.get("pull_request", {})
            pr_number = pr.get("number", "")
            title = pr.get("title", "")
            reference_id = f"pr:{repo_name}:{pr_number}"
            if action == "opened":
                event_type = "pr_opened"
            elif action == "closed" and pr.get("merged"):
                event_type = "pr_merged"
            else:
                event_type = f"pr_{action}"
            metadata["branch"] = pr.get("head", {}).get("ref", "")
            metadata["pr_number"] = pr_number

        elif event_type_raw == "IssuesEvent":
            action = payload.get("action", "")
            issue = payload.get("issue", {})
            title = issue.get("title", "")
            reference_id = f"issue:{repo_name}:{issue.get('number', '')}"
            event_type = f"issue_{action}"

        elif event_type_raw == "PushEvent":
            commits = payload.get("commits", [])
            branch = payload.get("ref", "").replace("refs/heads/", "")
            title = f"Pushed {len(commits)} commit(s) to {branch}"
            reference_id = f"push:{event_id}"
            event_type = "push"
            metadata["branch"] = branch
            metadata["commit_count"] = len(commits)

        elif event_type_raw == "CreateEvent":
            ref_type = payload.get("ref_type", "")
            ref = payload.get("ref", "")
            title = f"Created {ref_type} {ref}" if ref else f"Created {ref_type}"
            reference_id = f"create:{event_id}"
            event_type = f"created_{ref_type}"

        elif event_type_raw == "PullRequestReviewEvent":
            pr = payload.get("pull_request", {})
            review = payload.get("review", {})
            title = f"Reviewed PR: {pr.get('title', '')}"
            reference_id = f"review:{event_id}"
            event_type = "pr_reviewed"
            metadata["pr_number"] = pr.get("number", "")
            metadata["review_state"] = review.get("state", "")

        else:
            continue

        if not title or not event_type:
            continue

        collected.append({
            "user_id": user_id,
            "source": "github",
            "event_type": event_type,
            "title": title,
            "summary": None,
            "reference_id": reference_id,
            "metadata": metadata,
        })

    return collected, 0


# ---------------------------------------------------------------------------
# Delegation thread ingestion (fetch-then-write)
# ---------------------------------------------------------------------------

async def _ingest_delegation_threads(user_id: UUID) -> int:
    """
    For all active delegated todos owned by this user, fetch new Slack thread
    replies and store them as TodoUpdate entries.
    Manages own short-lived sessions.
    """
    from db.models import Todo, TodoUpdate

    # Phase A: Read todos + existing dedup data + slack token (short session)
    todo_data: list[dict] = []
    slack_token: str | None = None

    async with session_manager.get_session() as session:
        result = await session.execute(
            select(Todo).where(
                Todo.user_id == user_id,
                Todo.completed == False,
                Todo.delegated_to.isnot(None),
                Todo.slack_channel_id.isnot(None),
                Todo.slack_thread_ts.isnot(None),
            )
        )
        todos = result.scalars().all()
        if not todos:
            return 0

        # Get slack token
        int_result = await session.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "slack",
                Integration.is_active == True,
            ).limit(1)
        )
        integ = int_result.scalar_one_or_none()
        if not integ:
            return 0

        slack_token = decrypt(integ.access_token) if integ.access_token else None
        if not slack_token:
            slack_token = decrypt(integ.bot_token) if integ.bot_token else None
        if not slack_token:
            return 0

        # Build the Slack id→name map up front so dedup-set content and
        # newly-fetched message text are compared in the same (decoded) form.
        # Older TodoUpdate.content rows were stored raw (`<@U...>`); without
        # decoding them on load we would re-ingest those messages as new.
        slack_id_to_name = await build_decode_map(session)

        # Batch-load dedup data: all todo IDs + their linked todo IDs in bulk
        all_todo_ids = [todo.id for todo in todos]

        # 1. Bulk fetch existing TodoUpdate content for all todos
        existing_result = await session.execute(
            select(TodoUpdate.todo_id, TodoUpdate.content)
            .where(TodoUpdate.todo_id.in_(all_todo_ids), TodoUpdate.source == "slack")
        )
        existing_by_todo: dict[UUID, set[str]] = {}
        for row in existing_result.all():
            existing_by_todo.setdefault(row[0], set()).add(decode_with_map(row[1] or "", slack_id_to_name))

        # 2. Bulk fetch linked todos (delegated-to-me mirrors)
        linked_result = await session.execute(
            select(Todo.delegated_from_todo_id, Todo.id)
            .where(Todo.delegated_from_todo_id.in_(all_todo_ids))
        )
        linked_by_parent: dict[UUID, list[UUID]] = {}
        all_linked_ids: list[UUID] = []
        for row in linked_result.all():
            linked_by_parent.setdefault(row[0], []).append(row[1])
            all_linked_ids.append(row[1])

        # 3. Bulk fetch existing TodoUpdate content for all linked todos
        linked_existing_by_todo: dict[UUID, set[str]] = {}
        if all_linked_ids:
            lr = await session.execute(
                select(TodoUpdate.todo_id, TodoUpdate.content)
                .where(TodoUpdate.todo_id.in_(all_linked_ids), TodoUpdate.source == "slack")
            )
            for row in lr.all():
                linked_existing_by_todo.setdefault(row[0], set()).add(decode_with_map(row[1] or "", slack_id_to_name))

        # Build todo_data from the bulk results
        for todo in todos:
            linked_ids = linked_by_parent.get(todo.id, [])
            linked_existing_contents: dict[str, set[str]] = {
                str(lid): linked_existing_by_todo.get(lid, set()) for lid in linked_ids
            }
            todo_data.append({
                "todo_id": todo.id,
                "slack_channel_id": todo.slack_channel_id,
                "slack_thread_ts": todo.slack_thread_ts,
                "existing_contents": existing_by_todo.get(todo.id, set()),
                "linked_ids": linked_ids,
                "linked_existing_contents": linked_existing_contents,
            })
    # Session closed

    # Phase B: Fetch all thread replies from Slack (no DB session)
    client = get_slack_client(token=slack_token)
    sender_name_cache: dict[str, str] = {}
    all_updates: list[dict] = []

    for td in todo_data:
        try:
            resp = await client.conversations_replies(
                channel=td["slack_channel_id"],
                ts=td["slack_thread_ts"],
                limit=50,
            )
            messages = resp.get("messages", [])

            for msg in messages:
                ts = msg.get("ts", "")
                if ts == td["slack_thread_ts"]:
                    continue
                if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                    continue

                text = decode_with_map(msg.get("text", "").strip(), slack_id_to_name)
                if not text:
                    continue

                truncated_text = text[:1000]
                if truncated_text in td["existing_contents"]:
                    continue

                # Cache users_info to avoid N+1
                slack_uid = msg.get("user", "Unknown")
                if slack_uid not in sender_name_cache:
                    try:
                        user_info = await client.users_info(user=slack_uid)
                        profile = user_info.get("user", {}).get("profile", {})
                        sender_name_cache[slack_uid] = (
                            profile.get("real_name") or profile.get("display_name") or slack_uid
                        )
                    except Exception:
                        sender_name_cache[slack_uid] = slack_uid
                sender_name = sender_name_cache[slack_uid]

                all_updates.append({
                    "todo_id": td["todo_id"],
                    "author_name": sender_name,
                    "content": truncated_text,
                    "linked_ids": td["linked_ids"],
                    "linked_existing_contents": td["linked_existing_contents"],
                })
                td["existing_contents"].add(truncated_text)  # prevent intra-batch duplicates

        except Exception as e:
            logger.warning(
                "Failed to fetch delegation thread",
                todo_id=str(td["todo_id"]),
                error=str(e),
            )
        await asyncio.sleep(0.3)

    if not all_updates:
        return 0

    # Phase C: Write all updates (short session)
    async with session_manager.get_session() as session:
        for upd in all_updates:
            session.add(TodoUpdate(
                todo_id=upd["todo_id"],
                author_name=upd["author_name"],
                content=upd["content"],
                source="slack",
            ))

            # Propagate to linked todos
            for lid in upd["linked_ids"]:
                lid_str = str(lid)
                if upd["content"] not in upd["linked_existing_contents"].get(lid_str, set()):
                    session.add(TodoUpdate(
                        todo_id=lid,
                        author_name=upd["author_name"],
                        content=upd["content"],
                        source="slack",
                    ))
                    upd["linked_existing_contents"].setdefault(lid_str, set()).add(upd["content"])

        await session.flush()

    return len(all_updates)


# ---------------------------------------------------------------------------
# Linear token refresh helper
# ---------------------------------------------------------------------------

async def _refresh_linear_token(user_id: UUID, info: dict) -> str | None:
    """Attempt to refresh a Linear token. Returns new access_token or None."""
    try:
        from services.oauth.linear_oauth import LinearOAuthProvider
        from services.encryption_service import encrypt

        new_tokens = await LinearOAuthProvider().refresh_access_token(
            decrypt(info["refresh_token"])
        )
        # Write refreshed token (short session)
        async with session_manager.get_session() as session:
            result = await session.execute(
                select(Integration).where(Integration.id == info["integration_id"])
            )
            integration = result.scalar_one_or_none()
            if integration:
                integration.access_token = encrypt(new_tokens.access_token)
                if new_tokens.refresh_token:
                    integration.refresh_token = encrypt(new_tokens.refresh_token)
                integration.token_expiry = new_tokens.token_expiry
                await session.flush()
        logger.info("Linear token refreshed during ingestion", user_id=str(user_id))
        return new_tokens.access_token
    except Exception as e:
        logger.error("Linear refresh failed during ingestion", error=str(e))
        return None


# ---------------------------------------------------------------------------
# Main orchestrator (manages own sessions — no session parameter)
# ---------------------------------------------------------------------------

async def ingest_user_activities(user_id: UUID, since: datetime | None = None) -> int:
    """Poll all connected integrations and log new activities.
    Manages own short-lived DB sessions. Returns count of new entries."""

    # Phase 1: Load integrations + resolve tokens (short session)
    provider_info: dict[str, dict] = {}
    async with session_manager.get_session() as session:
        result = await session.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.is_active == True,
            )
        )
        integrations = result.scalars().all()

        for integration in integrations:
            if integration.provider in provider_info:
                continue
            token = await _resolve_token(integration, session)
            provider_info[integration.provider] = {
                "token": token,
                "integration_id": integration.id,
                "error_count": integration.error_count or 0,
                "refresh_token": integration.refresh_token,
            }
    # Session closed. All external API calls below happen without a DB connection.

    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    all_activities: list[dict] = []
    calendar_updates: list[dict] = []
    succeeded_providers: list[dict] = []
    failed_providers: list[tuple[dict, str]] = []

    # Phase 2: Fetch from all external APIs (no DB session held)
    for provider, info in provider_info.items():
        if not info["token"]:
            continue

        try:
            if provider == "google":
                google_ok = True
                try:
                    gmail_acts, gmail_rc = await _ingest_gmail(user_id, info["token"], since)
                    all_activities.extend(gmail_acts)
                except Exception as e:
                    google_ok = False
                    logger.error("Gmail ingestion failed", user_id=str(user_id), error=str(e))

                try:
                    cal_acts, cal_upds, cal_rc = await _ingest_calendar(user_id, info["token"], since)
                    all_activities.extend(cal_acts)
                    calendar_updates.extend(cal_upds)
                except Exception as e:
                    google_ok = False
                    logger.error("Calendar ingestion failed", user_id=str(user_id), error=str(e))

                if google_ok:
                    succeeded_providers.append(info)
                else:
                    failed_providers.append((info, "Google ingestion failed"))

            elif provider == "slack":
                acts, rc = await _ingest_slack(user_id, info["token"], since)
                all_activities.extend(acts)
                if rc == -1:
                    failed_providers.append((info, "Slack auth failure"))
                else:
                    succeeded_providers.append(info)

            elif provider == "linear":
                acts, rc = await _ingest_linear(user_id, info["token"], since)
                if rc == -1 and info["refresh_token"]:
                    new_token = await _refresh_linear_token(user_id, info)
                    if new_token:
                        acts, rc = await _ingest_linear(user_id, new_token, since)
                    else:
                        failed_providers.append((info, "Linear token refresh failed"))
                        continue

                all_activities.extend(acts)
                if rc == -1:
                    failed_providers.append((info, "Linear ingestion auth failure"))
                else:
                    succeeded_providers.append(info)

            elif provider == "github":
                acts, rc = await _ingest_github(user_id, info["token"], since)
                all_activities.extend(acts)
                if rc == -1:
                    failed_providers.append((info, "GitHub auth failure"))
                else:
                    succeeded_providers.append(info)

        except Exception as e:
            logger.error(
                "Activity ingestion failed for provider",
                user_id=str(user_id),
                provider=provider,
                error=str(e),
            )
            failed_providers.append((info, f"Ingestion failed: {e}"))

    # Phase 3: Batch-write all activities + integration health (short session)
    total_new = 0
    async with session_manager.get_session() as session:
        for activity_data in all_activities:
            result = await _create_activity(
                user_id=activity_data["user_id"],
                source=activity_data["source"],
                event_type=activity_data["event_type"],
                title=activity_data["title"],
                summary=activity_data.get("summary"),
                reference_id=activity_data["reference_id"],
                metadata=activity_data["metadata"],
                session=session,
            )
            if result:
                total_new += 1

        # Handle calendar updates (events modified since last ingestion)
        for upd in calendar_updates:
            existing = await session.execute(
                select(ActivityLog).where(
                    ActivityLog.user_id == user_id,
                    ActivityLog.source == "calendar",
                    ActivityLog.reference_id == upd["reference_id"],
                )
            )
            row = existing.scalar_one_or_none()
            if row:
                old_updated = (row.extra_data or {}).get("updated", "")
                if upd["updated"] != old_updated:
                    row.title = upd["data"]["title"]
                    row.summary = upd["data"].get("summary")
                    row.event_type = upd["data"]["event_type"]
                    row.extra_data = upd["data"]["metadata"]
                    await session.flush()
                    logger.info("Calendar event updated", event_id=upd["reference_id"], user_id=str(user_id))

        # Integration health: record errors / clear on success
        for info in succeeded_providers:
            if info["error_count"] > 0:
                await clear_integration_errors(session, info["integration_id"])
        for info, error in failed_providers:
            await record_integration_error(session, info["integration_id"], error)

    # Log provider counts
    for provider, info in provider_info.items():
        if info["token"]:
            if provider == "google":
                provider_count = sum(1 for a in all_activities if a["source"] in ("gmail", "calendar"))
            else:
                provider_count = sum(1 for a in all_activities if a["source"] == provider)
            logger.info("Ingested activities", user_id=str(user_id), provider=provider, new_count=provider_count)

    # Phase 4: Delegation threads (manages own short sessions)
    try:
        delegation_count = await _ingest_delegation_threads(user_id)
        if delegation_count:
            logger.info("Ingested delegation thread replies", user_id=str(user_id), new_count=delegation_count)
    except Exception as e:
        logger.warning("Delegation thread ingestion failed", user_id=str(user_id), error=str(e))

    # Phase 5: Topic assignment (short session)
    try:
        async with session_manager.get_session() as session:
            await assign_topics_for_user(user_id, session)
    except Exception as e:
        logger.warning("Topic assignment failed", user_id=str(user_id), error=str(e))

    return total_new
