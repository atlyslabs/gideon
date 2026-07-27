"""
Calendar Tools — list, read, and create events via Google Calendar API.
"""

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from typing import Any, Optional

import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from tools.base import BaseTool

logger = structlog.get_logger()


# Turn-scoped cache for recurring-event master lookups. If the chat loop sets
# this at the start of a turn, multiple `list_events` / `get_event` calls in
# the same turn share one fetch per master event ID. Outside a turn (e.g.
# workflow execution) the var is None and we fall back to per-call dedup,
# which `_hydrate_recurring_attendees_sync` already does internally.
RECURRING_MASTER_CACHE: ContextVar[Optional[dict[str, dict]]] = ContextVar(
    "calendar_recurring_master_cache", default=None
)


def _get_calendar_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _parse_event(event: dict) -> dict:
    start = event.get("start", {})
    end = event.get("end", {})
    return {
        "id": event["id"],
        "summary": event.get("summary", "(No title)"),
        "description": event.get("description", ""),
        "location": event.get("location", ""),
        "start": start.get("dateTime") or start.get("date", ""),
        "end": end.get("dateTime") or end.get("date", ""),
        "attendees": [
            {"email": a.get("email"), "name": a.get("displayName"), "status": a.get("responseStatus")}
            for a in event.get("attendees", [])
        ],
        "organizer": event.get("organizer", {}).get("email", ""),
        "html_link": event.get("htmlLink", ""),
        "status": event.get("status", ""),
    }


class ListTodayEvents(BaseTool):
    @property
    def name(self) -> str:
        return "list_today_events"

    @property
    def description(self) -> str:
        return "List all calendar events for today. Shows title, time, attendees, and location."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        end_of_day = start_of_day + timedelta(days=1)

        def _fetch_sync() -> list[dict]:
            service = _get_calendar_service(access_token)
            results = service.events().list(
                calendarId="primary",
                timeMin=start_of_day.isoformat(),
                timeMax=end_of_day.isoformat(),
                singleEvents=True,
                orderBy="startTime",
                maxResults=50,
            ).execute()
            items = results.get("items", [])
            _hydrate_recurring_attendees_sync(service, items)
            return items

        raw = await asyncio.to_thread(_fetch_sync)
        events = [_parse_event(e) for e in raw]
        return {"events": events, "count": len(events), "date": now.strftime("%Y-%m-%d")}


_LIST_HARD_CAP = 500
_LIST_PAGE_SIZE = 250  # Google Calendar max per call


def _hydrate_recurring_attendees_sync(service, raw_events: list[dict]) -> None:
    """In-place: for instances of recurring events that came back without
    attendees, fetch the master once and copy its attendees onto every
    instance. Google sometimes returns instances with empty `attendees` even
    when the master has them — this is the gotcha that caused the calendar
    transcript to lose Rahul Chugh on Office/Keka/3:01 events.

    Uses the turn-scoped `RECURRING_MASTER_CACHE` if the caller set it, so
    a `list_events` followed by a `get_event` in the same chat turn shares
    one master fetch per recurring ID. Outside a turn the cache is None and
    we still dedupe within a single call.
    """
    turn_cache = RECURRING_MASTER_CACHE.get()
    masters_to_fetch: dict[str, list[dict]] = {}
    for event in raw_events:
        recurring_id = event.get("recurringEventId")
        if not recurring_id:
            continue
        if event.get("attendees"):
            continue
        masters_to_fetch.setdefault(recurring_id, []).append(event)

    for master_id, instances in masters_to_fetch.items():
        master: Optional[dict] = None
        if turn_cache is not None and master_id in turn_cache:
            master = turn_cache[master_id]
        else:
            try:
                master = service.events().get(
                    calendarId="primary", eventId=master_id,
                ).execute()
            except Exception as e:
                # Hydration is best-effort — a 404 (master deleted) or 410
                # (gone) is normal and just means we can't fill in attendees
                # for these instances. But 401/429/5xx are operational
                # failures we want to see in Loki, otherwise "why are
                # recurring attendees missing" becomes impossible to debug.
                status = None
                resp = getattr(e, "resp", None)
                if resp is not None:
                    status = getattr(resp, "status", None)
                logger.warning(
                    "calendar recurring-master fetch failed",
                    master_id=master_id,
                    instance_count=len(instances),
                    status=status,
                    error_type=type(e).__name__,
                    error=str(e)[:200],
                )
                master = None
            if turn_cache is not None:
                # Cache the lookup result (including failure-as-empty-dict)
                # so the same master ID isn't re-fetched in this turn.
                turn_cache[master_id] = master if master is not None else {}
        if not master:
            continue
        master_attendees = master.get("attendees")
        if not master_attendees:
            continue
        for instance in instances:
            instance["attendees"] = master_attendees


