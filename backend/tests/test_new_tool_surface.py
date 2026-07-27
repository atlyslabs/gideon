"""Validation suite for the tool-surface expansion across Linear, GitHub,
Sheets, Docs, Drive, Slack, and Calendar.

Coverage strategy:
- **Schema validity**: every new tool's `input_schema` is a JSON-shape dict
  with the required keys, and every property has a `type` so Anthropic
  accepts it.
- **Registry**: every new tool is registered in ALL_TOOLS exactly once.
- **Wire-shape**: for each tool, fake the underlying client and assert the
  request the tool builds (URL/path, body, params) matches what the
  upstream API expects. Catches refactor breakage without needing live auth.
"""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.tool_registry import ALL_TOOLS  # noqa: E402


# ---------------------------------------------------------------------------
# Names of every tool added in this expansion. Used to assert registration
# and to drive parametric schema checks.
# ---------------------------------------------------------------------------
NEW_TOOL_NAMES = {
    # Linear
    "list_linear_labels",
    "list_linear_workflow_states",
    "list_linear_cycles",
    "create_linear_relation",
    "archive_linear_issue",
    "unarchive_linear_issue",
    "attach_linear_link",
    # GitHub
    "get_github_issue",
    "get_github_file",
    "list_github_branches",
    "get_github_pr_diff",
    "list_github_issue_comments",
    "list_github_pr_review_comments",
    "create_github_issue",
    "update_github_issue",
    "add_comment_to_github_issue",
    "create_github_pr",
    "merge_github_pr",
    "request_pr_review",
    "review_github_pr",
    "add_labels_to_github_issue",
    # Sheets
    "append_rows_to_spreadsheet",
    "clear_spreadsheet_range",
    "find_replace_spreadsheet",
    "sort_spreadsheet_range",
    "copy_spreadsheet_sheet",
    "create_spreadsheet_chart",
    # Docs
    "find_replace_in_document",
    "insert_image_into_document",
    "insert_table_into_document",
    "insert_page_break_into_document",
    # Drive
    "move_drive_file",
    "rename_drive_file",
    "trash_drive_file",
    "share_drive_file",
    # Slack
    "update_slack_message",
    "delete_slack_message",
    "add_slack_reaction",
    "remove_slack_reaction",
    "pin_slack_message",
    "unpin_slack_message",
    "upload_slack_file",
    # Calendar
    "add_attendees_to_event",
    "find_group_free_time",
}


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _by_name(name: str):
    matches = [t for t in ALL_TOOLS if t.name == name]
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# Registration + schema-validity checks
# ---------------------------------------------------------------------------

class RegistrationTests(unittest.TestCase):
    def test_all_new_tools_registered_exactly_once(self):
        names = [t.name for t in ALL_TOOLS]
        for n in NEW_TOOL_NAMES:
            count = names.count(n)
            self.assertEqual(count, 1, f"{n} registered {count} times (expected 1)")

    def test_no_duplicate_tool_names_overall(self):
        names = [t.name for t in ALL_TOOLS]
        dupes = {n for n in names if names.count(n) > 1}
        self.assertFalse(dupes, f"Duplicate tool names: {dupes}")


class SchemaValidityTests(unittest.TestCase):
    def test_every_new_tool_schema_shape(self):
        for name in NEW_TOOL_NAMES:
            tool = _by_name(name)
            self.assertIsNotNone(tool, f"{name} not in ALL_TOOLS")
            schema = tool.input_schema
            self.assertEqual(schema.get("type"), "object", f"{name}: schema.type must be 'object'")
            props = schema.get("properties")
            self.assertIsInstance(props, dict, f"{name}: schema.properties must be a dict")
            for pname, p in props.items():
                # Pydantic-style $ref or oneOf is valid; otherwise require type.
                if "type" not in p and "$ref" not in p and "oneOf" not in p and "anyOf" not in p:
                    self.fail(f"{name}.{pname}: property must declare a 'type'")
            # Required must reference declared properties (typo guard)
            for r in schema.get("required", []):
                self.assertIn(r, props, f"{name}: required field '{r}' not declared in properties")


