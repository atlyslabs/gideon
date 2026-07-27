"""
Internal tools — operate on Gideon's own database, no external integration needed.
Covers: todos, suggestions, memories.
These tools receive a DB session + user_id instead of an OAuth token.
"""

from typing import Any
from uuid import UUID

from datetime import datetime, timezone

from sqlalchemy import select, update, delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from db.models import Todo, TodoSuggestion, Memory, ActivityLog
from tools.base import BaseTool


class InternalTool(BaseTool):
    """Base for internal tools that use DB session instead of OAuth token."""

    @property
    def provider(self) -> str:
        return "internal"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict[str, Any]:
        raise NotImplementedError("Internal tools use execute_internal()")

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict[str, Any]:
        raise NotImplementedError


def _subtask_to_dict(s: Todo) -> dict:
    """Minimal projection for nested subtasks — keeps the LLM payload small.
    Delegation / Slack / snooze fields don't apply to subtasks in the UI, so
    they're omitted here too."""
    return {
        "id": str(s.id),
        "text": s.content,
        "completed": s.completed,
        "priority": s.priority,
    }


class ListTodos(InternalTool):
    name = "list_todos"
    description = (
        "List the user's TOP-LEVEL todo items with their subtasks inlined, plus "
        "delegation context. "
        "Use `filter` to scope: 'active' (default — uncompleted, not snoozed, not delegated outward, not delegated-to-me), "
        "'delegated' (todos the user delegated to others), 'delegated_to_me' (todos others delegated to the user — mirror rows), "
        "'snoozed' (currently snoozed), 'completed', or 'all'. "
        "Each parent row includes a `subtasks` array (child todos under that parent) "
        "and the usual delegation fields when applicable. "
        "Subtasks themselves are NOT returned as their own top-level rows — they only appear nested "
        "under their parent. Filtering applies to the parent's bucket, not its subtasks."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "string",
                "enum": ["active", "delegated", "delegated_to_me", "snoozed", "completed", "all"],
                "description": "Which subset to return. Default 'active'.",
            },
            "show_completed": {
                "type": "boolean",
                "description": "Deprecated. Use filter='completed' or filter='all' instead. Kept for backward compat.",
            },
        },
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        # Map legacy `show_completed=true` to filter='all' for back-compat with
        # older Claude turns / workflows that still send it.
        f = params.get("filter") or ("all" if params.get("show_completed") else "active")
        now = datetime.now(timezone.utc)

        query = (
            select(Todo)
            .options(
                selectinload(Todo.delegated_from_user),
                selectinload(Todo.subtasks),
            )
            .where(Todo.user_id == user_id, Todo.parent_id.is_(None))
            .order_by(Todo.created_at.desc())
        )
        result = await session.execute(query)
        all_todos = result.scalars().unique().all()

        def bucket_of(t: Todo) -> str:
            if t.completed:
                return "completed"
            if t.snoozed_until and t.snoozed_until > now:
                return "snoozed"
            if t.delegated_from_todo_id or t.delegated_from_user_id:
                return "delegated_to_me"
            if t.delegated_to:
                return "delegated"
            return "active"

        if f != "all":
            all_todos = [t for t in all_todos if bucket_of(t) == f]

        todos_out = []
        for t in all_todos:
            delegated_from_name = None
            if t.delegated_from_user_id and t.delegated_from_user:
                delegated_from_name = t.delegated_from_user.name
            subtasks = sorted(t.subtasks or [], key=lambda s: s.created_at or now)
            todos_out.append({
                "id": str(t.id),
                "text": t.content,
                "completed": t.completed,
                "priority": t.priority,
                "source": t.source,
                "bucket": bucket_of(t),
                "delegated_to": t.delegated_to,  # decrypted via EncryptedJSON
                "delegated_from_user_name": delegated_from_name,
                "slack_channel_id": t.slack_channel_id,
                "slack_thread_ts": t.slack_thread_ts,
                "reminder_cadence": t.reminder_cadence,
                "next_reminder_at": t.next_reminder_at.isoformat() if t.next_reminder_at else None,
                "snoozed_until": t.snoozed_until.isoformat() if t.snoozed_until else None,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "subtasks": [_subtask_to_dict(s) for s in subtasks],
            })
        return {
            "todos": todos_out,
            "count": len(todos_out),
            "filter": f,
        }


