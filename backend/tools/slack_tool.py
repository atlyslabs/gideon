"""
Slack Tools — channels, messages, search via slack_sdk AsyncWebClient.
"""

import asyncio
from contextvars import ContextVar
from datetime import date, datetime, time as dt_time, timedelta, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

import structlog

from db.database import AsyncPostgresSessionGideon
from services.slack_format import markdown_to_slack_mrkdwn
from services.slack_mention_resolver import (
    build_decode_map,
    decode_with_map,
    encode_slack_mentions,
)
from services.workspace_member_service import get_all_members
from slack_sdk.web.async_client import AsyncWebClient
from tools.base import BaseTool

from services.slack_client import get_slack_client


logger = structlog.get_logger()


def _get_slack_client(bot_token: str) -> AsyncWebClient:
    # Thin delegate to the shared retry-enabled factory (services.slack_client).
    return get_slack_client(bot_token)


# ---------------------------------------------------------------------------
# Human-friendly time windows.
#
# Slack's history/search APIs want Unix epoch seconds, but making the model do
# date -> epoch arithmetic in its head is unreliable — it silently miscomputes
# day boundaries and timezones (observed: a "what did X send me on Friday" run
# recomputing the epoch several times and still getting it wrong). So
# read_slack_channel / read_slack_dm accept `oldest`/`latest` as EITHER an epoch
# number OR a human string: an ISO date ("2026-07-10"), an ISO datetime, or a
# relative word ("today", "yesterday", "friday"). Bare dates and relative words
# resolve in the USER's timezone, carried here per turn via SLACK_USER_TZ (set
# by chat_service / workflow_execution_service before tool calls; defaults to
# UTC when unset, e.g. tests / internal callers). Search uses Slack's native
# `after:`/`before:`/`on:` operators instead, which take dates directly.
# ---------------------------------------------------------------------------

SLACK_USER_TZ: ContextVar[Optional[str]] = ContextVar("SLACK_USER_TZ", default=None)

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _active_tz() -> Any:
    """Resolve the turn's user timezone, falling back to UTC (unset, or a bad
    IANA name that would otherwise raise)."""
    name = SLACK_USER_TZ.get()
    if name:
        try:
            return ZoneInfo(name)
        except Exception:
            return timezone.utc
    return timezone.utc


def _most_recent_weekday(now: datetime, weekday: int) -> date:
    """The most recent date (today or earlier) whose weekday == `weekday`."""
    delta = (now.weekday() - weekday) % 7
    return (now - timedelta(days=delta)).date()


def _try_parse_iso(s: str, tz: Any) -> Optional[datetime]:
    """Parse an ISO date or datetime; a naive value is assumed to be in `tz`.
    Returns None (not a raise) when the string isn't ISO-shaped."""
    raw = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return None
        return datetime.combine(d, dt_time.min, tzinfo=tz)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def _resolve_time_bound(value: Any, is_latest: bool) -> Optional[str]:
    """Convert an oldest/latest bound to a Slack epoch-seconds string.

    Accepts: None (-> None); an epoch number or numeric string (passed through);
    an ISO date/datetime; or a relative word (today/now/yesterday/tomorrow or a
    weekday). Bare dates and relative words resolve to the START of that day for
    `oldest` and the END (23:59:59.999999) for `latest`, in the user's timezone.
    Raises ValueError on an unparseable non-empty string so the caller surfaces
    a clear error instead of silently dropping the filter (which would widen the
    window to 'everything')."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return str(value)
    s = str(value).strip()
    if not s:
        return None
    try:
        return str(float(s))  # numeric string == epoch already
    except ValueError:
        pass
    tz = _active_tz()
    now = datetime.now(tz)
    low = s.lower()
    target_date: Optional[date] = None
    if low in ("today", "now"):
        target_date = now.date()
    elif low == "yesterday":
        target_date = (now - timedelta(days=1)).date()
    elif low == "tomorrow":
        target_date = (now + timedelta(days=1)).date()
    elif low in _WEEKDAYS:
        target_date = _most_recent_weekday(now, _WEEKDAYS[low])
    if target_date is not None:
        end = dt_time.max if is_latest else dt_time.min
        return str(datetime.combine(target_date, end, tzinfo=tz).timestamp())
    dt = _try_parse_iso(s, tz)
    if dt is not None:
        # A bare date used as the UPPER bound means "through the end of that day".
        if is_latest and len(s) <= 10:
            dt = datetime.combine(dt.date(), dt_time.max, tzinfo=tz)
        return str(dt.timestamp())
    raise ValueError(
        f"Could not parse time value {value!r}. Use epoch seconds, an ISO date "
        "like 2026-07-10, or a word like 'today' / 'yesterday' / 'friday'."
    )


async def _fetch_history(
    client: AsyncWebClient,
    channel_id: str,
    oldest: Optional[str],
    latest: Optional[str],
    limit_param: Any,
) -> tuple[list, bool]:
    """Fetch conversation history, shared by read_slack_channel and
    read_slack_dm. Returns (raw_messages, windowed_truncated).

    A windowed read (oldest/latest given) paginates the whole [oldest, latest]
    range so 'read all of yesterday' can't silently miss older messages (Slack
    returns newest first, ~200/page). A plain read keeps the old single-page,
    small-cap behavior so casual lookups don't blow up the context."""
    windowed = oldest is not None or latest is not None
    hist_kwargs: dict[str, Any] = {}
    if oldest is not None:
        hist_kwargs["oldest"] = oldest
    if latest is not None:
        hist_kwargs["latest"] = latest
    if windowed:
        hist_kwargs["inclusive"] = True

    windowed_truncated = False
    if windowed:
        # Clamp to a sane positive cap: a 0/negative limit would otherwise
        # return empty + a false "truncated" flag.
        try:
            target = int(limit_param if limit_param is not None else 1000)
        except (TypeError, ValueError):
            target = 1000
        target = max(1, min(target, 1000))
        raw_messages: list = []
        cursor: Optional[str] = None
        for _ in range(20):  # pagination safety cap (20 * 200 = 4000)
            result = await client.conversations_history(
                channel=channel_id,
                limit=200,
                cursor=cursor or None,
                **hist_kwargs,
            )
            raw_messages.extend(result.get("messages", []) or [])
            cursor = (result.get("response_metadata") or {}).get("next_cursor")
            if len(raw_messages) >= target:
                # More remains if we trimmed mid-collection or Slack has another page.
                windowed_truncated = len(raw_messages) > target or bool(cursor)
                raw_messages = raw_messages[:target]
                break
            if not cursor:
                break  # natural end of the window — fully covered
        else:
            # Exhausted the 20-page safety cap with a cursor still pending.
            windowed_truncated = bool(cursor)
    else:
        try:
            plain = int(limit_param if limit_param is not None else 15)
        except (TypeError, ValueError):
            plain = 15
        result = await client.conversations_history(
            channel=channel_id, limit=min(plain, 50)
        )
        raw_messages = result.get("messages", [])
    return raw_messages, windowed_truncated


