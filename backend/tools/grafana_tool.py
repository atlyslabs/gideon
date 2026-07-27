"""
Grafana tools — read dashboards and query panel data with per-user RBAC.
Uses a global Admin service account token. User permissions are checked
by looking up the user in Grafana and verifying dashboard access.

User-info lookups read from a Redis cache that's pre-warmed every scheduler
cycle by `services.grafana_rbac_sync_service.sync_grafana_rbac` — so the
request path is normally a single Redis GET, not the slow N+1 fan-out that
caused an early-morning timeout. The live API fallback below remains for
cold-start / new-user cases and shares the same retry helper.
"""

import asyncio
import json
import re
import time
from typing import Any
from uuid import UUID

import httpx
import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import GRAFANA_URL, GRAFANA_TOKEN
from db.models import User
from services.grafana_rbac_sync_service import (
    get_cached_perm_decision,
    get_cached_user_info,
    request_with_retry,
    set_cached_perm_decision,
    write_user_fallback,
)
from services.oauth.token_refresh import get_valid_access_token
from tools.internal_tools import InternalTool

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# RBAC: user info cache + permission checks
# ---------------------------------------------------------------------------

# In-process memoization on top of Redis — survives within a single tool
# call's tight loop without re-hitting Redis. Short TTL since the canonical
# cache is the Redis map refreshed every scheduler cycle.
_user_cache: dict[str, tuple[dict, float]] = {}  # email -> (info, timestamp)
_team_members_cache: dict[int, tuple[set, float]] = {}  # team_id -> (set of emails, timestamp)
_CACHE_TTL = 3600  # 1 hour

# Stampede guard: parallel callers for the same email share one in-flight
# live-fetch. Without this, 4 simultaneous query_grafana_panel calls each
# fired their own org/users + teams/search + N team scans — the original
# the same failure mode.
_user_fetch_locks: dict[str, asyncio.Lock] = {}


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {GRAFANA_TOKEN}", "Accept": "application/json"}


async def _get_team_member_emails(team_id: int) -> set[str]:
    """Get all member emails for a team. Cached 1 hour.
    Used only by the live-API fallback below — the scheduler-driven sync
    path doesn't go through this."""
    now = time.time()
    if team_id in _team_members_cache:
        emails, ts = _team_members_cache[team_id]
        if now - ts < _CACHE_TTL:
            return emails

    resp = await request_with_retry("GET", f"/api/teams/{team_id}/members", timeout_sec=10)
    if resp is None or resp.status_code != 200:
        return set()
    members = resp.json()
    emails = {m.get("email", "").lower() for m in members}
    _team_members_cache[team_id] = (emails, now)
    return emails


async def _get_grafana_user_info(email: str) -> dict | None:
    """Resolve a user's Grafana role + team memberships.

    Lookup order:
    1. Process-local memoization (sub-call within this Python process)
    2. Redis map populated by `sync_grafana_rbac` every scheduler cycle —
       the hot path under normal operation
    3. Live API fallback (with retry helper + stampede guard) — covers
       cold-start, brand-new users not yet in the synced map, etc.
    """
    if not email:
        return None
    email_lc = email.lower()
    now = time.time()

    # 1. Process-local memo
    cached = _user_cache.get(email_lc)
    if cached:
        info, ts = cached
        if now - ts < _CACHE_TTL:
            return info

    # 2. Redis map (the warm cache the scheduler keeps fresh)
    redis_info = await get_cached_user_info(email_lc)
    if redis_info is not None:
        _user_cache[email_lc] = (redis_info, now)
        return redis_info

    # 3. Live fallback. Stampede-guarded so N parallel callers share one
    # in-flight fetch.
    lock = _user_fetch_locks.get(email_lc)
    if lock is None:
        lock = asyncio.Lock()
        _user_fetch_locks[email_lc] = lock

    async with lock:
        # Recheck cache: another caller may have populated it while we
        # waited. Re-capture `time.time()` here — the function-entry `now`
        # could be many seconds old by the time we acquire the lock, and a
        # near-TTL entry would otherwise look fresh against that stale clock.
        now_after_lock = time.time()
        cached = _user_cache.get(email_lc)
        if cached:
            info, ts = cached
            if now_after_lock - ts < _CACHE_TTL:
                return info
        return await _live_fetch_user_info(email_lc)


