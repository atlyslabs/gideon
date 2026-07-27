"""
Linear Tools — issues and teams via Linear GraphQL API.
"""

from typing import Any

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

LINEAR_API_URL = "https://api.linear.app/graphql"


async def _linear_gql(token: str, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query against the Linear API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            LINEAR_API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
        )
    if resp.status_code != 200:
        raise Exception(f"Linear API error: {resp.status_code} — {resp.text}")
    data = resp.json()
    if "errors" in data:
        raise Exception(f"Linear GraphQL error: {data['errors']}")
    return data.get("data", {})


# Public alias for cross-module use (avoids importing private _linear_gql)
linear_gql = _linear_gql


class ListLinearIssues(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_issues"

    @property
    def description(self) -> str:
        return (
            "List Linear issues assigned to the current user, optionally filtered by team name or status. "
            "Returns issue identifier, title, status, priority, and assignee."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Filter by team name (case-insensitive partial match)",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status name (e.g. 'In Progress', 'Todo', 'Done')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return (default 20, max 50)",
                    "default": 20,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 20), 50)

        # Build filter
        filter_parts = []
        filter_vars = {}

        filter_parts.append("assignee: { isMe: { eq: true } }")

        if params.get("team_name"):
            filter_parts.append('team: { name: { containsIgnoreCase: $teamName } }')
            filter_vars["teamName"] = params["team_name"]

        if params.get("status"):
            filter_parts.append('state: { name: { eqIgnoreCase: $statusName } }')
            filter_vars["statusName"] = params["status"]

        var_defs = []
        if "teamName" in filter_vars:
            var_defs.append("$teamName: String!")
        if "statusName" in filter_vars:
            var_defs.append("$statusName: String!")

        var_def_str = f"({', '.join(var_defs)})" if var_defs else ""
        filter_str = ", ".join(filter_parts)

        query = f"""
        query ListMyIssues{var_def_str} {{
            issues(
                first: {limit},
                filter: {{ {filter_str} }}
                orderBy: updatedAt
            ) {{
                nodes {{
                    id
                    identifier
                    title
                    priority
                    url
                    state {{
                        name
                    }}
                    team {{
                        name
                        key
                    }}
                    assignee {{
                        name
                        email
                    }}
                    createdAt
                    updatedAt
                }}
            }}
        }}
        """

        data = await _linear_gql(access_token, query, filter_vars)
        nodes = (data.get("issues") or {}).get("nodes", [])
        issues = [
            {
                "id": n["id"],
                "identifier": n["identifier"],
                "title": n["title"],
                "status": (n.get("state") or {}).get("name", ""),
                "priority": n.get("priority"),
                "team": (n.get("team") or {}).get("name", ""),
                "team_key": (n.get("team") or {}).get("key", ""),
                "assignee": (n.get("assignee") or {}).get("name", ""),
                "url": n.get("url", ""),
                "created_at": n.get("createdAt", ""),
                "updated_at": n.get("updatedAt", ""),
            }
            for n in nodes
        ]
        return {"issues": issues, "count": len(issues)}


class SearchLinearIssues(BaseTool):
    @property
    def name(self) -> str:
        return "search_linear_issues"

    @property
    def description(self) -> str:
        return "Search Linear issues by text query across titles and descriptions."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text to search for in issue titles and descriptions",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 15, max 50)",
                    "default": 15,
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 15), 50)
        query = """
        query SearchIssues($query: String!, $first: Int!) {
            searchIssues(term: $query, first: $first) {
                nodes {
                    id
                    identifier
                    title
                    priority
                    url
                    state {
                        name
                    }
                    team {
                        name
                        key
                    }
                    assignee {
                        name
                    }
                    createdAt
                    updatedAt
                }
            }
        }
        """
        data = await _linear_gql(access_token, query, {"query": params["query"], "first": limit})
        nodes = (data.get("searchIssues") or {}).get("nodes", [])
        issues = [
            {
                "id": n["id"],
                "identifier": n["identifier"],
                "title": n["title"],
                "status": (n.get("state") or {}).get("name", ""),
                "priority": n.get("priority"),
                "team": (n.get("team") or {}).get("name", ""),
                "assignee": (n.get("assignee") or {}).get("name", ""),
                "url": n.get("url", ""),
            }
            for n in nodes
        ]
        return {"issues": issues, "count": len(issues)}