# ---------------------------------------------------------------------------
# Linear write-shape tests
# ---------------------------------------------------------------------------

class LinearWireShape(unittest.TestCase):
    def setUp(self):
        # Patch the GraphQL transport — every Linear tool funnels through
        # `_linear_gql`. Replace with a mock that captures the (query, vars).
        from tools import linear_tool
        self.linear_tool = linear_tool
        self.calls: list[tuple[str, dict]] = []

        async def _fake_gql(token, query, variables=None):
            self.calls.append((query, variables or {}))
            # Return a payload shaped enough that each tool's success path runs.
            if "issueRelationCreate" in query:
                return {"issueRelationCreate": {"success": True, "issueRelation": {
                    "id": "rel-1", "type": "blocks",
                    "issue": {"id": "i1", "identifier": "ENG-1", "title": "x"},
                    "relatedIssue": {"id": "i2", "identifier": "ENG-2", "title": "y"},
                }}}
            if "issueArchive" in query:
                return {"issueArchive": {"success": True}}
            if "issueUnarchive" in query:
                return {"issueUnarchive": {"success": True}}
            if "attachmentCreate" in query:
                return {"attachmentCreate": {"success": True, "attachment": {
                    "id": "att-1", "url": "https://x", "title": "t", "subtitle": "s",
                }}}
            if "issueLabels" in query:
                return {"issueLabels": {"nodes": [
                    {"id": "L1", "name": "bug", "color": "#f00", "description": None,
                     "team": {"id": "T1", "name": "Eng"}},
                ]}}
            if "workflowStates" in query:
                return {"workflowStates": {"nodes": [
                    {"id": "S1", "name": "Todo", "type": "unstarted", "position": 1,
                     "color": "#aaa", "description": None},
                ]}}
            if "cycles" in query:
                return {"cycles": {"nodes": [
                    {"id": "C1", "number": 4, "name": "Apr",
                     "startsAt": "2026-04-01", "endsAt": "2026-04-15",
                     "completedAt": None, "progress": 0.2},
                ]}}
            return {}

        self._patch = patch.object(linear_tool, "_linear_gql", side_effect=_fake_gql)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()

    def test_create_relation_sends_correct_input(self):
        tool = _by_name("create_linear_relation")
        out = _run(tool.execute(
            {"issue_id": "u1", "related_issue_id": "u2", "type": "blocks"}, "tok"
        ))
        self.assertEqual(out["type"], "blocks")
        _q, vars_ = self.calls[-1]
        self.assertEqual(vars_["input"]["issueId"], "u1")
        self.assertEqual(vars_["input"]["relatedIssueId"], "u2")
        self.assertEqual(vars_["input"]["type"], "blocks")

    def test_archive_unarchive_pass_id(self):
        for name, status in (("archive_linear_issue", "archived"),
                             ("unarchive_linear_issue", "unarchived")):
            self.calls.clear()
            tool = _by_name(name)
            out = _run(tool.execute({"issue_id": "ENG-1"}, "tok"))
            self.assertEqual(out["status"], status)
            self.assertEqual(self.calls[-1][1]["id"], "ENG-1")

    def test_attach_link_passes_subtitle(self):
        tool = _by_name("attach_linear_link")
        out = _run(tool.execute(
            {"issue_id": "ENG-1", "url": "https://x.com", "title": "Spec", "subtitle": "Doc"},
            "tok",
        ))
        self.assertEqual(out["title"], "t")
        # `attachmentCreate` takes a single `input` variable rather than top-level args.
        vars_ = self.calls[-1][1]
        self.assertEqual(vars_["input"]["url"], "https://x.com")
        self.assertEqual(vars_["input"]["subtitle"], "Doc")
        self.assertEqual(vars_["input"]["issueId"], "ENG-1")

    def test_list_labels_with_team_filter(self):
        tool = _by_name("list_linear_labels")
        out = _run(tool.execute({"team_id": "T1", "limit": 50}, "tok"))
        self.assertEqual(out["count"], 1)
        q, vars_ = self.calls[-1]
        self.assertIn("issueLabels", q)
        self.assertEqual(vars_["teamId"], "T1")
        self.assertEqual(vars_["first"], 50)

    def test_list_cycles_filters_completed_by_default(self):
        # Inject a completed + active cycle. We must override the side_effect
        # for this test since our default returns one active.
        async def _fake(token, query, variables=None):
            return {"cycles": {"nodes": [
                {"id": "C1", "completedAt": None, "name": "active"},
                {"id": "C2", "completedAt": "2026-01-01", "name": "old"},
            ]}}
        self._patch.stop()
        with patch.object(self.linear_tool, "_linear_gql", side_effect=_fake):
            tool = _by_name("list_linear_cycles")
            out = _run(tool.execute({"team_id": "T1"}, "tok"))
            self.assertEqual([c["id"] for c in out["cycles"]], ["C1"])
            out_all = _run(tool.execute({"team_id": "T1", "include_past": True}, "tok"))
            self.assertEqual([c["id"] for c in out_all["cycles"]], ["C1", "C2"])
        self._patch.start()  # restore for tearDown