async def _live_fetch_user_info(email_lc: str) -> dict | None:
    """Fall back to the org/users + teams scan when Redis doesn't have the
    user. Uses the shared retry helper for transient errors and parallelizes
    the per-team membership scan."""
    org_users_resp = await request_with_retry("GET", "/api/org/users", timeout_sec=10)
    if org_users_resp is None or org_users_resp.status_code != 200:
        logger.warning(
            "Grafana org users fetch failed (live fallback)",
            status=org_users_resp.status_code if org_users_resp else "no_response",
            email=email_lc,
        )
        return None

    org_users = org_users_resp.json()
    user_data = next(
        (u for u in org_users if (u.get("email") or "").lower() == email_lc),
        None,
    )
    if not user_data:
        logger.warning("Grafana user not found in org", email=email_lc)
        return None

    user_id = user_data["userId"]
    role = user_data.get("role", "Viewer")

    teams_resp = await request_with_retry("GET", "/api/teams/search", timeout_sec=10)
    team_ids: set[int] = set()
    failed_team_ids: list[int] = []
    if teams_resp is not None and teams_resp.status_code == 200:
        all_teams = teams_resp.json().get("teams", [])
        # Parallel scan instead of serial — no more single-slow-team kills the budget.
        member_lookups = await asyncio.gather(
            *[_get_team_member_emails(t["id"]) for t in all_teams],
            return_exceptions=True,
        )
        for team, members in zip(all_teams, member_lookups):
            if isinstance(members, BaseException):
                # Don't silently swallow — a per-team lookup failure flips
                # the decision from allow → deny invisibly. Log the team and
                # exception so we can correlate against permission surprises.
                failed_team_ids.append(team["id"])
                logger.warning(
                    "Grafana team-members lookup failed during user info resolve",
                    team_id=team["id"],
                    team_name=team.get("name"),
                    email=email_lc,
                    error_type=type(members).__name__,
                    error=repr(members),
                )
                continue
            if email_lc in members:
                team_ids.add(team["id"])
        if failed_team_ids:
            # Aggregate breadcrumb so a permission-decision surprise has a
            # single log line to correlate against (alongside the per-team
            # warnings above).
            logger.warning(
                "Grafana user info resolved with partial team data",
                email=email_lc,
                failed_team_count=len(failed_team_ids),
                failed_team_ids=failed_team_ids[:20],
            )

    info = {"id": user_id, "role": role, "team_ids": team_ids}
    _user_cache[email_lc] = (info, time.time())
    # Lazy write-back: persist to Redis so the next caller (and any parallel
    # caller waiting on a different process's lock) skips the fan-out. Daily
    # sync eventually folds them into the main map.
    await write_user_fallback(email_lc, info)
    logger.info("Grafana user info resolved (live fallback)", role=role, teams=len(team_ids))
    return info


async def _check_dashboard_access(
    email: str,
    dashboard_uid: str,
    folder_perms_cache: dict | None = None,
) -> bool:
    """Check if user has access to a specific dashboard via Grafana RBAC.

    Checks dashboard-level permissions first, then walks up the full folder
    ancestor chain. Grafana folders can be nested and permissions are
    inherited. Each Grafana call goes through `request_with_retry` so a
    single transient blip doesn't take the access check down.

    Caches the final allow/deny decision per (email, dashboard_uid) for 5
    min so back-to-back panel queries from the same dashboard skip the
    folder walk entirely.
    """
    # Decision cache hit — skip everything below.
    cached_decision = await get_cached_perm_decision(email, dashboard_uid)
    if cached_decision is not None:
        return cached_decision

    user_info = await _get_grafana_user_info(email)
    if not user_info:
        return False

    # Org admins see everything
    if user_info["role"] == "Admin":
        await set_cached_perm_decision(email, dashboard_uid, True)
        return True

    # `decision` distinguishes "definitively allowed/denied" from "couldn't
    # determine because Grafana was unreachable". We only cache definitive
    # decisions — caching a transient failure as "deny" would lock the user
    # out of a dashboard for 5 min after a single network blip.
    decision: bool | None = None
    try:
        # Check dashboard-level permissions
        resp = await request_with_retry(
            "GET", f"/api/dashboards/uid/{dashboard_uid}/permissions", timeout_sec=10
        )
        if resp is None or resp.status_code != 200:
            return False  # transient — don't cache

        permissions = resp.json()
        if _user_matches_permissions(user_info, permissions):
            decision = True
        else:
            dash_resp = await request_with_retry(
                "GET", f"/api/dashboards/uid/{dashboard_uid}", timeout_sec=10
            )
            if dash_resp is None or dash_resp.status_code != 200:
                return False  # transient — don't cache
            folder_uid = dash_resp.json().get("meta", {}).get("folderUid")

            # Walk up folder ancestors (max 10 levels to prevent infinite loops).
            # If we complete the walk without a match, that's a definitive deny.
            walk_completed = True
            for _ in range(10):
                if not folder_uid:
                    break

                if folder_perms_cache is not None and folder_uid in folder_perms_cache:
                    folder_perms = folder_perms_cache[folder_uid]
                else:
                    folder_resp = await request_with_retry(
                        "GET", f"/api/folders/{folder_uid}/permissions", timeout_sec=10
                    )
                    if folder_resp is None or folder_resp.status_code != 200:
                        walk_completed = False
                        break
                    folder_perms = folder_resp.json()
                    if folder_perms_cache is not None:
                        folder_perms_cache[folder_uid] = folder_perms

                if _user_matches_permissions(user_info, folder_perms):
                    decision = True
                    break

                parent_resp = await request_with_retry(
                    "GET", f"/api/folders/{folder_uid}", timeout_sec=10
                )
                if parent_resp is None or parent_resp.status_code != 200:
                    walk_completed = False
                    break
                folder_uid = parent_resp.json().get("parentUid")

            if decision is None and walk_completed:
                # Walked the full ancestor chain, no match — definitive deny.
                decision = False
    except Exception as e:
        logger.error(
            "Grafana permission check failed",
            uid=dashboard_uid,
            error_type=type(e).__name__,
            error=repr(e),
        )
        return False  # don't cache exceptions

    if decision is not None:
        await set_cached_perm_decision(email, dashboard_uid, decision)
        return decision
    return False  # transient walk-failure — don't cache


def _user_matches_permissions(user_info: dict, permissions: list) -> bool:
    """Check if user matches any permission entry."""
    uid = user_info["id"]
    team_ids = user_info["team_ids"]
    user_role = user_info["role"]

    for perm in permissions:
        if perm.get("userId") == uid:
            return True
        if perm.get("teamId") in team_ids:
            return True
        # Role-based: e.g. all Viewers/Editors in the org
        perm_role = perm.get("role") or perm.get("builtInRole") or ""
        if perm_role == "Viewer" and user_role in ("Viewer", "Editor", "Admin"):
            return True
        if perm_role == "Editor" and user_role in ("Editor", "Admin"):
            return True

    return False


