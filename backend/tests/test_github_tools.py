"""
Integration tests for GitHub tools.
Requires the test user to have a connected GitHub integration.
"""

import pytest

from tests.conftest import run_tool

pytestmark = pytest.mark.asyncio


class TestGitHub:
    async def test_list_github_repos(self, github_token):
        if not github_token:
            pytest.skip("No GitHub integration connected")
        result = await run_tool(
            "list_github_repos",
            {"sort": "updated", "limit": 5},
            github_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "repos" in result
        assert isinstance(result["repos"], list)
        assert "count" in result
        if result["count"] > 0:
            repo = result["repos"][0]
            assert "full_name" in repo
            assert "url" in repo

    async def test_list_github_issues(self, github_token):
        if not github_token:
            pytest.skip("No GitHub integration connected")
        # Get a repo first
        repos = await run_tool("list_github_repos", {"limit": 5}, github_token)
        if not repos.get("repos"):
            pytest.skip("No GitHub repos accessible")
        repo_name = repos["repos"][0]["full_name"]

        result = await run_tool(
            "list_github_issues",
            {"repo": repo_name, "state": "open", "limit": 5},
            github_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "issues" in result
        assert isinstance(result["issues"], list)

    async def test_list_github_pull_requests(self, github_token):
        if not github_token:
            pytest.skip("No GitHub integration connected")
        # Get a repo first
        repos = await run_tool("list_github_repos", {"limit": 5}, github_token)
        if not repos.get("repos"):
            pytest.skip("No GitHub repos accessible")
        repo_name = repos["repos"][0]["full_name"]

        result = await run_tool(
            "list_github_pull_requests",
            {"repo": repo_name, "state": "all", "limit": 5},
            github_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "pull_requests" in result
        assert isinstance(result["pull_requests"], list)

    async def test_list_github_notifications(self, github_token):
        if not github_token:
            pytest.skip("No GitHub integration connected")
        result = await run_tool(
            "list_github_notifications",
            {"all": True, "limit": 5},
            github_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "notifications" in result
        assert isinstance(result["notifications"], list)
        assert "count" in result