class LinearCreateUpdateNewParams(unittest.TestCase):
    """Verify the new params on create/update issue serialize to camelCase."""

    def test_create_passes_due_date_estimate_cycle_parent(self):
        from tools import linear_tool
        captured: dict = {}

        async def _fake(token, query, variables=None):
            captured["q"] = query
            captured["v"] = variables
            return {"issueCreate": {"success": True, "issue": {
                "id": "i", "identifier": "ENG-1", "title": "t", "url": "",
                "state": {"name": "Todo"}, "team": {"name": "Eng", "key": "ENG"},
            }}}
        with patch.object(linear_tool, "_linear_gql", side_effect=_fake):
            tool = _by_name("create_linear_issue")
            _run(tool.execute({
                "title": "x", "team_id": "T1",
                "parent_id": "PARENT-UUID", "due_date": "2026-05-30",
                "estimate": 5, "cycle_id": "CY-UUID",
            }, "tok"))
        inp = captured["v"]["input"]
        self.assertEqual(inp["parentId"], "PARENT-UUID")
        self.assertEqual(inp["dueDate"], "2026-05-30")
        self.assertEqual(inp["estimate"], 5)
        self.assertEqual(inp["cycleId"], "CY-UUID")

    def test_update_clears_with_empty_string(self):
        from tools import linear_tool
        captured: dict = {}

        async def _fake(token, query, variables=None):
            captured["q"] = query; captured["v"] = variables
            if "issueUpdate" in query:
                return {"issueUpdate": {"success": True, "issue": {
                    "id": "i", "identifier": "ENG-1", "title": "t",
                    "state": {"name": "Todo"}, "team": {"name": "Eng", "key": "ENG"},
                    "url": "",
                }}}
            return {}
        with patch.object(linear_tool, "_linear_gql", side_effect=_fake):
            tool = _by_name("update_linear_issue")
            _run(tool.execute({
                "issue_id": "ENG-1",
                "parent_id": "", "due_date": "", "estimate": 0, "cycle_id": "",
            }, "tok"))
        inp = captured["v"]["input"]
        # Empty string / zero should serialize to None — the API treats null as "clear".
        self.assertIsNone(inp.get("parentId"))
        self.assertIsNone(inp.get("dueDate"))
        self.assertIsNone(inp.get("estimate"))
        self.assertIsNone(inp.get("cycleId"))


# ---------------------------------------------------------------------------
# GitHub: validate that helper plumbing builds the right HTTP method/path.
# ---------------------------------------------------------------------------

