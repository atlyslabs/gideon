"""
Delegation service.

Helpers used by the delegation flow that don't belong on the request path:
- cadence parsing + next-reminder computation
- sending reminder nudges to the existing Slack delegation thread
- notifying the delegator when their delegated todo is completed by the
  delegatee (or vice versa) via a 1:1 Slack DM from Gideon's bot

Reminders are RECURRING — at each fire we bump `next_reminder_at` by the
cadence interval until the todo is marked done. The cadence enum is
enforced at the application layer (see `normalize_cadence`); the DB column
is intentionally unconstrained so adding a new cadence later doesn't need
a schema migration.

Reminders are driven by the data scheduler loop (see services/scheduler.py).
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional
from uuid import UUID

import structlog
from slack_sdk.web.async_client import AsyncWebClient
from services.slack_client import get_slack_client
from sqlalchemy import delete as sa_delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from db.database import AsyncPostgresSessionGideon
from db.models import Integration, Todo, TodoUpdate, User
from services.encryption_service import decrypt
from services.slack_format import markdown_to_slack_mrkdwn

logger = structlog.get_logger()

# Optional Slack user-ID overrides for emails whose `users.lookupByEmail`
# lookup is unreliable. Anyone listed here gets their Slack ID returned
# without an API call. Configure with SLACK_ID_OVERRIDES, a JSON object of
# {"email": "U012345"}. Empty by default — the API lookup handles the rest.
def _load_slack_id_overrides() -> dict[str, str]:
    raw = os.environ.get("SLACK_ID_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return {str(k).lower(): str(v) for k, v in data.items()}
    except Exception:
        logger.warning("SLACK_ID_OVERRIDES failed to parse; ignoring")
        return {}


SLACK_ID_OVERRIDES = _load_slack_id_overrides()

VALID_CADENCES = {"never", "daily", "every_2_days", "weekly"}

_CADENCE_TO_DELTA = {
    "daily": timedelta(days=1),
    "every_2_days": timedelta(days=2),
    "weekly": timedelta(days=7),
}


def normalize_cadence(value: Optional[str]) -> Optional[str]:
    """Return a valid cadence string or None. Anything outside the allow list
    becomes None so callers can treat it as 'no reminders'."""
    if not value:
        return None
    v = value.strip().lower()
    if v in VALID_CADENCES:
        return v
    return None


def compute_next_reminder_at(cadence: Optional[str], from_time: Optional[datetime] = None) -> Optional[datetime]:
    """Compute the next reminder timestamp from `from_time` (default now-UTC)
    given a cadence. Returns None for `never` or unknown cadences."""
    delta = _CADENCE_TO_DELTA.get(cadence or "")
    if not delta:
        return None
    base = from_time or datetime.now(timezone.utc)
    return base + delta


async def _bot_token_for_user(session: AsyncSession, user_id: UUID) -> Optional[str]:
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "slack",
            Integration.is_active == True,
            Integration.bot_token.isnot(None),
        ).limit(1)
    )
    integ = result.scalar_one_or_none()
    if not integ or not integ.bot_token:
        return None
    return decrypt(integ.bot_token)


async def _user_token_for_user(session: AsyncSession, user_id: UUID) -> Optional[str]:
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "slack",
            Integration.is_active == True,
        ).limit(1)
    )
    integ = result.scalar_one_or_none()
    if not integ or not integ.access_token:
        return None
    return decrypt(integ.access_token)


async def resolve_delegatee_slack_ids_cached(
    todo: Todo, token: str,
) -> tuple[list[str], Optional[list[dict]]]:
    """Cache-first resolution of delegatees' Slack user IDs.

    Reads `slack_user_id` from each entry in `todo.delegated_to`. For any
    entry missing the field, looks up via `users.lookupByEmail`.

    Returns:
      - List of resolved Slack IDs in delegatee order (entries that
        couldn't be resolved are skipped). May be empty.
      - The updated delegatees list IF at least one new ID was resolved
        via API (else None). Caller persists this back to the row so
        subsequent calls hit cache — see `send_reminder_to_thread` for the
        cron path, `nudge_todo` for the request path.

    Pure read + Slack API; holds no DB session, so it's safe to call from
    request handlers AND background loops (CLAUDE.md DB guideline #3).
    """
    delegatees = todo.delegated_to or []
    if not delegatees:
        return [], None

    # Shallow-copy each dict so mutations don't aliasing-bite the caller's
    # in-memory `todo.delegated_to` until they explicitly assign it back.
    updated = [dict(d) for d in delegatees]
    ids: list[str] = []
    dirty = False
    client: Optional[AsyncWebClient] = None

    for d in updated:
        cached = d.get("slack_user_id")
        if cached:
            ids.append(cached)
            continue
        email = d.get("email")
        if not email:
            continue
        if client is None:
            client = get_slack_client(token=token)
        try:
            resp = await client.users_lookupByEmail(email=email)
            if resp.get("ok"):
                sid = resp["user"]["id"]
                d["slack_user_id"] = sid
                ids.append(sid)
                dirty = True
        except Exception as e:
            logger.warning(
                "delegatee Slack lookup failed",
                todo_id=str(todo.id), email=email, err=str(e),
            )

    return ids, (updated if dirty else None)


async def send_reminder_to_thread(
    todo: Todo, token: Optional[str],
) -> tuple[bool, Optional[list[dict]]]:
    """Post a friendly nudge in the existing delegation Slack thread.

    `token` should be the delegator's user token if available (so the message
    looks like it came from them — same identity that opened the DM), else the
    bot token. Caller pre-resolves it to keep this off the request path.

    Returns:
      - `True` iff the message was successfully posted.
      - The updated delegatees list IF this call resolved any new
        `slack_user_id`s via the Slack API. Caller persists it so the next
        reminder skips the lookup. `None` means no write-back needed.

    Why @-mentions for group DMs (2+ delegatees):
      In a 1:1 Slack DM, every message (including thread replies) notifies
      the recipient. In a group DM (3+ members, which delegation creates
      with 2+ delegatees + the delegator), thread replies notify ONLY users
      explicitly @-mentioned or the thread root author. Without explicit
      mentions here, the only person who'd see a reminder is the delegator —
      exactly the opposite of intent.

    Used by BOTH the auto cadence reminder (scheduler.py → run_due_reminders)
    AND the manual "Nudge" button on the delegated todo card.
    """
    if not (todo.slack_channel_id and todo.slack_thread_ts):
        return False, None

    if not token:
        logger.warning("delegation reminder skipped: no Slack token", todo_id=str(todo.id))
        return False, None

    # `todo.content` is user-authored — could carry markdown bold/italic
    # that Slack would render as literal characters. Convert before
    # wrapping in our outer `*…*` so the inner bolds resolve cleanly.
    body = markdown_to_slack_mrkdwn((todo.content or "this task").strip())
    text = f"Gentle reminder on this task: *{body[:200]}*"

    # Tag every delegatee regardless of count. 1:1 DM thread replies would
    # technically notify the recipient by default, but explicit @-mentions
    # trigger a louder badge/sound that's harder to miss, and the consistent
    # behavior matches what users expect after seeing it for 2+ delegations.
    updated_delegatees: Optional[list[dict]] = None
    if todo.delegated_to:
        ids, updated_delegatees = await resolve_delegatee_slack_ids_cached(todo, token)
        if ids:
            text = " ".join(f"<@{uid}>" for uid in ids) + "\n" + text

    client = get_slack_client(token=token)
    try:
        await client.chat_postMessage(
            channel=todo.slack_channel_id,
            thread_ts=todo.slack_thread_ts,
            text=text,
        )
        return True, updated_delegatees
    except Exception as e:
        logger.warning("delegation reminder Slack post failed", todo_id=str(todo.id), err=str(e))
        return False, updated_delegatees


async def post_reassignment_in_thread(
    token: Optional[str],
    channel_id: str,
    thread_ts: str,
) -> bool:
    """Drop a short notice in the OLD delegation thread so the previous
    delegatee(s) see the task has moved off them. Intentionally does NOT
    name the new delegatee — the previous group should know they're off
    the hook, not who picked it up.

    Best-effort: returns False on any Slack failure but never raises, so
    a flaky old thread can't block the redelegation flow.
    """
    if not (channel_id and thread_ts and token):
        return False
    client = get_slack_client(token=token)
    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=":arrows_counterclockwise: This task has been reassigned. You're no longer on the hook here.",
        )
    except Exception as e:
        logger.warning(
            "reassignment notice post failed",
            channel=channel_id,
            err=str(e),
        )
        return False
    # Stamp the thread root so it's visible from the channel scroll too.
    try:
        await client.reactions_add(
            channel=channel_id,
            timestamp=thread_ts,
            name="arrows_counterclockwise",
        )
    except Exception as e:
        msg = str(e)
        if "already_reacted" not in msg:
            logger.warning(
                "reassignment notice reaction failed",
                channel=channel_id,
                err=msg,
            )
    return True


async def _open_dm_and_send(token: str, slack_user_id: str, text: str) -> bool:
    client = get_slack_client(token=token)
    try:
        opened = await client.conversations_open(users=slack_user_id)
        channel_id = opened.get("channel", {}).get("id")
        if not channel_id:
            return False
        await client.chat_postMessage(channel=channel_id, text=markdown_to_slack_mrkdwn(text))
        return True
    except Exception as e:
        logger.warning("delegation DM failed", err=str(e))
        return False


async def _get_thread_permalink(
    token: str, channel_id: str, thread_ts: str
) -> Optional[str]:
    """Resolve a Slack permalink to the delegation thread root so we can
    deep-link into it from the completion DM. Returns None on failure."""
    client = get_slack_client(token=token)
    try:
        resp = await client.chat_getPermalink(channel=channel_id, message_ts=thread_ts)
        return resp.get("permalink")
    except Exception as e:
        logger.warning("delegation permalink fetch failed", err=str(e))
        return None


async def post_completion_in_thread(
    delegator_user_id: UUID,
    channel_id: str,
    thread_ts: str,
    completed_by_name: str,
    completed_by_user_id: Optional[UUID] = None,
) -> Optional[str]:
    """Post a 'Marked done by {name}' message inside the delegation thread.

    Returns the permalink to the thread root if obtainable so the caller can
    include it in the DM. We post as the actual completer when their user
    token is available (so Slack shows their avatar / identity on the reply,
    matching the body text), falling back to the delegator's user token
    (guaranteed to be a member of the DM) and finally the delegator's bot
    token."""
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        token: Optional[str] = None
        if completed_by_user_id is not None:
            token = await _user_token_for_user(session, completed_by_user_id)
        if not token:
            token = await _user_token_for_user(session, delegator_user_id)
        if not token:
            token = await _bot_token_for_user(session, delegator_user_id)
    if not token:
        logger.warning(
            "completion thread post skipped: no Slack token",
            delegator_user_id=str(delegator_user_id),
        )
        return None

    permalink = await _get_thread_permalink(token, channel_id, thread_ts)
    text = f":white_check_mark: Marked done by *{completed_by_name}*."

    client = get_slack_client(token=token)
    try:
        await client.chat_postMessage(
            channel=channel_id,
            thread_ts=thread_ts,
            text=text,
        )
    except Exception as e:
        logger.warning(
            "completion thread post failed",
            channel=channel_id,
            err=str(e),
        )

    # Also react to the thread root so anyone scrolling the channel sees the
    # task is done without opening the thread. Best-effort — a duplicate
    # `already_reacted` is fine, just suppress.
    try:
        await client.reactions_add(
            channel=channel_id,
            timestamp=thread_ts,
            name="white_check_mark",
        )
    except Exception as e:
        msg = str(e)
        if "already_reacted" not in msg:
            logger.warning(
                "completion thread reaction failed",
                channel=channel_id,
                err=msg,
            )
    return permalink


async def notify_completion(
    completed_by_user: User,
    notify_user: User,
    todo_text: str,
    direction: str,
    thread_permalink: Optional[str] = None,
) -> None:
    """DM `notify_user` from Gideon's bot that `completed_by_user` finished
    `todo_text`. `direction` is 'delegatee_completed' (delegatee finished a
    mirror) or 'delegator_completed' (delegator marked their own done).
    Includes a Slack permalink back to the delegation thread when available.
    """
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        # Resolve the recipient's Slack user ID via their Slack integration.
        result = await session.execute(
            select(Integration).where(
                Integration.user_id == notify_user.id,
                Integration.provider == "slack",
                Integration.is_active == True,
            ).limit(1)
        )
        integ = result.scalar_one_or_none()
        recipient_slack_id: Optional[str] = None
        if integ and integ.extra_data:
            recipient_slack_id = (integ.extra_data or {}).get("user_id")
        # Bot token of the notify_user's workspace (Gideon DMs them as the bot).
        bot_token = await _bot_token_for_user(session, notify_user.id)

    if not recipient_slack_id or not bot_token:
        logger.info(
            "delegation completion DM skipped: missing slack id or bot token",
            notify_user_id=str(notify_user.id),
        )
        return

    actor = completed_by_user.name or completed_by_user.email or "your colleague"
    snippet = (todo_text or "").strip()[:200]
    if direction == "delegatee_completed":
        text = f":white_check_mark: {actor} marked the task you delegated as done: *{snippet}*"
    else:
        text = f":white_check_mark: {actor} marked the task they delegated to you as done: *{snippet}*"
    if thread_permalink:
        text += f"\n<{thread_permalink}|Open the delegation thread>"

    await _open_dm_and_send(bot_token, recipient_slack_id, text)


async def find_due_reminders(session: AsyncSession, now: datetime, limit: int = 200) -> list[Todo]:
    """Return up to `limit` open delegated todos whose next_reminder_at is in
    the past. Ordered oldest-due-first so we drain the backlog cleanly."""
    result = await session.execute(
        select(Todo)
        .where(
            Todo.next_reminder_at.isnot(None),
            Todo.next_reminder_at <= now,
            Todo.completed == False,
            Todo.slack_channel_id.isnot(None),
            Todo.slack_thread_ts.isnot(None),
        )
        .order_by(Todo.next_reminder_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def run_due_reminders() -> int:
    """One reminder pass. Selects due todos, sends nudges, advances the
    `next_reminder_at` cursor by the cadence interval. Returns the number of
    reminders sent (for logging)."""
    sent = 0
    sm = AsyncPostgresSessionGideon()
    now = datetime.now(timezone.utc)

    # Read phase: load due todos + their delegators + their Slack tokens in
    # one session, so the send loop below holds zero DB sessions.
    async with sm.get_session() as session:
        due = await find_due_reminders(session, now)
        if not due:
            return 0
        delegator_ids = {t.user_id for t in due}
        users_res = await session.execute(select(User).where(User.id.in_(delegator_ids)))
        users_by_id = {u.id: u for u in users_res.scalars().all()}
        tokens_by_user_id: dict[UUID, Optional[str]] = {}
        for uid in delegator_ids:
            token = await _user_token_for_user(session, uid)
            if not token:
                token = await _bot_token_for_user(session, uid)
            tokens_by_user_id[uid] = token

    # Send phase: post nudges (no DB sessions held — tokens already resolved).
    advanced_ids: list[UUID] = []
    next_at_by_id: dict[UUID, Optional[datetime]] = {}
    # Capture any delegatees lists that got their `slack_user_id` cache filled
    # during this send pass, so the write phase persists them and future
    # reminders skip `users.lookupByEmail` entirely.
    delegated_to_by_id: dict[UUID, list[dict]] = {}

    for todo in due:
        delegator = users_by_id.get(todo.user_id)
        if not delegator:
            continue
        ok, updated_delegatees = await send_reminder_to_thread(
            todo, tokens_by_user_id.get(todo.user_id)
        )
        if updated_delegatees is not None:
            delegated_to_by_id[todo.id] = updated_delegatees
        if ok:
            sent += 1
            advanced_ids.append(todo.id)
            next_at_by_id[todo.id] = compute_next_reminder_at(todo.reminder_cadence, now)

    if not advanced_ids and not delegated_to_by_id:
        return 0

    # Write phase: bump next_reminder_at + persist any newly-resolved slack
    # user IDs in a single short session.
    async with sm.get_session() as session:
        # First, persist the cache fills (even for todos whose reminder POST
        # failed — we paid the API cost, may as well keep the data).
        for todo_id, updated_delegatees in delegated_to_by_id.items():
            await session.execute(
                update(Todo)
                .where(Todo.id == todo_id)
                .values(delegated_to=updated_delegatees)
            )

        # Then advance cadence + add timeline entries for successful sends.
        for todo_id in advanced_ids:
            await session.execute(
                update(Todo)
                .where(Todo.id == todo_id)
                .values(next_reminder_at=next_at_by_id.get(todo_id))
            )
            session.add(TodoUpdate(
                todo_id=todo_id,
                author_name="Gideon",
                author_email=None,
                content="Sent a reminder in the Slack thread.",
                source="reminder",
            ))
        await session.flush()

    return sent


# ---------------------------------------------------------------------------
# Public Slack-token helpers
# ---------------------------------------------------------------------------
# These are thin re-exports of the private `_bot_token_for_user` /
# `_user_token_for_user` versions used by the reminder loop. Made public so
# the hub_router endpoints (delegate/nudge) and the LLM `delegate_todo`
# tool can share one implementation instead of each defining their own.

async def get_slack_bot_token(session: AsyncSession, user_id: UUID) -> Optional[str]:
    return await _bot_token_for_user(session, user_id)


async def get_slack_user_token(session: AsyncSession, user_id: UUID) -> Optional[str]:
    return await _user_token_for_user(session, user_id)


async def lookup_slack_user_ids(bot_token: str, emails: list[str]) -> dict[str, str]:
    """Look up Slack user IDs by email. Returns {email: slack_user_id}.
    Emails in `SLACK_ID_OVERRIDES` skip the API call.
    """
    client = get_slack_client(token=bot_token)
    result: dict[str, str] = {}
    for email in emails:
        if email in SLACK_ID_OVERRIDES:
            result[email] = SLACK_ID_OVERRIDES[email]
            continue
        try:
            resp = await client.users_lookupByEmail(email=email)
            if resp.get("ok"):
                result[email] = resp["user"]["id"]
        except Exception as e:
            logger.warning("Slack user lookup failed", error_type=type(e).__name__)
    return result


# ---------------------------------------------------------------------------
# delegate_or_reassign — single entry point for first-delegation AND
# reassignment. Replaces what used to live inline in
# `hub_router.delegate_todo`. The router and the LLM `delegate_todo` tool
# both call this so behavior stays in sync.
# ---------------------------------------------------------------------------


class DelegationError(Exception):
    """Raised when delegation can't proceed. `status_code` mirrors the HTTP
    code the router used to raise so callers can preserve the mapping.
    """
    def __init__(self, status_code: int, detail: str):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class DelegateeRef:
    """One delegatee as returned from `delegate_or_reassign`. Mirrors the
    minimal subset of `todos.delegated_to` JSON callers actually use —
    name, email, optional avatar. The cached `slack_user_id` from the row
    is intentionally NOT exposed here (it's an internal optimization)."""
    name: str
    email: str
    avatar_url: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DelegationOutcome:
    """Typed result of `delegate_or_reassign`. Replaces an earlier dict so
    callers don't index by string keys (which silently masks renames)."""
    status: str  # currently always "delegated"; reserved for future states.
    delegated_to: list[DelegateeRef] = field(default_factory=list)
    not_on_gideon: list[DelegateeRef] = field(default_factory=list)


async def delegate_or_reassign(
    user_id: UUID,
    todo_id: UUID,
    delegatees: list[dict],
    message: Optional[str] = None,
    reminder_cadence: Optional[str] = None,
    follow_up_at_iso: Optional[str] = None,
) -> DelegationOutcome:
    """Delegate (or reassign) a todo via Slack group DM. `delegatees` is the
    DESIRED FULL set, not a delta:

      - first-time delegation → opens a Slack group DM and creates mirrors
      - same delegatees, only cadence/follow-up changed → in-place update,
        no new DM, no thread message
      - any delegatee added or removed → REASSIGNMENT: fresh group DM,
        notice in old thread (no new-delegatee name), Slack pointer swap
        on parent + surviving mirrors, mirror cleanup for removed users

    DB sessions are short-lived and split into phases so Slack roundtrips
    (~1–3s each) never hold a checked-out connection (CLAUDE.md DB rule #3):
      Phase 1 (read):  load user/todo/tokens, compute diff. Fast-path
                       returns from inside this session.
      Phase 2 (Slack): no DB session held. Opens DM, posts message, reacts
                       to old thread.
      Phase 3 (write): fresh session for the cluster of DB mutations.

    Raises ``DelegationError`` with an HTTP-shaped status code for any
    validation / Slack failure so the caller can map cleanly.
    """
    if not delegatees:
        raise DelegationError(400, "At least one delegatee required")

    # Dedupe by lowercased email. Two callers can produce duplicates: the
    # FE @-mention picker if the user adds the same person twice across
    # sessions, and the LLM tool when the model re-uses the same address
    # under two different display-name spellings. Without dedupe we'd
    # store the duplicate in `todos.delegated_to` JSONB AND @-mention the
    # same Slack user twice in the DM. First occurrence wins (preserves
    # caller's intended display name + avatar).
    seen_emails: set[str] = set()
    deduped: list[dict] = []
    duplicate_count = 0
    for d in delegatees:
        email_l = (d.get("email") or "").lower().strip()
        if not email_l:
            raise DelegationError(400, "Each delegatee must include a non-empty email.")
        if email_l in seen_emails:
            duplicate_count += 1
            continue
        seen_emails.add(email_l)
        deduped.append(d)
    if duplicate_count:
        # NEVER log raw emails — PII. Log the count only so we can spot a
        # caller that's submitting noisy duplicates without leaking who.
        logger.info("delegation deduped duplicate emails", count=duplicate_count)
    delegatees = deduped

    parsed_follow_up_at: Optional[datetime] = None
    if follow_up_at_iso:
        try:
            parsed_follow_up_at = datetime.fromisoformat(follow_up_at_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise DelegationError(400, "Invalid follow_up_at format, expected ISO datetime")

    cadence = normalize_cadence(reminder_cadence)
    sm = AsyncPostgresSessionGideon()

    # ------------------------------------------------------------------
    # Phase 1 — READ. Snapshot everything we need into locals so the
    # session can close before any Slack call. The fast path (cadence-
    # only edit) also writes inside this session and returns early.
    # ------------------------------------------------------------------
    async with sm.get_session() as session:
        user_res = await session.execute(select(User).where(User.id == user_id))
        user = user_res.scalar_one_or_none()
        if not user:
            raise DelegationError(404, "User not found")
        user_display = user.name or user.email
        user_email = user.email

        todo_res = await session.execute(
            select(Todo).where(Todo.id == todo_id, Todo.user_id == user_id)
        )
        todo = todo_res.scalar_one_or_none()
        if not todo:
            raise DelegationError(404, "Todo not found")
        todo_content = todo.content
        todo_source_detail = todo.source_detail
        todo_priority = todo.priority
        old_channel_id = todo.slack_channel_id
        old_thread_ts = todo.slack_thread_ts
        existing_delegatees = todo.delegated_to or []

        # Diff existing vs requested delegatee sets (case-insensitive emails).
        existing_by_email: dict[str, dict] = {
            (d.get("email") or "").lower(): d for d in existing_delegatees
        }
        new_by_body: dict[str, dict] = {
            (d.get("email") or "").lower(): d for d in delegatees
        }
        existing_email_set = set(existing_by_email.keys())
        new_email_set = set(new_by_body.keys())
        added_emails = new_email_set - existing_email_set
        removed_emails = existing_email_set - new_email_set
        delegatee_set_changed = bool(added_emails) or bool(removed_emails)

        # Build new delegatees_data, preserving cached `slack_user_id` for
        # unchanged entries so reminder/nudge paths still hit cache.
        delegatees_data: list[dict] = []
        for body_d in delegatees:
            email_l = (body_d.get("email") or "").lower()
            existing = existing_by_email.get(email_l)
            if existing:
                merged = dict(existing)
                merged["name"] = body_d.get("name", existing.get("name"))
                merged["email"] = body_d.get("email", existing.get("email"))
                merged["avatar_url"] = body_d.get("avatar_url", existing.get("avatar_url"))
                delegatees_data.append(merged)
            else:
                delegatees_data.append({
                    "name": body_d.get("name", body_d.get("email", "")),
                    "email": body_d.get("email", ""),
                    "avatar_url": body_d.get("avatar_url"),
                })

        all_out = [
            DelegateeRef(name=d["name"], email=d["email"], avatar_url=d.get("avatar_url"))
            for d in delegatees_data
        ]

        # Fast path — no delegatee change. Update cadence/follow-up here
        # in the same session and return. No Slack call, no Phase 2/3.
        if not delegatee_set_changed and existing_delegatees:
            now = datetime.now(timezone.utc)
            next_reminder_at = parsed_follow_up_at or compute_next_reminder_at(cadence, now)
            await session.execute(
                update(Todo)
                .where(Todo.id == todo_id)
                .values(
                    delegated_to=delegatees_data,
                    reminder_cadence=cadence,
                    next_reminder_at=next_reminder_at,
                    updated_at=now,
                )
            )
            if message:
                session.add(TodoUpdate(
                    todo_id=todo_id,
                    author_name=user_display,
                    author_email=user_email,
                    content=message,
                    source="delegation",
                ))
            return DelegationOutcome(status="delegated", delegated_to=all_out, not_on_gideon=[])

        # Slow path needs Slack tokens — fetch while still inside session.
        user_token = await get_slack_user_token(session, user_id)
        if not user_token:
            user_token = await get_slack_bot_token(session, user_id)
        if not user_token:
            raise DelegationError(400, "Slack not connected. Please connect Slack first.")
        bot_token = await get_slack_bot_token(session, user_id) or user_token

    # ------------------------------------------------------------------
    # Phase 2 — SLACK. No DB session held. lookup → open DM → post →
    # (optional) notice on old thread.
    # ------------------------------------------------------------------
    all_emails = [d["email"] for d in delegatees_data]
    slack_ids = await lookup_slack_user_ids(bot_token, all_emails)

    # Every delegatee MUST resolve to a Slack user. Partial resolution
    # used to silently drop the unresolved emails from the DM while still
    # creating mirror todos that pointed at a thread the dropped user was
    # never in — invisible work assigned to someone who never saw it.
    # If anyone fails to resolve we abort with the offending emails so
    # the caller can fix typos / chase a Slack invite before retrying.
    unresolved = [email for email in all_emails if not slack_ids.get(email)]
    if unresolved:
        listed = ", ".join(unresolved)
        raise DelegationError(
            400,
            f"Couldn't find a Slack user for: {listed}. "
            "Double-check the email(s), or make sure they've joined the Slack workspace, then retry.",
        )

    dm_user_ids = [slack_ids[email] for email in all_emails]
    for d in delegatees_data:
        sid = slack_ids.get(d.get("email"))
        if sid:
            d["slack_user_id"] = sid

    is_reassignment = bool(existing_delegatees)
    client = get_slack_client(token=user_token)
    try:
        open_resp = await client.conversations_open(users=dm_user_ids)
        slack_channel_id = open_resp["channel"]["id"]

        msg_text = f"*{todo_content}*"
        if todo_source_detail:
            msg_text += f"\n{todo_source_detail}"
        if message:
            msg_text += f"\n\n{message}"
        msg_text += "\n\n:thread: Reply in this thread for updates."

        converted = markdown_to_slack_mrkdwn(msg_text)
        mentions = " ".join(f"<@{uid}>" for uid in dm_user_ids)
        converted = f"{mentions}\n{converted}"

        post_resp = await client.chat_postMessage(channel=slack_channel_id, text=converted)
        slack_thread_ts = post_resp["ts"]
    except Exception as e:
        logger.error("Failed to send delegation Slack message", error_type=type(e).__name__)
        raise DelegationError(502, "Failed to send Slack message. Please try again.")

    if is_reassignment and old_channel_id and old_thread_ts:
        await post_reassignment_in_thread(user_token, old_channel_id, old_thread_ts)

    # ------------------------------------------------------------------
    # Phase 3 — WRITE. Fresh short session for all post-Slack mutations.
    # ------------------------------------------------------------------
    now = datetime.now(timezone.utc)
    next_reminder_at = parsed_follow_up_at or compute_next_reminder_at(cadence, now)
    not_on_gideon: list[DelegateeRef] = []

    async with sm.get_session() as session:
        await session.execute(
            update(Todo)
            .where(Todo.id == todo_id)
            .values(
                delegated_to=delegatees_data,
                slack_channel_id=slack_channel_id,
                slack_thread_ts=slack_thread_ts,
                reminder_cadence=cadence,
                next_reminder_at=next_reminder_at,
                updated_at=now,
            )
        )

        # Cleanup mirror todos for removed delegatees.
        removed_user_ids: list[UUID] = []
        if removed_emails:
            removed_users_res = await session.execute(
                select(User).where(func.lower(User.email).in_(removed_emails))
            )
            for ru in removed_users_res.scalars().all():
                removed_user_ids.append(ru.id)
            if removed_user_ids:
                await session.execute(
                    sa_delete(Todo).where(
                        Todo.delegated_from_todo_id == todo_id,
                        Todo.user_id.in_(removed_user_ids),
                        Todo.completed == False,
                    )
                )
                await session.execute(
                    update(Todo)
                    .where(
                        Todo.delegated_from_todo_id == todo_id,
                        Todo.user_id.in_(removed_user_ids),
                        Todo.completed == True,
                    )
                    .values(slack_channel_id=None, slack_thread_ts=None)
                )

        # Repoint surviving mirrors to the new thread.
        repoint_q = (
            update(Todo)
            .where(Todo.delegated_from_todo_id == todo_id)
            .values(slack_channel_id=slack_channel_id, slack_thread_ts=slack_thread_ts)
        )
        if removed_user_ids:
            repoint_q = repoint_q.where(~Todo.user_id.in_(removed_user_ids))
        await session.execute(repoint_q)

        # Create mirror todos for newly-added delegatees.
        added_delegatee_entries = [
            d for d in delegatees if (d.get("email") or "").lower() in added_emails
        ]
        for d in added_delegatee_entries:
            d_email = d.get("email", "")
            d_name = d.get("name") or d_email
            delegatee_res = await session.execute(
                select(User).where(func.lower(User.email) == d_email.lower())
            )
            delegatee_user = delegatee_res.scalar_one_or_none()
            if not delegatee_user:
                not_on_gideon.append(DelegateeRef(name=d_name, email=d_email, avatar_url=d.get("avatar_url")))
                continue

            existing_linked = await session.execute(
                select(Todo.id).where(
                    Todo.user_id == delegatee_user.id,
                    Todo.delegated_from_todo_id == todo_id,
                )
            )
            if existing_linked.scalar_one_or_none():
                continue

            try:
                async with session.begin_nested():
                    session.add(Todo(
                        user_id=delegatee_user.id,
                        content=todo_content,
                        priority=todo_priority,
                        source="delegation",
                        source_detail=f"From {user_display}" + (f": {message}" if message else ""),
                        delegated_from_todo_id=todo_id,
                        delegated_from_user_id=user_id,
                        slack_channel_id=slack_channel_id,
                        slack_thread_ts=slack_thread_ts,
                    ))
            except IntegrityError:
                continue

        # Timeline entry — wording differs for first delegation vs reassignment.
        added_names = ", ".join(
            (d.get("name") or d.get("email", ""))
            for d in delegatees if (d.get("email") or "").lower() in added_emails
        )
        removed_names = ", ".join(
            (existing_by_email[e].get("name") or e) for e in removed_emails
        )
        if is_reassignment:
            if added_emails and removed_emails:
                timeline_content = f"Reassigned: added {added_names}, removed {removed_names}"
            elif added_emails:
                timeline_content = f"Added {added_names}"
            else:
                timeline_content = f"Removed {removed_names}"
        else:
            timeline_content = f"Delegated to {added_names}"
        if message:
            timeline_content += f": {message}"
        session.add(TodoUpdate(
            todo_id=todo_id,
            author_name=user_display,
            author_email=user_email,
            content=timeline_content,
            source="delegation",
        ))

    # Telemetry — fire-and-forget, no session.
    try:
        from services.events_service import track_todo_delegated
        track_todo_delegated(str(user_id), delegatee_count=len(delegatees_data))
    except Exception:
        pass  # Telemetry failure must never block delegation.

    return DelegationOutcome(status="delegated", delegated_to=all_out, not_on_gideon=not_on_gideon)

