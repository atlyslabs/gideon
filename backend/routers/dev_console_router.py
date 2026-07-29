"""
Dev Console Router — fresh GitHub + Linear data for the right-panel "console" tab.

Reads pull straight from GitHub REST / Linear GraphQL on every hit so the panel
shows real-time state. Writes (merge / comment / transition) go straight to the
provider with the user's OAuth token, mirroring what the chat tools do.
"""

from typing import Optional
from uuid import UUID

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db import get_db
from db.database import session_manager
from db.models import Integration, User
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.encryption_service import decrypt
from services.oauth.token_refresh import get_valid_access_token
from services.user_service import get_or_create_user

logger = structlog.get_logger()

router = APIRouter()


async def _require_dev_console_enabled(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
) -> User:
    """Gate every /dev-console endpoint on the per-user feature flag.

    Returns the User row so handlers can reuse it instead of re-resolving.
    """
    user = await get_or_create_user(session, auth_user)
    if not (user.feature_dev_console or False):
        raise HTTPException(
            status_code=403,
            detail={"error": "feature_disabled", "feature": "dev_console"},
        )
    return user


async def _resolve_token(provider: str, user_id: UUID) -> str | None:
    """Return a usable access token, handling both refreshable providers and
    static-token providers (e.g. GitHub, which has no refresh_token and tokens
    that don't expire).

    `get_valid_access_token` is designed for providers that issue short-lived
    tokens with refresh_tokens (linear, notion, google, ...). It returns None
    for GitHub because step 1 short-circuits on the missing refresh_token. The
    chat tool path reads + decrypts the integration row directly in that case;
    we mirror that pattern here so the panel doesn't show "not connected" for
    a perfectly healthy GitHub integration.
    """
    token = await get_valid_access_token(provider, user_id)
    if token:
        return token
    # Fallback: read the active integration row and decrypt access_token.
    async with session_manager.get_session() as s:
        row = await s.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == provider,
                Integration.is_active == True,  # noqa: E712 — SQLAlchemy boolean comparison
            )
        )
        integration = row.scalar_one_or_none()
    if not integration or not integration.access_token:
        return None
    try:
        return decrypt(integration.access_token)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------

class PullRequestRef(BaseModel):
    repo: str
    number: int
    title: str
    url: str
    author: str
    state: str  # "open" | "closed" | "merged"
    draft: bool = False
    is_review_request: bool = False  # user is a requested reviewer
    additions: Optional[int] = None
    deletions: Optional[int] = None
    updated_at: str
    checks_state: Optional[str] = None  # "success" | "failure" | "pending" | None


class GitHubPayload(BaseModel):
    connected: bool
    login: Optional[str] = None
    review_requests: list[PullRequestRef] = []
    authored: list[PullRequestRef] = []


class LinearIssueRef(BaseModel):
    id: str
    identifier: str
    title: str
    url: str
    state: str
    state_type: str  # "backlog" | "unstarted" | "started" | "completed" | "canceled" | "triage"
    priority: int
    team: str
    updated_at: str


class LinearWorkflowState(BaseModel):
    id: str
    name: str
    type: str
    team_id: str
    team_key: str


class LinearPayload(BaseModel):
    connected: bool
    issues: list[LinearIssueRef] = []
    workflow_states: list[LinearWorkflowState] = []


class CommentBody(BaseModel):
    body: str


class MergeBody(BaseModel):
    method: str = "merge"  # "merge" | "squash" | "rebase"


class StateBody(BaseModel):
    state_id: str


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------

GITHUB_API = "https://api.github.com"


async def _gh_get(token: str, path: str, params: dict | None = None) -> dict | list:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{GITHUB_API}/{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
            },
            params=params,
        )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail={"error": "github_unauthorized", "reconnect": "github"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"GitHub: {r.status_code} {r.text[:200]}")
    return r.json()


