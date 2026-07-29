"""
GitHub Tools — repos, issues, PRs, code search, notifications via GitHub REST API.
"""

from typing import Any

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

GITHUB_API_URL = "https://api.github.com"


async def _github_get(token: str, path: str, params: dict | None = None,
                      accept: str = "application/vnd.github.v3+json") -> Any:
    """Execute a GET request against the GitHub REST API."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{GITHUB_API_URL}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": accept,
            },
            params=params,
        )
    if resp.status_code != 200:
        raise Exception(f"GitHub API error: {resp.status_code} — {resp.text[:500]}")
    if accept.startswith("application/vnd.github.diff") or accept == "application/vnd.github.raw":
        return resp.text
    return resp.json()


async def _github_request(
    method: str, token: str, path: str, json_body: dict | None = None,
    params: dict | None = None, ok_status: tuple[int, ...] = (200, 201),
) -> Any:
    """POST/PATCH/PUT/DELETE helper for GitHub REST writes."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.request(
            method,
            f"{GITHUB_API_URL}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github.v3+json",
                "Content-Type": "application/json",
            },
            json=json_body,
            params=params,
        )
    if resp.status_code not in ok_status:
        raise Exception(f"GitHub API error: {resp.status_code} — {resp.text[:500]}")
    if resp.status_code == 204 or not resp.text.strip():
        return {}
    return resp.json()