async def _get_user_email(user_id: UUID, session: AsyncSession) -> str | None:
    result = await session.execute(select(User.email).where(User.id == user_id))
    row = result.first()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Variable substitution
# ---------------------------------------------------------------------------

def _apply_modifier(value: str, modifier: str) -> str:
    """Apply a Grafana template variable modifier to a value."""
    values = [v.strip() for v in value.split(",")]
    if modifier in ("singlequote", "sqlstring"):
        return ", ".join(f"'{v}'" for v in values)
    if modifier == "doublequote":
        return ", ".join(f'"{v}"' for v in values)
    if modifier == "csv":
        return ", ".join(values)
    if modifier == "pipe":
        return "|".join(values)
    if modifier == "regex":
        return "|".join(re.escape(v) for v in values)
    if modifier == "glob":
        return "{" + ",".join(values) + "}"
    # raw, json, text, queryparam, lucene — just return as-is
    return value


_ALL_VARS_SENTINEL = "__GIDEON_ALL__"

_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _needs_sql_quoting(value: str) -> bool:
    """True if value (possibly comma-separated) contains anything that needs quoting in SQL.
    Pure numeric items (ints/floats) stay unquoted; everything else gets singlequoted.
    """
    parts = [p.strip() for p in value.split(",")]
    return any(not _NUMERIC_RE.match(p) for p in parts if p)


def _substitute_variables(obj: Any, variables: dict[str, str]) -> Any:
    """Recursively substitute Grafana template variables in strings.

    Handles:
    - ${name:modifier}  e.g. ${date_aggregation:singlequote}
    - ${name}           e.g. ${device_type}
    - $name             e.g. $date_aggregation (bare, word-boundary delimited)
    - $__all variables:  entire AND condition removed so no filter is applied

    Note: Grafana macros like $__timeFilter() are resolved server-side by the
    Grafana API, so we do NOT handle them here.

    Sorts by name length descending to prevent overlap ($host vs $hostname).
    """
    if isinstance(obj, str):
        # Phase 1: Handle $__all variables.
        # We replace `column [NOT] IN ($var)` with `column IS NOT NULL`. This
        # deliberately matches Grafana's NATIVE $__all behavior, not a true
        # no-op. When a Grafana user picks "All", Grafana substitutes the
        # variable with a comma-separated list of every option value, so
        # `column IN ($var)` becomes `column IN ('opt1','opt2',...,'optN')`.
        # For a row where column IS NULL, `NULL IN (...)` evaluates to NULL
        # → treated as FALSE in WHERE → that row is excluded. Same for
        # `NOT IN`. So the rendered Grafana dashboard itself excludes NULL
        # rows under $__all. Our `IS NOT NULL` replacement returns the SAME
        # row set the live dashboard shows — important since users
        # cross-reference our output against Grafana.
        #
        # Why not `1=1` (a true no-op)? It would require capturing the
        # column expression so the dangling `<col>` can be dropped, and
        # column expressions can be arbitrarily complex (qualified names,
        # function calls, COALESCE, CASE) — fragile to match with regex.
        # `IS NOT NULL` works as a suffix to any expression and matches
        # Grafana semantics exactly.
        #
        # The older `LIKE '%'` substitution worked only on string columns
        # and tripped ClickHouse error 43 (`Illegal type UInt8 of argument
        # of function like`) on numeric columns. `IS NOT NULL` is
        # type-agnostic. We also cannot use "OR TRUE" because it breaks
        # SQL AND/OR precedence in the surrounding clause.
        all_var_names = [n for n, v in variables.items() if v == _ALL_VARS_SENTINEL]
        for name in all_var_names:
            pattern = re.escape(name)
            # Handle THEN <col> [NOT] IN (${var}) / IN ($var) inside CASE blocks
            # FIRST (before the general IN replacement, to avoid double-matching).
            # Both `IN` and `NOT IN` collapse to TRUE under $__all (no filter).
            obj = re.sub(
                r"THEN\s+\w+\s+(?:NOT\s+)?IN\s*\(\s*\$\{?" + pattern + r"(?::\w+)?\}?\s*\)",
                "THEN TRUE",
                obj,
            )
            # Replace [NOT] IN (${var:modifier}) and [NOT] IN ($var) with
            # IS NOT NULL (always true for non-null rows, no precedence issues,
            # type-safe). The optional `NOT` MUST be consumed too — otherwise
            # `col NOT IN ($var)` becomes `col NOT IS NOT NULL`, which
            # ClickHouse rejects with `Syntax error ... Expected BETWEEN, LIKE,
            # ILIKE, IN`. Bare form is critical — many ClickHouse panels write
            # IN ($destination_country) without braces.
            obj = re.sub(
                r"(?:NOT\s+)?IN\s*\(\s*\$\{" + pattern + r"(?::\w+)?\}\s*\)",
                "IS NOT NULL",
                obj,
            )
            obj = re.sub(
                r"(?:NOT\s+)?IN\s*\(\s*\$" + pattern + r"\b\s*\)",
                "IS NOT NULL",
                obj,
            )
            # Handle remaining ${var:modifier} forms — replace with NULL.
            # NULL is valid in any SQL context: in `'${var:raw}' = 'yes'` it
            # becomes the string literal `'NULL'` (harmless false compare); in
            # an unquoted `column = ${var:raw}` it becomes `column = NULL`
            # (evaluates to NULL → false). The older `__disabled__` bareword
            # broke unquoted contexts because it parsed as an unknown identifier.
            obj = re.sub(
                r"\$\{" + pattern + r"(?::(\w+))?\}",
                "NULL",
                obj,
            )
            # Catch-all for any remaining bare $var references under $__all.
            # Without this, a leftover `$destination_country` in SELECT/JOIN/WHERE
            # gets parsed by ClickHouse as a table identifier and fails.
            obj = re.sub(
                r"\$" + pattern + r"(?=\b|[^a-zA-Z0-9_]|$)",
                "NULL",
                obj,
            )

        # Phase 2: Substitute remaining variables with actual values
        for name in sorted(variables, key=len, reverse=True):
            value = variables[name]
            if value == _ALL_VARS_SENTINEL:
                continue

            # ${name:modifier} — e.g. ${date_aggregation:singlequote}
            obj = re.sub(
                r"\$\{" + re.escape(name) + r":(\w+)\}",
                lambda m, v=value: _apply_modifier(v, m.group(1)),
                obj,
            )
            # IN ($name) / IN (${name}) bare form — auto-apply singlequote for
            # string-like values (contain space, comma, or non-numeric chars).
            # Panels written as `IN ($var)` rely on Grafana's datasource-specific
            # auto-quoting. Without this, values like "Cancelled By Owner" or
            # multi-value lists break SQL when injected raw. Pure-numeric values
            # (e.g. user_id="123") stay unquoted to avoid breaking typed columns.
            in_replacement = (
                f"IN ({_apply_modifier(value, 'singlequote')})"
                if _needs_sql_quoting(value)
                else f"IN ({value})"
            )
            obj = re.sub(
                r"IN\s*\(\s*\$\{" + re.escape(name) + r"\}\s*\)",
                lambda m, r=in_replacement: r,
                obj,
            )
            obj = re.sub(
                r"IN\s*\(\s*\$" + re.escape(name) + r"\b\s*\)",
                lambda m, r=in_replacement: r,
                obj,
            )
            # ${name} — plain braced form
            obj = re.sub(r"\$\{" + re.escape(name) + r"\}", value, obj)
            # $name — bare form with word boundary
            obj = re.sub(r"\$" + re.escape(name) + r"(?=\b|[^a-zA-Z0-9_]|$)", value, obj)
        return obj
    if isinstance(obj, dict):
        return {k: _substitute_variables(v, variables) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_substitute_variables(item, variables) for item in obj]
    return obj


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ListGrafanaDashboards(InternalTool):
    name = "list_grafana_dashboards"
    description = (
        "Search and list Grafana dashboards. Returns dashboard titles, UIDs, and folders. "
        "Use this to find a dashboard before querying its panels."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query to filter dashboards by name. Leave empty to list all.",
            },
            "limit": {
                "type": "integer",
                "description": "Max dashboards to return. Default 20.",
            },
        },
    }

    @property
    def provider(self) -> str:
        return "grafana"

    async def execute_internal(self, params, user_id, session, **kwargs):
        email = await _get_user_email(user_id, session)
        if not email:
            return {"error": "User not found"}

        user_info = await _get_grafana_user_info(email)
        if not user_info:
            return {"error": f"User {email} not found in Grafana. Make sure your Grafana email matches."}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                query_params = {"type": "dash-db", "limit": params.get("limit", 20)}
                if params.get("query"):
                    query_params["query"] = params["query"]

                resp = await client.get(
                    f"{GRAFANA_URL}/api/search",
                    params=query_params,
                    headers=_headers(),
                )
                if resp.status_code != 200:
                    return {"error": f"Grafana API error: {resp.status_code}"}

                dashboards = resp.json()

                # Filter by user access (skip if org admin)
                if user_info["role"] != "Admin":
                    folder_cache: dict = {}
                    accessible = []
                    for d in dashboards:
                        if await _check_dashboard_access(email, d["uid"], folder_perms_cache=folder_cache):
                            accessible.append(d)
                    dashboards = accessible

                return {
                    "dashboards": [
                        {
                            "uid": d["uid"],
                            "title": d.get("title", ""),
                            "folder": d.get("folderTitle", "General"),
                            "url": d.get("url", ""),
                            "tags": d.get("tags", []),
                        }
                        for d in dashboards
                    ],
                    "count": len(dashboards),
                }
        except Exception as e:
            return {"error": f"Failed to list dashboards: {str(e)}"}