class ListEvents(BaseTool):
    @property
    def name(self) -> str:
        return "list_events"

    @property
    def description(self) -> str:
        return (
            "List calendar events within a date range. Paginates through every event in the window "
            f"up to {_LIST_HARD_CAP} (no per-call truncation), and auto-hydrates attendees on "
            "recurring-event instances by fetching the master event when needed. "
            "Optional `attendee_email` and `organizer_email` filters apply server-side after "
            "hydration — prefer these over fetching everything and filtering in prompt. The "
            "response includes a `coverage` block with `scanned_before_filter` (raw fetched), "
            "`returned` (after filters), and `hit_cap` (true if the cap was reached). When "
            "`hit_cap` is true, narrow the window and call again rather than claiming a complete list."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "date_from": {
                    "type": "string",
                    "description": "Start date in YYYY-MM-DD format",
                },
                "date_to": {
                    "type": "string",
                    "description": "End date in YYYY-MM-DD format",
                },
                "attendee_email": {
                    "type": "string",
                    "description": "Optional. Only return events where this email is in the attendee list (case-insensitive, exact match). Use this for queries like 'meetings with rahul.chugh@example.com'.",
                },
                "organizer_email": {
                    "type": "string",
                    "description": "Optional. Only return events organized by this email (case-insensitive, exact match). Use this for queries like 'meetings I organized'.",
                },
            },
            "required": ["date_from", "date_to"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        time_min = f"{params['date_from']}T00:00:00Z"
        time_max = f"{params['date_to']}T23:59:59Z"
        attendee_filter = (params.get("attendee_email") or "").strip().lower() or None
        organizer_filter = (params.get("organizer_email") or "").strip().lower() or None

        def _fetch_all_sync() -> tuple[list[dict], bool]:
            service = _get_calendar_service(access_token)
            collected: list[dict] = []
            page_token: str | None = None
            hit_cap = False
            while True:
                results = service.events().list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=_LIST_PAGE_SIZE,
                    pageToken=page_token,
                ).execute()
                collected.extend(results.get("items", []))
                if len(collected) >= _LIST_HARD_CAP:
                    collected = collected[:_LIST_HARD_CAP]
                    hit_cap = True
                    break
                page_token = results.get("nextPageToken")
                if not page_token:
                    break
            _hydrate_recurring_attendees_sync(service, collected)
            return collected, hit_cap

        raw, hit_cap = await asyncio.to_thread(_fetch_all_sync)
        scanned_before_filter = len(raw)

        # Server-side post-filters — applied after attendee hydration so the
        # filter sees the master's attendees, not the (sometimes empty)
        # instance attendees.
        if attendee_filter:
            raw = [
                e for e in raw
                if any((a.get("email") or "").lower() == attendee_filter for a in e.get("attendees", []))
            ]
        if organizer_filter:
            raw = [
                e for e in raw
                if (e.get("organizer", {}).get("email") or "").lower() == organizer_filter
            ]

        events = [_parse_event(e) for e in raw]
        return {
            "events": events,
            "count": len(events),
            "coverage": {
                "time_min": time_min,
                "time_max": time_max,
                "scanned_before_filter": scanned_before_filter,
                "returned": len(events),
                "hit_cap": hit_cap,
                "filters": {
                    "attendee_email": attendee_filter,
                    "organizer_email": organizer_filter,
                },
            },
        }