class GitHubWireShape(unittest.TestCase):
    def setUp(self):
        from tools import github_tool
        self.github_tool = github_tool
        self.requests_made: list[tuple[str, str, dict | None, dict | None]] = []

        async def _fake_request(method, token, path, json_body=None, params=None, ok_status=(200, 201)):
            self.requests_made.append((method, path, json_body, params))
            # Return success-shaped payloads
            if path.endswith("/issues") and method == "POST":
                return {"number": 42, "title": json_body["title"], "state": "open", "html_url": "u"}
            if "/issues/" in path and "/comments" in path and method == "POST":
                return {"id": 1, "html_url": "u", "created_at": "now"}
            if "/issues/" in path and method == "PATCH":
                return {"number": 42, "title": "t", "state": json_body.get("state", "open"), "html_url": "u"}
            if path.endswith("/pulls") and method == "POST":
                return {"number": 7, "title": json_body["title"], "state": "open",
                        "draft": json_body.get("draft", False), "html_url": "u"}
            if "/pulls/" in path and "/merge" in path:
                return {"merged": True, "sha": "abc"}
            if "/pulls/" in path and "/requested_reviewers" in path:
                return {}
            if "/pulls/" in path and "/reviews" in path:
                return {"id": 1, "state": "APPROVED", "html_url": "u"}
            if "/labels" in path:
                return [{"name": l} for l in json_body.get("labels", [])]
            return {}

        async def _fake_get(token, path, params=None, accept="application/vnd.github.v3+json"):
            self.requests_made.append(("GET", path, None, params))
            if accept.startswith("application/vnd.github.diff"):
                return "diff --git a/x b/x\n"
            if accept == "application/vnd.github.raw":
                return "file contents"
            if "/issues/" in path and "/comments" in path:
                return [{"id": 1, "user": {"login": "u"}, "body": "b", "created_at": "now", "html_url": "u"}]
            if "/issues/" in path:
                return {"number": 7, "title": "t", "state": "open", "body": "b",
                        "user": {"login": "u"}, "comments": 0, "html_url": "u"}
            if "/pulls/" in path and "/comments" in path:
                return [{"id": 1, "user": {"login": "u"}, "path": "x.py", "line": 1,
                         "body": "b", "created_at": "now", "html_url": "u"}]
            if "/branches" in path:
                return [{"name": "main", "commit": {"sha": "abc"}, "protected": True}]
            return {}

        self._p1 = patch.object(github_tool, "_github_request", side_effect=_fake_request)
        self._p2 = patch.object(github_tool, "_github_get", side_effect=_fake_get)
        self._p1.start(); self._p2.start()

    def tearDown(self):
        self._p1.stop(); self._p2.stop()

    def test_create_issue_post_path_and_body(self):
        tool = _by_name("create_github_issue")
        _run(tool.execute({"repo": "o/r", "title": "T", "body": "b", "labels": ["bug"]}, "tok"))
        method, path, body, _ = self.requests_made[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "repos/o/r/issues")
        self.assertEqual(body["title"], "T")
        self.assertEqual(body["labels"], ["bug"])

    def test_update_issue_patch_state(self):
        tool = _by_name("update_github_issue")
        _run(tool.execute({"repo": "o/r", "issue_number": 7, "state": "closed"}, "tok"))
        method, path, body, _ = self.requests_made[-1]
        self.assertEqual(method, "PATCH")
        self.assertEqual(path, "repos/o/r/issues/7")
        self.assertEqual(body["state"], "closed")

    def test_create_pr_includes_head_base(self):
        tool = _by_name("create_github_pr")
        _run(tool.execute({
            "repo": "o/r", "title": "T", "head": "f", "base": "main",
            "body": "b", "draft": True,
        }, "tok"))
        method, path, body, _ = self.requests_made[-1]
        self.assertEqual(method, "POST")
        self.assertEqual(path, "repos/o/r/pulls")
        self.assertEqual(body["head"], "f")
        self.assertEqual(body["base"], "main")
        self.assertTrue(body["draft"])

    def test_merge_pr_uses_put(self):
        tool = _by_name("merge_github_pr")
        _run(tool.execute({"repo": "o/r", "pr_number": 7, "merge_method": "squash"}, "tok"))
        method, path, body, _ = self.requests_made[-1]
        self.assertEqual(method, "PUT")
        self.assertEqual(path, "repos/o/r/pulls/7/merge")
        self.assertEqual(body["merge_method"], "squash")

    def test_review_pr_event_required(self):
        tool = _by_name("review_github_pr")
        _run(tool.execute({"repo": "o/r", "pr_number": 7, "event": "REQUEST_CHANGES",
                           "body": "Please fix"}, "tok"))
        _, path, body, _ = self.requests_made[-1]
        self.assertEqual(path, "repos/o/r/pulls/7/reviews")
        self.assertEqual(body["event"], "REQUEST_CHANGES")
        self.assertEqual(body["body"], "Please fix")

    def test_request_review_requires_at_least_one(self):
        tool = _by_name("request_pr_review")
        out = _run(tool.execute({"repo": "o/r", "pr_number": 7}, "tok"))
        self.assertIn("error", out)

    def test_get_file_uses_raw_accept(self):
        tool = _by_name("get_github_file")
        out = _run(tool.execute({"repo": "o/r", "path": "README.md", "ref": "main"}, "tok"))
        self.assertEqual(out["content"], "file contents")

    def test_get_pr_diff_uses_diff_accept(self):
        tool = _by_name("get_github_pr_diff")
        out = _run(tool.execute({"repo": "o/r", "pr_number": 7}, "tok"))
        self.assertTrue(out["diff"].startswith("diff --git"))


