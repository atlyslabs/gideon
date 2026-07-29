"""
Integration tests for Slack tools.
Requires the test user to have a connected Slack integration.
"""

import pytest

from tests.conftest import run_tool

pytestmark = pytest.mark.asyncio


class TestSlack:
    async def test_list_slack_channels(self, slack_token):
        if not slack_token:
            pytest.skip("No Slack integration connected")
        result = await run_tool("list_slack_channels", {"limit": 10}, slack_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "channels" in result
        assert isinstance(result["channels"], list)
        assert "count" in result
        if result["count"] > 0:
            ch = result["channels"][0]
            assert "id" in ch
            assert "name" in ch

    async def test_read_slack_channel(self, slack_token):
        if not slack_token:
            pytest.skip("No Slack integration connected")
        # List channels first to get a channel ID
        listing = await run_tool("list_slack_channels", {"limit": 5}, slack_token)
        if not listing.get("channels"):
            pytest.skip("No Slack channels accessible")
        channel_id = listing["channels"][0]["id"]

        result = await run_tool(
            "read_slack_channel",
            {"channel_id": channel_id, "limit": 5},
            slack_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "messages" in result
        assert isinstance(result["messages"], list)
        assert "count" in result

    async def test_search_slack_messages(self, slack_token):
        if not slack_token:
            pytest.skip("No Slack integration connected")
        result = await run_tool(
            "search_slack_messages",
            {"query": "hello", "count": 5},
            slack_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "messages" in result
        assert isinstance(result["messages"], list)
        assert "count" in result
