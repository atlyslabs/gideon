"""
Minimal read-only ClickHouse transport, shared by the analytics tools.

This module is deliberately not a tool. `tools/analytics_tool.py` owns the
whole model-facing surface (schema discovery, SELECT-only validation, row
limits, permission gating); this is just the HTTP call underneath it.
"""

import httpx
import structlog

from config import (
    CLICKHOUSE_HOST,
    CLICKHOUSE_PORT,
    CLICKHOUSE_USER,
    CLICKHOUSE_PASSWORD,
    CLICKHOUSE_SECURE,
)

logger = structlog.get_logger()

# Every query is scoped to this database. `analytics_tool` additionally
# rejects db-qualified table names pointing anywhere else, so a broad-read
# user can't reach `system.*` or another database.
CLICKHOUSE_DATABASE = "analytics"


def _ch_url() -> str:
    scheme = "https" if CLICKHOUSE_SECURE else "http"
    return f"{scheme}://{CLICKHOUSE_HOST}:{CLICKHOUSE_PORT}"


async def run_query(query: str) -> list[dict]:
    """Execute a read-only ClickHouse query and return its rows."""
    async with httpx.AsyncClient(timeout=30, verify=True) as client:
        resp = await client.post(
            _ch_url(),
            params={
                "query": query + " FORMAT JSON",
                "database": CLICKHOUSE_DATABASE,
                "user": CLICKHOUSE_USER,
                "password": CLICKHOUSE_PASSWORD,
            },
            content="",
        )
        if resp.status_code != 200:
            raise Exception(f"ClickHouse error {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        return data.get("data", [])