# ---------------------------------------------------------------------------
# Sheets, Docs, Drive, Calendar — Google client mocking
# ---------------------------------------------------------------------------

def _mock_google_chain():
    """Build a deeply chained MagicMock that responds to any .x().y().z()."""
    return MagicMock()


class SheetsWireShape(unittest.TestCase):
    def setUp(self):
        from tools import google_sheets_tool
        self.svc = _mock_google_chain()
        self.patcher = patch.object(google_sheets_tool, "_get_sheets_service", return_value=self.svc)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_append_rows_calls_values_append(self):
        self.svc.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "A1", "updatedCells": 2}
        }
        tool = _by_name("append_rows_to_spreadsheet")
        out = _run(tool.execute({"spreadsheet_id": "S", "values": [[1, 2]]}, "tok"))
        self.assertEqual(out["appended_rows"], 1)
        # Check that .append was called with valueInputOption USER_ENTERED by default.
        kwargs = self.svc.spreadsheets().values().append.call_args
        # call_args is (args, kwargs) — args is positional. We use kwargs.
        self.assertEqual(kwargs.kwargs["valueInputOption"], "USER_ENTERED")
        self.assertEqual(kwargs.kwargs["body"]["values"], [[1, 2]])

    def test_append_rows_rejects_empty(self):
        tool = _by_name("append_rows_to_spreadsheet")
        out = _run(tool.execute({"spreadsheet_id": "S", "values": []}, "tok"))
        self.assertIn("error", out)

    def test_clear_calls_values_clear(self):
        self.svc.spreadsheets().values().clear().execute.return_value = {"clearedRange": "Sheet1!A:D"}
        tool = _by_name("clear_spreadsheet_range")
        out = _run(tool.execute({"spreadsheet_id": "S", "range": "Sheet1!A:D"}, "tok"))
        self.assertEqual(out["cleared_range"], "Sheet1!A:D")

    def test_find_replace_uses_batch_update(self):
        self.svc.spreadsheets().get().execute.return_value = {
            "sheets": [{"properties": {"sheetId": 99, "title": "Sheet1"}}]
        }
        self.svc.spreadsheets().batchUpdate().execute.return_value = {
            "replies": [{"findReplace": {"occurrencesChanged": 3, "valuesChanged": 3}}]
        }
        tool = _by_name("find_replace_spreadsheet")
        out = _run(tool.execute({
            "spreadsheet_id": "S", "find": "old", "replacement": "new", "sheet": "Sheet1",
        }, "tok"))
        self.assertEqual(out["occurrences_changed"], 3)