def _gh_pr_from_search(item: dict, is_review_request: bool) -> PullRequestRef:
    # `repository_url` looks like https://api.github.com/repos/octocat/hello-world
    repo_url: str = item.get("repository_url", "")
    repo = "/".join(repo_url.split("/")[-2:]) if repo_url else ""
    return PullRequestRef(
        repo=repo,
        number=item.get("number", 0),
        title=item.get("title", ""),
        url=item.get("html_url", ""),
        author=(item.get("user") or {}).get("login", ""),
        state="merged" if item.get("pull_request", {}).get("merged_at") else item.get("state", "open"),
        draft=bool(item.get("draft", False) or (item.get("pull_request") or {}).get("draft", False)),
        is_review_request=is_review_request,
        updated_at=item.get("updated_at", ""),
    )


@router.get("/github", response_model=GitHubPayload)
async def get_github_panel(
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("github", user.id)
    if not token:
        return GitHubPayload(connected=False)

    # 1. Get login (cheap; cached on GH side too)
    try:
        me = await _gh_get(token, "user")
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("dev_console.github.me_failed", error=str(e))
        return GitHubPayload(connected=False)

    login = me.get("login", "") if isinstance(me, dict) else ""
    if not login:
        return GitHubPayload(connected=True)

    # 2. Two search queries in parallel-ish — sequential is fine, GH is fast
    try:
        review_req = await _gh_get(
            token,
            "search/issues",
            params={
                "q": f"is:open is:pr review-requested:{login} archived:false",
                "per_page": 20,
                "sort": "updated",
                "order": "desc",
            },
        )
        authored = await _gh_get(
            token,
            "search/issues",
            params={
                "q": f"is:open is:pr author:{login} archived:false",
                "per_page": 20,
                "sort": "updated",
                "order": "desc",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("dev_console.github.search_failed", error=str(e))
        return GitHubPayload(connected=True, login=login)

    return GitHubPayload(
        connected=True,
        login=login,
        review_requests=[_gh_pr_from_search(it, True) for it in (review_req.get("items") or [])],
        authored=[_gh_pr_from_search(it, False) for it in (authored.get("items") or [])],
    )


@router.post("/github/pr/{owner}/{repo}/{number}/comment")
async def github_comment_pr(
    owner: str,
    repo: str,
    number: int,
    body: CommentBody,
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("github", user.id)
    if not token:
        raise HTTPException(status_code=400, detail={"error": "github_not_connected", "reconnect": "github"})
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Empty comment")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/issues/{number}/comments",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"body": body.body},
        )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail={"error": "github_unauthorized", "reconnect": "github"})
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"GitHub: {r.status_code} {r.text[:200]}")
    data = r.json()
    return {"ok": True, "comment_url": data.get("html_url", "")}


@router.post("/github/pr/{owner}/{repo}/{number}/merge")
async def github_merge_pr(
    owner: str,
    repo: str,
    number: int,
    body: MergeBody,
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("github", user.id)
    if not token:
        raise HTTPException(status_code=400, detail={"error": "github_not_connected", "reconnect": "github"})
    if body.method not in ("merge", "squash", "rebase"):
        raise HTTPException(status_code=422, detail="Invalid merge method")
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.put(
            f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{number}/merge",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            json={"merge_method": body.method},
        )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail={"error": "github_unauthorized", "reconnect": "github"})
    if r.status_code == 405:
        # Not mergeable — conflicts, required reviews, failing checks
        try:
            msg = r.json().get("message", "Not mergeable")
        except Exception:
            msg = "Not mergeable"
        raise HTTPException(status_code=409, detail=msg)
    if r.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"GitHub: {r.status_code} {r.text[:200]}")
    data = r.json()
    return {"ok": bool(data.get("merged")), "sha": data.get("sha", ""), "message": data.get("message", "")}


# ---------------------------------------------------------------------------
# Linear
# ---------------------------------------------------------------------------

LINEAR_API = "https://api.linear.app/graphql"


