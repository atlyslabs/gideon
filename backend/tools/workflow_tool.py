"""
Workflow tools — lets Claude manage workflows from chat.
Create, list, activate/deactivate, and manually run workflows.
"""

import asyncio
import re
from datetime import date
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from db.models import Workflow
from routers.workflows_router import validate_workflow_fields
from tools.internal_tools import InternalTool

logger = structlog.get_logger()

_DOW_LABELS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}

# Runs take ~30-90s and cost tokens, so a sub-5-minute cadence would overlap.
_MIN_MINUTE_INTERVAL = 5


def _schedule_phrase(interval: int, unit: str, time: str | None, dow: str | None) -> str:
    """Human-readable cadence. Sub-daily (minute/hour) units have no time-of-day."""
    every = f"every {interval} {unit}{'s' if interval != 1 else ''}"
    if unit in ("minute", "hour"):
        return every
    phrase = every
    if unit == "week" and dow:
        phrase += f" on {_DOW_LABELS.get(dow, dow)}"
    return f"{phrase} at {time or '09:00'}"


class CreateWorkflow(InternalTool):
    name = "create_workflow"
    description = (
        "Create an automated workflow for the user. Use this when the user wants to "
        "automate a repetitive task (e.g. 'every morning summarize my emails', "
        "'check Linear issues every Friday'). The workflow will run on a schedule "
        "and execute the instructions using the user's connected tools. "
        "Workflows created here are active by default; mention that the user can "
        "pause or edit them from the Workflows page."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Short name for the workflow (under 50 chars).",
            },
            "prompt": {
                "type": "string",
                "description": "Detailed instructions for the AI agent that will execute this workflow. Be specific about what to read, what to do, and what output to produce.",
            },
            "schedule_unit": {
                "type": "string",
                "enum": ["minute", "hour", "day", "week"],
                "description": (
                    "Cadence unit. Use 'minute'/'hour' to repeat through the day "
                    "(e.g. 'every 30 minutes', 'every 2 hours') — these ignore "
                    "schedule_time. Use 'day'/'week' to run once per day/week at a "
                    "fixed schedule_time. Default 'day'."
                ),
            },
            "schedule_interval": {
                "type": "integer",
                "description": "Run every N units (e.g. 30 with unit 'minute' = every 30 min). Default 1. Minimum 5 for 'minute'.",
            },
            "schedule_time": {
                "type": "string",
                "description": "Time to run in HH:MM 24h format. Used only for 'day'/'week' units; ignored for 'minute'/'hour'. Default '09:00'.",
            },
            "schedule_day_of_week": {
                "type": "string",
                "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                "description": "Day of week for weekly workflows. Only used when schedule_unit is 'week'.",
            },
        },
        "required": ["name", "prompt"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        name = params.get("name", "Custom Workflow")[:255]
        prompt = params.get("prompt", "")
        if not prompt:
            return {"error": "Workflow prompt is required."}

        schedule_unit = params.get("schedule_unit", "day")
        if schedule_unit not in ("minute", "hour", "day", "week"):
            schedule_unit = "day"

        try:
            schedule_interval = max(1, int(params.get("schedule_interval", 1)))
        except (ValueError, TypeError):
            schedule_interval = 1
        # Clamp sub-5-minute cadences up to the floor (runs would overlap below it).
        if schedule_unit == "minute":
            schedule_interval = max(_MIN_MINUTE_INTERVAL, schedule_interval)

        schedule_time = params.get("schedule_time", "09:00")
        if not isinstance(schedule_time, str) or not re.match(r"^\d{2}:\d{2}$", schedule_time):
            schedule_time = "09:00"
        else:
            h, m = int(schedule_time[:2]), int(schedule_time[3:])
            if not (0 <= h < 24 and 0 <= m < 60):
                schedule_time = "09:00"

        schedule_dow = params.get("schedule_day_of_week")
        if schedule_dow and schedule_dow not in ("MO", "TU", "WE", "TH", "FR", "SA", "SU"):
            schedule_dow = None

        # Detect integrations mentioned in the prompt
        integration_keywords = {
            "google": ["email", "gmail", "calendar", "drive", "sheets", "docs"],
            "slack": ["slack", "channel", "message"],
            "notion": ["notion", "page", "database"],
            "linear": ["linear", "issue", "ticket"],
            "github": ["github", "repo", "pull request", "pr"],
        }
        prompt_lower = prompt.lower()
        integrations_used = [
            provider for provider, keywords in integration_keywords.items()
            if any(kw in prompt_lower for kw in keywords)
        ]

        workflow = Workflow(
            user_id=user_id,
            name=name,
            prompt=prompt,
            when_type="schedule",
            schedule_start_date=date.today(),
            schedule_interval=schedule_interval,
            schedule_unit=schedule_unit,
            schedule_day_of_week=schedule_dow if schedule_unit == "week" else None,
            schedule_time=schedule_time,
            integrations_used=integrations_used,
            status="active",
        )
        session.add(workflow)
        await session.flush()
        await session.refresh(workflow)

        logger.info("Workflow created via chat", name=name, workflow_id=str(workflow.id), user_id=str(user_id))

        phrase = _schedule_phrase(schedule_interval, schedule_unit, schedule_time, schedule_dow)
        return {
            "success": True,
            "workflow_id": str(workflow.id),
            "name": name,
            "schedule": phrase,
            "status": "active",
            "message": f"Workflow '{name}' created and activated. It will run {phrase} until the user pauses it.",
        }


def _format_workflow(wf: Workflow) -> dict:
    schedule = _schedule_phrase(
        wf.schedule_interval or 1,
        wf.schedule_unit or "day",
        wf.schedule_time or "09:00",
        wf.schedule_day_of_week,
    )
    return {
        "id": str(wf.id),
        "name": wf.name,
        "status": wf.status or "inactive",
        "schedule": schedule,
        "prompt_preview": (wf.prompt or "")[:120] + ("..." if wf.prompt and len(wf.prompt) > 120 else ""),
    }


class ListWorkflows(InternalTool):
    name = "list_workflows"
    description = (
        "List the user's workflows. Use this to check what workflows exist, "
        "find a workflow by name, or answer questions like 'what workflows do I have?'"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "status_filter": {
                "type": "string",
                "enum": ["all", "active", "inactive"],
                "description": "Filter by status. Default 'all'.",
            },
        },
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        query = select(Workflow).where(Workflow.user_id == user_id).order_by(Workflow.created_at.desc())
        status_filter = params.get("status_filter", "all")
        if status_filter == "active":
            query = query.where(Workflow.status == "active")
        elif status_filter == "inactive":
            query = query.where(Workflow.status == "inactive")

        result = await session.execute(query)
        workflows = result.scalars().all()

        return {
            "workflows": [_format_workflow(wf) for wf in workflows],
            "count": len(workflows),
        }


class ActivateWorkflow(InternalTool):
    name = "activate_workflow"
    description = (
        "Activate or deactivate a workflow by its ID. Use this when the user asks to "
        "turn on, enable, pause, or disable a workflow. Use list_workflows first to "
        "find the workflow ID if needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "The UUID of the workflow to activate or deactivate.",
            },
            "active": {
                "type": "boolean",
                "description": "True to activate, false to deactivate.",
            },
        },
        "required": ["workflow_id", "active"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        workflow_id = params.get("workflow_id", "")
        active = params.get("active", True)

        try:
            wf_uuid = UUID(workflow_id)
        except (ValueError, AttributeError):
            return {"error": "Workflow not found."}

        result = await session.execute(
            select(Workflow).where(Workflow.id == wf_uuid, Workflow.user_id == user_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return {"error": "Workflow not found."}

        new_status = "active" if active else "inactive"
        workflow.status = new_status
        await session.flush()

        action = "activated" if active else "deactivated"
        logger.info("Workflow status changed via chat", action=action, workflow_id=workflow_id, user_id=str(user_id))

        return {
            "success": True,
            "workflow_id": workflow_id,
            "name": workflow.name,
            "status": new_status,
            "message": f"Workflow '{workflow.name}' {action}.",
        }


class UpdateWorkflow(InternalTool):
    name = "update_workflow"
    description = (
        "Edit an EXISTING workflow the user owns. Use this when the user wants to "
        "change a workflow's instructions, name, schedule, or status — e.g. 'update "
        "my email summary to also include Slack', 'change the Friday report to run "
        "at 8am', 'rename my standup workflow'. "
        "Do NOT use create_workflow to edit an existing workflow — that creates a "
        "duplicate. Use list_workflows first to find the workflow_id if needed. "
        "Only the fields you pass are changed; everything else is left as-is."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "The UUID of the workflow to update.",
            },
            "name": {
                "type": "string",
                "description": "New name for the workflow (under 255 chars).",
            },
            "description": {
                "type": "string",
                "description": "New one-line description.",
            },
            "prompt": {
                "type": "string",
                "description": "New instructions for the AI agent that executes this workflow.",
            },
            "schedule_unit": {
                "type": "string",
                "enum": ["minute", "hour", "day", "week"],
                "description": (
                    "Cadence unit. 'minute'/'hour' repeat through the day and ignore "
                    "schedule_time; 'day'/'week' run once at a fixed schedule_time."
                ),
            },
            "schedule_interval": {
                "type": "integer",
                "description": "Run every N units. Minimum 5 for 'minute'.",
            },
            "schedule_time": {
                "type": "string",
                "description": "Time to run in HH:MM 24h format (only for 'day'/'week' units).",
            },
            "schedule_day_of_week": {
                "type": "string",
                "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"],
                "description": "Day of week for weekly workflows. Only used when schedule_unit is 'week'.",
            },
            "schedule_days_of_week": {
                "type": "array",
                "items": {"type": "string", "enum": ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]},
                "description": (
                    "Restrict a daily/sub-daily schedule to specific weekdays "
                    "(e.g. ['MO','TU','WE','TH','FR'] for weekdays only). Pass an empty "
                    "list to clear the restriction."
                ),
            },
            "status": {
                "type": "string",
                "enum": ["active", "inactive"],
                "description": "Set the workflow active (running on schedule) or inactive (paused).",
            },
        },
        "required": ["workflow_id"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        workflow_id = params.get("workflow_id", "")
        try:
            wf_uuid = UUID(workflow_id)
        except (ValueError, AttributeError):
            return {"error": "Workflow not found."}

        result = await session.execute(
            select(Workflow).where(Workflow.id == wf_uuid, Workflow.user_id == user_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return {"error": "Workflow not found."}

        status = params.get("status")
        if status is not None and status not in ("active", "inactive"):
            return {"error": "Invalid status. Must be 'active' or 'inactive'."}

        # Validate the EFFECTIVE post-update unit+interval (merge body over the
        # current row) so a partial update can't slip past the minute floor.
        effective_unit = params["schedule_unit"] if "schedule_unit" in params else workflow.schedule_unit
        # Coerce the interval to int BEFORE validation. validate_workflow_fields
        # compares `schedule_interval < MIN_MINUTE_INTERVAL` for minute schedules,
        # which raises an uncaught TypeError on a non-int string — so parse here
        # and return a friendly error instead of crashing. The parsed value is
        # reused for the assignment below.
        interval_provided = "schedule_interval" in params and params["schedule_interval"] is not None
        if interval_provided:
            try:
                effective_interval = int(params["schedule_interval"])
            except (TypeError, ValueError):
                return {"error": "schedule_interval must be a positive integer (e.g. 1, 2, 30)."}
        else:
            effective_interval = workflow.schedule_interval
        try:
            validate_workflow_fields(
                schedule_unit=effective_unit,
                schedule_interval=effective_interval,
                schedule_day_of_week=params.get("schedule_day_of_week"),
                schedule_days_of_week=params.get("schedule_days_of_week"),
                schedule_time=params.get("schedule_time"),
            )
        except HTTPException as e:
            return {"error": str(e.detail)}

        if "name" in params and params["name"]:
            workflow.name = str(params["name"])[:255]
        if "description" in params:
            workflow.description = params["description"]
        if "prompt" in params and params["prompt"] is not None:
            workflow.prompt = params["prompt"]
        if "schedule_unit" in params and params["schedule_unit"] is not None:
            workflow.schedule_unit = params["schedule_unit"]
        if interval_provided:
            workflow.schedule_interval = max(1, effective_interval)
        if "schedule_time" in params and params["schedule_time"] is not None:
            workflow.schedule_time = params["schedule_time"]
        if "schedule_day_of_week" in params and params["schedule_day_of_week"] is not None:
            workflow.schedule_day_of_week = params["schedule_day_of_week"]
        if "schedule_days_of_week" in params:
            workflow.schedule_days_of_week = params["schedule_days_of_week"]
        if status is not None:
            workflow.status = status

        # Re-clamp the minute floor on the final stored interval.
        if workflow.schedule_unit == "minute":
            workflow.schedule_interval = max(_MIN_MINUTE_INTERVAL, workflow.schedule_interval or 1)

        await session.flush()
        await session.refresh(workflow)

        logger.info("Workflow updated via chat", workflow_id=workflow_id, user_id=str(user_id))

        summary = _format_workflow(workflow)
        return {
            "success": True,
            "workflow": summary,
            "message": f"Workflow '{workflow.name}' updated.",
        }


class DeactivateWorkflow(InternalTool):
    name = "deactivate_workflow"
    description = (
        "Pause a workflow by setting its status to inactive, so it stops running on "
        "schedule. Use this when the user says 'pause', 'turn off', 'stop', or "
        "'disable' a workflow. Idempotent — pausing an already-paused workflow is a "
        "no-op. Use list_workflows first to find the ID if needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "The UUID of the workflow to deactivate.",
            },
        },
        "required": ["workflow_id"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        workflow_id = params.get("workflow_id", "")
        try:
            wf_uuid = UUID(workflow_id)
        except (ValueError, AttributeError):
            return {"error": "Workflow not found."}

        result = await session.execute(
            select(Workflow).where(Workflow.id == wf_uuid, Workflow.user_id == user_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return {"error": "Workflow not found."}

        already_inactive = workflow.status == "inactive"
        workflow.status = "inactive"
        await session.flush()

        logger.info("Workflow deactivated via chat", workflow_id=workflow_id, user_id=str(user_id))

        message = (
            f"Workflow '{workflow.name}' is already paused."
            if already_inactive
            else f"Workflow '{workflow.name}' paused."
        )
        return {
            "success": True,
            "workflow_id": workflow_id,
            "name": workflow.name,
            "status": "inactive",
            "message": message,
        }


async def _run_workflow_in_background(workflow_id: UUID, user_id: UUID) -> None:
    """Execute a workflow in a background session."""
    # Lazy imports to avoid circular: tool_registry -> workflow_tool -> workflow_execution_service -> tool_executor -> tool_registry
    from db.database import session_manager
    from services.workflow_execution_service import execute_workflow

    async with session_manager.get_session() as bg_session:
        try:
            await execute_workflow(workflow_id, user_id, bg_session)
        except Exception as e:
            logger.error("Background workflow run failed", workflow_id=str(workflow_id), error=str(e))


class RunWorkflow(InternalTool):
    name = "run_workflow"
    description = (
        "Manually trigger a workflow to run right now. Use this when the user says "
        "'run my email summary now' or 'execute the Friday report'. The workflow "
        "runs in the background. Use list_workflows first to find the ID if needed."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "workflow_id": {
                "type": "string",
                "description": "The UUID of the workflow to run.",
            },
        },
        "required": ["workflow_id"],
    }

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        workflow_id = params.get("workflow_id", "")

        try:
            wf_uuid = UUID(workflow_id)
        except (ValueError, AttributeError):
            return {"error": "Workflow not found."}

        result = await session.execute(
            select(Workflow).where(Workflow.id == wf_uuid, Workflow.user_id == user_id)
        )
        workflow = result.scalar_one_or_none()
        if not workflow:
            return {"error": "Workflow not found."}

        # Run in background so the chat isn't blocked
        asyncio.create_task(_run_workflow_in_background(wf_uuid, user_id))

        logger.info("Workflow triggered via chat", workflow_id=workflow_id, user_id=str(user_id))

        return {
            "success": True,
            "workflow_id": workflow_id,
            "name": workflow.name,
            "message": f"Workflow '{workflow.name}' is now running in the background. Check the Workflows page for results.",
        }
