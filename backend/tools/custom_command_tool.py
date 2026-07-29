"""
Custom slash command tools — let Claude create and edit a user's own slash
commands (`/<slug>`) from chat.

A custom slash command is an ON-DEMAND, reusable prompt the user triggers by
typing `/<slug>` in chat. It is NOT scheduled — that's a workflow. Use these
tools when the user asks for "a command I can run", "a /slash command", or "a
reusable prompt I trigger on demand".

Rows live in `custom_slash_commands`; name/description/param_hint/prompt_template
are Fernet-encrypted via the ORM model, so they MUST be written through the
CustomSlashCommand model (never raw SQL). Slug uniqueness is per-user
(`uq_custom_slash_commands_user_slug`).
"""

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from db.models import CustomSlashCommand
from services.slash_commands import SLUG_RE
from tools.internal_tools import InternalTool

logger = structlog.get_logger()


class CreateCustomCommand(InternalTool):
    name = "create_custom_command"
    description = (
        "Create a reusable slash command for the user — an ON-DEMAND prompt they "
        "trigger by typing `/<slug>` in chat. Use this when the user asks for 'a "
        "command I can run', 'a /slash command', or 'a reusable prompt I trigger on "
        "demand'. This is NOT scheduled — if the user wants something to run "
        "automatically on a schedule, use create_workflow instead. "
        "The prompt_template is the instruction block injected when the command is "
        "invoked; it can reference '{{asset:relative/path}}' to inline bundled assets."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": (
                    "The /name the user types to invoke this command, without the "
                    "leading slash. Must start with a lowercase letter and contain "
                    "only lowercase letters, digits, and hyphens (max 64 chars), "
                    "e.g. 'standup', 'weekly-recap'."
                ),
            },
            "name": {
                "type": "string",
                "description": "Short human-readable name shown in the command dropdown.",
            },
            "description": {
                "type": "string",
                "description": "One-line description of what the command does (shown in the dropdown).",
            },
            "param_hint": {
                "type": "string",
                "description": "Optional hint for any args the command expects, e.g. 'person name | company'.",
            },
            "prompt_template": {
                "type": "string",
                "description": (
                    "The instruction block injected into the system prompt when the "
                    "user invokes this command. Write it as directions to the AI."
                ),
            },
        },
        "required": ["slug", "name", "prompt_template"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        slug = str(params.get("slug", "")).strip().lower()
        if not SLUG_RE.match(slug):
            return {
                "error": (
                    "Invalid slug. It must start with a lowercase letter and contain "
                    "only lowercase letters, digits, and hyphens (max 64 chars)."
                )
            }

        name = str(params.get("name", "")).strip()
        if not name:
            return {"error": "A name is required."}

        template = str(params.get("prompt_template", "")).strip()
        if not template:
            return {"error": "A prompt_template is required."}

        description = str(params.get("description", "")).strip() or None
        param_hint = str(params.get("param_hint", "")).strip() or None

        row = CustomSlashCommand(
            user_id=user_id,
            slug=slug,
            name=name,
            description=description,
            param_hint=param_hint,
            prompt_template=template,
        )
        # Per-user slug uniqueness is enforced by the DB constraint. Do the
        # add + flush INSIDE the savepoint so a duplicate-slug IntegrityError is
        # fully contained: on rollback the pending row is discarded with the
        # savepoint, leaving the outer transaction clean. (Adding before the
        # savepoint leaves `row` pending and re-fails at the next outer commit.)
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            return {"error": f"You already have a command with slug '/{slug}'. Use update_custom_command to edit it."}

        await session.refresh(row)
        logger.info("Custom command created via chat", slug=slug, user_id=str(user_id))

        return {
            "success": True,
            "command": {
                "id": str(row.id),
                "slug": row.slug,
                "name": row.name,
                "description": row.description,
                "param_hint": row.param_hint,
            },
            "message": f"Command created. The user can now type /{slug} in chat to run it.",
        }


class UpdateCustomCommand(InternalTool):
    name = "update_custom_command"
    description = (
        "Edit an EXISTING custom slash command the user owns. Use this when the user "
        "wants to change what a /<slug> command does — its name, description, "
        "param_hint, or prompt_template. The slug is immutable (chat history "
        "references it), so it cannot be changed. Only the fields you pass are "
        "updated. Identify the command by its slug (or id)."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "The slug of the command to edit (without the leading slash). Provide this or 'id'.",
            },
            "id": {
                "type": "string",
                "description": "The UUID of the command to edit. Alternative to 'slug'.",
            },
            "name": {
                "type": "string",
                "description": "New human-readable name.",
            },
            "description": {
                "type": "string",
                "description": "New one-line description.",
            },
            "param_hint": {
                "type": "string",
                "description": "New args hint.",
            },
            "prompt_template": {
                "type": "string",
                "description": "New instruction block injected when the command is invoked.",
            },
        },
        "required": [],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        command_id = params.get("id")
        slug = params.get("slug")
        if not command_id and not slug:
            return {"error": "Provide either 'slug' or 'id' to identify the command."}

        query = select(CustomSlashCommand).where(CustomSlashCommand.user_id == user_id)
        if command_id:
            try:
                query = query.where(CustomSlashCommand.id == UUID(str(command_id)))
            except (ValueError, AttributeError):
                return {"error": "Custom command not found."}
        else:
            query = query.where(CustomSlashCommand.slug == str(slug).strip().lower())

        result = await session.execute(query)
        row = result.scalar_one_or_none()
        if not row:
            return {"error": "Custom command not found."}

        if "name" in params and params["name"] is not None:
            new_name = str(params["name"]).strip()
            if not new_name:
                return {"error": "Name cannot be empty."}
            row.name = new_name
        if "description" in params and params["description"] is not None:
            row.description = str(params["description"]).strip() or None
        if "param_hint" in params and params["param_hint"] is not None:
            row.param_hint = str(params["param_hint"]).strip() or None
        if "prompt_template" in params and params["prompt_template"] is not None:
            new_template = str(params["prompt_template"]).strip()
            if not new_template:
                return {"error": "Prompt template cannot be empty."}
            row.prompt_template = new_template

        await session.flush()
        await session.refresh(row)
        logger.info("Custom command updated via chat", slug=row.slug, user_id=str(user_id))

        return {
            "success": True,
            "command": {
                "id": str(row.id),
                "slug": row.slug,
                "name": row.name,
                "description": row.description,
                "param_hint": row.param_hint,
            },
            "message": f"Command /{row.slug} updated.",
        }