class ListGitHubRepos(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_repos"

    @property
    def description(self) -> str:
        return (
            "List GitHub repositories the authenticated user has access to. "
            "Returns repo name, description, language, and star count."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "sort": {
                    "type": "string",
                    "description": "Sort by: 'updated', 'pushed', 'full_name', 'created' (default 'updated')",
                    "default": "updated",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max repos to return (default 20, max 100)",
                    "default": 20,
                },
                "type": {
                    "type": "string",
                    "description": "Filter by type: 'all', 'owner', 'public', 'private', 'member' (default 'all')",
                    "default": "all",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 20), 100)
        data = await _github_get(access_token, "user/repos", {
            "sort": params.get("sort", "updated"),
            "per_page": limit,
            "type": params.get("type", "all"),
        })
        repos = [
            {
                "full_name": r["full_name"],
                "description": r.get("description", ""),
                "language": r.get("language", ""),
                "stars": r.get("stargazers_count", 0),
                "forks": r.get("forks_count", 0),
                "private": r.get("private", False),
                "url": r.get("html_url", ""),
                "updated_at": r.get("updated_at", ""),
            }
            for r in data
        ]
        return {"repos": repos, "count": len(repos)}


class ListGitHubIssues(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_issues"

    @property
    def description(self) -> str:
        return (
            "List issues in a GitHub repository. "
            "Provide repo in 'owner/repo' format (e.g. 'facebook/react')."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in 'owner/repo' format",
                },
                "state": {
                    "type": "string",
                    "description": "Filter by state: 'open', 'closed', 'all' (default 'open')",
                    "default": "open",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max issues to return (default 20, max 100)",
                    "default": 20,
                },
                "labels": {
                    "type": "string",
                    "description": "Comma-separated list of label names to filter by",
                },
            },
            "required": ["repo"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        limit = min(params.get("limit", 20), 100)
        query_params: dict[str, Any] = {
            "state": params.get("state", "open"),
            "per_page": limit,
        }
        if params.get("labels"):
            query_params["labels"] = params["labels"]

        data = await _github_get(access_token, f"repos/{repo}/issues", query_params)
        # GitHub's issues endpoint also returns PRs — filter them out
        issues = [
            {
                "number": i["number"],
                "title": i["title"],
                "state": i["state"],
                "user": i.get("user", {}).get("login", ""),
                "labels": [l["name"] for l in i.get("labels", [])],
                "assignees": [a["login"] for a in i.get("assignees", [])],
                "comments": i.get("comments", 0),
                "url": i.get("html_url", ""),
                "created_at": i.get("created_at", ""),
                "updated_at": i.get("updated_at", ""),
            }
            for i in data
            if "pull_request" not in i
        ]
        return {"issues": issues, "count": len(issues)}


class GetGitHubPullRequests(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_pull_requests"

    @property
    def description(self) -> str:
        return (
            "List pull requests in a GitHub repository. "
            "Provide repo in 'owner/repo' format."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in 'owner/repo' format",
                },
                "state": {
                    "type": "string",
                    "description": "Filter by state: 'open', 'closed', 'all' (default 'open')",
                    "default": "open",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max PRs to return (default 20, max 100)",
                    "default": 20,
                },
            },
            "required": ["repo"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        limit = min(params.get("limit", 20), 100)
        data = await _github_get(access_token, f"repos/{repo}/pulls", {
            "state": params.get("state", "open"),
            "per_page": limit,
        })
        prs = [
            {
                "number": pr["number"],
                "title": pr["title"],
                "state": pr["state"],
                "user": pr.get("user", {}).get("login", ""),
                "head": pr.get("head", {}).get("ref", ""),
                "base": pr.get("base", {}).get("ref", ""),
                "draft": pr.get("draft", False),
                "mergeable": pr.get("mergeable"),
                "url": pr.get("html_url", ""),
                "created_at": pr.get("created_at", ""),
                "updated_at": pr.get("updated_at", ""),
            }
            for pr in data
        ]
        return {"pull_requests": prs, "count": len(prs)}


class GetGitHubPR(BaseTool):
    @property
    def name(self) -> str:
        return "get_github_pr"

    @property
    def description(self) -> str:
        return (
            "Get detailed information about a specific GitHub pull request, "
            "including description, review status, and changed files."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "Repository in 'owner/repo' format",
                },
                "pr_number": {
                    "type": "integer",
                    "description": "Pull request number",
                },
            },
            "required": ["repo", "pr_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        pr_number = params["pr_number"]

        pr = await _github_get(access_token, f"repos/{repo}/pulls/{pr_number}")

        # Also fetch review comments count and files changed
        files = await _github_get(access_token, f"repos/{repo}/pulls/{pr_number}/files", {"per_page": 30})
        reviews = await _github_get(access_token, f"repos/{repo}/pulls/{pr_number}/reviews")

        return {
            "number": pr["number"],
            "title": pr["title"],
            "body": (pr.get("body") or "")[:3000],
            "state": pr["state"],
            "user": pr.get("user", {}).get("login", ""),
            "head": pr.get("head", {}).get("ref", ""),
            "base": pr.get("base", {}).get("ref", ""),
            "draft": pr.get("draft", False),
            "mergeable": pr.get("mergeable"),
            "merged": pr.get("merged", False),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "changed_files": pr.get("changed_files", 0),
            "url": pr.get("html_url", ""),
            "files": [
                {
                    "filename": f["filename"],
                    "status": f["status"],
                    "additions": f["additions"],
                    "deletions": f["deletions"],
                }
                for f in files[:30]
            ],
            "reviews": [
                {
                    "user": r.get("user", {}).get("login", ""),
                    "state": r.get("state", ""),
                    "body": (r.get("body") or "")[:500],
                }
                for r in reviews
            ],
            "created_at": pr.get("created_at", ""),
            "updated_at": pr.get("updated_at", ""),
        }


class SearchGitHubCode(BaseTool):
    @property
    def name(self) -> str:
        return "search_github_code"

    @property
    def description(self) -> str:
        return (
            "Search code across GitHub repositories. "
            "Use GitHub search syntax (e.g. 'repo:owner/repo filename:foo.py def bar')."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "GitHub code search query (e.g. 'repo:owner/repo language:python class MyClass')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 15, max 30)",
                    "default": 15,
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 15), 30)
        data = await _github_get(access_token, "search/code", {
            "q": params["query"],
            "per_page": limit,
        })
        items = data.get("items", [])
        results = [
            {
                "name": item["name"],
                "path": item["path"],
                "repo": item.get("repository", {}).get("full_name", ""),
                "url": item.get("html_url", ""),
                "score": item.get("score", 0),
            }
            for item in items
        ]
        return {"results": results, "total_count": data.get("total_count", 0), "count": len(results)}


class ListGitHubNotifications(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_notifications"

    @property
    def description(self) -> str:
        return "List the authenticated user's GitHub notifications (PR reviews, issue mentions, etc.)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "all": {
                    "type": "boolean",
                    "description": "Include read notifications (default false, only unread)",
                    "default": False,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max notifications to return (default 20, max 50)",
                    "default": 20,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 20), 50)
        data = await _github_get(access_token, "notifications", {
            "all": str(params.get("all", False)).lower(),
            "per_page": limit,
        })
        notifications = [
            {
                "id": n["id"],
                "reason": n.get("reason", ""),
                "unread": n.get("unread", True),
                "subject_type": n.get("subject", {}).get("type", ""),
                "subject_title": n.get("subject", {}).get("title", ""),
                "repo": n.get("repository", {}).get("full_name", ""),
                "updated_at": n.get("updated_at", ""),
            }
            for n in data
        ]
        return {"notifications": notifications, "count": len(notifications)}


# ---------------------------------------------------------------------------
# Read-side: single-issue lookup, file contents, branches, comments, diff
# ---------------------------------------------------------------------------

class GetGitHubIssue(BaseTool):
    @property
    def name(self) -> str:
        return "get_github_issue"

    @property
    def description(self) -> str:
        return (
            "Read a single GitHub issue (or PR — the GitHub API treats PRs as a "
            "kind of issue) by number. Returns title, body, state, labels, "
            "assignees, milestone, and the latest comments. For PR-specific "
            "fields (head/base ref, mergeable status, reviews) use get_github_pr."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name' (e.g. 'octocat/hello-world')."},
                "issue_number": {"type": "integer", "description": "Issue or PR number."},
            },
            "required": ["repo", "issue_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["issue_number"]
        issue = await _github_get(access_token, f"repos/{repo}/issues/{n}")
        comments_data = await _github_get(access_token, f"repos/{repo}/issues/{n}/comments", {"per_page": 20})
        return {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue.get("state"),
            "is_pull_request": "pull_request" in issue,
            "body": (issue.get("body") or "")[:5000],
            "user": (issue.get("user") or {}).get("login"),
            "assignees": [a["login"] for a in (issue.get("assignees") or [])],
            "labels": [l["name"] for l in (issue.get("labels") or [])],
            "milestone": (issue.get("milestone") or {}).get("title"),
            "url": issue.get("html_url"),
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "closed_at": issue.get("closed_at"),
            "comments_count": issue.get("comments", 0),
            "comments": [
                {
                    "user": (c.get("user") or {}).get("login"),
                    "body": (c.get("body") or "")[:1000],
                    "created_at": c.get("created_at"),
                }
                for c in comments_data
            ],
        }


class GetGitHubFile(BaseTool):
    @property
    def name(self) -> str:
        return "get_github_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file at a path in a GitHub repo. Returns "
            "decoded text up to ~500KB. Optional `ref` can be a branch, tag, "
            "or commit SHA — defaults to the repo's default branch."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "path": {"type": "string", "description": "File path within the repo (e.g. 'src/index.ts')."},
                "ref": {"type": "string", "description": "Optional branch / tag / commit SHA. Defaults to the default branch."},
            },
            "required": ["repo", "path"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        path = params["path"].lstrip("/")
        ref = params.get("ref")
        # `application/vnd.github.raw` returns the raw bytes; the helper unpacks as text.
        try:
            content = await _github_get(
                access_token,
                f"repos/{repo}/contents/{path}",
                {"ref": ref} if ref else None,
                accept="application/vnd.github.raw",
            )
        except Exception as e:
            return {"error": f"Could not read file: {e}"}
        if not isinstance(content, str):
            # API returned JSON metadata (path is a directory). Surface that.
            return {"error": f"'{path}' is not a file (or content was not text)."}
        # Capture the original size BEFORE truncation so callers know the
        # actual file size, not the post-truncation buffer length.
        original_size = len(content)
        truncated = original_size > 500_000
        if truncated:
            content = content[:500_000] + "\n\n[... truncated, showing first 500K chars ...]"
        return {
            "repo": repo,
            "path": path,
            "ref": ref,
            "content": content,
            "size_bytes": original_size,
            "truncated": truncated,
        }


class ListGitHubBranches(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_branches"

    @property
    def description(self) -> str:
        return "List branches in a GitHub repo. Returns up to 100 branches with their head commit SHAs."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "limit": {"type": "integer", "description": "Max branches to return (default 50, max 100).", "default": 50},
            },
            "required": ["repo"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        limit = min(int(params.get("limit") or 50), 100)
        data = await _github_get(access_token, f"repos/{repo}/branches", {"per_page": limit})
        return {
            "branches": [
                {"name": b["name"], "sha": (b.get("commit") or {}).get("sha"),
                 "protected": b.get("protected", False)}
                for b in data
            ],
            "count": len(data),
        }


class GetGitHubPRDiff(BaseTool):
    @property
    def name(self) -> str:
        return "get_github_pr_diff"

    @property
    def description(self) -> str:
        return (
            "Get the unified diff (patch text) for a pull request. Use this when "
            "you need to review the actual changes — get_github_pr returns "
            "file metadata, this returns the readable diff."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "pr_number": {"type": "integer", "description": "Pull request number."},
            },
            "required": ["repo", "pr_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["pr_number"]
        try:
            diff = await _github_get(
                access_token, f"repos/{repo}/pulls/{n}",
                accept="application/vnd.github.diff",
            )
        except Exception as e:
            return {"error": f"Could not fetch diff: {e}"}
        truncated = len(diff) > 200_000
        if truncated:
            diff = diff[:200_000] + "\n\n[... truncated, showing first 200K chars ...]"
        return {"repo": repo, "pr_number": n, "diff": diff, "truncated": truncated}


class ListGitHubIssueComments(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_issue_comments"

    @property
    def description(self) -> str:
        return (
            "List comments on a GitHub issue or pull request (PRs are issues). "
            "Use this to read the existing discussion before adding a new "
            "comment. For PR review comments tied to specific code lines, "
            "use list_github_pr_review_comments instead."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "issue_number": {"type": "integer", "description": "Issue or PR number."},
                "limit": {"type": "integer", "description": "Max comments (default 30, max 100).", "default": 30},
            },
            "required": ["repo", "issue_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["issue_number"]
        limit = min(int(params.get("limit") or 30), 100)
        data = await _github_get(access_token, f"repos/{repo}/issues/{n}/comments", {"per_page": limit})
        return {
            "comments": [
                {
                    "id": c["id"],
                    "user": (c.get("user") or {}).get("login"),
                    "body": (c.get("body") or "")[:2000],
                    "created_at": c.get("created_at"),
                    "url": c.get("html_url"),
                }
                for c in data
            ],
            "count": len(data),
        }


class ListGitHubPRReviewComments(BaseTool):
    @property
    def name(self) -> str:
        return "list_github_pr_review_comments"

    @property
    def description(self) -> str:
        return (
            "List inline review comments on a PR (the ones tied to a specific "
            "file + line). For top-level PR discussion (which appears alongside "
            "issue comments), use list_github_issue_comments."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "pr_number": {"type": "integer", "description": "PR number."},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["repo", "pr_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["pr_number"]
        limit = min(int(params.get("limit") or 30), 100)
        data = await _github_get(access_token, f"repos/{repo}/pulls/{n}/comments", {"per_page": limit})
        return {
            "comments": [
                {
                    "id": c["id"],
                    "user": (c.get("user") or {}).get("login"),
                    "path": c.get("path"),
                    "line": c.get("line") or c.get("original_line"),
                    "body": (c.get("body") or "")[:2000],
                    "created_at": c.get("created_at"),
                    "url": c.get("html_url"),
                }
                for c in data
            ],
            "count": len(data),
        }


# ---------------------------------------------------------------------------
# Write-side: create / update / comment / merge / review
# Issues and PRs share endpoints in GitHub's API (a PR IS an issue with extra
# pull-request fields). So `update_github_issue` works on both.
# ---------------------------------------------------------------------------

class CreateGitHubIssue(BaseTool):
    @property
    def name(self) -> str:
        return "create_github_issue"

    @property
    def description(self) -> str:
        return (
            "Create a new GitHub issue. Requires a repo and title. Optional body "
            "(markdown), assignees, labels, milestone."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "title": {"type": "string", "description": "Issue title."},
                "body": {"type": "string", "description": "Issue body (markdown)."},
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "GitHub usernames to assign.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Label names to apply (must already exist on the repo).",
                },
                "milestone": {
                    "type": "integer",
                    "description": "Optional milestone number (not name).",
                },
            },
            "required": ["repo", "title"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        body: dict[str, Any] = {"title": params["title"]}
        if params.get("body"):
            body["body"] = params["body"]
        if params.get("assignees"):
            body["assignees"] = params["assignees"]
        if params.get("labels"):
            body["labels"] = params["labels"]
        if params.get("milestone") is not None:
            body["milestone"] = params["milestone"]
        try:
            issue = await _github_request("POST", access_token, f"repos/{repo}/issues", json_body=body)
        except Exception as e:
            return {"error": f"Create issue failed: {e}"}
        return {
            "number": issue["number"],
            "title": issue["title"],
            "url": issue.get("html_url"),
            "state": issue.get("state"),
        }


class UpdateGitHubIssue(BaseTool):
    @property
    def name(self) -> str:
        return "update_github_issue"

    @property
    def description(self) -> str:
        return (
            "Update an existing GitHub issue or PR. Can change title, body, "
            "state (open/closed), assignees, labels, milestone. Only pass the "
            "fields you want to change. To close, pass state='closed'. PRs are "
            "issues — this works on both."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "issue_number": {"type": "integer", "description": "Issue or PR number."},
                "title": {"type": "string"},
                "body": {"type": "string", "description": "Replaces existing body. Read first if you want to merge."},
                "state": {"type": "string", "enum": ["open", "closed"]},
                "state_reason": {
                    "type": "string",
                    "enum": ["completed", "not_planned", "reopened"],
                    "description": "Optional reason when changing state — affects how the issue renders.",
                },
                "assignees": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "REPLACES the assignee list. Pass [] to unassign all.",
                },
                "labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "REPLACES the label set. Pass [] to remove all.",
                },
                "milestone": {
                    "type": "integer",
                    "description": "Milestone number, or 0/null to clear.",
                },
            },
            "required": ["repo", "issue_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["issue_number"]
        body: dict[str, Any] = {}
        for k in ("title", "body", "state", "state_reason"):
            if params.get(k) is not None:
                body[k] = params[k]
        if "assignees" in params and isinstance(params["assignees"], list):
            body["assignees"] = params["assignees"]
        if "labels" in params and isinstance(params["labels"], list):
            body["labels"] = params["labels"]
        if "milestone" in params:
            body["milestone"] = params["milestone"] if params["milestone"] else None
        if not body:
            return {"error": "No fields to update."}
        try:
            issue = await _github_request("PATCH", access_token, f"repos/{repo}/issues/{n}", json_body=body)
        except Exception as e:
            return {"error": f"Update failed: {e}"}
        return {
            "number": issue["number"],
            "title": issue["title"],
            "state": issue.get("state"),
            "url": issue.get("html_url"),
        }


class AddCommentToGitHubIssue(BaseTool):
    @property
    def name(self) -> str:
        return "add_comment_to_github_issue"

    @property
    def description(self) -> str:
        return (
            "Add a comment to a GitHub issue or PR. PRs are issues, so the same "
            "endpoint works for both. For inline review comments tied to a "
            "specific file + line on a PR, use add_pr_review_comment instead."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "issue_number": {"type": "integer", "description": "Issue or PR number."},
                "body": {"type": "string", "description": "Comment body (markdown)."},
            },
            "required": ["repo", "issue_number", "body"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["issue_number"]
        try:
            comment = await _github_request(
                "POST", access_token, f"repos/{repo}/issues/{n}/comments",
                json_body={"body": params["body"]},
            )
        except Exception as e:
            return {"error": f"Add comment failed: {e}"}
        return {
            "id": comment["id"],
            "url": comment.get("html_url"),
            "created_at": comment.get("created_at"),
        }


class CreateGitHubPR(BaseTool):
    @property
    def name(self) -> str:
        return "create_github_pr"

    @property
    def description(self) -> str:
        return (
            "Open a new pull request. Requires repo, title, head (branch the PR "
            "comes from), and base (branch to merge into). Optional body, draft "
            "flag. Note: head/base must be branch NAMES, not refs like 'refs/heads/x'."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string", "description": "Repo as 'owner/name'."},
                "title": {"type": "string"},
                "head": {"type": "string", "description": "Source branch name. For cross-repo PRs, prefix with 'fork-owner:'."},
                "base": {"type": "string", "description": "Target branch (typically 'main')."},
                "body": {"type": "string", "description": "PR body (markdown)."},
                "draft": {"type": "boolean", "description": "Open as draft (default false)."},
            },
            "required": ["repo", "title", "head", "base"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        body: dict[str, Any] = {
            "title": params["title"],
            "head": params["head"],
            "base": params["base"],
        }
        if params.get("body"):
            body["body"] = params["body"]
        if params.get("draft"):
            body["draft"] = True
        try:
            pr = await _github_request("POST", access_token, f"repos/{repo}/pulls", json_body=body)
        except Exception as e:
            return {"error": f"Create PR failed: {e}"}
        return {
            "number": pr["number"],
            "title": pr["title"],
            "state": pr.get("state"),
            "draft": pr.get("draft", False),
            "url": pr.get("html_url"),
        }


class MergeGitHubPR(BaseTool):
    @property
    def name(self) -> str:
        return "merge_github_pr"

    @property
    def description(self) -> str:
        return (
            "Merge an open PR. Requires repo and pr_number. Optional commit "
            "title/message override and merge_method (merge / squash / rebase; "
            "default 'merge'). Fails if the PR isn't mergeable (conflicts, "
            "review required, status checks failing)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
                "commit_title": {"type": "string", "description": "Override the merge commit title."},
                "commit_message": {"type": "string", "description": "Override the merge commit message body."},
                "merge_method": {
                    "type": "string",
                    "enum": ["merge", "squash", "rebase"],
                    "description": "How to integrate. Default 'merge' (creates a merge commit).",
                },
            },
            "required": ["repo", "pr_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["pr_number"]
        body: dict[str, Any] = {}
        if params.get("commit_title"):
            body["commit_title"] = params["commit_title"]
        if params.get("commit_message"):
            body["commit_message"] = params["commit_message"]
        if params.get("merge_method"):
            body["merge_method"] = params["merge_method"]
        try:
            result = await _github_request(
                "PUT", access_token, f"repos/{repo}/pulls/{n}/merge",
                json_body=body, ok_status=(200, 201),
            )
        except Exception as e:
            return {"error": f"Merge failed: {e}"}
        return {
            "merged": result.get("merged", False),
            "sha": result.get("sha"),
            "message": result.get("message"),
        }


class RequestPRReview(BaseTool):
    @property
    def name(self) -> str:
        return "request_pr_review"

    @property
    def description(self) -> str:
        return (
            "Request review(s) on a PR from individual users and/or teams. "
            "Pass `reviewers` (list of GitHub usernames) and/or `team_reviewers` "
            "(list of team slugs scoped to the repo's org)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
                "reviewers": {"type": "array", "items": {"type": "string"}},
                "team_reviewers": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo", "pr_number"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["pr_number"]
        body: dict[str, Any] = {}
        if params.get("reviewers"):
            body["reviewers"] = params["reviewers"]
        if params.get("team_reviewers"):
            body["team_reviewers"] = params["team_reviewers"]
        if not body:
            return {"error": "Pass at least one reviewer or team_reviewer."}
        try:
            await _github_request(
                "POST", access_token, f"repos/{repo}/pulls/{n}/requested_reviewers",
                json_body=body, ok_status=(200, 201),
            )
        except Exception as e:
            return {"error": f"Request review failed: {e}"}
        return {
            "status": "review_requested",
            "pr_number": n,
            "reviewers": params.get("reviewers", []),
            "team_reviewers": params.get("team_reviewers", []),
        }


class ReviewGitHubPR(BaseTool):
    @property
    def name(self) -> str:
        return "review_github_pr"

    @property
    def description(self) -> str:
        return (
            "Submit a review on a PR with an event of APPROVE, REQUEST_CHANGES, "
            "or COMMENT. Pass an optional body for top-level review remarks. "
            "Use review_github_pr to actually approve/block; for plain comments "
            "use add_comment_to_github_issue (PR-as-issue) instead."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
                "event": {
                    "type": "string",
                    "enum": ["APPROVE", "REQUEST_CHANGES", "COMMENT"],
                    "description": "APPROVE = approve, REQUEST_CHANGES = block, COMMENT = neutral.",
                },
                "body": {"type": "string", "description": "Top-level review body (markdown). REQUEST_CHANGES requires a body."},
            },
            "required": ["repo", "pr_number", "event"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["pr_number"]
        body: dict[str, Any] = {"event": params["event"]}
        if params.get("body"):
            body["body"] = params["body"]
        try:
            review = await _github_request(
                "POST", access_token, f"repos/{repo}/pulls/{n}/reviews",
                json_body=body, ok_status=(200, 201),
            )
        except Exception as e:
            return {"error": f"Review failed: {e}"}
        return {
            "id": review.get("id"),
            "state": review.get("state"),
            "url": review.get("html_url"),
        }


class AddLabelsToGitHubIssue(BaseTool):
    @property
    def name(self) -> str:
        return "add_labels_to_github_issue"

    @property
    def description(self) -> str:
        return (
            "Add labels to an issue or PR. Existing labels are kept; this is "
            "additive. To replace the label set, use update_github_issue with a "
            "labels=[...] array."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "issue_number": {"type": "integer"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["repo", "issue_number", "labels"],
        }

    @property
    def provider(self) -> str:
        return "github"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        repo = params["repo"]
        n = params["issue_number"]
        try:
            data = await _github_request(
                "POST", access_token, f"repos/{repo}/issues/{n}/labels",
                json_body={"labels": params["labels"]}, ok_status=(200,),
            )
        except Exception as e:
            return {"error": f"Add labels failed: {e}"}
        return {
            "labels": [l["name"] for l in data] if isinstance(data, list) else [],
            "issue_number": n,
        }
