"""
Integration tests for Google tools (Gmail, Calendar, Drive, Sheets, Docs).
Requires the test user to have a connected Google integration.
"""

from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from tests.conftest import run_tool

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


class TestGmail:
    async def test_list_emails(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("list_emails", {"max_results": 5}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "emails" in result
        assert isinstance(result["emails"], list)
        assert "count" in result
        if result["count"] > 0:
            email = result["emails"][0]
            assert "id" in email
            assert "subject" in email
            assert "from" in email

    async def test_read_email(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        # First list to get an email ID
        listing = await run_tool("list_emails", {"max_results": 1}, google_token)
        if not listing.get("emails"):
            pytest.skip("No emails in inbox")
        msg_id = listing["emails"][0]["id"]

        result = await run_tool("read_email", {"message_id": msg_id}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "id" in result
        assert result["id"] == msg_id
        assert "subject" in result
        assert "body" in result
        assert "from" in result

    async def test_search_emails(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("search_emails", {"query": "is:inbox", "max_results": 3}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "emails" in result
        assert isinstance(result["emails"], list)

    async def test_list_drafts(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("list_drafts", {"limit": 5}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "drafts" in result
        assert isinstance(result["drafts"], list)
        assert "count" in result


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


class TestCalendar:
    async def test_list_today_events(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("list_today_events", {}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "events" in result
        assert isinstance(result["events"], list)
        assert "count" in result
        assert "date" in result

    async def test_list_events(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        today = datetime.utcnow()
        date_from = today.strftime("%Y-%m-%d")
        date_to = (today + timedelta(days=7)).strftime("%Y-%m-%d")
        result = await run_tool(
            "list_events",
            {"date_from": date_from, "date_to": date_to, "max_results": 10},
            google_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "events" in result
        assert isinstance(result["events"], list)
        if result["count"] > 0:
            event = result["events"][0]
            assert "id" in event
            assert "summary" in event
            assert "start" in event


# ---------------------------------------------------------------------------
# Drive
# ---------------------------------------------------------------------------


class TestDrive:
    async def test_list_drive_files(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("list_drive_files", {"max_results": 5}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "files" in result
        assert isinstance(result["files"], list)
        assert "count" in result
        if result["count"] > 0:
            f = result["files"][0]
            assert "id" in f
            assert "name" in f
            assert "mime_type" in f

    async def test_search_drive_files(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        result = await run_tool("search_drive_files", {"query": "test", "max_results": 5}, google_token)
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "files" in result
        assert isinstance(result["files"], list)


# ---------------------------------------------------------------------------
# Sheets
# ---------------------------------------------------------------------------


class TestSheets:
    async def test_read_spreadsheet(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        # First find a spreadsheet via Drive
        listing = await run_tool(
            "list_drive_files",
            {"mime_type": "application/vnd.google-apps.spreadsheet", "max_results": 1},
            google_token,
        )
        if not listing.get("files"):
            pytest.skip("No spreadsheets found in Drive")
        spreadsheet_id = listing["files"][0]["id"]

        result = await run_tool(
            "read_spreadsheet",
            {"spreadsheet_id": spreadsheet_id, "range": "Sheet1"},
            google_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "data" in result
        assert "rows" in result


# ---------------------------------------------------------------------------
# Docs
# ---------------------------------------------------------------------------


class TestDocs:
    async def test_read_google_doc(self, google_token):
        if not google_token:
            pytest.skip("No Google integration connected")
        # Find a Google Doc via Drive
        listing = await run_tool(
            "list_drive_files",
            {"mime_type": "application/vnd.google-apps.document", "max_results": 1},
            google_token,
        )
        if not listing.get("files"):
            pytest.skip("No Google Docs found in Drive")
        doc_id = listing["files"][0]["id"]

        result = await run_tool(
            "read_google_doc",
            {"document_id": doc_id},
            google_token,
        )
        assert "error" not in result, f"Tool returned error: {result.get('error')}"
        assert "document_id" in result
        assert "title" in result
        assert "content" in result