class GetGrafanaDashboard(InternalTool):
    name = "get_grafana_dashboard"
    description = (
        "Get details of a specific Grafana dashboard including all its panels and template variables (filters). "
        "Returns panel names, types, datasource info, and available filter variables with their options. "
        "Use the UID from list_grafana_dashboards. Check the variables section to know what filters can be applied when querying panels."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "uid": {
                "type": "string",
                "description": "The dashboard UID.",
            },
        },
        "required": ["uid"],
    }

    @property
    def provider(self) -> str:
        return "grafana"

    async def execute_internal(self, params, user_id, session, **kwargs):
        email = await _get_user_email(user_id, session)
        if not email:
            return {"error": "User not found"}

        uid = params["uid"]

        if not await _check_dashboard_access(email, uid):
            return {"error": "You don't have access to this dashboard."}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{GRAFANA_URL}/api/dashboards/uid/{uid}",
                    headers=_headers(),
                )
                if resp.status_code != 200:
                    return {"error": f"Dashboard not found or access denied: {resp.status_code}"}

                data = resp.json()
                dashboard = data.get("dashboard", {})
                panels = dashboard.get("panels", [])

                # Extract template variables (filters)
                templating = dashboard.get("templating", {}).get("list", [])
                variables = []
                for v in templating:
                    var_info: dict[str, Any] = {
                        "name": v.get("name", ""),
                        "label": v.get("label") or v.get("name", ""),
                        "type": v.get("type", ""),
                        "description": v.get("description", ""),
                    }
                    # Include current value and whether it's actively filtering
                    current = v.get("current", {})
                    if current:
                        val = current.get("value", "")
                        var_info["current_value"] = val
                        var_info["current_text"] = current.get("text", "")
                        is_all = val == "$__all" or val == ["$__all"] or current.get("text") == "All"
                        var_info["is_filtering"] = not is_all
                    # Include available options (capped to avoid token overflow)
                    options = v.get("options", [])
                    if options:
                        var_info["options"] = [
                            {"text": o.get("text", ""), "value": o.get("value", "")}
                            for o in options[:50]
                        ]
                    # For query-type variables, include the query definition
                    if v.get("type") == "query" and v.get("definition"):
                        var_info["definition"] = v["definition"]
                    variables.append(var_info)

                # Flatten panels: extract nested panels from collapsed rows
                all_panels = []
                for p in panels:
                    if p.get("type") == "row":
                        # Row panels can contain nested panels when collapsed
                        for sub in p.get("panels", []):
                            all_panels.append((p.get("title", ""), sub))
                    else:
                        all_panels.append(("", p))

                # Collect all variable names for reference scanning
                var_names = {v.get("name", "") for v in templating if v.get("name")}

                formatted_panels = []
                for section, p in all_panels:
                    if p.get("type") in ("text",):
                        continue
                    entry: dict[str, Any] = {
                        "id": p.get("id"),
                        "title": p.get("title", ""),
                        "type": p.get("type", ""),
                        "datasource": (p.get("datasource") or {}).get("type", "") if isinstance(p.get("datasource"), dict) else str(p.get("datasource", "")),
                    }
                    if section:
                        entry["section"] = section
                    if p.get("description"):
                        entry["description"] = p["description"]
                    # Scan entire panel JSON for referenced variables
                    # Handles: $var, ${var}, ${var:modifier}
                    used_vars = set()
                    panel_str = json.dumps(p)
                    for vname in var_names:
                        if ("$" + vname) in panel_str or ("${" + vname + "}") in panel_str or ("${" + vname + ":") in panel_str:
                            used_vars.add(vname)
                    if used_vars:
                        entry["uses_variables"] = sorted(used_vars)
                    formatted_panels.append(entry)

                return {
                    "title": dashboard.get("title", ""),
                    "uid": uid,
                    "description": dashboard.get("description", ""),
                    "tags": dashboard.get("tags", []),
                    "variables": variables,
                    "panels": formatted_panels,
                    "panel_count": len(formatted_panels),
                }
        except Exception as e:
            return {"error": f"Failed to get dashboard: {str(e)}"}


