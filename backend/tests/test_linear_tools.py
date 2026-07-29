"""
Integration tests for Linear tools.
Requires the test user to have a connected Linear integration.
"""

import pytest

from tests.conftest import run_tool

pytestmark = pytest.mark.asyncio


class TestLinear:
    async def test_list_linear_teams(self, linear_token):
        if not linear_token:
            pytest.skip("No Linear integration connected")
        result = await run_tool("list_linear_teams", {}, linear_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "teams" in result
        assert isinstance(result["teams"], list)
        assert "count" in result
        if result["count"] > 0:
            team = result["teams"][0]
            assert "id" in team
            assert "name" in team
            assert "key" in team

    async def test_list_linear_issues(self, linear_token):
        if not linear_token:
            pytest.skip("No Linear integration connected")
        result = await run_tool("list_linear_issues", {"limit": 5}, linear_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "issues" in result
        assert isinstance(result["issues"], list)
        assert "count" in result
        if result["count"] > 0:
            issue = result["issues"][0]
            assert "id" in issue
            assert "identifier" in issue
            assert "title" in issue
            assert "status" in issue

    async def test_search_linear_issues(self, linear_token):
        if not linear_token:
            pytest.skip("No Linear integration connected")
        result = await run_tool(
            "search_linear_issues",
            {"query": "bug", "limit": 5},
            linear_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "issues" in result
        assert isinstance(result["issues"], list)

    async def test_get_linear_issue(self, linear_token):
        if not linear_token:
            pytest.skip("No Linear integration connected")
        # List issues first to get a valid issue ID
        listing = await run_tool("list_linear_issues", {"limit": 1}, linear_token)
        if not listing.get("issues"):
            pytest.skip("No Linear issues found for this user")
        issue_id = listing["issues"][0]["id"]

        result = await run_tool("get_linear_issue", {"issue_id": issue_id}, linear_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "id" in result
        assert result["id"] == issue_id
        assert "identifier" in result
        assert "title" in result
        assert "description" in result
        assert "status" in result
        assert "comments" in result
