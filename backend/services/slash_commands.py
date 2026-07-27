"""
Slash Commands — user-invokable prompt templates.

These are NOT the same as Skills:
- Skills are passive style overlays auto-injected into every chat turn.
- Slash commands are explicit, user-typed mini-recipes (`/eod`,
  `/eod`, `/research`) that bind a one-turn system-prompt addon.

The two compose: a `/followup` command can still pick up the user's
"Email Tone" skill on top.

Two sources of commands:
- `BUILTIN_COMMANDS` — curated by us, hardcoded here. Available to every user.
- `custom_slash_commands` table — user-defined. A user row with the same slug
  as a built-in OVERRIDES the built-in for that user.

The frontend reads the merged list from `GET /v1/commands/manifest`. The
chat loop calls `expand_slash_command()` which does the same merge for the
specific (user_id, slug) being invoked.
"""

import re
from pathlib import Path
from typing import Optional, TypedDict
from uuid import UUID

import structlog
from sqlalchemy import select
from sqlalchemy.exc import ProgrammingError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import CustomSlashCommand

logger = structlog.get_logger()

# Static assets bundled with the service. Used by built-in commands that need
# to reference larger reference material (design briefs, HTML templates,
# etc.) without inflating every chat turn's prompt for unrelated commands.
_ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"

# Inline-asset substitution. A `prompt_template` containing
# `{{asset:<path>}}` is rewritten at expand time to include the
# file's contents. Path-traversal is blocked: any reference outside
# `_ASSETS_DIR` resolves to a placeholder string instead of escaping the
# sandbox. Missing files produce a clear error rather than crashing the turn.
_ASSET_RE = re.compile(r"\{\{asset:([\w./-]+)\}\}")


def _substitute_assets(template: str) -> str:
    def _replace(m: re.Match[str]) -> str:
        rel = m.group(1)
        candidate = (_ASSETS_DIR / rel).resolve()
        try:
            candidate.relative_to(_ASSETS_DIR.resolve())
        except ValueError:
            logger.warning("slash command asset escape blocked", path=rel)
            return f"[asset {rel} blocked: path traversal]"
        if not candidate.is_file():
            logger.warning("slash command asset missing", path=rel)
            return f"[asset {rel} not found]"
        try:
            return candidate.read_text()
        except OSError as e:
            logger.warning("slash command asset read failed", path=rel, err=str(e))
            return f"[asset {rel} read failed: {e}]"
    return _ASSET_RE.sub(_replace, template)


class SlashCommand(TypedDict):
    slug: str
    name: str
    description: str
    param_hint: str  # e.g. "uid=... app=..." — shown in the dropdown
    prompt_template: str
    is_builtin: bool


BUILTIN_COMMANDS: list[SlashCommand] = [
    {
        "slug": "eod",
        "name": "End of Day Digest",
        "description": "What did I do today? Calendar, todos closed, threads worth replying to.",
        "param_hint": "(no args)",
        "prompt_template": (
            "The user invoked `/eod`. Build an end-of-day digest with three sections:\n"
            "1. **Today's calendar** — every event from `list_today_events`, with brief outcomes if you can infer them.\n"
            "2. **Todos closed today** — query their internal todos list for completions in the last 24h.\n"
            "3. **Threads worth replying to** — recent unread Slack DMs and Gmail threads where the user is on the To: line and hasn't replied. Cap at 5.\n\n"
            "Format as three concise sections with bullet points. Don't add unprompted analysis — "
            "just the facts the user needs to leave their desk cleanly."
        ),
        "is_builtin": True,
    },
    {
        "slug": "research",
        "name": "Research a Person or Company",
        "description": "Exa-driven background research, LinkedIn-aware.",
        "param_hint": "person name | company name | URL",
        "prompt_template": (
            "The user invoked `/research`. Use `web_search` (Exa) to research the named person or company. "
            "For people: include `linkedin.com` in domain filters, prefer recent posts, and surface "
            "current role + company + 2-3 distinctive signals. For companies: surface what they do, "
            "size, recent news, and any public signals about traction or funding. Cap searches at 5. "
            "Return a single concise brief — no preamble, no 'Let me search...' filler."
        ),
        "is_builtin": True,
    },
]


_BUILTIN_BY_SLUG: dict[str, SlashCommand] = {c["slug"]: c for c in BUILTIN_COMMANDS}