class GetLinearIssue(BaseTool):
    @property
    def name(self) -> str:
        return "get_linear_issue"

    @property
    def description(self) -> str:
        return "Get detailed information about a specific Linear issue by its ID or identifier (e.g. 'ENG-123')."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue ID (UUID) or identifier (e.g. 'ENG-123')",
                },
            },
            "required": ["issue_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        issue_id = params["issue_id"]

        # Linear's issue(id:) accepts both UUID and identifier (e.g. "ENG-123")
        query = """
        query GetIssue($id: String!) {
            issue(id: $id) {
                id
                identifier
                title
                description
                priority
                url
                state { name }
                team { name key }
                assignee { name email }
                labels { nodes { name } }
                comments { nodes { body user { name } createdAt } }
                parent { id identifier title }
                children { nodes { id identifier title state { name } } }
                createdAt
                updatedAt
            }
        }
        """
        data = await _linear_gql(access_token, query, {"id": issue_id})
        issue = data.get("issue")
        if not issue:
            return {"error": f"Issue {issue_id} not found"}

        parent = issue.get("parent")
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "description": (issue.get("description") or "")[:3000],
            "status": (issue.get("state") or {}).get("name", ""),
            "priority": issue.get("priority"),
            "team": (issue.get("team") or {}).get("name", ""),
            "assignee": (issue.get("assignee") or {}).get("name", ""),
            "assignee_email": (issue.get("assignee") or {}).get("email", ""),
            "labels": [l["name"] for l in (issue.get("labels") or {}).get("nodes", [])],
            "comments": [
                {"author": (c.get("user") or {}).get("name", ""), "body": c["body"][:500], "created_at": c["createdAt"]}
                for c in (issue.get("comments") or {}).get("nodes", [])[:10]
            ],
            # Sub-issue context. `parent` is the parent issue (None if this is
            # top-level). `children` is the sub-issue list. Pass the UUIDs back
            # to update_linear_issue's `parent_id` / create_linear_issue's
            # `parent_id` to re-parent or create new sub-issues.
            "parent": (
                {
                    "id": parent["id"],
                    "identifier": parent.get("identifier", ""),
                    "title": parent.get("title", ""),
                }
                if parent else None
            ),
            "children": [
                {
                    "id": c["id"],
                    "identifier": c.get("identifier", ""),
                    "title": c.get("title", ""),
                    "status": (c.get("state") or {}).get("name", ""),
                }
                for c in (issue.get("children") or {}).get("nodes", [])
            ],
            "url": issue.get("url", ""),
            "created_at": issue.get("createdAt", ""),
            "updated_at": issue.get("updatedAt", ""),
        }


class CreateLinearIssue(BaseTool):
    @property
    def name(self) -> str:
        return "create_linear_issue"

    @property
    def description(self) -> str:
        return (
            "Create a new Linear issue. Requires a title and team_id. Use list_linear_teams to find team IDs. "
            "Optionally drop the issue straight into a project (project_id) — use list_linear_projects to find project UUIDs. "
            "Pass `parent_id` (the Linear issue UUID, NOT the identifier like 'ENG-123') to make the new issue a sub-issue "
            "of an existing one. Linear's API supports this directly — there is no need to ask the user to drag issues "
            "in the UI afterward."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Issue title",
                },
                "description": {
                    "type": "string",
                    "description": "Issue description (Markdown supported)",
                },
                "team_id": {
                    "type": "string",
                    "description": "Team ID to create the issue in (use list_linear_teams to find)",
                },
                "priority": {
                    "type": "integer",
                    "description": "Priority: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "User ID to assign the issue to",
                },
                "project_id": {
                    "type": "string",
                    "description": "Optional project UUID to attach the issue to at creation. Use list_linear_projects to find the ID.",
                },
                "label_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional list of label UUIDs to apply.",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Optional parent issue UUID. Setting this makes the new issue a sub-issue of the parent. Pass the issue's UUID (from get_linear_issue / search_linear_issues), NOT the human identifier (e.g. 'ENG-123').",
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date as ISO 8601 'YYYY-MM-DD'. Linear treats this as the target completion date.",
                },
                "estimate": {
                    "type": "integer",
                    "description": "Story-point estimate (1-21 typical). The team's estimation scale must be enabled for this to apply.",
                },
                "cycle_id": {
                    "type": "string",
                    "description": "Optional cycle (sprint) UUID to add the issue to. Use list_linear_cycles to find the current/next cycle.",
                },
            },
            "required": ["title", "team_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        input_fields = {
            "title": params["title"],
            "teamId": params["team_id"],
        }
        if params.get("description"):
            input_fields["description"] = params["description"]
        if params.get("priority") is not None:
            input_fields["priority"] = params["priority"]
        if params.get("assignee_id"):
            input_fields["assigneeId"] = params["assignee_id"]
        if params.get("project_id"):
            input_fields["projectId"] = params["project_id"]
        if params.get("label_ids"):
            input_fields["labelIds"] = params["label_ids"]
        if params.get("parent_id"):
            input_fields["parentId"] = params["parent_id"]
        if params.get("due_date"):
            input_fields["dueDate"] = params["due_date"]
        if params.get("estimate") is not None:
            input_fields["estimate"] = params["estimate"]
        if params.get("cycle_id"):
            input_fields["cycleId"] = params["cycle_id"]

        query = """
        mutation CreateIssue($input: IssueCreateInput!) {
            issueCreate(input: $input) {
                success
                issue {
                    id
                    identifier
                    title
                    url
                    state { name }
                    team { name key }
                }
            }
        }
        """
        data = await _linear_gql(access_token, query, {"input": input_fields})
        result = data.get("issueCreate", {})
        if not result.get("success"):
            return {"error": "Failed to create issue"}

        issue = result.get("issue", {})
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "url": issue.get("url", ""),
            "status": (issue.get("state") or {}).get("name", ""),
            "team": (issue.get("team") or {}).get("name", ""),
        }


