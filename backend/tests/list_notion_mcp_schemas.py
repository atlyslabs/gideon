"""Dump the Notion MCP server's tool list with exact input schemas."""

import asyncio
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.run_notion_sweep import _force_refresh_notion_token
from tools.notion_tool import _mcp_call


async def main() -> None:
    token = await _force_refresh_notion_token()
    result = await _mcp_call(token, "tools/list", {})
    tools = result.get("tools", [])
    for t in tools:
        name = t.get("name")
        if name and name.startswith("notion-"):
            print(f"\n### {name}")
            print(f"  description: {t.get('description', '')[:200]}")
            schema = t.get("inputSchema", {})
            print("  schema:")
            print(json.dumps(schema, indent=2)[:2000])


if __name__ == "__main__":
    asyncio.run(main())