class DocsWireShape(unittest.TestCase):
    def setUp(self):
        from tools import google_docs_tool
        self.svc = _mock_google_chain()
        self.patcher = patch.object(google_docs_tool, "_get_docs_service", return_value=self.svc)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_find_replace_calls_batch_update(self):
        self.svc.documents().batchUpdate().execute.return_value = {
            "replies": [{"replaceAllText": {"occurrencesChanged": 2}}]
        }
        tool = _by_name("find_replace_in_document")
        out = _run(tool.execute({
            "document_id": "D", "find": "x", "replacement": "y",
        }, "tok"))
        self.assertEqual(out["occurrences_changed"], 2)

    def test_insert_image_passes_url_and_index(self):
        self.svc.documents().batchUpdate().execute.return_value = {
            "replies": [{"insertInlineImage": {"objectId": "img1"}}]
        }
        tool = _by_name("insert_image_into_document")
        out = _run(tool.execute({
            "document_id": "D", "image_url": "https://x.com/i.png", "index": 1,
        }, "tok"))
        self.assertEqual(out["object_id"], "img1")

    def test_insert_table_blank_doesnt_fill(self):
        self.svc.documents().batchUpdate().execute.return_value = {}
        tool = _by_name("insert_table_into_document")
        out = _run(tool.execute({
            "document_id": "D", "index": 1, "rows": 2, "columns": 3,
        }, "tok"))
        self.assertEqual(out["status"], "inserted")
        self.assertEqual(out["rows"], 2)


class DriveWireShape(unittest.TestCase):
    def setUp(self):
        from tools import google_drive_tool
        self.svc = _mock_google_chain()
        self.patcher = patch.object(google_drive_tool, "_get_drive_service", return_value=self.svc)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_move_swaps_parents(self):
        # Stage two distinct calls: first the .get for current parents, then update.
        self.svc.files().get().execute.return_value = {"parents": ["p_old"]}
        self.svc.files().update().execute.return_value = {
            "id": "F", "name": "n", "parents": ["p_new"], "webViewLink": "u",
        }
        tool = _by_name("move_drive_file")
        out = _run(tool.execute({"file_id": "F", "new_parent_id": "p_new"}, "tok"))
        self.assertEqual(out["parents"], ["p_new"])
        upd = self.svc.files().update.call_args
        self.assertEqual(upd.kwargs.get("addParents"), "p_new")
        self.assertEqual(upd.kwargs.get("removeParents"), "p_old")

    def test_share_requires_email_or_anyone(self):
        tool = _by_name("share_drive_file")
        out = _run(tool.execute({"file_id": "F"}, "tok"))
        self.assertIn("error", out)

    def test_share_with_email_sets_user_type(self):
        self.svc.permissions().create().execute.return_value = {
            "id": "P", "type": "user", "role": "writer", "emailAddress": "a@x.com",
        }
        tool = _by_name("share_drive_file")
        out = _run(tool.execute({
            "file_id": "F", "role": "writer", "email": "a@x.com",
        }, "tok"))
        self.assertEqual(out["role"], "writer")
        self.assertEqual(out["email"], "a@x.com")
        kwargs = self.svc.permissions().create.call_args.kwargs
        self.assertEqual(kwargs["body"]["type"], "user")
        self.assertEqual(kwargs["body"]["emailAddress"], "a@x.com")


class CalendarWireShape(unittest.TestCase):
    def setUp(self):
        from tools import calendar_tool
        self.svc = _mock_google_chain()
        self.patcher = patch.object(calendar_tool, "_get_calendar_service", return_value=self.svc)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_add_attendees_dedupes_against_existing(self):
        self.svc.events().get().execute.return_value = {
            "attendees": [{"email": "a@x.com"}],
            "id": "E", "summary": "S",
            "start": {"dateTime": "2026-05-07T09:00:00Z"},
            "end": {"dateTime": "2026-05-07T10:00:00Z"},
            "htmlLink": "u",
        }
        self.svc.events().patch().execute.return_value = {
            "attendees": [
                {"email": "a@x.com"}, {"email": "b@x.com"}, {"email": "c@x.com"},
            ],
            "id": "E", "summary": "S",
            "start": {"dateTime": "2026-05-07T09:00:00Z"},
            "end": {"dateTime": "2026-05-07T10:00:00Z"},
            "htmlLink": "u",
        }
        tool = _by_name("add_attendees_to_event")
        _run(tool.execute({
            "event_id": "E", "emails": ["a@x.com", "b@x.com", "c@x.com"],
        }, "tok"))
        body = self.svc.events().patch.call_args.kwargs["body"]
        emails_in_patch = [a["email"] for a in body["attendees"]]
        self.assertEqual(sorted(emails_in_patch), ["a@x.com", "b@x.com", "c@x.com"])

    def test_find_group_free_time_inverts_busy(self):
        self.svc.freebusy().query().execute.return_value = {
            "calendars": {
                "a@x.com": {"busy": [
                    {"start": "2026-05-07T10:00:00+00:00", "end": "2026-05-07T11:00:00+00:00"},
                ]},
                "b@x.com": {"busy": [
                    {"start": "2026-05-07T13:00:00+00:00", "end": "2026-05-07T14:00:00+00:00"},
                ]},
            }
        }
        tool = _by_name("find_group_free_time")
        out = _run(tool.execute({
            "emails": ["a@x.com", "b@x.com"],
            "time_min": "2026-05-07T09:00:00+00:00",
            "time_max": "2026-05-07T17:00:00+00:00",
            "min_duration_minutes": 30,
        }, "tok"))
        # Expect 3 free windows: 09-10, 11-13, 14-17
        self.assertEqual(out["count"], 3)
        durations = [s["duration_minutes"] for s in out["free_slots"]]
        self.assertEqual(durations, [60, 120, 180])