class UpdateLinearIssue(BaseTool):
    @property
    def name(self) -> str:
        return "update_linear_issue"

    @property
    def description(self) -> str:
        return (
            "Update a Linear issue. Can change title, description (markdown), status, "
            "priority, assignee, project, labels, or PARENT (sub-issue relationship). "
            "Only pass the fields you want to change — others stay as-is. "
            "Accepts either a UUID or an identifier like 'ENG-123' for issue_id. "
            "Pass project_id to move the issue into a project (find it with list_linear_projects). "
            "Pass parent_id to re-parent the issue under another (or empty string to detach). "
            "Linear's API supports parent-child relationships directly — never tell the user to "
            "drag issues in the UI; use parent_id here instead."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'ENG-123').",
                },
                "title": {
                    "type": "string",
                    "description": "New title (optional).",
                },
                "description": {
                    "type": "string",
                    "description": "New full description (markdown). This REPLACES the existing description — if the user asked to 'add' or 'edit' something, read the current description with get_linear_issue first and send the merged result.",
                },
                "status": {
                    "type": "string",
                    "description": "New status name (e.g. 'In Progress', 'Done'). Will find the matching state ID.",
                },
                "priority": {
                    "type": "integer",
                    "description": "New priority: 0=No priority, 1=Urgent, 2=High, 3=Medium, 4=Low",
                },
                "assignee_id": {
                    "type": "string",
                    "description": "New assignee user ID (or null to unassign)",
                },
                "project_id": {
                    "type": "string",
                    "description": "Move the issue to a project. Pass the project UUID. Pass an empty string to detach the issue from its current project.",
                },
                "label_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Replace label set with these UUIDs (pass an empty array to clear all labels).",
                },
                "parent_id": {
                    "type": "string",
                    "description": "Re-parent the issue. Pass the parent's UUID (NOT the human identifier like 'ENG-123') to make this issue a sub-issue. Pass an empty string to detach from its current parent and become top-level again.",
                },
                "due_date": {
                    "type": "string",
                    "description": "Due date as ISO 8601 'YYYY-MM-DD'. Pass an empty string to clear it.",
                },
                "estimate": {
                    "type": "integer",
                    "description": "Story-point estimate (1-21 typical). Pass 0 to clear the estimate.",
                },
                "cycle_id": {
                    "type": "string",
                    "description": "Move to a cycle (sprint). Pass the cycle UUID, or empty string to detach.",
                },
            },
            "required": ["issue_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        issue_id = params["issue_id"]
        input_fields: dict[str, Any] = {}

        if params.get("title") is not None:
            input_fields["title"] = params["title"]

        if params.get("description") is not None:
            input_fields["description"] = params["description"]

        if params.get("priority") is not None:
            input_fields["priority"] = params["priority"]

        if "assignee_id" in params:
            input_fields["assigneeId"] = params["assignee_id"]

        # Project move: empty string detaches, any other value attaches.
        if "project_id" in params:
            pid = params["project_id"]
            input_fields["projectId"] = pid if pid else None

        # Parent (sub-issue): empty string detaches, any other value re-parents.
        if "parent_id" in params:
            pid = params["parent_id"]
            input_fields["parentId"] = pid if pid else None

        # Due date / estimate / cycle: empty string / 0 → clear.
        if "due_date" in params:
            input_fields["dueDate"] = params["due_date"] or None
        if "estimate" in params:
            est = params["estimate"]
            input_fields["estimate"] = None if (est is None or est == 0) else est
        if "cycle_id" in params:
            cid = params["cycle_id"]
            input_fields["cycleId"] = cid if cid else None

        if "label_ids" in params and isinstance(params["label_ids"], list):
            input_fields["labelIds"] = params["label_ids"]

        # If status is provided, look up the workflow state ID
        if params.get("status"):
            # First get the issue's team to find correct workflow states
            issue_query = """
            query GetIssueTeam($id: String!) {
                issue(id: $id) {
                    team { id }
                }
            }
            """
            issue_data = await _linear_gql(access_token, issue_query, {"id": issue_id})
            team = (issue_data.get("issue") or {}).get("team") or {}
            team_id = team.get("id")

            if team_id:
                states_query = """
                query GetStates($teamId: ID!) {
                    workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                        nodes { id name }
                    }
                }
                """
                states_data = await _linear_gql(access_token, states_query, {"teamId": team_id})
                states = (states_data.get("workflowStates") or {}).get("nodes", [])
                target = params["status"].lower()
                match = next((s for s in states if s["name"].lower() == target), None)
                if match:
                    input_fields["stateId"] = match["id"]
                else:
                    available = [s["name"] for s in states]
                    return {"error": f"Status '{params['status']}' not found. Available: {', '.join(available)}"}

        if not input_fields:
            return {"error": "No fields to update. Provide title, description, status, priority, or assignee_id."}

        query = """
        mutation UpdateIssue($id: String!, $input: IssueUpdateInput!) {
            issueUpdate(id: $id, input: $input) {
                success
                issue {
                    id
                    identifier
                    title
                    url
                    state { name }
                    priority
                    assignee { name }
                }
            }
        }
        """
        data = await _linear_gql(access_token, query, {"id": issue_id, "input": input_fields})
        result = data.get("issueUpdate", {})
        if not result.get("success"):
            return {"error": "Failed to update issue"}

        issue = result.get("issue", {})
        return {
            "id": issue["id"],
            "identifier": issue["identifier"],
            "title": issue["title"],
            "status": (issue.get("state") or {}).get("name", ""),
            "priority": issue.get("priority"),
            "assignee": (issue.get("assignee") or {}).get("name", ""),
            "url": issue.get("url", ""),
        }


class ListLinearTeams(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_teams"

    @property
    def description(self) -> str:
        return "List Linear teams the user has access to. Returns team IDs, names, and keys."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {},
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = """
        query ListTeams {
            teams {
                nodes {
                    id
                    name
                    key
                    description
                    members {
                        nodes {
                            id
                            name
                            email
                        }
                    }
                }
            }
        }
        """
        data = await _linear_gql(access_token, query)
        nodes = (data.get("teams") or {}).get("nodes", [])
        teams = [
            {
                "id": t["id"],
                "name": t["name"],
                "key": t["key"],
                "description": t.get("description", ""),
                "members": [
                    {"id": m["id"], "name": m["name"], "email": m.get("email", "")}
                    for m in (t.get("members") or {}).get("nodes", [])
                ],
            }
            for t in nodes
        ]
        return {"teams": teams, "count": len(teams)}


class CreateLinearComment(BaseTool):
    @property
    def name(self) -> str:
        return "create_linear_comment"

    @property
    def description(self) -> str:
        return (
            "Post a comment on a Linear issue. Accepts either an issue UUID or an identifier "
            "like 'ENG-123'. Body supports Linear's markdown."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'ENG-123').",
                },
                "body": {
                    "type": "string",
                    "description": "Comment body (markdown supported).",
                },
            },
            "required": ["issue_id", "body"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Resolve identifier → UUID if needed (Linear's commentCreate requires UUID).
        issue_ref = params["issue_id"]
        resolve_query = """
        query($id: String!) {
          issue(id: $id) { id identifier title }
        }
        """
        try:
            resolved = await _linear_gql(access_token, resolve_query, {"id": issue_ref})
        except Exception as e:
            return {"error": f"Could not resolve issue '{issue_ref}': {e}"}

        issue = resolved.get("issue")
        if not issue:
            return {"error": f"Issue not found: {issue_ref}"}

        mutation = """
        mutation($input: CommentCreateInput!) {
          commentCreate(input: $input) {
            success
            comment {
              id body url createdAt
              user { id name email }
            }
          }
        }
        """
        try:
            result = await _linear_gql(
                access_token,
                mutation,
                {"input": {"issueId": issue["id"], "body": params["body"]}},
            )
        except Exception as e:
            return {"error": f"Comment create failed: {e}"}

        payload = result.get("commentCreate") or {}
        if not payload.get("success"):
            return {"error": "Linear returned success=false on comment create."}
        c = payload.get("comment") or {}
        return {
            "status": "created",
            "issue": {"id": issue["id"], "identifier": issue["identifier"], "title": issue["title"]},
            "comment": {
                "id": c.get("id"),
                "body": c.get("body"),
                "url": c.get("url"),
                "created_at": c.get("createdAt"),
                "author": (c.get("user") or {}).get("name"),
            },
        }


class ListLinearProjects(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_projects"

    @property
    def description(self) -> str:
        return (
            "List Linear projects. Optionally filter by team name or project state "
            "(e.g. 'started', 'planned', 'completed'). Useful for roadmap context."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "team_name": {
                    "type": "string",
                    "description": "Optional team name to filter by (case-insensitive substring).",
                },
                "state": {
                    "type": "string",
                    "description": "Optional state filter: 'backlog', 'planned', 'started', 'paused', 'completed', 'canceled'.",
                },
                "limit": {"type": "integer", "description": "Max projects (default 50, cap 100).", "default": 50},
            },
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            raw_limit = int(params.get("limit", 50))
        except (TypeError, ValueError):
            raw_limit = 50
        limit = max(1, min(raw_limit, 100))
        # Linear's projectFilter has different shape per version — keep query simple
        # and filter client-side for team name / state.
        query = """
        query($first: Int!) {
          projects(first: $first) {
            nodes {
              id name description state progress
              startDate targetDate
              url
              lead { id name email }
              teams(first: 5) { nodes { id name key } }
            }
          }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {"first": limit})
        except Exception as e:
            return {"error": f"List projects failed: {e}"}

        nodes = (data.get("projects") or {}).get("nodes", [])
        team_filter = (params.get("team_name") or "").strip().lower()
        state_filter = (params.get("state") or "").strip().lower()

        projects = []
        for p in nodes:
            team_names = [t.get("name", "").lower() for t in (p.get("teams") or {}).get("nodes", [])]
            if team_filter and not any(team_filter in n for n in team_names):
                continue
            if state_filter and (p.get("state") or "").lower() != state_filter:
                continue
            projects.append({
                "id": p["id"],
                "name": p["name"],
                "state": p.get("state"),
                "progress": p.get("progress"),
                "start_date": p.get("startDate"),
                "target_date": p.get("targetDate"),
                "url": p.get("url"),
                "lead": (p.get("lead") or {}).get("name"),
                "teams": [
                    {"name": t.get("name"), "key": t.get("key")}
                    for t in (p.get("teams") or {}).get("nodes", [])
                ],
            })
        return {"projects": projects, "count": len(projects)}


class GetLinearProject(BaseTool):
    @property
    def name(self) -> str:
        return "get_linear_project"

    @property
    def description(self) -> str:
        return (
            "Get full details of a Linear project by ID — includes description, progress, "
            "milestones, lead, teams, and a list of issues in the project."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Linear project UUID."},
                "issue_limit": {
                    "type": "integer",
                    "description": "How many issues to include (default 20, cap 100).",
                    "default": 20,
                },
            },
            "required": ["project_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            raw_issue_limit = int(params.get("issue_limit", 20))
        except (TypeError, ValueError):
            raw_issue_limit = 20
        issue_limit = max(1, min(raw_issue_limit, 100))
        query = """
        query($id: String!, $issueLimit: Int!) {
          project(id: $id) {
            id name description state progress
            startDate targetDate url
            lead { id name email }
            teams(first: 10) { nodes { id name key } }
            issues(first: $issueLimit) {
              nodes {
                id identifier title
                state { name type }
                assignee { name }
                priority
              }
            }
          }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {
                "id": params["project_id"],
                "issueLimit": issue_limit,
            })
        except Exception as e:
            return {"error": f"Get project failed: {e}"}

        p = data.get("project")
        if not p:
            return {"error": f"Project not found: {params['project_id']}"}

        return {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description"),
            "state": p.get("state"),
            "progress": p.get("progress"),
            "start_date": p.get("startDate"),
            "target_date": p.get("targetDate"),
            "url": p.get("url"),
            "lead": (p.get("lead") or {}).get("name"),
            "teams": [
                {"id": t["id"], "name": t.get("name"), "key": t.get("key")}
                for t in (p.get("teams") or {}).get("nodes", [])
            ],
            "issues": [
                {
                    "id": i["id"],
                    "identifier": i.get("identifier"),
                    "title": i.get("title"),
                    "status": (i.get("state") or {}).get("name"),
                    "status_type": (i.get("state") or {}).get("type"),
                    "assignee": (i.get("assignee") or {}).get("name"),
                    "priority": i.get("priority"),
                }
                for i in (p.get("issues") or {}).get("nodes", [])
            ],
        }


class CreateLinearProject(BaseTool):
    @property
    def name(self) -> str:
        return "create_linear_project"

    @property
    def description(self) -> str:
        return (
            "Create a new Linear project. Requires a name and at least one team. "
            "Find team UUIDs with list_linear_teams. After creation, drop issues into the "
            "project by calling update_linear_issue with project_id (or pass project_id when "
            "creating new issues)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Project name."},
                "description": {
                    "type": "string",
                    "description": "Project description in Markdown (optional).",
                },
                "team_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Team UUIDs this project belongs to. Pass at least one.",
                },
                "lead_id": {
                    "type": "string",
                    "description": "Optional user UUID for the project lead.",
                },
                "start_date": {
                    "type": "string",
                    "description": "Optional start date (YYYY-MM-DD).",
                },
                "target_date": {
                    "type": "string",
                    "description": "Optional target date (YYYY-MM-DD).",
                },
                "state": {
                    "type": "string",
                    "description": "Optional state: 'backlog', 'planned', 'started', 'paused', 'completed', 'canceled'.",
                },
                "priority": {
                    "type": "integer",
                    "description": "Optional priority: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low.",
                },
            },
            "required": ["name", "team_ids"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        team_ids = params.get("team_ids") or []
        if not team_ids:
            return {"error": "team_ids is required (at least one team UUID)."}

        input_fields: dict[str, Any] = {
            "name": params["name"],
            "teamIds": team_ids,
        }
        if params.get("description"):
            input_fields["description"] = params["description"]
        if params.get("lead_id"):
            input_fields["leadId"] = params["lead_id"]
        if params.get("start_date"):
            input_fields["startDate"] = params["start_date"]
        if params.get("target_date"):
            input_fields["targetDate"] = params["target_date"]
        if params.get("state"):
            input_fields["state"] = params["state"]
        if params.get("priority") is not None:
            input_fields["priority"] = params["priority"]

        query = """
        mutation CreateProject($input: ProjectCreateInput!) {
            projectCreate(input: $input) {
                success
                project {
                    id name description state progress
                    startDate targetDate url
                    lead { id name }
                    teams(first: 10) { nodes { id name key } }
                }
            }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {"input": input_fields})
        except Exception as e:
            return {"error": f"Create project failed: {e}"}

        result = data.get("projectCreate", {})
        if not result.get("success"):
            return {"error": "Failed to create project"}

        p = result.get("project", {})
        return {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description"),
            "state": p.get("state"),
            "url": p.get("url"),
            "lead": (p.get("lead") or {}).get("name"),
            "teams": [
                {"id": t["id"], "name": t.get("name"), "key": t.get("key")}
                for t in (p.get("teams") or {}).get("nodes", [])
            ],
        }


class UpdateLinearProject(BaseTool):
    @property
    def name(self) -> str:
        return "update_linear_project"

    @property
    def description(self) -> str:
        return (
            "Update a Linear project. Patch name, description, dates, lead, state, or priority. "
            "Only pass the fields you want to change. description is Markdown and REPLACES the "
            "existing one — read the current value with get_linear_project first if you want to merge."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "Project UUID."},
                "name": {"type": "string", "description": "New name."},
                "description": {
                    "type": "string",
                    "description": "New full description in Markdown (REPLACES existing).",
                },
                "lead_id": {
                    "type": "string",
                    "description": "New lead user UUID. Pass an empty string to unset.",
                },
                "start_date": {"type": "string", "description": "New start date (YYYY-MM-DD)."},
                "target_date": {"type": "string", "description": "New target date (YYYY-MM-DD)."},
                "state": {
                    "type": "string",
                    "description": "New state: 'backlog', 'planned', 'started', 'paused', 'completed', 'canceled'.",
                },
                "priority": {
                    "type": "integer",
                    "description": "New priority: 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low.",
                },
            },
            "required": ["project_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        input_fields: dict[str, Any] = {}
        if params.get("name") is not None:
            input_fields["name"] = params["name"]
        if params.get("description") is not None:
            input_fields["description"] = params["description"]
        if "lead_id" in params:
            lid = params["lead_id"]
            input_fields["leadId"] = lid if lid else None
        if params.get("start_date") is not None:
            input_fields["startDate"] = params["start_date"]
        if params.get("target_date") is not None:
            input_fields["targetDate"] = params["target_date"]
        if params.get("state"):
            input_fields["state"] = params["state"]
        if params.get("priority") is not None:
            input_fields["priority"] = params["priority"]

        if not input_fields:
            return {"error": "No fields to update. Provide name, description, dates, lead_id, state, or priority."}

        query = """
        mutation UpdateProject($id: String!, $input: ProjectUpdateInput!) {
            projectUpdate(id: $id, input: $input) {
                success
                project {
                    id name description state progress
                    startDate targetDate url
                    lead { id name }
                }
            }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {
                "id": params["project_id"],
                "input": input_fields,
            })
        except Exception as e:
            return {"error": f"Update project failed: {e}"}

        result = data.get("projectUpdate", {})
        if not result.get("success"):
            return {"error": "Failed to update project"}

        p = result.get("project", {})
        return {
            "id": p["id"],
            "name": p["name"],
            "description": p.get("description"),
            "state": p.get("state"),
            "start_date": p.get("startDate"),
            "target_date": p.get("targetDate"),
            "url": p.get("url"),
            "lead": (p.get("lead") or {}).get("name"),
        }


# ---------------------------------------------------------------------------
# Discovery: labels / workflow states / cycles
# Closes the gap where the model can apply label_ids / cycle_id / state but
# can't look up the UUIDs by name. Without these, users had to paste UUIDs.
# ---------------------------------------------------------------------------

class ListLinearLabels(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_labels"

    @property
    def description(self) -> str:
        return (
            "List Linear issue labels (tags). Use the returned UUIDs to set "
            "`label_ids` on create_linear_issue / update_linear_issue. Pass a "
            "team_id to scope to that team's labels (a label can be team-scoped "
            "or workspace-wide); omit it to get all labels the user can see."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "Optional team UUID to scope labels to one team.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max labels to return (default 100, max 250).",
                    "default": 100,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(int(params.get("limit", 100) or 100), 250)
        team_id = params.get("team_id")
        if team_id:
            query = """
            query Labels($teamId: ID!, $first: Int!) {
                issueLabels(first: $first, filter: { team: { id: { eq: $teamId } } }) {
                    nodes { id name color description team { id name } }
                }
            }
            """
            variables = {"teamId": team_id, "first": limit}
        else:
            query = """
            query Labels($first: Int!) {
                issueLabels(first: $first) {
                    nodes { id name color description team { id name } }
                }
            }
            """
            variables = {"first": limit}
        data = await _linear_gql(access_token, query, variables)
        labels = (data.get("issueLabels") or {}).get("nodes", [])
        return {
            "labels": [
                {
                    "id": l["id"],
                    "name": l["name"],
                    "color": l.get("color"),
                    "description": l.get("description"),
                    "team": (l.get("team") or {}).get("name") if l.get("team") else None,
                }
                for l in labels
            ],
            "count": len(labels),
        }


class ListLinearWorkflowStates(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_workflow_states"

    @property
    def description(self) -> str:
        return (
            "List the workflow states (statuses) for a Linear team — Backlog, Todo, "
            "In Progress, Done, etc. Each state has a UUID and a `type` "
            "(backlog / unstarted / started / completed / canceled / triage). "
            "Use this when filtering search_linear_issues by status, or to show "
            "the user the available status names before update_linear_issue."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "Team UUID — required (workflow states are team-scoped). Use list_linear_teams to find.",
                },
            },
            "required": ["team_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = """
        query States($teamId: ID!) {
            workflowStates(filter: { team: { id: { eq: $teamId } } }) {
                nodes { id name type position color description }
            }
        }
        """
        data = await _linear_gql(access_token, query, {"teamId": params["team_id"]})
        states = (data.get("workflowStates") or {}).get("nodes", [])
        states.sort(key=lambda s: s.get("position", 0))
        return {
            "states": [
                {
                    "id": s["id"],
                    "name": s["name"],
                    "type": s.get("type"),
                    "color": s.get("color"),
                    "description": s.get("description"),
                }
                for s in states
            ],
            "count": len(states),
        }


class ListLinearCycles(BaseTool):
    @property
    def name(self) -> str:
        return "list_linear_cycles"

    @property
    def description(self) -> str:
        return (
            "List a team's Linear cycles (sprints). Defaults to current + upcoming "
            "cycles; pass include_past=true to also see completed ones. Returns the "
            "cycle UUID needed for `cycle_id` on create_linear_issue / "
            "update_linear_issue."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "team_id": {
                    "type": "string",
                    "description": "Team UUID. Required (cycles are team-scoped).",
                },
                "include_past": {
                    "type": "boolean",
                    "description": "Include completed cycles in addition to current/upcoming. Default false.",
                    "default": False,
                },
            },
            "required": ["team_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        include_past = bool(params.get("include_past"))
        query = """
        query Cycles($teamId: ID!) {
            cycles(filter: { team: { id: { eq: $teamId } } }, orderBy: createdAt) {
                nodes {
                    id number name startsAt endsAt completedAt
                    progress issueCountHistory
                }
            }
        }
        """
        data = await _linear_gql(access_token, query, {"teamId": params["team_id"]})
        cycles = (data.get("cycles") or {}).get("nodes", [])
        if not include_past:
            cycles = [c for c in cycles if not c.get("completedAt")]
        return {
            "cycles": [
                {
                    "id": c["id"],
                    "number": c.get("number"),
                    "name": c.get("name"),
                    "starts_at": c.get("startsAt"),
                    "ends_at": c.get("endsAt"),
                    "completed_at": c.get("completedAt"),
                    "progress": c.get("progress"),
                }
                for c in cycles
            ],
            "count": len(cycles),
        }


# ---------------------------------------------------------------------------
# Issue relations (blocks / related / duplicate). Different concept from
# parent-child sub-issues — these are cross-issue dependencies, e.g. "ENG-200
# is blocked by ENG-150".
# ---------------------------------------------------------------------------

class CreateLinearRelation(BaseTool):
    @property
    def name(self) -> str:
        return "create_linear_relation"

    @property
    def description(self) -> str:
        return (
            "Create a relationship between two Linear issues. Different from "
            "sub-issue (parent-child): relations are cross-issue dependencies. "
            "Types:\n"
            "  • `blocks` — issue_id blocks related_issue_id (the inverse "
            "    `blocked_by` is created automatically).\n"
            "  • `related` — soft 'see also' link.\n"
            "  • `duplicate` — issue_id duplicates related_issue_id.\n"
            "Pass UUIDs (not human identifiers like 'ENG-123') for both fields."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "UUID of the source issue (the one that 'blocks' / 'is related to' / 'duplicates' the other).",
                },
                "related_issue_id": {
                    "type": "string",
                    "description": "UUID of the target issue.",
                },
                "type": {
                    "type": "string",
                    "enum": ["blocks", "related", "duplicate"],
                    "description": "Relation kind. Linear automatically creates the inverse where applicable.",
                },
            },
            "required": ["issue_id", "related_issue_id", "type"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = """
        mutation CreateRel($input: IssueRelationCreateInput!) {
            issueRelationCreate(input: $input) {
                success
                issueRelation {
                    id type
                    issue { id identifier title }
                    relatedIssue { id identifier title }
                }
            }
        }
        """
        variables = {
            "input": {
                "issueId": params["issue_id"],
                "relatedIssueId": params["related_issue_id"],
                "type": params["type"],
            }
        }
        try:
            data = await _linear_gql(access_token, query, variables)
        except Exception as e:
            return {"error": f"Create relation failed: {e}"}
        result = data.get("issueRelationCreate", {})
        if not result.get("success"):
            return {"error": "Failed to create relation"}
        rel = result.get("issueRelation") or {}
        return {
            "id": rel.get("id"),
            "type": rel.get("type"),
            "issue": (rel.get("issue") or {}).get("identifier"),
            "related_issue": (rel.get("relatedIssue") or {}).get("identifier"),
        }


# ---------------------------------------------------------------------------
# Lifecycle: archive / unarchive
# ---------------------------------------------------------------------------

class ArchiveLinearIssue(BaseTool):
    @property
    def name(self) -> str:
        return "archive_linear_issue"

    @property
    def description(self) -> str:
        return (
            "Archive a Linear issue. The issue is hidden from default views but "
            "preserved (use unarchive_linear_issue to restore). Prefer this over "
            "deletion for closed-out work; Linear has no hard-delete via API."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'ENG-123').",
                },
            },
            "required": ["issue_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = """
        mutation Archive($id: String!) {
            issueArchive(id: $id) { success }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {"id": params["issue_id"]})
        except Exception as e:
            return {"error": f"Archive failed: {e}"}
        if not (data.get("issueArchive") or {}).get("success"):
            return {"error": "Failed to archive issue"}
        return {"status": "archived", "issue_id": params["issue_id"]}


class UnarchiveLinearIssue(BaseTool):
    @property
    def name(self) -> str:
        return "unarchive_linear_issue"

    @property
    def description(self) -> str:
        return "Unarchive (restore) a previously-archived Linear issue."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier.",
                },
            },
            "required": ["issue_id"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        query = """
        mutation Unarchive($id: String!) {
            issueUnarchive(id: $id) { success }
        }
        """
        try:
            data = await _linear_gql(access_token, query, {"id": params["issue_id"]})
        except Exception as e:
            return {"error": f"Unarchive failed: {e}"}
        if not (data.get("issueUnarchive") or {}).get("success"):
            return {"error": "Failed to unarchive issue"}
        return {"status": "unarchived", "issue_id": params["issue_id"]}


# ---------------------------------------------------------------------------
# Attachments — link external URLs (Doc, Notion, Figma, Slack thread) to an
# issue as a first-class attachment with title + subtitle. Better than
# embedding the URL in the description because it shows up in the issue's
# Attachments rail.
# ---------------------------------------------------------------------------

class AttachLinearLink(BaseTool):
    @property
    def name(self) -> str:
        return "attach_linear_link"

    @property
    def description(self) -> str:
        return (
            "Attach a URL to a Linear issue as a first-class attachment (shows in "
            "the issue's Attachments rail). Use this when you've created a related "
            "Doc / Notion page / Figma file / Slack thread and want to link it to "
            "the issue, instead of just pasting the URL into the description."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "issue_id": {
                    "type": "string",
                    "description": "Linear issue UUID or identifier (e.g. 'ENG-123').",
                },
                "url": {
                    "type": "string",
                    "description": "Absolute URL to attach.",
                },
                "title": {
                    "type": "string",
                    "description": "Short title shown in the attachment row.",
                },
                "subtitle": {
                    "type": "string",
                    "description": "Optional sub-line (e.g. 'Design spec', 'Slack thread', file name).",
                },
            },
            "required": ["issue_id", "url", "title"],
        }

    @property
    def provider(self) -> str:
        return "linear"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Linear has two attachment mutations: `attachmentLinkURL` (simple,
        # no subtitle support) and `attachmentCreate` (full input including
        # subtitle, metadata, displayIconUrl). We use the latter so callers
        # can pass a useful subtitle.
        query = """
        mutation AttachLink($input: AttachmentCreateInput!) {
            attachmentCreate(input: $input) {
                success
                attachment { id url title subtitle }
            }
        }
        """
        attach_input: dict[str, Any] = {
            "issueId": params["issue_id"],
            "url": params["url"],
            "title": params["title"],
        }
        if params.get("subtitle"):
            attach_input["subtitle"] = params["subtitle"]
        try:
            data = await _linear_gql(access_token, query, {"input": attach_input})
        except Exception as e:
            return {"error": f"Attach link failed: {e}"}
        result = data.get("attachmentCreate", {})
        if not result.get("success"):
            return {"error": "Failed to attach link"}
        att = result.get("attachment") or {}
        return {
            "id": att.get("id"),
            "url": att.get("url"),
            "title": att.get("title"),
            "subtitle": att.get("subtitle"),
        }