class QueryGrafanaPanel(InternalTool):
    name = "query_grafana_panel"
    description = (
        "Execute a Grafana panel's saved query and return the raw data (numbers, time series, tables). "
        "First use get_grafana_dashboard to find the panel ID and available variables, then query it here. "
        "IMPORTANT: Always use this tool to get data from Grafana dashboards. Never query ClickHouse analytics tables directly as a fallback. "
        "CRITICAL: Dashboard variables already have sensible defaults (most commonly used values). "
        "Before querying: 1) Check each panel's 'uses_variables' to know which variables the panel's SQL actually references. "
        "2) For every referenced variable, check its current_value — if the default doesn't match what the user wants, override it. "
        "3) Briefly tell the user which filters will be applied. "
        "Example: if panel uses_variables includes 'funnel_step_2' with default 'Apply Now' but user wants 'Txn', you MUST override it. "
        "The result includes active_filters showing all non-$__all filters that were applied."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "dashboard_uid": {
                "type": "string",
                "description": "The dashboard UID.",
            },
            "panel_id": {
                "type": "integer",
                "description": "The panel ID within the dashboard.",
            },
            "from_time": {
                "type": "string",
                "description": "Start time. Grafana relative format like 'now-24h', 'now-7d', 'now-1h'. Default 'now-24h'.",
            },
            "to_time": {
                "type": "string",
                "description": "End time. Default 'now'.",
            },
            "variables": {
                "type": "object",
                "description": (
                    "Override specific dashboard variables. Only pass variables the user wants DIFFERENT from the dashboard defaults. "
                    "Dashboard defaults are already set to commonly used values. Check current_value from get_grafana_dashboard."
                ),
                "additionalProperties": {"type": "string"},
            },
        },
        "required": ["dashboard_uid", "panel_id"],
    }

    @property
    def provider(self) -> str:
        return "grafana"

    async def execute_internal(self, params, user_id, session, **kwargs):
        email = await _get_user_email(user_id, session)
        if not email:
            return {"error": "User not found"}

        uid = params["dashboard_uid"]
        panel_id = params["panel_id"]
        from_time = params.get("from_time", "now-24h")
        to_time = params.get("to_time", "now")
        variables = params.get("variables", {})
        if not isinstance(variables, dict):
            return {"error": "variables must be a JSON object (key-value pairs), not a list or string."}

        if not await _check_dashboard_access(email, uid):
            return {"error": "You don't have access to this dashboard."}

        return await self.run_query(uid, panel_id, from_time, to_time, variables)

    async def run_query(
        self,
        uid: str,
        panel_id: int,
        from_time: str,
        to_time: str,
        variables: dict,
        row_limit: int = 500,
        timeout: float = 30.0,
    ) -> dict:
        """Run the panel's saved query and return formatted rows.

        Extracted from execute_internal so the bulk-export tool can reuse the
        EXACT same variable merge + `$__all` normalization + funnel-step
        inference + frame formatting, just with a higher row cap. Callers MUST
        do their own RBAC (email + `_check_dashboard_access`) before calling —
        this method assumes access is already granted.
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                # First get the dashboard to extract the panel's query
                dash_resp = await client.get(
                    f"{GRAFANA_URL}/api/dashboards/uid/{uid}",
                    headers=_headers(),
                )
                if dash_resp.status_code != 200:
                    return {"error": f"Dashboard not found: {dash_resp.status_code}"}

                dashboard = dash_resp.json().get("dashboard", {})

                # Merge dashboard's current variable values with user-provided overrides.
                # $__all means "select all" — use sentinel so _substitute_variables
                # replaces the reference with "1" (making IN(1) or CASE...THEN TRUE).
                all_variables = {}
                for v in dashboard.get("templating", {}).get("list", []):
                    current = v.get("current", {})
                    val = current.get("value")
                    if not val:
                        continue
                    if val == "$__all" or val == ["$__all"]:
                        all_variables[v["name"]] = _ALL_VARS_SENTINEL
                    elif isinstance(val, str):
                        all_variables[v["name"]] = val
                    elif isinstance(val, (list, tuple)):
                        filtered = [str(x) for x in val if x != "$__all"]
                        all_variables[v["name"]] = ",".join(filtered) if filtered else _ALL_VARS_SENTINEL
                    else:
                        all_variables[v["name"]] = str(val)
                # Normalize user overrides so that "$__all" (or ["$__all"]) maps to
                # the internal _ALL_VARS_SENTINEL. Workflows and chat callers
                # routinely pass `variables: {"group_name": "$__all"}` (mirroring
                # Grafana's URL syntax). Without this normalization the literal
                # string "$__all" reaches Phase 2 substitution and renders the
                # filter as `IN ('$__all')` — which matches zero rows for any
                # real column. Same goes for `${name:sqlstring}` rendering to
                # `'$__all'`. See run cac55f43 (Boomerang) where this masqueraded
                # as a "pipeline outage".
                normalized_overrides: dict[str, str] = {}
                for k, v in variables.items():
                    if v == "$__all" or v == ["$__all"]:
                        normalized_overrides[k] = _ALL_VARS_SENTINEL
                    elif isinstance(v, (list, tuple)):
                        filtered = [str(x) for x in v if x != "$__all"]
                        normalized_overrides[k] = ",".join(filtered) if filtered else _ALL_VARS_SENTINEL
                    else:
                        normalized_overrides[k] = str(v)
                all_variables.update(normalized_overrides)  # user overrides take precedence

                panel = None
                for p in dashboard.get("panels", []):
                    if p.get("id") == panel_id:
                        panel = p
                        break
                    # Check collapsed row panels
                    for sub in p.get("panels", []):
                        if sub.get("id") == panel_id:
                            panel = sub
                            break

                if not panel:
                    return {"error": f"Panel {panel_id} not found in dashboard {uid}"}

                # Auto-infer funnel step variables from panel title.
                # Panel titles like "$date_aggregation CLP -> Txn" encode the
                # funnel steps. Parse the "X -> Y" pattern and map to
                # funnel_step_1=X, funnel_step_2=Y.
                panel_title = panel.get("title", "")
                templating = dashboard.get("templating", {}).get("list", [])
                # Strip $variable references from title to get the literal text
                clean_title = re.sub(r"\$\{?\w+\}?", "", panel_title).strip()
                # Look for "X -> Y" or "X → Y" pattern
                arrow_match = re.search(r"(\S+)\s*(?:->|→)\s*(\S+)", clean_title)
                if arrow_match:
                    step_left = arrow_match.group(1).strip()
                    step_right = arrow_match.group(2).strip()
                    # Build option lookup for step variables
                    step_var_map = {}  # {var_name: {option_val: True}}
                    for v in templating:
                        vname = v.get("name", "")
                        if "step" not in vname.lower() and "funnel" not in vname.lower():
                            continue
                        opts = {o.get("value", "") for o in v.get("options", []) if o.get("value") and o.get("value") != "$__all"}
                        if opts:
                            step_var_map[vname] = opts
                    # Map: variable with "1" or "entering" or "from" -> left side
                    #       variable with "2" or "converting" or "to" -> right side
                    for vname, opts in step_var_map.items():
                        if vname in variables:
                            continue  # user explicitly set it
                        if any(k in vname.lower() for k in ("_1", "step_1", "entering", "from")):
                            if step_left in opts:
                                all_variables[vname] = step_left
                        elif any(k in vname.lower() for k in ("_2", "step_2", "converting", "to")):
                            if step_right in opts:
                                all_variables[vname] = step_right

                # Extract targets (queries) from the panel
                targets = panel.get("targets", [])
                if not targets:
                    return {"error": "Panel has no queries configured."}

                datasource = panel.get("datasource", {})
                ds_uid = datasource.get("uid") if isinstance(datasource, dict) else None

                # Build the query payload with variable substitution
                queries = []
                for i, target in enumerate(targets):
                    q = {**target}
                    if ds_uid:
                        q["datasource"] = {"uid": ds_uid}
                    if "refId" not in q:
                        q["refId"] = chr(65 + i)  # A, B, C...
                    # Substitute template variables in the query
                    if all_variables:
                        q = _substitute_variables(q, all_variables)
                    queries.append(q)

                payload = {
                    "queries": queries,
                    "from": from_time,
                    "to": to_time,
                }

                # Execute the query via Grafana's datasource proxy
                query_resp = await client.post(
                    f"{GRAFANA_URL}/api/ds/query",
                    json=payload,
                    headers=_headers(),
                )

                if query_resp.status_code != 200:
                    return {"error": f"Query failed: {query_resp.status_code} - {query_resp.text[:2000]}"}

                result = query_resp.json()
                frames = result.get("results", {})

                # Format the results
                formatted = {}
                for ref_id, frame_data in frames.items():
                    frame_list = frame_data.get("frames", [])
                    for frame in frame_list:
                        schema = frame.get("schema", {})
                        data = frame.get("data", {})
                        fields = schema.get("fields", [])
                        values = data.get("values", [])

                        field_names = [f.get("name", f"field_{i}") for i, f in enumerate(fields)]

                        # Build rows from columnar data, capped at `row_limit`
                        # (default 500 for the model-facing query_grafana_panel —
                        # large multi-day breakdowns easily exceed a 100-row cap
                        # and Claude lost visibility past the first few days; 500
                        # keeps tool output bounded (~50KB) while surfacing enough
                        # for week/month analysis). The server-side bulk-export
                        # tool passes a much higher cap since its rows never enter
                        # the model's token stream.
                        if values and len(values) > 0:
                            num_rows = len(values[0]) if values else 0
                            rows = []
                            for row_idx in range(min(num_rows, row_limit)):
                                row = {}
                                for col_idx, name in enumerate(field_names):
                                    if col_idx < len(values):
                                        row[name] = values[col_idx][row_idx] if row_idx < len(values[col_idx]) else None
                                rows.append(row)
                            entry: dict[str, Any] = {
                                "fields": field_names,
                                "row_count": num_rows,
                                "rows": rows,
                            }
                            if num_rows > row_limit:
                                entry["truncated"] = True
                                entry["rows_returned"] = row_limit
                            formatted[ref_id] = entry

                # Build a human-readable summary of active filters (non-$__all)
                active_filters = {
                    k: v for k, v in all_variables.items()
                    if v != _ALL_VARS_SENTINEL
                }
                result_data: dict[str, Any] = {
                    "panel_title": panel.get("title", ""),
                    "query_results": formatted,
                    "time_range": {"from": from_time, "to": to_time},
                    "active_filters": active_filters,
                }
                if all_variables:
                    result_data["applied_filters"] = all_variables
                return result_data
        except Exception as e:
            return {"error": f"Failed to query panel: {str(e)}"}


# Hard cap on rows moved by a single bulk export. Far above any real daily
# dump (these workflows are hundreds of rows), but bounds memory + the Sheets
# write-request size if a panel/time-range accidentally returns a huge set.
_EXPORT_ROW_CAP = 50_000

# Reused for run_query() — it carries no per-call state, so one shared instance
# is fine. (run_query does its own HTTP client per call.)
_PANEL_QUERY = QueryGrafanaPanel()


class ExportGrafanaPanelToSheet(InternalTool):
    name = "export_grafana_panel_to_sheet"
    description = (
        "Run a Grafana panel's query and write ALL of its rows straight into a "
        "Google Sheet, server-side, in a SINGLE call. Use this for bulk "
        "Grafana->Sheet syncs (e.g. a daily 'dump this panel into the Raw tab' "
        "workflow) INSTEAD OF calling query_grafana_panel and then "
        "append_rows_to_spreadsheet / update_spreadsheet yourself. Doing the "
        "transfer here keeps the dataset out of your token stream entirely: the "
        "rows are never returned to you and never re-typed as tool input, so a "
        "300+ row export finishes in one fast step instead of many slow rounds "
        "that risk truncation. You get back only a summary (rows_written, "
        "columns, url). Find the panel with get_grafana_dashboard first, same as "
        "query_grafana_panel. The destination tab is overwritten by default "
        "(its values are cleared, then header + rows written from A1); pass "
        "mode='append' to add to the bottom instead. If you need to TRANSFORM "
        "the data first, request only a sample via query_grafana_panel to decide "
        "the column layout, then call this for the bulk move."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "dashboard_uid": {"type": "string", "description": "The dashboard UID."},
            "panel_id": {"type": "integer", "description": "The panel ID within the dashboard."},
            "from_time": {
                "type": "string",
                "description": "Start time, Grafana relative format ('now-24h', 'now/d', 'now-7d'). Default 'now-24h'.",
            },
            "to_time": {"type": "string", "description": "End time. Default 'now'."},
            "variables": {
                "type": "object",
                "description": "Override dashboard variables, same semantics as query_grafana_panel. Only pass ones that differ from the dashboard defaults.",
                "additionalProperties": {"type": "string"},
            },
            "spreadsheet_id": {"type": "string", "description": "Destination spreadsheet ID."},
            "sheet": {
                "type": "string",
                "description": "Destination tab name (e.g. 'Raw'). Defaults to 'Sheet1'.",
            },
            "mode": {
                "type": "string",
                "enum": ["overwrite", "append"],
                "description": "overwrite (default): clear the tab then write header+rows from A1. append: add rows to the bottom.",
            },
            "columns": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional subset/reordering of panel field names to write. Default: all panel fields, in panel order.",
            },
            "include_header": {
                "type": "boolean",
                "description": "Write the column names as the first row. Default true. Set false when mode='append' to a sheet that already has a header.",
            },
            "ref_id": {
                "type": "string",
                "description": "Which query frame to export when a panel has multiple (e.g. 'A'). Default: the first frame that has rows.",
            },
        },
        "required": ["dashboard_uid", "panel_id", "spreadsheet_id"],
    }

    @property
    def provider(self) -> str:
        # 'grafana' so the executor injects (user_id, session) and skips an
        # Integration lookup. The Google token is resolved inside execute_internal
        # via get_valid_access_token (the same path the google-provider tools use).
        return "grafana"

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        email = await _get_user_email(user_id, session)
        if not email:
            return {"error": "User not found"}

        uid = params.get("dashboard_uid")
        panel_id = params.get("panel_id")
        if not uid or panel_id is None:
            return {"error": "dashboard_uid and panel_id are required."}
        spreadsheet_id = params.get("spreadsheet_id")
        if not spreadsheet_id:
            return {"error": "spreadsheet_id is required."}

        sheet = params.get("sheet") or "Sheet1"
        mode = (params.get("mode") or "overwrite").lower()
        if mode not in ("overwrite", "append"):
            return {"error": "mode must be 'overwrite' or 'append'."}
        include_header = params.get("include_header", True)
        columns = params.get("columns")
        if columns is not None and (
            not isinstance(columns, list) or not all(isinstance(c, str) for c in columns)
        ):
            return {"error": "columns must be a list of field-name strings."}

        from_time = params.get("from_time", "now-24h")
        to_time = params.get("to_time", "now")
        variables = params.get("variables", {})
        if not isinstance(variables, dict):
            return {"error": "variables must be a JSON object (key-value pairs), not a list or string."}

        if not await _check_dashboard_access(email, uid):
            return {"error": "You don't have access to this dashboard."}

        # Release the request DB connection before the long external work below.
        # The only DB read this tool does is `_get_user_email` above; everything
        # after (Grafana query, Google token refresh, Sheets write) is network I/O
        # that can run for many seconds. Committing here ends the implicit
        # transaction so we don't pin a pooled connection / trip
        # idle_in_transaction_session_timeout during that I/O (per the database conventions
        # rules: no external calls inside an open DB session). `get_valid_access_token`
        # opens its own short-lived session, so it doesn't need this one.
        await session.commit()

        # 1) Pull the rows server-side with a high cap (a daily dump must not be
        #    silently capped at the 500 the model-facing tool uses). 60s timeout —
        #    same SQL, but give heavy panels headroom.
        panel = await _PANEL_QUERY.run_query(
            uid, panel_id, from_time, to_time, variables,
            row_limit=_EXPORT_ROW_CAP, timeout=60.0,
        )
        if "error" in panel:
            return panel

        query_results = panel.get("query_results", {})
        if not query_results:
            return {
                "error": "Panel returned no data frames for the given filters/time range.",
                "panel_title": panel.get("panel_title"),
                "active_filters": panel.get("active_filters", {}),
            }

        # 2) Pick the frame: requested ref_id, else the first frame with rows.
        ref_id = params.get("ref_id")
        frame = None
        if ref_id and ref_id in query_results:
            frame = query_results[ref_id]
        else:
            for rid, fr in query_results.items():
                if fr.get("rows"):
                    frame = fr
                    ref_id = rid
                    break
        if not frame or not frame.get("rows"):
            return {
                "error": "No data frame with rows to export.",
                "panel_title": panel.get("panel_title"),
            }

        fields = frame.get("fields", [])
        rows = frame.get("rows", [])
        out_cols = columns or fields
        if columns:
            missing = [c for c in columns if c not in fields]
            if missing:
                return {"error": f"Requested columns not found in panel: {missing}. Available: {fields}"}

        # 3) Build the 2D matrix. Rows never leave the server.
        matrix: list[list[Any]] = []
        if include_header:
            matrix.append(list(out_cols))
        for r in rows:
            matrix.append([r.get(c) for c in out_cols])

        source_count = frame.get("row_count", len(rows))
        truncated = source_count > _EXPORT_ROW_CAP

        # 4) Resolve the user's Google token (handles refresh; None => reconnect).
        token = await get_valid_access_token("google", user_id)
        if not token:
            return {
                "error": "Google token expired and refresh failed. Please reconnect Google.",
                "reconnect": "google",
            }

        # 5) Write server-side in a single Sheets call. A1 sheet names with a
        #    literal apostrophe are escaped by doubling it (Sheets A1 convention).
        a1_sheet = "'" + sheet.replace("'", "''") + "'"

        def _write_sync() -> dict:
            svc = build("sheets", "v4", credentials=Credentials(token=token), cache_discovery=False)
            values_api = svc.spreadsheets().values()
            if mode == "overwrite":
                # Clear the whole tab (formatting preserved), then write from A1.
                values_api.clear(spreadsheetId=spreadsheet_id, range=a1_sheet, body={}).execute()
                res = values_api.update(
                    spreadsheetId=spreadsheet_id,
                    range=f"{a1_sheet}!A1",
                    valueInputOption="USER_ENTERED",
                    body={"values": matrix},
                ).execute()
                return {
                    "updated_cells": res.get("updatedCells", 0),
                    "updated_range": res.get("updatedRange", ""),
                }
            res = values_api.append(
                spreadsheetId=spreadsheet_id,
                range=f"{a1_sheet}!A1",
                valueInputOption="USER_ENTERED",
                insertDataOption="INSERT_ROWS",
                body={"values": matrix},
            ).execute()
            upd = res.get("updates", {})
            return {
                "updated_cells": upd.get("updatedCells", 0),
                "updated_range": upd.get("updatedRange", ""),
            }

        try:
            write = await asyncio.to_thread(_write_sync)
        except Exception as e:
            return {"error": f"Sheet write failed: {e}"}

        data_rows = len(matrix) - (1 if include_header else 0)
        result: dict[str, Any] = {
            "ok": True,
            "rows_written": data_rows,
            "columns": list(out_cols),
            "header_written": bool(include_header),
            "mode": mode,
            "sheet": sheet,
            "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}",
            "panel_title": panel.get("panel_title"),
            "source_row_count": source_count,
            "active_filters": panel.get("active_filters", {}),
            "sheets_api": write,
        }
        if truncated:
            result["truncated"] = True
            result["note"] = (
                f"Panel returned {source_count} rows; only the first {_EXPORT_ROW_CAP} "
                "were written. Narrow the time range or filters to export the rest."
            )
        return result


