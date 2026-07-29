"""
Integration tests for Notion tools (via Notion MCP server).
Requires the test user to have a connected Notion integration.
"""

import pytest

from tests.conftest import run_tool

pytestmark = pytest.mark.asyncio


class TestNotion:
    async def test_search_notion_pages(self, notion_token):
        if not notion_token:
            pytest.skip("No Notion integration connected")
        result = await run_tool(
            "search_notion_pages",
            {"query": "meeting"},
            notion_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        # Notion MCP returns either parsed pages or raw text
        has_pages = "pages" in result and isinstance(result["pages"], list)
        has_raw = "results" in result or "raw_response" in result
        has_formatted = "formatted" in result
        assert has_pages or has_raw or has_formatted, (
            f"Expected pages, results, formatted, or raw_response in result keys: {list(result.keys())}"
        )

    async def test_read_notion_page(self, notion_token):
        if not notion_token:
            pytest.skip("No Notion integration connected")
        # Search first to find a page
        search = await run_tool("search_notion_pages", {"query": "meeting"}, notion_token)
        pages = search.get("pages", [])
        if not pages:
            pytest.skip("No Notion pages found matching 'meeting'")
        page_url = pages[0].get("url", "")
        if not page_url:
            pytest.skip("No URL in first search result")

        result = await run_tool("read_notion_page", {"page_url": page_url}, notion_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        has_content = "content" in result
        has_raw = "raw_response" in result
        assert has_content or has_raw, (
            f"Expected content or raw_response in result keys: {list(result.keys())}"
        )