class CreateTodo(InternalTool):
    name = "create_todo"
    description = (
        "Create a new todo for the user. Three modes:\n"
        "1. Top-level: pass only `text` (optionally `priority` + `subtasks` to create the parent AND its subtasks in one call).\n"
        "2. Subtask under an existing parent: pass `text` + `parent_id` (id from `list_todos`).\n"
        "3. Parent with children in one call: pass `text` + `subtasks` (array of child strings). The parent is created first and each subtask is attached to it.\n\n"
        "Hierarchy is ONE level deep — `parent_id` must point to a top-level todo, and a subtask cannot itself have subtasks. "
        "Don't use this for breaking down a request the user phrased as a single task unless they explicitly asked for substeps."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": "The todo text/description.",
            },
            "priority": {
                "type": "string",
                "enum": ["none", "low", "medium", "high"],
                "description": "Priority level. OMIT this field unless the user explicitly asks for a priority (e.g. 'urgent', 'high priority', 'low priority'). Do not default to 'medium' — leave it unassigned.",
            },
            "parent_id": {
                "type": "string",
                "description": "Optional. If set, the new todo is created as a SUBTASK under this top-level todo id. Cannot be combined with `subtasks`.",
            },
            "subtasks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional. If set, each string becomes a subtask attached to the newly created parent todo. Cannot be combined with `parent_id`. Max 20 subtasks per call.",
            },
        },
        "required": ["text"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        parent_id = params.get("parent_id")
        subtask_texts = params.get("subtasks") or []

        if parent_id and subtask_texts:
            return {"error": "Use either parent_id (create one subtask) or subtasks (create parent + many children), not both."}

        if subtask_texts and len(subtask_texts) > 20:
            return {"error": "At most 20 subtasks per call. Make multiple calls if needed."}

        # Subtask creation under an existing parent.
        if parent_id:
            try:
                parent_uuid = UUID(parent_id)
            except (ValueError, TypeError):
                return {"error": "parent_id must be a valid UUID."}

            result = await session.execute(
                select(Todo).where(Todo.id == parent_uuid, Todo.user_id == user_id)
            )
            parent = result.scalar_one_or_none()
            if not parent:
                return {"error": "Parent todo not found."}
            if parent.parent_id is not None:
                return {"error": "Cannot nest under a subtask — hierarchy is one level deep. Pick a top-level todo as the parent."}
            if parent.completed:
                return {"error": "Cannot add a subtask to a completed todo."}
            # Refuse mirror rows — these belong to the user but are read-only
            # echoes of a delegator's todo. Subtasks added here would never
            # surface in the delegator's hub or get pushed back to the
            # delegation Slack thread, so they'd be invisible work.
            if parent.delegated_from_todo_id is not None or parent.delegated_from_user_id is not None:
                return {"error": "Cannot add a subtask to a todo that was delegated to you — it's a read-only mirror of the delegator's todo. Add your subtasks to one of your own top-level todos instead."}

            todo = Todo(
                user_id=user_id,
                content=params["text"],
                priority=params.get("priority", "none"),
                parent_id=parent_uuid,
            )
            session.add(todo)
            await session.flush()
            await session.refresh(todo)
            return {
                "success": True,
                "todo": {
                    "id": str(todo.id),
                    "text": todo.content,
                    "priority": todo.priority,
                    "parent_id": str(parent_uuid),
                },
                "message": f"Subtask added under '{parent.content}': {todo.content}",
            }

        # Top-level todo, optionally with subtasks.
        parent = Todo(
            user_id=user_id,
            content=params["text"],
            priority=params.get("priority", "none"),
        )
        session.add(parent)
        await session.flush()
        await session.refresh(parent)

        created_subtasks = []
        for child_text in subtask_texts:
            stripped = (child_text or "").strip()
            if not stripped:
                continue
            sub = Todo(
                user_id=user_id,
                content=stripped,
                priority="none",
                parent_id=parent.id,
            )
            session.add(sub)
            created_subtasks.append(sub)
        if created_subtasks:
            await session.flush()
            for s in created_subtasks:
                await session.refresh(s)

        return {
            "success": True,
            "todo": {
                "id": str(parent.id),
                "text": parent.content,
                "priority": parent.priority,
                "subtasks": [
                    {"id": str(s.id), "text": s.content, "priority": s.priority}
                    for s in created_subtasks
                ],
            },
            "message": (
                f"Todo created with {len(created_subtasks)} subtask(s): {parent.content}"
                if created_subtasks else f"Todo created: {parent.content}"
            ),
        }


class CompleteTodo(InternalTool):
    name = "complete_todo"
    description = "Mark a todo as completed. Use when the user says they finished a task or wants to check it off."
    input_schema = {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The ID of the todo to complete.",
            },
            "text_match": {
                "type": "string",
                "description": "If you don't have the ID, provide text to match against todo items.",
            },
        },
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        if params.get("todo_id"):
            result = await session.execute(
                select(Todo).where(Todo.id == params["todo_id"], Todo.user_id == user_id)
            )
            todo = result.scalar_one_or_none()
        elif params.get("text_match"):
            # Content is encrypted; can't use ILIKE. Load user's open todos and filter in Python.
            result = await session.execute(
                select(Todo).where(Todo.user_id == user_id, Todo.completed == False)
            )
            needle = params["text_match"].lower()
            todo = next(
                (t for t in result.scalars() if needle in (t.content or "").lower()),
                None,
            )
        else:
            return {"error": "Provide either todo_id or text_match."}

        if not todo:
            return {"error": "Todo not found."}

        todo.completed = True
        await session.flush()
        return {"success": True, "message": f"Completed: {todo.content}"}