# Leading slash + slug; allows trailing args (free-form) on the same line.
_SLASH_RE = re.compile(r"^/([a-z][a-z0-9-]*)\b\s*(.*)$", re.IGNORECASE | re.DOTALL)

# Slug must match the regex above and stay short. Used for both parser AND
# the create endpoint — keeps user-defined slugs invokable.
SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _parse_slash_prefix(message: str) -> Optional[tuple[str, str]]:
    """Return `(slug, args)` if `message` starts with `/<slug>`. Slug is
    lowercased. Does not check whether the slug is registered."""
    if not message:
        return None
    stripped = message.lstrip()
    match = _SLASH_RE.match(stripped)
    if not match:
        return None
    return match.group(1).lower(), match.group(2).strip()


async def _load_user_rows(
    user_id: UUID,
    session: AsyncSession,
) -> list[CustomSlashCommand]:
    """Load this user's custom slash command rows. Tolerates a missing
    `custom_slash_commands` table (migration not yet applied in this env)
    by logging and returning []. Without this guard the manifest endpoint
    500s on every call until the migration runs, which leaves the chat
    dropdown blank for everyone — a much worse failure mode than just
    serving built-ins for a few minutes."""
    try:
        result = await session.execute(
            select(CustomSlashCommand).where(CustomSlashCommand.user_id == user_id)
        )
        return list(result.scalars().all())
    except (ProgrammingError, OperationalError) as e:
        await session.rollback()
        logger.warning(
            "custom_slash_commands table missing — serving built-ins only. "
            "Apply migrations/init.sql to enable user-defined commands.",
            error=str(e)[:200],
        )
        return []


async def list_commands_for_user(
    user_id: UUID,
    session: AsyncSession,
) -> list[SlashCommand]:
    """Return the merged slash command list for this user: built-ins +
    user-defined rows, with user rows overriding built-ins by slug."""
    user_rows = await _load_user_rows(user_id, session)

    by_slug: dict[str, SlashCommand] = {c["slug"]: dict(c) for c in BUILTIN_COMMANDS}  # type: ignore[misc]
    for row in user_rows:
        by_slug[row.slug] = SlashCommand(
            slug=row.slug,
            name=row.name,
            description=row.description or "",
            param_hint=row.param_hint or "",
            prompt_template=row.prompt_template,
            is_builtin=False,
        )
    # Stable order: alphabetic by slug.
    return [by_slug[k] for k in sorted(by_slug.keys())]


async def get_command_for_user(
    user_id: UUID,
    slug: str,
    session: AsyncSession,
) -> Optional[SlashCommand]:
    """Resolve a single command for a user — user-defined wins, falls back
    to built-in. Returns None if neither has a match. Tolerates the table
    being missing the same way `_load_user_rows` does."""
    try:
        result = await session.execute(
            select(CustomSlashCommand).where(
                CustomSlashCommand.user_id == user_id,
                CustomSlashCommand.slug == slug,
            )
        )
        row = result.scalar_one_or_none()
    except (ProgrammingError, OperationalError) as e:
        await session.rollback()
        logger.warning(
            "custom_slash_commands lookup failed — falling back to built-in",
            slug=slug,
            error=str(e)[:200],
        )
        row = None
    if row:
        return SlashCommand(
            slug=row.slug,
            name=row.name,
            description=row.description or "",
            param_hint=row.param_hint or "",
            prompt_template=row.prompt_template,
            is_builtin=False,
        )
    return _BUILTIN_BY_SLUG.get(slug)


async def expand_slash_command(
    message: str,
    user_id: UUID,
    session: AsyncSession,
) -> Optional[str]:
    """If `message` starts with a recognized slash command for this user,
    return the system-prompt addon to append for this turn only.

    User-defined commands take precedence over built-ins of the same slug.
    Returns None if no command matched."""
    parsed = _parse_slash_prefix(message)
    if not parsed:
        return None
    slug, args = parsed
    cmd = await get_command_for_user(user_id, slug, session)
    if not cmd:
        return None
    args_block = f"\nUser-supplied args: `{args}`" if args else "\nNo args supplied."
    template = _substitute_assets(cmd["prompt_template"])
    return (
        f"\n\n## Active Slash Command: /{cmd['slug']} ({cmd['name']})\n"
        f"{template}"
        f"{args_block}"
    )
