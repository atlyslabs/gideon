"""
Integration tests for Grafana tools.
These are internal tools that use DB session + user_id (no OAuth token).
Requires GRAFANA_URL and GRAFANA_TOKEN to be configured.
"""

import pytest

from config import GRAFANA_URL
from tests.conftest import run_internal_tool

pytestmark = pytest.mark.asyncio

skip_no_grafana = pytest.mark.skipif(
    not GRAFANA_URL,
    reason="GRAFANA_URL not configured",
)


@skip_no_grafana
class TestGrafana:
    async def test_list_grafana_dashboards(self, db_session, user_id):
        result = await run_internal_tool(
            "list_grafana_dashboards",
            {"limit": 5},
            user_id,
            db_session,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "dashboards" in result
        assert isinstance(result["dashboards"], list)
        assert "count" in result
        if result["count"] > 0:
            dash = result["dashboards"][0]
            assert "uid" in dash
            assert "title" in dash

    async def test_get_grafana_dashboard(self, db_session, user_id):
        # List first to get a dashboard UID
        listing = await run_internal_tool(
            "list_grafana_dashboards",
            {"limit": 1},
            user_id,
            db_session,
        )
        if not listing.get("dashboards"):
            pytest.skip("No Grafana dashboards accessible for this user")
        uid = listing["dashboards"][0]["uid"]

        result = await run_internal_tool(
            "get_grafana_dashboard",
            {"uid": uid},
            user_id,
            db_session,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "title" in result
        assert "uid" in result
        assert result["uid"] == uid
        assert "panels" in result
        assert isinstance(result["panels"], list)
        assert "panel_count" in result