def _format_channel_messages(raw_messages: list, id_to_name: dict[str, str]) -> list[dict]:
    """Resolve author IDs to names + decode `<@U...>` mentions, and surface Block
    Kit / legacy attachment content (the `text` field is often empty for
    app/alert messages whose real data lives in blocks). Shared by
    read_slack_channel and read_slack_dm so their output shape can't drift."""
    messages = []
    for m in raw_messages:
        sid = m.get("user", "")
        entry = {
            "user_name": id_to_name.get(sid, "") if sid else "",
            "slack_id": sid,
            "text": decode_with_map(m.get("text", ""), id_to_name),
            "ts": m.get("ts", ""),
            "thread_ts": m.get("thread_ts"),
        }
        if m.get("blocks"):
            entry["parsed_blocks"] = parse_slack_blocks(m["blocks"], id_to_name)
        if m.get("attachments"):
            entry["parsed_attachments"] = parse_slack_attachments(m["attachments"], id_to_name)
        messages.append(entry)
    return messages


# ---------------------------------------------------------------------------
# Block Kit / legacy-attachment parsing.
#
# Slack returns rich structured content in `blocks` (Block Kit) and
# `attachments` (legacy), but the message `text` field is often empty or just a
# notification fallback. Critical alert data (user IDs, record IDs, status
# codes, etc.) lives INSIDE these structures, so read_slack_channel
# / read_slack_thread surface them under `parsed_blocks` / `parsed_attachments`.
# Mention syntax (<@U…>) inside text is decoded to @name; everything else
# (plain-text IDs, codes, URLs) is preserved verbatim. Unknown block types are
# kept under `raw` so nothing is ever silently dropped.
# ---------------------------------------------------------------------------

def _as_list(value: Any) -> list:
    """Validate an iteration precondition: return `value` only if it's actually a
    list, else []. Slack payloads are normally well-formed, but a malformed (or
    future-changed) shape — e.g. `blocks`/`fields`/`elements` arriving as a non-
    list — must NOT raise and fail the whole channel/thread read."""
    return value if isinstance(value, list) else []


def _decode_str(value: Any, id_to_name: dict[str, str]) -> Any:
    return decode_with_map(value, id_to_name) if isinstance(value, str) else value


def _block_text(obj: Any) -> str:
    """Pull `.text` out of a Block Kit text object ({type, text}); pass strings through."""
    if isinstance(obj, dict):
        return obj.get("text", "") or ""
    return obj if isinstance(obj, str) else ""


def _parse_block_element(el: Any, id_to_name: dict[str, str]) -> dict:
    """Parse one element of an `actions`/`context` block (button, image, text, or
    any other interactive element) into a flat, readable dict."""
    if not isinstance(el, dict):
        return {"raw": el}
    etype = el.get("type", "")
    if etype == "button":
        return {
            "type": "button",
            "text": _decode_str(_block_text(el.get("text")), id_to_name),
            "value": el.get("value"),
            "url": el.get("url"),
            "action_id": el.get("action_id"),
        }
    if etype == "image":
        return {"type": "image", "image_url": el.get("image_url"), "alt_text": el.get("alt_text")}
    if etype in ("mrkdwn", "plain_text"):
        return {"type": "text", "text": _decode_str(el.get("text", ""), id_to_name)}
    # Any other interactive element (select, datepicker, overflow, …): keep the
    # type + whatever identifying/text/value/url fields it carries.
    out: dict[str, Any] = {"type": etype or "element"}
    if "text" in el:
        out["text"] = _decode_str(_block_text(el.get("text")), id_to_name)
    if el.get("action_id"):
        out["action_id"] = el["action_id"]
    if el.get("value") is not None:
        out["value"] = el["value"]
    if el.get("url"):
        out["url"] = el["url"]
    return out


def _rich_text_to_str(block: dict, id_to_name: dict[str, str]) -> str:
    """Flatten a `rich_text` block (nested sections → elements) to plain text,
    resolving user mentions, links, and emoji."""
    parts: list[str] = []
    for section in _as_list(block.get("elements")):
        if not isinstance(section, dict):
            continue
        for el in _as_list(section.get("elements")):
            if not isinstance(el, dict):
                continue
            t = el.get("type")
            if t == "text":
                parts.append(el.get("text", ""))
            elif t == "link":
                parts.append(el.get("text") or el.get("url", ""))
            elif t == "user":
                uid = el.get("user_id", "")
                parts.append("@" + id_to_name.get(uid, uid))
            elif t == "emoji":
                parts.append(f":{el.get('name', '')}:")
            elif "text" in el:
                parts.append(el.get("text", ""))
        parts.append("\n")
    return "".join(parts).strip()


def parse_slack_blocks(blocks: Any, id_to_name: dict[str, str]) -> list[dict]:
    """Parse a message's Block Kit `blocks` into a structured, readable list."""
    parsed: list[dict] = []
    for b in _as_list(blocks):
        if not isinstance(b, dict):
            continue
        t = b.get("type", "")
        try:
            if t == "section":
                entry: dict[str, Any] = {"type": "section"}
                if b.get("text"):
                    entry["text"] = _decode_str(_block_text(b["text"]), id_to_name)
                if b.get("fields"):
                    entry["fields"] = [_decode_str(_block_text(f), id_to_name) for f in _as_list(b.get("fields"))]
                if b.get("accessory"):
                    entry["accessory"] = _parse_block_element(b["accessory"], id_to_name)
                parsed.append(entry)
            elif t == "divider":
                parsed.append({"type": "divider"})
            elif t == "image":
                parsed.append({
                    "type": "image",
                    "image_url": b.get("image_url"),
                    "alt_text": b.get("alt_text"),
                    "title": _decode_str(_block_text(b.get("title")), id_to_name) if b.get("title") else None,
                })
            elif t == "actions":
                parsed.append({"type": "actions", "elements": [_parse_block_element(e, id_to_name) for e in _as_list(b.get("elements"))]})
            elif t == "context":
                parsed.append({"type": "context", "elements": [_parse_block_element(e, id_to_name) for e in _as_list(b.get("elements"))]})
            elif t == "header":
                parsed.append({"type": "header", "text": _decode_str(_block_text(b.get("text")), id_to_name)})
            elif t == "rich_text":
                parsed.append({"type": "rich_text", "text": _decode_str(_rich_text_to_str(b, id_to_name), id_to_name)})
            elif t in ("file", "video"):
                entry = {"type": t}
                if b.get("title"):
                    entry["title"] = _decode_str(_block_text(b.get("title")), id_to_name)
                for k in ("external_id", "url", "thumbnail_url", "title_url", "description", "name", "mimetype", "filetype", "permalink", "alt_text"):
                    if b.get(k) is not None:
                        entry[k] = b[k]
                parsed.append(entry)
            else:
                # Unknown / future block type — preserve it verbatim so structured
                # data is never silently lost.
                parsed.append({"type": t or "unknown", "raw": b})
        except Exception as e:
            # A single malformed block must not fail the whole read — keep the raw
            # block (so data isn't lost) and move on.
            logger.warning("Slack block parse failed; preserving raw", block_type=t, error=str(e)[:160])
            parsed.append({"type": t or "unknown", "raw": b, "parse_error": True})
    return parsed


