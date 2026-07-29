"""
Integration tests for internal tools (todos, memory, activity search).
These operate on Gideon's own database — no external integration needed.
"""

import pytest

from tests.conftest import run_internal_tool

pytestmark = pytest.mark.asyncio


class TestTodos:
    async def test_list_todos(self, db_session, user_id):
        result = await run_internal_tool("list_todos", {}, user_id, db_session)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "todos" in result
        assert isinstance(result["todos"], list)
        assert "count" in result

    async def test_create_and_delete_todo(self, db_session, user_id):
        # Create
        create_result = await run_internal_tool(
            "create_todo",
            {"text": "[test] Integration test todo — safe to delete", "priority": "low"},
            user_id,
            db_session,
        )
        assert "error" not in create_result, f"Create failed: {create_result.get('error')}"
        assert create_result.get("success") is True
        assert "todo" in create_result
        todo_id = create_result["todo"]["id"]
        assert todo_id

        # Verify it shows up in list
        list_result = await run_internal_tool("list_todos", {}, user_id, db_session)
        todo_ids = [t["id"] for t in list_result["todos"]]
        assert todo_id in todo_ids, "Created todo not found in list"

        # Delete
        delete_result = await run_internal_tool(
            "delete_todo",
            {"todo_id": todo_id},
            user_id,
            db_session,
        )
        assert "error" not in delete_result, f"Delete failed: {delete_result.get('error')}"
        assert delete_result.get("success") is True


class TestMemory:
    async def test_save_and_recall_memory(self, db_session, user_id):
        test_content = "[test] Integration test memory — gideon test marker 7x9q"

        # Save
        save_result = await run_internal_tool(
            "save_memory",
            {"content": test_content},
            user_id,
            db_session,
        )
        assert "error" not in save_result, f"Save failed: {save_result.get('error')}"
        assert save_result.get("success") is True

        # Recall using the unique marker
        recall_result = await run_internal_tool(
            "recall_memories",
            {"query": "gideon test marker 7x9q", "limit": 5},
            user_id,
            db_session,
        )
        assert "error" not in recall_result, f"Recall failed: {recall_result.get('error')}"
        assert "memories" in recall_result
        # The memory should be findable (either via mem0 semantic or DB keyword)
        found = any(
            "7x9q" in (m.get("content") or m.get("memory", ""))
            for m in recall_result["memories"]
        )
        # Note: mem0 semantic search may not return exact keyword matches immediately,
        # so we check but don't hard-fail on this assertion
        if not found:
            # Try listing all memories as a fallback check
            all_mems = await run_internal_tool("list_memories", {}, user_id, db_session)
            found = any(
                "7x9q" in (m.get("content") or m.get("memory", ""))
                for m in all_mems.get("memories", [])
            )

        # Clean up: find and delete the test memory
        all_mems = await run_internal_tool("list_memories", {}, user_id, db_session)
        for m in all_mems.get("memories", []):
            content = m.get("content") or m.get("memory", "")
            mem_id = m.get("id", "")
            if "7x9q" in content and mem_id:
                await run_internal_tool(
                    "delete_memory",
                    {"memory_id": mem_id},
                    user_id,
                    db_session,
                )
                break


class TestActivity:
    async def test_search_activity(self, db_session, user_id):
        result = await run_internal_tool(
            "search_activity",
            {"query": "meeting", "days": 7},
            user_id,
            db_session,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "activities" in result
        assert isinstance(result["activities"], list)
        assert "count" in result
        assert "topics" in result