class DeleteTodo(InternalTool):
    name = "delete_todo"
    description = "Delete a todo item. Use when the user wants to remove a task."
    input_schema = {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The ID of the todo to delete.",
            },
            "text_match": {
                "type": "string",
                "description": "If you don't have the ID, provide text to match against todo items.",
            },
        },
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        if params.get("todo_id"):
            result = await session.execute(
                select(Todo).where(Todo.id == params["todo_id"], Todo.user_id == user_id)
            )
            todo = result.scalar_one_or_none()
        elif params.get("text_match"):
            # Content is encrypted; can't use ILIKE. Load user's todos and filter in Python.
            result = await session.execute(
                select(Todo).where(Todo.user_id == user_id)
            )
            needle = params["text_match"].lower()
            todo = next(
                (t for t in result.scalars() if needle in (t.content or "").lower()),
                None,
            )
        else:
            return {"error": "Provide either todo_id or text_match."}

        if not todo:
            return {"error": "Todo not found."}

        text = todo.content
        await session.execute(delete(Todo).where(Todo.id == todo.id))
        return {"success": True, "message": f"Deleted: {text}"}


class DelegateTodo(InternalTool):
    name = "delegate_todo"
    description = (
        "Delegate a todo (or REASSIGN an already-delegated one) to one or more colleagues by opening a Slack group DM. "
        "Use only when the user explicitly asks to delegate / hand off / reassign / send to someone. "
        "`delegatees` is the DESIRED FULL set, not a delta — pass the entire list of who should be on the task. "
        "Adding or removing any name vs the current delegatees triggers a fresh Slack thread with the new group, leaves a 'reassigned' notice in the old thread, and cleans up mirror todos for removed delegatees. "
        "Same `delegatees` set with only cadence / message changed = in-place update (no new DM, no thread message). "
        "Works on both top-level todos AND subtasks (pass the subtask's id). "
        "Each delegatee MUST be a real colleague's name + email — never make up emails. If you don't have the email, call `list_todos` first or ask the user."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todo_id": {
                "type": "string",
                "description": "The ID of the todo (or subtask) to delegate. Get this from `list_todos`.",
            },
            "delegatees": {
                "type": "array",
                "description": "Full desired delegatee set (not a delta). Each entry: {name, email, avatar_url?}.",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "Display name (e.g. 'Alex Kim')."},
                        "email": {"type": "string", "description": "Work email of the colleague."},
                        "avatar_url": {"type": "string", "description": "Optional profile image URL."},
                    },
                    "required": ["name", "email"],
                },
                "minItems": 1,
            },
            "message": {
                "type": "string",
                "description": "Optional short note included in the Slack DM body and timeline entry.",
            },
            "reminder_cadence": {
                "type": "string",
                "enum": ["never", "daily", "every_2_days", "weekly"],
                "description": "Optional recurring nudge cadence in the Slack thread. Default 'never'.",
            },
            "follow_up_at": {
                "type": "string",
                "description": "Optional ISO datetime to anchor the FIRST reminder (e.g. '2026-05-25T10:00:00Z'). Useful when the user says 'remind them tomorrow at 10am'.",
            },
        },
        "required": ["todo_id", "delegatees"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        # Local import keeps tool_registry import cycle-free.
        from services.delegation_service import DelegationError, delegate_or_reassign

        try:
            todo_uuid = UUID(params["todo_id"])
        except (ValueError, TypeError):
            return {"error": "todo_id must be a valid UUID."}

        delegatees = params.get("delegatees") or []
        if not delegatees:
            return {"error": "At least one delegatee required."}

        # delegate_or_reassign manages its own short-lived DB sessions so
        # the caller's session isn't held across the Slack roundtrip.
        try:
            outcome = await delegate_or_reassign(
                user_id=user_id,
                todo_id=todo_uuid,
                delegatees=delegatees,
                message=params.get("message"),
                reminder_cadence=params.get("reminder_cadence"),
                follow_up_at_iso=params.get("follow_up_at"),
            )
        except DelegationError as e:
            return {"error": e.detail, "status_code": e.status_code}

        # Compose a human-readable summary for the model so it can speak to
        # the user without needing to re-parse the structured payload.
        names = ", ".join(d.name for d in outcome.delegated_to) or "(no one)"
        msg = f"Delegated/reassigned to {names}."
        if outcome.not_on_gideon:
            missing = ", ".join(d.name for d in outcome.not_on_gideon)
            msg += f" Note: {missing} got the Slack DM but isn't on Gideon yet, so no mirror todo was created in their hub."
        return {
            "success": True,
            "status": outcome.status,
            "delegated_to": [
                {"name": d.name, "email": d.email, "avatar_url": d.avatar_url}
                for d in outcome.delegated_to
            ],
            "not_on_gideon": [
                {"name": d.name, "email": d.email, "avatar_url": d.avatar_url}
                for d in outcome.not_on_gideon
            ],
            "message": msg,
        }


# ---------------------------------------------------------------------------
# Memory tools
# ---------------------------------------------------------------------------


class SaveMemory(InternalTool):
    name = "save_memory"
    description = (
        "Save a fact, preference, or piece of information about the user for future reference. "
        "Use this when the user states a preference, shares a fact about themselves, mentions something about a person or project, or corrects you. "
        "Gideon will remember this across conversations. Powered by semantic memory — deduplication is automatic."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": "The fact or preference to remember. Be concise but complete.",
            },
        },
        "required": ["content"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        content = params["content"].strip()
        if not content:
            return {"error": "Content cannot be empty."}

        # Try Graphiti first
        from services.graphiti_service import save_memory as mem0_save
        result = await mem0_save(str(user_id), content)
        if result is not None:
            from services.events_service import track_memory_created
            track_memory_created(str(user_id), source="tool")
            return {"success": True, "message": f"Remembered: {content}"}

        # Fallback: save to DB
        memory = Memory(user_id=user_id, content=content, category="general", source="tool")
        session.add(memory)
        await session.flush()
        from services.events_service import track_memory_created
        track_memory_created(str(user_id), source="tool")
        return {"success": True, "message": f"Remembered: {content}"}


class RecallMemories(InternalTool):
    name = "recall_memories"
    description = (
        "Search through saved memories using semantic similarity. "
        "Use this when past context about the user, their preferences, people they know, or projects might help answer their question."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What you want to recall. Uses semantic search — doesn't need exact keywords.",
            },
            "limit": {
                "type": "integer",
                "description": "Max number of results. Default 10.",
            },
        },
        "required": ["query"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        query = params["query"].strip()
        limit = min(params.get("limit", 10), 30)
        if not query:
            return {"error": "Query cannot be empty."}

        # Try mem0 first
        from services.graphiti_service import search_memories as mem0_search
        memories = await mem0_search(str(user_id), query, limit=limit)
        if memories:
            return {"memories": memories, "count": len(memories)}

        # Fallback: DB keyword search
        from sqlalchemy import or_
        words = query.split()
        stmt = select(Memory).where(Memory.user_id == user_id, Memory.is_active == True)
        stmt = stmt.where(or_(*[Memory.content.ilike(f"%{w}%") for w in words]))
        stmt = stmt.order_by(Memory.updated_at.desc()).limit(limit)
        result = await session.execute(stmt)
        db_memories = result.scalars().all()
        return {
            "memories": [{"content": m.content, "score": 0} for m in db_memories],
            "count": len(db_memories),
        }


class ListMemories(InternalTool):
    name = "list_memories"
    description = "List all saved memories for this user."
    input_schema = {
        "type": "object",
        "properties": {},
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        # Try mem0 first
        from services.graphiti_service import get_all_memories as mem0_list
        memories = await mem0_list(str(user_id))
        if memories:
            return {"memories": memories, "count": len(memories)}

        # Fallback: DB
        stmt = select(Memory).where(Memory.user_id == user_id, Memory.is_active == True)
        stmt = stmt.order_by(Memory.created_at.desc())
        result = await session.execute(stmt)
        db_memories = result.scalars().all()
        return {
            "memories": [{"id": str(m.id), "content": m.content, "created_at": m.created_at.isoformat() if m.created_at else ""} for m in db_memories],
            "count": len(db_memories),
        }


class DeleteMemory(InternalTool):
    name = "delete_memory"
    description = "Delete a memory by ID. Use when the user wants to forget something."
    input_schema = {
        "type": "object",
        "properties": {
            "memory_id": {
                "type": "string",
                "description": "The ID of the memory to delete.",
            },
        },
        "required": ["memory_id"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        # Try mem0 first
        from services.graphiti_service import delete_memory as mem0_delete
        success = await mem0_delete(params["memory_id"], str(user_id))
        if success:
            from services.events_service import track_memory_deleted
            track_memory_deleted(str(user_id))
            return {"success": True, "message": "Memory deleted."}

        # Fallback: DB soft-delete
        result = await session.execute(
            select(Memory).where(Memory.id == params["memory_id"], Memory.user_id == user_id, Memory.is_active == True)
        )
        memory = result.scalar_one_or_none()
        if memory:
            memory.is_active = False
            await session.flush()
            from services.events_service import track_memory_deleted
            track_memory_deleted(str(user_id))
            return {"success": True, "message": "Memory deleted."}
        return {"error": "Memory not found."}


# ---------------------------------------------------------------------------
# Activity search tool
# ---------------------------------------------------------------------------


class SearchActivity(InternalTool):
    name = "search_activity"
    description = (
        "Search the user's recent activity across all connected tools (email, Slack, calendar, Linear, GitHub). "
        "Use this when the user asks about past events, meetings, messages, or anything that happened recently."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query",
            },
            "source": {
                "type": "string",
                "description": "Optional: filter by source",
                "enum": ["gmail", "slack", "calendar", "linear", "github"],
            },
            "days": {
                "type": "integer",
                "description": "How many days back to search (default 7)",
                "default": 7,
            },
        },
        "required": ["query"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        from datetime import datetime, timedelta, timezone
        from sqlalchemy import or_

        query_text = params["query"].strip()
        days = min(params.get("days", 7), 30)
        source_filter = params.get("source")
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        # 1. Search topics for contextual matches
        topic_results = []
        try:
            from services.topic_service import get_topic_context
            topic_results = await get_topic_context(str(user_id), query_text, session)
        except Exception:
            pass

        # 2. Direct keyword match on title/summary — encrypted, so load recent + Python filter
        stmt = (
            select(ActivityLog)
            .where(
                ActivityLog.user_id == user_id,
                ActivityLog.created_at >= cutoff,
            )
        )
        if source_filter:
            stmt = stmt.where(ActivityLog.source == source_filter)
        stmt = stmt.order_by(ActivityLog.created_at.desc()).limit(500)

        result = await session.execute(stmt)
        all_activities = result.scalars().all()
        needle = query_text.lower()
        activities = [
            a for a in all_activities
            if needle in (a.title or "").lower() or needle in (a.summary or "").lower()
        ][:20]

        # Combine results — dedup by activity title
        seen_titles = set()
        formatted = []

        for a in activities:
            key = f"{a.source}:{a.reference_id}"
            if key in seen_titles:
                continue
            seen_titles.add(key)
            formatted.append({
                "source": a.source,
                "event_type": a.event_type,
                "title": a.title,
                "summary": a.summary,
                "created_at": a.created_at.isoformat() if a.created_at else "",
                "metadata": a.extra_data or {},
            })

        # Add topic-based results that weren't in direct search
        for topic in topic_results:
            for ta in topic.get("activities", []):
                key = f"{ta['source']}:{ta['title']}"
                if key not in seen_titles:
                    seen_titles.add(key)
                    formatted.append({
                        "source": ta["source"],
                        "event_type": ta["event_type"],
                        "title": ta["title"],
                        "summary": ta.get("summary"),
                        "created_at": ta.get("created_at", ""),
                        "topic": topic["title"],
                    })

        return {
            "activities": formatted[:30],
            "count": len(formatted[:30]),
            "topics": [
                {"title": t["title"], "activity_count": t["activity_count"]}
                for t in topic_results
            ],
        }