def parse_slack_attachments(attachments: Any, id_to_name: dict[str, str]) -> list[dict]:
    """Parse legacy `attachments` into a full, un-truncated representation."""
    parsed: list[dict] = []
    _DECODE = {"pretext", "text", "title", "fallback", "author_name", "footer"}
    for a in _as_list(attachments):
        if not isinstance(a, dict):
            continue
        try:
            entry: dict[str, Any] = {}
            for k in (
                "pretext", "text", "title", "title_link", "author_name", "author_link",
                "image_url", "thumb_url", "footer", "ts", "color", "fallback",
            ):
                if a.get(k) is not None:
                    entry[k] = _decode_str(a[k], id_to_name) if k in _DECODE else a[k]
            if a.get("fields"):
                entry["fields"] = [
                    {
                        "title": _decode_str(f.get("title", ""), id_to_name),
                        "value": _decode_str(f.get("value", ""), id_to_name),
                        "short": f.get("short"),
                    }
                    for f in _as_list(a.get("fields")) if isinstance(f, dict)
                ]
            # Some attachments embed Block Kit blocks of their own.
            if a.get("blocks"):
                entry["parsed_blocks"] = parse_slack_blocks(a["blocks"], id_to_name)
            parsed.append(entry)
        except Exception as e:
            # One malformed attachment must not fail the whole read.
            logger.warning("Slack attachment parse failed; skipping", error=str(e)[:160])
    return parsed