class GetEvent(BaseTool):
    @property
    def name(self) -> str:
        return "get_event"

    @property
    def description(self) -> str:
        return "Get details of a specific calendar event by its event ID."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_id": {
                    "type": "string",
                    "description": "The Google Calendar event ID",
                },
            },
            "required": ["event_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _fetch_sync() -> dict:
            service = _get_calendar_service(access_token)
            event = service.events().get(
                calendarId="primary", eventId=params["event_id"],
            ).execute()
            # Same recurring-attendee gotcha as list_events: a single instance
            # can come back with empty attendees even though the master has
            # them. Hydrate before parsing.
            _hydrate_recurring_attendees_sync(service, [event])
            return event

        event = await asyncio.to_thread(_fetch_sync)
        return _parse_event(event)


class CreateEvent(BaseTool):
    @property
    def name(self) -> str:
        return "create_calendar_event"

    @property
    def description(self) -> str:
        return "Create a new Google Calendar event with title, time, attendees, and optional description/location. Sends invites to attendees automatically."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Event title",
                },
                "start_time": {
                    "type": "string",
                    "description": "Start time in ISO 8601 format (e.g. 2026-04-09T16:00:00+05:30)",
                },
                "end_time": {
                    "type": "string",
                    "description": "End time in ISO 8601 format. If not provided, defaults to 30 minutes after start_time.",
                },
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of attendee email addresses",
                },
                "description": {
                    "type": "string",
                    "description": "Event description (optional)",
                },
                "location": {
                    "type": "string",
                    "description": "Event location (optional)",
                },
            },
            "required": ["summary", "start_time"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_calendar_service(access_token)

        start_time = params["start_time"]
        try:
            start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return {"error": f"Invalid start_time format: {start_time}. Use ISO 8601 (e.g. 2026-04-09T16:00:00+05:30)."}

        if start_dt.tzinfo is None:
            return {"error": f"start_time must include a timezone offset (e.g. +05:30 or Z): {start_time}"}

        start_time = start_dt.isoformat()

        if params.get("end_time"):
            try:
                end_dt = datetime.fromisoformat(params["end_time"].replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return {"error": f"Invalid end_time format: {params['end_time']}. Use ISO 8601."}
            if end_dt.tzinfo is None:
                return {"error": f"end_time must include a timezone offset: {params['end_time']}"}
            end_time = end_dt.isoformat()
        else:
            end_dt = start_dt + timedelta(minutes=30)
            end_time = end_dt.isoformat()

        event_body: dict[str, Any] = {
            "summary": params["summary"],
            "start": {"dateTime": start_time},
            "end": {"dateTime": end_time},
        }

        if params.get("attendees"):
            event_body["attendees"] = [{"email": e} for e in params["attendees"]]

        if params.get("description"):
            event_body["description"] = params["description"]

        if params.get("location"):
            event_body["location"] = params["location"]

        def _create_sync():
            return service.events().insert(
                calendarId="primary",
                body=event_body,
                sendUpdates="all",
            ).execute()

        created = await asyncio.to_thread(_create_sync)

        return {
            "status": "created",
            "event": _parse_event(created),
            "html_link": created.get("htmlLink", ""),
        }


def _find_open_slots(
    busy_by_email: dict[str, list[dict]],
    time_min: datetime,
    time_max: datetime,
    duration_minutes: int,
) -> list[dict]:
    """Merge every attendee's busy blocks into a single conflict timeline,
    then walk forward in `duration_minutes` steps and return slots where
    everyone is free. Return up to 10 slots.
    """
    conflicts: list[tuple[datetime, datetime]] = []
    for blocks in busy_by_email.values():
        for b in blocks:
            try:
                s = datetime.fromisoformat(b["start"].replace("Z", "+00:00"))
                e = datetime.fromisoformat(b["end"].replace("Z", "+00:00"))
                conflicts.append((s, e))
            except (KeyError, ValueError):
                continue
    conflicts.sort()

    # Merge overlapping intervals.
    merged: list[tuple[datetime, datetime]] = []
    for s, e in conflicts:
        if merged and s <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))

    duration = timedelta(minutes=duration_minutes)
    slots: list[dict] = []
    cursor = time_min
    for s, e in merged:
        # Walk through the free gap in duration-sized steps so a 4-hour gap
        # with 30-min duration yields 8 candidate slots, not 1.
        while cursor + duration <= s and len(slots) < 10:
            slots.append({"start": cursor.isoformat(), "end": (cursor + duration).isoformat()})
            cursor = cursor + duration
        if len(slots) >= 10:
            return slots
        cursor = max(cursor, e)
    while cursor + duration <= time_max and len(slots) < 10:
        slots.append({"start": cursor.isoformat(), "end": (cursor + duration).isoformat()})
        cursor = cursor + duration
    return slots