async def _linear_gql(token: str, query: str, variables: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            LINEAR_API,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"query": query, "variables": variables or {}},
        )
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail={"error": "linear_unauthorized", "reconnect": "linear"})
    if r.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Linear: {r.status_code} {r.text[:200]}")
    data = r.json()
    if "errors" in data:
        raise HTTPException(status_code=502, detail=f"Linear: {data['errors']}")
    return data.get("data", {})


_LINEAR_PANEL_QUERY = """
query DevConsolePanel {
  issues(
    first: 25,
    filter: { assignee: { isMe: { eq: true } }, state: { type: { nin: ["completed", "canceled"] } } },
    orderBy: updatedAt
  ) {
    nodes {
      id
      identifier
      title
      url
      priority
      updatedAt
      state { id name type }
      team { id name key }
    }
  }
  workflowStates(first: 100) {
    nodes {
      id
      name
      type
      team { id key }
    }
  }
}
"""


@router.get("/linear", response_model=LinearPayload)
async def get_linear_panel(
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("linear", user.id)
    if not token:
        return LinearPayload(connected=False)

    try:
        data = await _linear_gql(token, _LINEAR_PANEL_QUERY)
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("dev_console.linear.failed", error=str(e))
        return LinearPayload(connected=False)

    issues_nodes = (data.get("issues") or {}).get("nodes", [])
    states_nodes = (data.get("workflowStates") or {}).get("nodes", [])

    issues = [
        LinearIssueRef(
            id=n["id"],
            identifier=n["identifier"],
            title=n["title"],
            url=n.get("url", ""),
            state=(n.get("state") or {}).get("name", ""),
            state_type=(n.get("state") or {}).get("type", ""),
            priority=int(n.get("priority") or 0),
            team=(n.get("team") or {}).get("name", ""),
            updated_at=n.get("updatedAt", ""),
        )
        for n in issues_nodes
    ]
    states = [
        LinearWorkflowState(
            id=s["id"],
            name=s["name"],
            type=s["type"],
            team_id=(s.get("team") or {}).get("id", ""),
            team_key=(s.get("team") or {}).get("key", ""),
        )
        for s in states_nodes
    ]
    return LinearPayload(connected=True, issues=issues, workflow_states=states)


@router.post("/linear/issue/{issue_id}/state")
async def linear_set_state(
    issue_id: str,
    body: StateBody,
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("linear", user.id)
    if not token:
        raise HTTPException(status_code=400, detail={"error": "linear_not_connected", "reconnect": "linear"})
    mutation = """
    mutation UpdateState($id: String!, $stateId: String!) {
      issueUpdate(id: $id, input: { stateId: $stateId }) {
        success
        issue { id state { name type } }
      }
    }
    """
    data = await _linear_gql(token, mutation, {"id": issue_id, "stateId": body.state_id})
    res = data.get("issueUpdate") or {}
    if not res.get("success"):
        raise HTTPException(status_code=502, detail="Linear: update failed")
    return {
        "ok": True,
        "state": (res.get("issue") or {}).get("state", {}),
    }


@router.post("/linear/issue/{issue_id}/comment")
async def linear_comment(
    issue_id: str,
    body: CommentBody,
    user: User = Depends(_require_dev_console_enabled),
):
    token = await _resolve_token("linear", user.id)
    if not token:
        raise HTTPException(status_code=400, detail={"error": "linear_not_connected", "reconnect": "linear"})
    if not body.body.strip():
        raise HTTPException(status_code=422, detail="Empty comment")
    mutation = """
    mutation CreateComment($issueId: String!, $body: String!) {
      commentCreate(input: { issueId: $issueId, body: $body }) {
        success
        comment { id url }
      }
    }
    """
    data = await _linear_gql(token, mutation, {"issueId": issue_id, "body": body.body})
    res = data.get("commentCreate") or {}
    if not res.get("success"):
        raise HTTPException(status_code=502, detail="Linear: comment failed")
    return {"ok": True, "comment_url": (res.get("comment") or {}).get("url", "")}