class ListChannels(BaseTool):
    @property
    def name(self) -> str:
        return "list_slack_channels"

    @property
    def description(self) -> str:
        return (
            "List Slack channels in the workspace — every public channel plus private channels "
            "the authenticated user belongs to. Bot membership is NOT required to list a channel; "
            "it IS required to read a channel's full feed via read_slack_channel. "
            "Paginates automatically until all matching channels are returned (up to `limit`). "
            "If you can't find a specific channel by name, raise `limit` or filter with `name_contains`."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Max channels to return across all pages (default 500, hard cap 1000).",
                    "default": 500,
                },
                "name_contains": {
                    "type": "string",
                    "description": "Optional case-insensitive substring to filter channel names (e.g. 'project' matches #project-atlas, #project-orion).",
                },
                "exclude_archived": {
                    "type": "boolean",
                    "description": "Skip archived channels (default true).",
                    "default": True,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        target_limit = min(int(params.get("limit", 500)), 1000)
        name_filter = (params.get("name_contains") or "").strip().lower()
        exclude_archived = params.get("exclude_archived", True)

        channels: list[dict[str, Any]] = []
        cursor: str | None = None
        # Will flip to True iff target_limit was hit AND we KNOW more data
        # exists — either we stopped before finishing the current page, or
        # Slack returned a next_cursor.
        more_available = False
        # Cap loop iterations so we never spin indefinitely on a weird response.
        for _ in range(20):
            result = await client.conversations_list(
                limit=200,  # Slack page size — independent of our target_limit
                types="public_channel,private_channel",
                exclude_archived=exclude_archived,
                cursor=cursor or None,
            )
            next_cursor = (result.get("response_metadata") or {}).get("next_cursor")
            page_channels = result.get("channels", []) or []
            stopped_mid_page = False
            for idx, c in enumerate(page_channels):
                name = c.get("name", "")
                if name_filter and name_filter not in name.lower():
                    continue
                channels.append(
                    {
                        "id": c["id"],
                        "name": name,
                        "topic": c.get("topic", {}).get("value", ""),
                        "num_members": c.get("num_members", 0),
                        "is_member": c.get("is_member", False),
                    }
                )
                if len(channels) >= target_limit:
                    # We know more exists on THIS page only if there are
                    # unread items after this index.
                    stopped_mid_page = (idx + 1) < len(page_channels)
                    break
            if len(channels) >= target_limit:
                more_available = stopped_mid_page or bool(next_cursor)
                break
            cursor = next_cursor
            if not cursor:
                break

        return {
            "channels": channels,
            "count": len(channels),
            "truncated": len(channels) >= target_limit and more_available,
        }


class ReadChannel(BaseTool):
    @property
    def name(self) -> str:
        return "read_slack_channel"

    @property
    def description(self) -> str:
        return (
            "Read messages from a Slack channel. Provide the channel ID. "
            "App/alert messages often have an empty `text` field — their real data "
            "(user IDs, record IDs, status codes, etc.) is in `parsed_blocks` "
            "(Block Kit) and `parsed_attachments` (legacy) on each message. Always "
            "check those for structured content. "
            "To read EVERY message in a time range (e.g. a full calendar day for a "
            "daily digest), pass `oldest` and `latest` — the tool then paginates the "
            "whole window (up to `limit`, default 1000) instead of returning only the "
            "most recent few. This is the reliable way to capture a day's messages; do "
            "NOT rely on search for that. `oldest`/`latest` accept Unix epoch seconds "
            "OR a plain date like '2026-07-10' OR a relative word ('today', "
            "'yesterday', 'friday') — dates/words are resolved in the user's timezone "
            "(a date as `oldest` means 00:00 that day; as `latest` means 23:59), so you "
            "do NOT need to compute epochs yourself. Without `oldest`/`latest` it "
            "returns just the most recent `limit` messages. "
            "This reads ONE channel. To scan across MANY channels (e.g. 'find "
            "ticket-worthy messages across my channels'), use `search_slack_messages` "
            "(one call searches ALL channels) — do NOT loop this once per channel, "
            "which exhausts a workflow's step budget."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID (e.g. C01234567)",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Max messages to fetch. Default 15 (cap 50) for a plain recent read; "
                        "when `oldest`/`latest` are given it paginates the window up to this "
                        "many (default 1000, hard cap 1000)."
                    ),
                },
                "oldest": {
                    "type": ["number", "string"],
                    "description": (
                        "Lower bound of the window. Unix epoch seconds, OR a date "
                        "'2026-07-10', OR a word 'today'/'yesterday'/'friday' "
                        "(resolved to 00:00 that day in the user's timezone)."
                    ),
                },
                "latest": {
                    "type": ["number", "string"],
                    "description": (
                        "Upper bound of the window. Unix epoch seconds, OR a date "
                        "'2026-07-10', OR a word 'today'/'yesterday'/'friday' "
                        "(a bare date/word resolves to 23:59 that day in the user's timezone)."
                    ),
                },
            },
            "required": ["channel_id"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            oldest = _resolve_time_bound(params.get("oldest"), is_latest=False)
            latest = _resolve_time_bound(params.get("latest"), is_latest=True)
        except ValueError as e:
            return {"error": str(e)}

        client = _get_slack_client(access_token)
        raw_messages, windowed_truncated = await _fetch_history(
            client, params["channel_id"], oldest, latest, params.get("limit")
        )
        windowed = oldest is not None or latest is not None

        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            id_to_name = await build_decode_map(session)

        # Resolve `user` IDs to names AND decode `<@U...>` in the body. Without
        # the author resolution, Claude sees raw IDs in the `user` field and
        # has been known to repeat them back to the user verbatim.
        messages = _format_channel_messages(raw_messages, id_to_name)
        out = {"messages": messages, "count": len(messages)}
        # Flag only when we actually stopped with more messages remaining in the
        # window (cap hit or pagination safety cap), so the caller knows the day
        # isn't fully covered. A window that ended naturally is NOT truncated.
        if windowed and windowed_truncated:
            out["truncated"] = True
            out["note"] = "Hit the message cap for this window — older messages in the range may be missing. Narrow the window or raise limit."
        return out


class ListChannelMembers(BaseTool):
    @property
    def name(self) -> str:
        return "list_channel_members"

    @property
    def description(self) -> str:
        return (
            "List the full member roster of a Slack channel — every user, not just "
            "recent senders. Returns each member's name + example.com email, joined "
            "from the Slack workspace cache, so you can pass emails directly to "
            "`find_free_time` or `create_calendar_event`. Paginates `conversations.members` "
            "until exhausted. Use this for 'schedule a meeting with everyone in #foo' "
            "or 'who's in #foo'. Members without a resolved email come back with "
            "`email: \"\"` and a `slack_id` — do NOT surface that raw ID to the "
            "user; instead say you couldn't resolve N members by name and ask if "
            "they want you to proceed with the resolved set."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID (e.g. C01234567).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Cap on total members returned across pages (default 500, max 1000).",
                    "default": 500,
                },
            },
            "required": ["channel_id"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        target_limit = min(int(params.get("limit", 500)), 1000)

        # Fetch the channel's member IDs (paginates).
        member_ids: list[str] = []
        cursor: str | None = None
        for _ in range(20):  # safety cap on pagination loops
            resp = await client.conversations_members(
                channel=params["channel_id"],
                limit=200,
                cursor=cursor or None,
            )
            member_ids.extend(resp.get("members", []) or [])
            if len(member_ids) >= target_limit:
                member_ids = member_ids[:target_limit]
                break
            cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not cursor:
                break

        # Join with the workspace-member cache (name + email keyed by slack_id).
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            members = await get_all_members(session)
        by_slack_id = {m.get("slack_id"): m for m in members if m.get("slack_id")}

        resolved: list[dict] = []
        cache_misses: list[str] = []
        for sid in member_ids:
            cached = by_slack_id.get(sid)
            if cached and cached.get("email"):
                resolved.append({
                    "name": cached.get("name", ""),
                    "email": cached["email"],
                    "slack_id": sid,
                })
            else:
                cache_misses.append(sid)

        # Cache misses are mostly bots (Gideon itself, Slackbot, integration
        # bots like Notion/Linear) and deactivated users — `_fetch_slack_members`
        # filters those out at the workspace level. None of them belong on a
        # calendar invite, and labelling them "unresolved" leaks misleading
        # noise to the user ("3 members couldn't be resolved" when in fact
        # everyone real WAS resolved). Classify each miss with a single
        # `users.info` call and only surface ACTUAL humans we couldn't email.
        #
        # Concurrency is bounded — `users.info` is Slack Tier 4 (~100/min) and
        # large channels (#general with N integration bots) can have dozens of
        # cache misses. An unbounded gather would burst N calls at once and
        # trip 429s. A semaphore of 8 keeps wall-clock fast while staying well
        # under Slack's burst threshold.
        classify_sem = asyncio.Semaphore(8)

        async def _classify(sid: str) -> tuple[str, str]:
            async with classify_sem:
                try:
                    info = await client.users_info(user=sid)
                    u = info.get("user", {}) or {}
                    if u.get("is_bot") or sid == "USLACKBOT":
                        return sid, "bot"
                    if u.get("deleted"):
                        return sid, "deleted"
                    return sid, "human"
                except Exception:
                    # Unknown shape — surface as unresolved-human; over-disclosure
                    # is safer than silently hiding a real attendee.
                    return sid, "human"

        if cache_misses:
            classified = await asyncio.gather(*[_classify(sid) for sid in cache_misses])
            unresolved = [sid for sid, kind in classified if kind == "human"]
            excluded_bots = sum(1 for _, kind in classified if kind == "bot")
            excluded_deleted = sum(1 for _, kind in classified if kind == "deleted")
        else:
            unresolved = []
            excluded_bots = 0
            excluded_deleted = 0

        return {
            "members": resolved,
            "count": len(resolved),
            # ONLY real humans we couldn't email-resolve. Bots and deleted
            # users are silently filtered out — they don't belong on calendar
            # invites and their presence in the channel isn't user-actionable.
            "unresolved_count": len(unresolved),
            "unresolved_slack_ids": unresolved,
            # Diagnostic counts (model can ignore unless something's clearly off).
            "excluded_bots": excluded_bots,
            "excluded_deleted": excluded_deleted,
        }


def _slack_user_operand(value: str) -> str:
    """Format a user for a `from:`/`with:` operator. A Slack ID (Uxxx/Wxxx) is
    wrapped as `<@ID>` (most reliable); anything else is treated as a handle and
    gets a leading `@`."""
    v = value.strip()
    if not v:
        return ""
    if (v.startswith("U") or v.startswith("W")) and " " not in v:
        return f"<@{v}>"
    return "@" + v.lstrip("@")


def _build_search_query(params: dict) -> str:
    """Compose the free-text query with any structured operators. Slack's search
    grammar (from:/with:/in:/after:/before:/on:) is folded in so the model can
    pass them as clean params instead of hand-assembling operator syntax."""
    parts: list[str] = []
    base = (params.get("query") or "").strip()
    if base:
        parts.append(base)
    # Strip + validate every structured field before emitting an operator. A
    # whitespace-only value (e.g. from_user="  ") is truthy but must NOT produce a
    # bare "from:" / "in:#" / "after:" — Slack treats a malformed operator as
    # literal text and silently degrades/misroutes the search.
    from_user = str(params.get("from_user") or "").strip()
    if from_user:
        operand = _slack_user_operand(from_user)
        if operand:
            parts.append("from:" + operand)
    with_user = str(params.get("with_user") or "").strip()
    if with_user:
        operand = _slack_user_operand(with_user)
        if operand:
            parts.append("with:" + operand)
    c = str(params.get("in_channel") or "").strip()
    if c:
        if (c.startswith("C") or c.startswith("G") or c.startswith("D")) and " " not in c:
            parts.append(f"in:<#{c}>")
        else:
            channel = c.lstrip("#").strip()
            if channel:
                parts.append("in:#" + channel)
    for op in ("after", "before", "on"):
        val = str(params.get(op) or "").strip()
        if val:
            parts.append(f"{op}:{val}")
    return " ".join(p for p in parts if p).strip()


class SearchSlackMessages(BaseTool):
    @property
    def name(self) -> str:
        return "search_slack_messages"

    @property
    def description(self) -> str:
        return (
            "Search Slack messages across ALL channels/DMs in ONE call. Results are "
            "sorted MOST-RECENT-FIRST by default (sort='timestamp'), so this is the "
            "right tool for 'what did X send me on Friday' / 'recent messages about Y'. "
            "Use the structured params instead of hand-writing operators: `from_user` "
            "(sender), `with_user` (a DM/conversation partner — best for 'messages from "
            "person X to me'), `in_channel`, and `after`/`before`/`on` (dates as "
            "'YYYY-MM-DD', interpreted in the workspace timezone). Pass a Slack user ID "
            "(Uxxx) for from_user/with_user when you have one — it is more reliable than "
            "a display name. Set sort='score' only when you want best-keyword-match "
            "rather than recency. To read a person's DM history directly, prefer "
            "`read_slack_dm`. To read full threads of many matches, batch with "
            "`read_slack_threads`."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Free-text keywords (optional if you pass from_user/with_user/in_channel/after).",
                },
                "from_user": {
                    "type": "string",
                    "description": "Only messages SENT BY this user. Slack user ID (Uxxx, preferred) or @handle.",
                },
                "with_user": {
                    "type": "string",
                    "description": "Only messages in a conversation WITH this user (DMs). Slack user ID (Uxxx, preferred) or @handle.",
                },
                "in_channel": {
                    "type": "string",
                    "description": "Restrict to one channel. Channel ID (Cxxx) or #name.",
                },
                "after": {
                    "type": "string",
                    "description": "Only messages after this date (YYYY-MM-DD), workspace timezone.",
                },
                "before": {
                    "type": "string",
                    "description": "Only messages before this date (YYYY-MM-DD), workspace timezone.",
                },
                "on": {
                    "type": "string",
                    "description": "Only messages on this exact date (YYYY-MM-DD), workspace timezone.",
                },
                "sort": {
                    "type": "string",
                    "enum": ["timestamp", "score"],
                    "description": "'timestamp' (default, most recent first) or 'score' (best keyword match).",
                    "default": "timestamp",
                },
                "count": {
                    "type": "integer",
                    "description": "Number of results (default 10, max 20)",
                    "default": 10,
                },
            },
            "required": [],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = _build_search_query(params)
        if not query:
            return {"error": "Provide a query and/or at least one of from_user / with_user / in_channel / after / before / on."}
        sort = params.get("sort") or "timestamp"
        if sort not in ("timestamp", "score"):
            sort = "timestamp"

        client = _get_slack_client(access_token)
        # sort_dir=desc → most recent first for timestamp; ignored for score.
        # search.messages requires a USER token (search:read); tool_executor
        # already prefers the user token for Slack, falling back to bot.
        result = await client.search_messages(
            query=query,
            count=min(params.get("count", 10), 20),
            sort=sort,
            sort_dir="desc",
        )
        matches = result.get("messages", {}).get("matches", [])
        messages = [
            {
                "text": m.get("text", ""),
                "user": m.get("username", ""),
                "channel": m.get("channel", {}).get("name", ""),
                "channel_id": m.get("channel", {}).get("id", ""),
                "ts": m.get("ts", ""),
                "permalink": m.get("permalink", ""),
            }
            for m in matches
        ]
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            id_to_name = await build_decode_map(session)
        # Pure regex work after closing the session.
        for msg in messages:
            msg["text"] = decode_with_map(msg["text"], id_to_name)
        return {"messages": messages, "count": len(messages), "query": query, "sort": sort}


class SendSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "send_slack_message"

    @property
    def description(self) -> str:
        return (
            "Send a message to a Slack channel or DM a user. Accepts a "
            "channel ID (Cxxx public, Gxxx private, Dxxx existing DM) OR a "
            "user ID (Uxxx, Wxxx) — user IDs auto-resolve to a DM channel "
            "via conversations.open. Pass the user ID directly when DMing "
            "someone; do NOT reuse a stale Dxxx channel from earlier context."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_or_user": {
                    "type": "string",
                    "description": (
                        "Channel ID (Cxxx/Gxxx/Dxxx) or user ID (Uxxx/Wxxx). "
                        "User IDs auto-open a DM. Always prefer a user ID "
                        "over a remembered Dxxx when DMing a specific person."
                    ),
                },
                "text": {
                    "type": "string",
                    "description": "Message text to send",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Thread timestamp to reply in a thread. If omitted, sends to the channel.",
                },
            },
            "required": ["channel_or_user", "text"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Accept legacy `channel_id` for any in-flight tool call composed
        # against the older schema; new turns will use `channel_or_user`.
        target = params.get("channel_or_user") or params.get("channel_id")
        if not target:
            return {"error": "channel_or_user is required."}

        # Encode outgoing @name references to <@Uxxx> so Slack renders them as
        # real user tags. Done before opening the Slack client so no DB session
        # is held across the external call.
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            encoded_text = await encode_slack_mentions(params["text"], session)
        encoded_text = markdown_to_slack_mrkdwn(encoded_text)

        client = _get_slack_client(access_token)

        # Auto-resolve user IDs to DM channels (mirrors ScheduleSlackMessage).
        # The previous behavior forced the model to convert Uxxx → Dxxx itself,
        # which led to stale-channel reuse from prior activity context (caused
        # the May 2026 "DM went to Devesh instead of the requested user" bug).
        if target.startswith("U") or target.startswith("W"):
            try:
                dm = await client.conversations_open(users=target)
                target = dm["channel"]["id"]
            except Exception as e:
                return {"error": f"Could not open DM with user {target}: {e}"}

        kwargs = {"channel": target, "text": encoded_text}
        if params.get("thread_ts"):
            kwargs["thread_ts"] = params["thread_ts"]
        result = await client.chat_postMessage(**kwargs)
        return {
            "ok": result.get("ok", False),
            "ts": result.get("ts", ""),
            "channel": result.get("channel", ""),
        }


class ReadSlackThread(BaseTool):
    @property
    def name(self) -> str:
        return "read_slack_thread"

    @property
    def description(self) -> str:
        return (
            "Read all messages in a Slack thread. Provide the channel ID and thread "
            "timestamp. App/alert messages often have an empty `text` field — their real "
            "data lives in `parsed_blocks` (Block Kit) and `parsed_attachments` (legacy) "
            "on each message. Always check those for structured content."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Slack channel ID containing the thread",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Thread timestamp (ts of the parent message)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages to fetch (default 30, max 100)",
                    "default": 30,
                },
            },
            "required": ["channel_id", "thread_ts"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        result = await client.conversations_replies(
            channel=params["channel_id"],
            ts=params["thread_ts"],
            limit=min(params.get("limit", 30), 100),
        )
        raw_messages = result.get("messages", [])
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            id_to_name = await build_decode_map(session)
        # Pure parsing after closing the session (no IO).
        messages = _parse_thread_messages(raw_messages, id_to_name)
        return {"messages": messages, "count": len(messages)}


def _parse_thread_messages(raw_messages: list, id_to_name: dict[str, str]) -> list[dict]:
    """Parse Slack thread/conversation replies into the compact per-message shape
    shared by read_slack_thread and read_slack_threads: {user, text, ts} plus
    parsed_blocks / parsed_attachments when present. Single source of truth so the
    two tools' output format can't drift."""
    out: list[dict] = []
    for m in raw_messages or []:
        entry: dict[str, Any] = {
            "user": m.get("user", ""),
            "text": decode_with_map(m.get("text", ""), id_to_name),
            "ts": m.get("ts", ""),
        }
        if m.get("blocks"):
            entry["parsed_blocks"] = parse_slack_blocks(m["blocks"], id_to_name)
        if m.get("attachments"):
            entry["parsed_attachments"] = parse_slack_attachments(m["attachments"], id_to_name)
        out.append(entry)
    return out


# Max threads a single read_slack_threads call will fetch. Guards against a
# runaway request; the tool tells the model to call again for the remainder.
_READ_THREADS_MAX = 50


async def _read_one_thread_compact(
    client: AsyncWebClient,
    channel_id: str,
    thread_ts: str,
    limit: int,
    id_to_name: dict[str, str],
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    """Fetch + parse ONE thread for read_slack_threads (module-level so it's not a
    nested function). Bounded by `sem`. Returns the same per-message shape as
    read_slack_thread, or `{thread_ts, error}` if that single thread fails — one
    bad thread never fails the whole batch."""
    async with sem:
        try:
            result = await client.conversations_replies(
                channel=channel_id, ts=thread_ts, limit=limit
            )
        except Exception as e:
            return {"thread_ts": thread_ts, "error": str(e)[:200]}
    messages = _parse_thread_messages(result.get("messages", []), id_to_name)
    return {"thread_ts": thread_ts, "count": len(messages), "messages": messages}


class ReadSlackThreads(BaseTool):
    @property
    def name(self) -> str:
        return "read_slack_threads"

    @property
    def description(self) -> str:
        return (
            "Read MANY Slack threads in ONE call (server-side, concurrent). Use this "
            "INSTEAD of calling read_slack_thread once per thread whenever you need to "
            "process multiple threads (e.g. one per matched message) — it collapses N "
            "lookups into a single tool call, so a large batch is one step instead of "
            "N. Pass `channel_id` and `thread_tss` (a list of each thread's parent-"
            "message `ts`). Returns one entry per thread — {thread_ts, count, messages} "
            "with the same per-message shape as read_slack_thread (user, text, ts, "
            "parsed_blocks, parsed_attachments); a thread that fails comes back as "
            "{thread_ts, error} without failing the batch. Max 50 threads per call — "
            "if you pass more, only the first 50 are read and `truncated` is set."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "The channel the threads live in.",
                },
                "thread_tss": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Thread parent `ts` values to read (max 50 per call).",
                },
                "limit_per_thread": {
                    "type": "integer",
                    "description": "Max replies fetched per thread (default 30, cap 100).",
                    "default": 30,
                },
            },
            "required": ["channel_id", "thread_tss"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        channel_id = params["channel_id"]
        # Type-guard thread_tss: a bare string would otherwise iterate PER CHARACTER
        # (fanning out one bad Slack call per char). Coerce a lone ts string to a
        # 1-element list; reject any other non-list shape; drop empties.
        raw_tss = params.get("thread_tss")
        if isinstance(raw_tss, str):
            thread_tss = [raw_tss] if raw_tss.strip() else []
        elif isinstance(raw_tss, list):
            thread_tss = [str(t).strip() for t in raw_tss if t and str(t).strip()]
        elif raw_tss is None:
            thread_tss = []
        else:
            return {
                "error": "thread_tss must be an array of thread `ts` strings.",
                "threads": [], "count": 0, "requested": 0,
            }
        requested = len(thread_tss)
        thread_tss = thread_tss[:_READ_THREADS_MAX]
        try:
            per = int(params.get("limit_per_thread", 30))
        except (TypeError, ValueError):
            per = 30
        per = max(1, min(per, 100))

        # One DB read builds the id→name decode map shared across every thread.
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            id_to_name = await build_decode_map(session)

        sem = asyncio.Semaphore(8)
        threads = await asyncio.gather(
            *[
                _read_one_thread_compact(client, channel_id, ts, per, id_to_name, sem)
                for ts in thread_tss
            ]
        ) if thread_tss else []

        out: dict[str, Any] = {"threads": list(threads), "count": len(threads), "requested": requested}
        if requested > _READ_THREADS_MAX:
            out["truncated"] = True
            out["note"] = (
                f"Only the first {_READ_THREADS_MAX} of {requested} threads were read. "
                "Call read_slack_threads again with the remaining thread_tss."
            )
        return out


def _resolve_dm_user(user: Any, members: list) -> tuple[Optional[str], Optional[str]]:
    """Resolve a `read_slack_dm` `user` arg to a Slack user ID. Accepts a Slack
    ID (Uxxx/Wxxx, returned as-is) or a name matched (exact, then substring)
    against the workspace member cache. Returns (user_id, error_message)."""
    u = (str(user) if user is not None else "").strip()
    if not u:
        return None, "user is required (a Slack user ID like U0123 or a person's name)."
    if (u.startswith("U") or u.startswith("W")) and " " not in u:
        return u, None
    low = u.lstrip("@").lower()
    exact = [m for m in members if m.get("slack_id") and (m.get("name") or "").lower() == low]
    matches = exact or [
        m for m in members if m.get("slack_id") and low in (m.get("name") or "").lower()
    ]
    ids = {m["slack_id"] for m in matches}
    if len(ids) == 1:
        return matches[0]["slack_id"], None
    if not ids:
        return None, f"Couldn't find a Slack user matching {user!r}. Pass their Slack user ID (Uxxx)."
    names = ", ".join(sorted({m.get("name", "") for m in matches})[:6])
    return None, f"{user!r} is ambiguous ({names}). Pass the specific Slack user ID (Uxxx)."


class ReadSlackDM(BaseTool):
    @property
    def name(self) -> str:
        return "read_slack_dm"

    @property
    def description(self) -> str:
        return (
            "Read your direct-message (1:1) history with ONE person in a single call — "
            "the right tool for 'what did X send me' / 'find the link X sent me on "
            "Friday'. Pass `user` as a Slack user ID (Uxxx, preferred) or a name (resolved "
            "against the workspace directory). Optionally scope with `oldest`/`latest` "
            "(epoch seconds, a date '2026-07-10', or a word 'today'/'yesterday'/'friday' — "
            "resolved in the user's timezone); a window paginates the whole range, newest "
            "first otherwise. Returns messages with `user_name`, `text`, `ts`, and "
            "`parsed_blocks`/`parsed_attachments` for app content. This opens the DM for "
            "you — do NOT hunt for the Dxxx channel via search first."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "user": {
                    "type": "string",
                    "description": "Slack user ID (Uxxx/Wxxx, preferred) or the person's name.",
                },
                "oldest": {
                    "type": ["number", "string"],
                    "description": "Lower bound: epoch seconds, a date '2026-07-10', or 'today'/'yesterday'/'friday' (00:00, user tz).",
                },
                "latest": {
                    "type": ["number", "string"],
                    "description": "Upper bound: epoch seconds, a date '2026-07-10', or 'today'/'yesterday'/'friday' (23:59, user tz).",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max messages. Default 15 (cap 50) for a plain recent read; a window paginates up to this (default 1000, cap 1000).",
                },
            },
            "required": ["user"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            oldest = _resolve_time_bound(params.get("oldest"), is_latest=False)
            latest = _resolve_time_bound(params.get("latest"), is_latest=True)
        except ValueError as e:
            return {"error": str(e)}

        # Only load the (heavier) member directory when we must resolve a name;
        # a Slack ID needs no lookup. Both DB reads happen before any Slack call
        # so no session is held across the network round-trip.
        u = (str(params.get("user")) if params.get("user") is not None else "").strip()
        need_members = not ((u.startswith("U") or u.startswith("W")) and " " not in u)
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            members = await get_all_members(session) if need_members else []
            id_to_name = await build_decode_map(session)

        user_id, err = _resolve_dm_user(params.get("user"), members)
        if err:
            return {"error": err}

        client = _get_slack_client(access_token)
        try:
            dm = await client.conversations_open(users=user_id)
            channel_id = dm["channel"]["id"]
        except Exception as e:
            return {"error": f"Could not open a DM with {user_id}: {e}"}

        raw_messages, windowed_truncated = await _fetch_history(
            client, channel_id, oldest, latest, params.get("limit")
        )
        messages = _format_channel_messages(raw_messages, id_to_name)
        out: dict[str, Any] = {
            "messages": messages,
            "count": len(messages),
            "user_id": user_id,
            "channel_id": channel_id,
        }
        if (oldest is not None or latest is not None) and windowed_truncated:
            out["truncated"] = True
            out["note"] = "Hit the message cap for this window — older messages may be missing. Narrow the window or raise limit."
        return out


class ScheduleSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "schedule_slack_message"

    @property
    def description(self) -> str:
        return (
            "Schedule a Slack message to be sent at a specific future time. "
            "Accepts a channel ID (public channel, private channel, or DM channel like D01...) "
            "or a user ID starting with 'U' — in which case a DM channel is opened first. "
            "`post_at` must be ISO 8601 with timezone OR a unix timestamp. Max 120 days in the future. "
            "Returns the scheduled_message_id which can be used to cancel before delivery. "
            "IMPORTANT: Slack cancel is only reliable when the message is more than ~10 minutes "
            "from its send time. Messages scheduled < 10 min out are committed to Slack's "
            "delivery pipeline immediately and cannot be canceled — tell the user this up front "
            "if they ask for a short-horizon schedule-with-possible-cancel flow."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_or_user": {
                    "type": "string",
                    "description": "Channel ID (C../G../D..) or user ID (U..). User IDs auto-resolve to a DM channel.",
                },
                "text": {
                    "type": "string",
                    "description": "Message text (Slack mrkdwn formatting supported).",
                },
                "post_at": {
                    "type": "string",
                    "description": "When to send — ISO 8601 with timezone (e.g. '2026-04-19T18:00:00+05:30') OR unix timestamp string.",
                },
                "thread_ts": {
                    "type": "string",
                    "description": "Optional thread ts if you want the scheduled message to land in a thread.",
                },
            },
            "required": ["channel_or_user", "text", "post_at"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        import datetime as _dt

        client = _get_slack_client(access_token)
        target = params["channel_or_user"]

        # Resolve user ID → DM channel.
        if target.startswith("U") or target.startswith("W"):
            try:
                dm = await client.conversations_open(users=target)
                target = dm["channel"]["id"]
            except Exception as e:
                return {"error": f"Could not open DM with user {target}: {e}"}

        # Parse post_at as ISO datetime first, fall back to unix timestamp.
        post_at_raw = str(params["post_at"])
        post_at_unix: int
        try:
            if post_at_raw.isdigit():
                post_at_unix = int(post_at_raw)
            else:
                dt = _dt.datetime.fromisoformat(post_at_raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return {
                        "error": "post_at ISO datetime must include a timezone offset."
                    }
                post_at_unix = int(dt.timestamp())
        except (ValueError, TypeError):
            return {
                "error": f"Invalid post_at: {post_at_raw}. Use ISO 8601 or unix timestamp."
            }

        now = int(_dt.datetime.now(_dt.timezone.utc).timestamp())
        if post_at_unix <= now:
            return {"error": "post_at must be in the future."}
        if post_at_unix > now + 120 * 86400:
            return {
                "error": "post_at cannot be more than 120 days in the future (Slack limit)."
            }

        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            encoded_text = await encode_slack_mentions(params["text"], session)
        encoded_text = markdown_to_slack_mrkdwn(encoded_text)

        kwargs: dict[str, Any] = {
            "channel": target,
            "text": encoded_text,
            "post_at": post_at_unix,
        }
        if params.get("thread_ts"):
            kwargs["thread_ts"] = params["thread_ts"]

        try:
            result = await client.chat_scheduleMessage(**kwargs)
        except Exception as e:
            return {"error": f"Schedule failed: {e}"}

        return {
            "status": "scheduled",
            "channel_id": result.get("channel", target),
            "scheduled_message_id": result.get("scheduled_message_id", ""),
            "post_at_unix": post_at_unix,
            "post_at_iso": _dt.datetime.fromtimestamp(
                post_at_unix, _dt.timezone.utc
            ).isoformat(),
        }


class CancelScheduledSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "cancel_scheduled_slack_message"

    @property
    def description(self) -> str:
        return (
            "Cancel a Slack message previously scheduled via schedule_slack_message. "
            "Requires the channel_id and scheduled_message_id returned by that call. "
            "IMPORTANT: Slack's cancel is only reliable when the message is > ~10 minutes "
            "from its send time. Closer to delivery, Slack commits the message to its send "
            "pipeline and the cancel appears to succeed but the message still delivers. "
            "Tool returns status 'canceled' (verified gone from queue), 'cancel_race' (queue "
            "lied, will likely deliver), or 'cancel_too_late' (Slack refused the delete)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Channel the message was scheduled to.",
                },
                "scheduled_message_id": {
                    "type": "string",
                    "description": "ID returned from schedule_slack_message.",
                },
            },
            "required": ["channel_id", "scheduled_message_id"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        sid = params["scheduled_message_id"]
        channel = params["channel_id"]

        try:
            await client.chat_deleteScheduledMessage(
                channel=channel,
                scheduled_message_id=sid,
            )
        except Exception as e:
            msg = str(e)
            # Slack uses `invalid_scheduled_message_id` when the message has
            # already passed its cancel window (can't delete within ~60s of
            # delivery). Surface that distinctly so the caller knows WHY.
            if "invalid_scheduled_message_id" in msg:
                return {
                    "status": "cancel_too_late",
                    "scheduled_message_id": sid,
                    "error": "Scheduled message is too close to its send time to cancel (Slack requires >60s lead). It will deliver.",
                }
            return {"error": f"Cancel failed: {e}"}

        # Verify it's actually gone — Slack occasionally returns ok:true but
        # leaves the message in the queue due to a race near the send time.
        # Paginate because a user with >100 scheduled messages in the channel
        # could have our target on a later page; single-page check would
        # falsely report "canceled".
        try:
            cursor: str | None = None
            for _ in range(20):  # cap iterations against pathological paging
                lst = await client.chat_scheduledMessages_list(
                    channel=channel,
                    limit=100,
                    cursor=cursor or None,
                )
                if any(m.get("id") == sid for m in lst.get("scheduled_messages", [])):
                    return {
                        "status": "cancel_race",
                        "scheduled_message_id": sid,
                        "error": "Slack accepted the delete but the message is still queued. It will likely deliver.",
                    }
                cursor = (lst.get("response_metadata") or {}).get("next_cursor")
                if not cursor:
                    break
        except Exception:
            # List call is best-effort verification — don't fail the cancel
            # just because we couldn't confirm.
            pass

        return {"status": "canceled", "scheduled_message_id": sid}


# ---------------------------------------------------------------------------
# Edit / delete / react / pin / upload — round out the Slack action surface.
# ---------------------------------------------------------------------------

class UpdateSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "update_slack_message"

    @property
    def description(self) -> str:
        return (
            "Edit a Slack message that you (this bot) sent. Pass channel_id "
            "and ts (the message timestamp from the original send_slack_message "
            "result). New text replaces the old. Slack only allows editing your "
            "own messages."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string", "description": "Message timestamp returned by send_slack_message."},
                "text": {"type": "string", "description": "New body. Markdown converted to Slack mrkdwn."},
            },
            "required": ["channel_id", "ts", "text"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            encoded = await encode_slack_mentions(params["text"], session)
        encoded = markdown_to_slack_mrkdwn(encoded)
        client = _get_slack_client(access_token)
        try:
            result = await client.chat_update(
                channel=params["channel_id"], ts=params["ts"], text=encoded,
            )
        except Exception as e:
            return {"error": f"Update message failed: {e}"}
        return {"ok": result.get("ok", False), "ts": result.get("ts", ""), "channel": result.get("channel", "")}


class DeleteSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "delete_slack_message"

    @property
    def description(self) -> str:
        return (
            "Delete a Slack message that you (this bot) sent. Same caveat as "
            "update — Slack only allows deleting your own messages."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel_id", "ts"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        try:
            result = await client.chat_delete(channel=params["channel_id"], ts=params["ts"])
        except Exception as e:
            return {"error": f"Delete message failed: {e}"}
        return {"ok": result.get("ok", False), "ts": params["ts"], "channel": params["channel_id"]}


class AddSlackReaction(BaseTool):
    @property
    def name(self) -> str:
        return "add_slack_reaction"

    @property
    def description(self) -> str:
        return (
            "Add an emoji reaction to a Slack message. `name` is the emoji "
            "shortcode WITHOUT colons (e.g. 'thumbsup', 'eyes', 'check'). "
            "Idempotent — adding the same reaction twice is a no-op."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string"},
                "name": {"type": "string", "description": "Emoji shortcode without colons (e.g. 'thumbsup')."},
            },
            "required": ["channel_id", "ts", "name"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        emoji = (params["name"] or "").strip(":")
        try:
            result = await client.reactions_add(
                channel=params["channel_id"], timestamp=params["ts"], name=emoji,
            )
        except Exception as e:
            err = str(e)
            if "already_reacted" in err:
                return {"ok": True, "note": "Reaction already present (no-op)."}
            return {"error": f"Add reaction failed: {err}"}
        return {"ok": result.get("ok", False), "name": emoji}


class RemoveSlackReaction(BaseTool):
    @property
    def name(self) -> str:
        return "remove_slack_reaction"

    @property
    def description(self) -> str:
        return "Remove an emoji reaction from a Slack message."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string"},
                "name": {"type": "string", "description": "Emoji shortcode without colons."},
            },
            "required": ["channel_id", "ts", "name"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        emoji = (params["name"] or "").strip(":")
        try:
            result = await client.reactions_remove(
                channel=params["channel_id"], timestamp=params["ts"], name=emoji,
            )
        except Exception as e:
            return {"error": f"Remove reaction failed: {e}"}
        return {"ok": result.get("ok", False), "name": emoji}


class PinSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "pin_slack_message"

    @property
    def description(self) -> str:
        return "Pin a Slack message to its channel. Pinning a message that's already pinned is a no-op."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel_id", "ts"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        try:
            result = await client.pins_add(channel=params["channel_id"], timestamp=params["ts"])
        except Exception as e:
            err = str(e)
            if "already_pinned" in err:
                return {"ok": True, "note": "Already pinned."}
            return {"error": f"Pin failed: {err}"}
        return {"ok": result.get("ok", False)}


class UnpinSlackMessage(BaseTool):
    @property
    def name(self) -> str:
        return "unpin_slack_message"

    @property
    def description(self) -> str:
        return "Unpin a previously-pinned Slack message."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_id": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel_id", "ts"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        client = _get_slack_client(access_token)
        try:
            result = await client.pins_remove(channel=params["channel_id"], timestamp=params["ts"])
        except Exception as e:
            return {"error": f"Unpin failed: {e}"}
        return {"ok": result.get("ok", False)}


class UploadSlackFile(BaseTool):
    @property
    def name(self) -> str:
        return "upload_slack_file"

    @property
    def description(self) -> str:
        return (
            "Upload a small text file (or text content) into a Slack channel, "
            "DM, or thread. Accepts a channel ID (Cxxx/Gxxx/Dxxx) OR a user "
            "ID (Uxxx/Wxxx) — user IDs auto-open a DM. Content is sent as a "
            "snippet, best for CSVs, log excerpts, or short text artifacts "
            "up to ~200KB. Larger or binary uploads aren't supported here."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "channel_or_user": {
                    "type": "string",
                    "description": "Channel ID (Cxxx/Gxxx/Dxxx) or user ID (Uxxx/Wxxx). User IDs auto-open a DM.",
                },
                "thread_ts": {"type": "string", "description": "Optional — post as a reply in this thread."},
                "filename": {"type": "string", "description": "Display filename (e.g. 'metrics.csv')."},
                "content": {"type": "string", "description": "Text content to upload."},
                "title": {"type": "string", "description": "Optional title shown in the Slack file card."},
                "initial_comment": {"type": "string", "description": "Optional message posted alongside the file."},
            },
            "required": ["channel_or_user", "filename", "content"],
        }

    @property
    def provider(self) -> str:
        return "slack"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        content = params["content"] or ""
        if len(content) > 200_000:
            return {"error": f"Content too large ({len(content)} chars). Max 200K."}
        # Accept legacy `channel_id` for any in-flight tool call composed
        # against the older schema; new turns will use `channel_or_user`.
        target = params.get("channel_or_user") or params.get("channel_id")
        if not target:
            return {"error": "channel_or_user is required."}
        client = _get_slack_client(access_token)
        if target.startswith("U") or target.startswith("W"):
            try:
                dm = await client.conversations_open(users=target)
                target = dm["channel"]["id"]
            except Exception as e:
                return {"error": f"Could not open DM with user {target}: {e}"}
        kwargs: dict[str, Any] = {
            "channel": target,
            "filename": params["filename"],
            "content": content,
        }
        if params.get("title"):
            kwargs["title"] = params["title"]
        if params.get("thread_ts"):
            kwargs["thread_ts"] = params["thread_ts"]
        if params.get("initial_comment"):
            kwargs["initial_comment"] = params["initial_comment"]
        try:
            # files_upload_v2 is the modern endpoint; v1 is deprecated.
            result = await client.files_upload_v2(**kwargs)
        except Exception as e:
            return {"error": f"Upload failed: {e}"}
        files = result.get("files") or [result.get("file")] if result.get("file") else result.get("files") or []
        first = files[0] if files else {}
        return {
            "ok": result.get("ok", False),
            "file_id": first.get("id") if isinstance(first, dict) else None,
            "permalink": first.get("permalink") if isinstance(first, dict) else None,
        }