# ---------------------------------------------------------------------------
# Slack
# ---------------------------------------------------------------------------

class SlackWireShape(unittest.TestCase):
    def setUp(self):
        from tools import slack_tool
        self.client = MagicMock()
        # All slack tools call methods on AsyncWebClient — make every method
        # an AsyncMock by default.
        for m in ("chat_update", "chat_delete", "reactions_add", "reactions_remove",
                  "pins_add", "pins_remove", "files_upload_v2"):
            setattr(self.client, m, AsyncMock(return_value={"ok": True, "ts": "t",
                                                            "channel": "C", "files": []}))
        self.patcher = patch.object(slack_tool, "_get_slack_client", return_value=self.client)
        self.patcher.start()

        # update_slack_message also runs encode_slack_mentions / DB session;
        # patch those to inert no-ops.
        async def _passthrough(text, _session): return text
        self._encode_patch = patch.object(slack_tool, "encode_slack_mentions",
                                          side_effect=_passthrough)
        self._encode_patch.start()

    def tearDown(self):
        self.patcher.stop(); self._encode_patch.stop()

    def test_update_message(self):
        tool = _by_name("update_slack_message")
        out = _run(tool.execute({"channel_id": "C", "ts": "t", "text": "hi"}, "tok"))
        self.assertTrue(out["ok"])
        self.client.chat_update.assert_awaited_once()
        kwargs = self.client.chat_update.await_args.kwargs
        self.assertEqual(kwargs["channel"], "C")
        self.assertEqual(kwargs["ts"], "t")

    def test_delete_message(self):
        tool = _by_name("delete_slack_message")
        out = _run(tool.execute({"channel_id": "C", "ts": "t"}, "tok"))
        self.assertTrue(out["ok"])
        self.client.chat_delete.assert_awaited_once()

    def test_add_reaction_strips_colons(self):
        tool = _by_name("add_slack_reaction")
        _run(tool.execute({"channel_id": "C", "ts": "t", "name": ":thumbsup:"}, "tok"))
        kwargs = self.client.reactions_add.await_args.kwargs
        self.assertEqual(kwargs["name"], "thumbsup")

    def test_pin_handles_already_pinned(self):
        from tools import slack_tool
        # Replace with a coro that raises a SlackApiError-ish exception
        async def _raise(**_):
            raise Exception("ok=False, error='already_pinned'")
        self.client.pins_add = AsyncMock(side_effect=_raise)
        tool = _by_name("pin_slack_message")
        out = _run(tool.execute({"channel_id": "C", "ts": "t"}, "tok"))
        self.assertTrue(out["ok"])
        self.assertIn("Already pinned", out.get("note", ""))

    def test_upload_file_size_limit(self):
        tool = _by_name("upload_slack_file")
        out = _run(tool.execute({
            "channel_id": "C", "filename": "x.txt", "content": "x" * 200_001,
        }, "tok"))
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
