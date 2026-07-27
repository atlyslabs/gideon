"""
Slack mention translation.

Decode: `<@U12345>` → `@Real Name` so Claude reads human-readable names instead
of opaque Slack user IDs in incoming webhook text and in message bodies returned
by the Slack tools.

Encode: `@name` / `@first.last` / `@First Last` → `<@U12345>` so messages Gideon
sends through `chat.postMessage` render as proper user tags in Slack.

Both directions lean on the workspace member cache (Redis-backed, ~1h TTL), so
cost per call is one Redis GET plus a regex sub.
"""

import re

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from services.workspace_member_service import get_all_members

logger = structlog.get_logger()

# Slack encodes a user mention as <@USERID> — USERID is upper-case alnum.
_INCOMING_MENTION_RE = re.compile(r"<@([A-Z0-9]+)>")

# Outgoing: match `@` followed by a name-ish token, optionally followed by a
# single second word (for `@First Last`). Bounded on the left so email
# addresses (foo@bar.com) and mid-word @ don't get rewritten. We eagerly
# capture a follow-up word and let the sub callback decide whether to consume
# it — if the two-word form doesn't resolve to a unique member, the sub
# restores the trailing word instead of eating an unrelated token.
_OUTGOING_MENTION_RE = re.compile(
    r"(?<![\w@])@([A-Za-z][A-Za-z0-9._-]{1,40})"
    r"(\s+([A-Za-z][A-Za-z0-9._-]{1,40}))?"
)


async def _slack_members(session: AsyncSession) -> list[dict]:
    members = await get_all_members(session)
    return [m for m in members if m.get("slack_id")]


async def build_decode_map(session: AsyncSession) -> dict[str, str]:
    """Return `{slack_id: display_name}` for all cached Slack members. Callers
    that decode many messages should build this once and pass it to
    `decode_with_map` per message to avoid repeated Redis / member lookups."""
    members = await _slack_members(session)
    return {
        m["slack_id"]: (m.get("name") or m.get("display_name") or m["slack_id"])
        for m in members
    }


def decode_with_map(text: str, id_to_name: dict[str, str]) -> str:
    """Pure-regex decode using a pre-built id→name map. No IO."""
    if not text or "<@" not in text:
        return text

    def _sub(match: re.Match) -> str:
        sid = match.group(1)
        name = id_to_name.get(sid)
        return f"@{name}" if name else match.group(0)

    return _INCOMING_MENTION_RE.sub(_sub, text)


async def decode_slack_mentions(text: str, session: AsyncSession) -> str:
    """Convenience wrapper for single-message decode. For batches, use
    `build_decode_map` + `decode_with_map` to avoid N+1 Redis reads."""
    if not text or "<@" not in text:
        return text
    id_to_name = await build_decode_map(session)
    return decode_with_map(text, id_to_name)


async def encode_slack_mentions(text: str, session: AsyncSession) -> str:
    """Replace `@token` / `@First Last` references with `<@U12345>` when
    exactly one member matches. Lookup order: display_name → full name
    (whitespace-squashed) → email local part → first name. Ambiguous matches
    (multiple members share a first name) are left alone so we never tag the
    wrong person. For two-word mentions, the full-name match is tried first;
    if it doesn't resolve, the trailing word is restored and only the first
    word is considered."""
    if not text or "@" not in text:
        return text

    members = await _slack_members(session)
    if not members:
        return text

    by_display: dict[str, list[str]] = {}
    by_email_local: dict[str, list[str]] = {}
    by_first: dict[str, list[str]] = {}
    by_full_squash: dict[str, list[str]] = {}  # "firstlast" with no separator

    for m in members:
        sid = m["slack_id"]
        name = (m.get("name") or "").strip()
        display = (m.get("display_name") or "").strip()
        email = (m.get("email") or "").strip().lower()

        if display:
            by_display.setdefault(display.lower(), []).append(sid)
        if email and "@" in email:
            local = email.split("@", 1)[0]
            by_email_local.setdefault(local, []).append(sid)
        if name:
            parts = name.split()
            if parts:
                by_first.setdefault(parts[0].lower(), []).append(sid)
            squash = "".join(parts).lower()
            if squash:
                by_full_squash.setdefault(squash, []).append(sid)

    def _resolve_single(token: str) -> str | None:
        key = token.lower()
        for lookup in (by_display, by_email_local, by_full_squash, by_first):
            hits = lookup.get(key)
            if hits and len(hits) == 1:
                return hits[0]
        return None

    def _sub(match: re.Match) -> str:
        first = match.group(1)
        trailing = match.group(2) or ""  # " Second" (with leading whitespace) or ""
        second = match.group(3)           # "Second" or None

        # If a second word was captured, try the full two-word form first.
        if second:
            full_key = f"{first}{second}".lower()
            hits = by_full_squash.get(full_key) or by_display.get(f"{first} {second}".lower())
            if hits and len(hits) == 1:
                return f"<@{hits[0]}>"

        # Fall back to resolving just the first token; if the regex captured
        # a second word, restore it verbatim so we don't drop user text.
        sid = _resolve_single(first)
        if sid:
            return f"<@{sid}>{trailing}"
        return match.group(0)

    return _OUTGOING_MENTION_RE.sub(_sub, text)