class FindFreeTime(BaseTool):
    @property
    def name(self) -> str:
        return "find_free_time"

    @property
    def description(self) -> str:
        return (
            "Check when one or more colleagues are busy and find meeting slots that work for everyone. "
            "Uses Google Calendar's free/busy API — returns only busy time ranges (no event titles, no details), "
            "same as Google Calendar's 'Meet with' / 'Find a time' feature. "
            "Works for any workspace email (free/busy is shared org-wide by default) and any external "
            "email that has made free/busy public. "
            "If `duration_minutes` is provided, also returns up to 10 open slots when all attendees are free. "
            "Call this BEFORE create_calendar_event to suggest times, instead of guessing."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Email addresses to check. Include the requesting user's own email if you want it considered.",
                },
                "time_min": {
                    "type": "string",
                    "description": "Start of the search window, ISO 8601 (e.g. '2026-04-21T09:00:00+05:30').",
                },
                "time_max": {
                    "type": "string",
                    "description": "End of the search window, ISO 8601.",
                },
                "duration_minutes": {
                    "type": "integer",
                    "description": "Optional. If provided, return up to 10 open slots of this length that work for all attendees.",
                },
            },
            "required": ["emails", "time_min", "time_max"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_calendar_service(access_token)

        emails = [e.strip() for e in params["emails"] if e and e.strip()]
        if not emails:
            return {"error": "No emails provided."}
        time_min_iso = params["time_min"]
        time_max_iso = params["time_max"]

        body = {
            "timeMin": time_min_iso,
            "timeMax": time_max_iso,
            "items": [{"id": e} for e in emails],
        }

        def _query_sync() -> dict:
            return service.freebusy().query(body=body).execute()

        try:
            result = await asyncio.to_thread(_query_sync)
        except Exception as e:
            return {"error": f"Free/busy query failed: {e}"}

        calendars = result.get("calendars", {})
        busy_by_email: dict[str, list[dict]] = {}
        errors_by_email: dict[str, str] = {}
        for email in emails:
            entry = calendars.get(email, {})
            if entry.get("errors"):
                errors_by_email[email] = entry["errors"][0].get("reason", "unknown")
                busy_by_email[email] = []
            else:
                busy_by_email[email] = entry.get("busy", [])

        response: dict[str, Any] = {
            "time_min": time_min_iso,
            "time_max": time_max_iso,
            "busy_by_email": busy_by_email,
        }
        if errors_by_email:
            response["errors_by_email"] = errors_by_email

        duration = params.get("duration_minutes")
        if duration and isinstance(duration, int) and duration > 0:
            try:
                t_min = datetime.fromisoformat(time_min_iso.replace("Z", "+00:00"))
                t_max = datetime.fromisoformat(time_max_iso.replace("Z", "+00:00"))
                response["open_slots"] = _find_open_slots(busy_by_email, t_min, t_max, duration)
            except ValueError:
                response["open_slots_error"] = "Could not parse time_min/time_max as ISO datetimes."

        return response


class UpdateEvent(BaseTool):
    @property
    def name(self) -> str:
        return "update_calendar_event"

    @property
    def description(self) -> str:
        return (
            "Update an existing Google Calendar event. Can change title, time, attendees, "
            "description, and location. Only the fields you pass are changed — omitted fields "
            "are left as-is. Sends update notifications to attendees automatically."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID."},
                "summary": {"type": "string", "description": "New title (optional)."},
                "start_time": {"type": "string", "description": "New start time, ISO 8601 with timezone."},
                "end_time": {"type": "string", "description": "New end time, ISO 8601 with timezone."},
                "attendees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "New full attendee list (replaces existing). Omit to leave unchanged.",
                },
                "description": {"type": "string", "description": "New description (optional)."},
                "location": {"type": "string", "description": "New location (optional)."},
            },
            "required": ["event_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_calendar_service(access_token)
        patch: dict[str, Any] = {}

        if "summary" in params:
            patch["summary"] = params["summary"]
        if "description" in params:
            patch["description"] = params["description"]
        if "location" in params:
            patch["location"] = params["location"]

        if "start_time" in params:
            try:
                dt = datetime.fromisoformat(params["start_time"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return {"error": "start_time must include a timezone offset."}
                patch["start"] = {"dateTime": dt.isoformat()}
            except ValueError:
                return {"error": f"Invalid start_time: {params['start_time']}"}

        if "end_time" in params:
            try:
                dt = datetime.fromisoformat(params["end_time"].replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    return {"error": "end_time must include a timezone offset."}
                patch["end"] = {"dateTime": dt.isoformat()}
            except ValueError:
                return {"error": f"Invalid end_time: {params['end_time']}"}

        if "attendees" in params:
            patch["attendees"] = [{"email": e} for e in params["attendees"]]

        if not patch:
            return {"error": "No fields to update. Pass at least one of summary/start_time/end_time/attendees/description/location."}

        def _patch_sync() -> dict:
            return service.events().patch(
                calendarId="primary",
                eventId=params["event_id"],
                body=patch,
                sendUpdates="all",
            ).execute()

        try:
            updated = await asyncio.to_thread(_patch_sync)
        except Exception as e:
            return {"error": f"Update failed: {e}"}

        return {
            "status": "updated",
            "event": _parse_event(updated),
            "html_link": updated.get("htmlLink", ""),
            "updated_fields": list(patch.keys()),
        }


class DeleteEvent(BaseTool):
    @property
    def name(self) -> str:
        return "delete_calendar_event"

    @property
    def description(self) -> str:
        return (
            "Cancel a Google Calendar event. Sends cancellation notifications to all attendees. "
            "IMPORTANT: this is irreversible — confirm with the user before calling."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID."},
            },
            "required": ["event_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_calendar_service(access_token)

        def _delete_sync() -> None:
            service.events().delete(
                calendarId="primary",
                eventId=params["event_id"],
                sendUpdates="all",
            ).execute()

        try:
            await asyncio.to_thread(_delete_sync)
        except Exception as e:
            return {"error": f"Delete failed: {e}"}

        return {"status": "deleted", "event_id": params["event_id"]}


class RsvpEvent(BaseTool):
    @property
    def name(self) -> str:
        return "rsvp_to_event"

    @property
    def description(self) -> str:
        return (
            "RSVP to a Google Calendar event you've been invited to. Response can be "
            "'accepted', 'declined', 'tentative', or 'needsAction'. Only affects your own "
            "attendee status — other attendees are unchanged."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string", "description": "Google Calendar event ID."},
                "response": {
                    "type": "string",
                    "enum": ["accepted", "declined", "tentative", "needsAction"],
                    "description": "Your RSVP response.",
                },
                "comment": {
                    "type": "string",
                    "description": "Optional short comment attached to your RSVP.",
                },
            },
            "required": ["event_id", "response"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_calendar_service(access_token)
        event_id = params["event_id"]
        response = params["response"]

        def _fetch_sync() -> dict:
            return service.events().get(calendarId="primary", eventId=event_id).execute()

        try:
            event = await asyncio.to_thread(_fetch_sync)
        except Exception as e:
            return {"error": f"Could not load event: {e}"}

        # Figure out which attendee entry is "me" — Google Calendar marks it with `self: True`.
        attendees = event.get("attendees", [])
        me_idx = None
        for i, a in enumerate(attendees):
            if a.get("self"):
                me_idx = i
                break

        if me_idx is None:
            return {
                "error": "You are not in the attendee list for this event. "
                "Only invitees can RSVP (organizers don't RSVP to their own events)."
            }

        attendees[me_idx]["responseStatus"] = response
        if params.get("comment"):
            attendees[me_idx]["comment"] = params["comment"]

        patch = {"attendees": attendees}

        def _patch_sync() -> dict:
            return service.events().patch(
                calendarId="primary",
                eventId=event_id,
                body=patch,
                sendUpdates="all",
            ).execute()

        try:
            updated = await asyncio.to_thread(_patch_sync)
        except Exception as e:
            return {"error": f"RSVP failed: {e}"}

        return {
            "status": "rsvp_updated",
            "event_id": event_id,
            "response": response,
            "event": _parse_event(updated),
        }


# ---------------------------------------------------------------------------
# Add attendees without overwriting + group free-time finder.
# ---------------------------------------------------------------------------

class AddAttendeesToEvent(BaseTool):
    @property
    def name(self) -> str:
        return "add_attendees_to_event"

    @property
    def description(self) -> str:
        return (
            "Add one or more attendees to an existing event WITHOUT replacing "
            "the existing attendee list. By default sends the standard Google "
            "update notification to all attendees; pass send_updates='none' "
            "or 'externalOnly' to suppress / scope it."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Emails to invite (added to the existing attendee list).",
                },
                "send_updates": {
                    "type": "string",
                    "enum": ["all", "externalOnly", "none"],
                    "default": "all",
                    "description": "Who Google notifies of the change. 'none' suppresses notifications.",
                },
            },
            "required": ["event_id", "emails"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        emails = params["emails"]
        if not isinstance(emails, list) or not emails:
            return {"error": "emails must be a non-empty list"}
        cal = params.get("calendar_id") or "primary"
        send_updates = params.get("send_updates") or "all"

        def _sync() -> dict:
            svc = _get_calendar_service(access_token)
            existing = svc.events().get(
                calendarId=cal, eventId=params["event_id"],
            ).execute()
            current_attendees = list(existing.get("attendees") or [])
            current_emails = {a.get("email", "").lower() for a in current_attendees}
            for em in emails:
                em_l = em.lower()
                if em_l not in current_emails:
                    current_attendees.append({"email": em})
                    current_emails.add(em_l)
            return svc.events().patch(
                calendarId=cal,
                eventId=params["event_id"],
                body={"attendees": current_attendees},
                sendUpdates=send_updates,
            ).execute()

        try:
            updated = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Add attendees failed: {e}"}
        return {
            "status": "attendees_added",
            "event_id": params["event_id"],
            "added": emails,
            "event": _parse_event(updated),
        }


class FindGroupFreeTime(BaseTool):
    @property
    def name(self) -> str:
        return "find_group_free_time"

    @property
    def description(self) -> str:
        return (
            "Find time windows where ALL of a list of attendees are free, "
            "using Google's freebusy API (sees only busy slots, no event "
            "details). Pass `emails`, a date window, and optional "
            "`min_duration_minutes` (default 30). Returns up to `limit` "
            "candidate slots inside the window."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "emails": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Emails to coordinate (include the asker if their busy slots should count).",
                },
                "time_min": {"type": "string", "description": "Window start, ISO 8601 (e.g. '2026-05-07T09:00:00-07:00')."},
                "time_max": {"type": "string", "description": "Window end, ISO 8601."},
                "min_duration_minutes": {"type": "integer", "default": 30},
                "limit": {"type": "integer", "default": 5},
            },
            "required": ["emails", "time_min", "time_max"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        from datetime import datetime as _dt, timedelta as _td

        emails = params["emails"]
        if not isinstance(emails, list) or not emails:
            return {"error": "emails must be a non-empty list"}
        try:
            min_minutes = int(params.get("min_duration_minutes") or 30)
        except (TypeError, ValueError):
            return {"error": "min_duration_minutes must be an integer."}
        try:
            limit = int(params.get("limit") or 5)
        except (TypeError, ValueError):
            return {"error": "limit must be an integer."}
        if min_minutes <= 0:
            return {"error": "min_duration_minutes must be a positive integer."}
        if limit <= 0:
            return {"error": "limit must be a positive integer."}

        def _sync() -> dict:
            svc = _get_calendar_service(access_token)
            return svc.freebusy().query(body={
                "timeMin": params["time_min"],
                "timeMax": params["time_max"],
                "items": [{"id": e} for e in emails],
            }).execute()

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Free/busy lookup failed: {e}"}

        cals = res.get("calendars", {})
        unreachable = [e for e in emails if cals.get(e, {}).get("errors")]
        busy_intervals: list[tuple[_dt, _dt]] = []
        for em in emails:
            for slot in (cals.get(em, {}).get("busy") or []):
                try:
                    s = _dt.fromisoformat(slot["start"].replace("Z", "+00:00"))
                    e = _dt.fromisoformat(slot["end"].replace("Z", "+00:00"))
                    busy_intervals.append((s, e))
                except Exception:
                    continue
        busy_intervals.sort()
        merged: list[list[_dt]] = []
        for s, e in busy_intervals:
            if merged and s <= merged[-1][1]:
                if e > merged[-1][1]:
                    merged[-1][1] = e
            else:
                merged.append([s, e])

        try:
            window_start = _dt.fromisoformat(params["time_min"].replace("Z", "+00:00"))
            window_end = _dt.fromisoformat(params["time_max"].replace("Z", "+00:00"))
        except Exception as e:
            return {"error": f"Invalid time_min/time_max: {e}"}
        # Naive datetimes can't be compared against the tz-aware busy
        # intervals Google returns — would crash with TypeError mid-loop.
        # Match CreateEvent's contract: require an explicit offset.
        if window_start.tzinfo is None or window_end.tzinfo is None:
            return {"error": "time_min and time_max must include a timezone offset (e.g. +05:30 or Z)."}

        free_slots = []
        cursor = window_start
        for s, e in merged:
            if cursor < s and (s - cursor) >= _td(minutes=min_minutes):
                free_slots.append((cursor, s))
            cursor = max(cursor, e)
        if cursor < window_end and (window_end - cursor) >= _td(minutes=min_minutes):
            free_slots.append((cursor, window_end))

        return {
            "free_slots": [
                {"start": s.isoformat(), "end": e.isoformat(),
                 "duration_minutes": int((e - s).total_seconds() // 60)}
                for s, e in free_slots[:limit]
            ],
            "count": len(free_slots),
            "unreachable_calendars": unreachable,
        }
