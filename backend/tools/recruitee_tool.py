"""
Recruitee Tools -- ATS (Applicant Tracking System) integration.

Uses the Recruitee admin REST API with per-user API keys stored in the
integrations table.
  Base URL: https://api.recruitee.com/c/{company_id}
  Auth:     Bearer token (personal API token from Recruitee Settings ->
            Apps and plugins -> Personal API tokens).
  Company ID stored in integration.extra_data["company_id"] AND packed into the
  access_token as "{api_key}||{company_id}" so it flows through the shared
  tool_executor token pipeline. Tools split it apart via _get_api_key /
  _get_company_id.

Scope: this covers the full recruiting workflow — candidate search (with the
full filter set), candidate / offer / placement / pipeline / note / task /
evaluation / interview / tag / source / department / admin / talent-pool reads,
plus the write surface (create candidate, move stage, disqualify, add note,
tag, create task, schedule interview, create/update offer). Pure config/CMS
plumbing (careers-site CMS, email templates, languages, subdomains, push
devices, OAuth-token management, sharing containers, texting) is intentionally
NOT exposed.

Recruitee write bodies follow the Rails convention of wrapping the resource:
``{"candidate": {...}}``, ``{"note": {...}}``, ``{"placement": {...}}`` etc.
Mutation tools should be smoke-tested against a live key before heavy reliance
(see scripts/smoke_new_tools.py).
"""

import json
from datetime import datetime, timezone
from typing import Any, Optional

import httpx
import structlog

from tools.base import BaseTool

logger = structlog.get_logger()

RECRUITEE_BASE = "https://api.recruitee.com/c"
_TIMEOUT = 20.0


# --------------------------------------------------------------------------- #
# HTTP plumbing
# --------------------------------------------------------------------------- #

def _get_company_id(access_token: str) -> str:
    """Extract company_id from the packed "{api_key}||{company_id}" token."""
    if "||" in access_token:
        return access_token.split("||", 1)[1]
    return ""


def _get_api_key(access_token: str) -> str:
    """Extract the actual API key from the packed token."""
    if "||" in access_token:
        return access_token.split("||", 1)[0]
    return access_token


async def _request(
    method: str,
    access_token: str,
    company_id: str,
    path: str,
    *,
    params: Optional[dict] = None,
    json_body: Optional[dict] = None,
) -> dict:
    """Single entry point for every Recruitee call (GET/POST/PATCH/DELETE).

    Returns the parsed JSON on success, or ``{"error": ...}`` on a non-2xx.
    Raises on 401 so tool_executor's reactive-refresh / reconnect flow fires.
    """
    if not company_id:
        return {"error": "Recruitee company ID not configured. Please reconnect.", "reconnect": "recruitee"}

    url = f"{RECRUITEE_BASE}/{company_id}{path}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json",
    }
    if json_body is not None:
        headers["Content-Type"] = "application/json"

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        response = await client.request(
            method, url, headers=headers, params=params, json=json_body
        )

    if response.status_code == 401:
        # Surface to tool_executor, which maps recruitee 401 -> reconnect prompt.
        raise Exception("401: Recruitee API key is invalid or expired")
    if response.status_code == 403:
        return {"error": "Recruitee 403 Forbidden — your API key's role/plan may not permit this resource."}
    if response.status_code == 404:
        return {"error": "Recruitee resource not found (404)."}
    if response.status_code not in (200, 201, 204):
        logger.error(
            "Recruitee API error",
            status=response.status_code, method=method, path=path,
            body=response.text[:500],
        )
        return {"error": f"Recruitee API error: {response.status_code}", "detail": response.text[:300]}

    if response.status_code == 204 or not response.content:
        return {"success": True}
    try:
        return response.json()
    except Exception:
        return {"success": True, "raw": response.text[:500]}


async def _recruitee_get(access_token: str, company_id: str, path: str, params: dict | None = None) -> dict:
    return await _request("GET", access_token, company_id, path, params=params)


def _iso_to_unix(value: str) -> Optional[int]:
    """Convert an ISO date/datetime to a unix timestamp (Recruitee date filters
    use unix seconds). Returns None if unparseable."""
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


# --------------------------------------------------------------------------- #
# Candidate search (full filter set)
# --------------------------------------------------------------------------- #

# Verified against /search/new/candidates: filters_json is an array of objects
# shaped as {"field": <name>, <operator>: <value>} (text uses {"field":"all",
# "query":...}). Operators: eq, gt, gte, lt, lte, in, not_in, contains,
# not_contains, has_any, has_none, has_one_of, has_all_of, all_in.
_SORT_OPTIONS = [
    "created_at_desc", "created_at_asc",
    "last_activity_desc", "last_activity_asc",
    "name_asc", "name_desc",
    "rating_desc", "rating_asc",
]


class SearchCandidates(BaseTool):
    @property
    def name(self) -> str:
        return "search_recruitee_candidates"

    @property
    def description(self) -> str:
        return (
            "Search candidates in the Recruitee ATS with the full filter set. "
            "Filter by free-text query, job/offer, pipeline stage, status "
            "(qualified/disqualified/hired), tags, sources, created-date range, "
            "minimum rating, talent pool, department, and assigned recruiter. "
            "For anything not covered by the named params, pass `raw_filters` "
            "(an array of Recruitee filter objects). Returns name, emails, "
            "source, current job + stage, rating, and created date."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Free-text search across name/email/profile. Omit to list all (subject to filters)."},
                "offer_id": {"type": "integer", "description": "Only candidates on this job/offer."},
                "stage": {"type": "string", "description": "Pipeline stage name to filter by (e.g. 'Onsite', 'Offer')."},
                "status": {"type": "string", "enum": ["qualified", "disqualified", "hired"], "description": "Candidate status filter."},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Match candidates having ANY of these tags."},
                "sources": {"type": "array", "items": {"type": "string"}, "description": "Match candidates from ANY of these sources."},
                "created_after": {"type": "string", "description": "ISO date/datetime — only candidates created on/after this."},
                "created_before": {"type": "string", "description": "ISO date/datetime — only candidates created on/before this."},
                "min_rating": {"type": "number", "description": "Minimum average score/rating (0-100)."},
                "talent_pool_id": {"type": "integer", "description": "Only candidates in this talent pool."},
                "department_id": {"type": "integer", "description": "Only candidates in this department's jobs."},
                "admin_id": {"type": "integer", "description": "Only candidates assigned to this recruiter (admin id)."},
                "raw_filters": {"type": "array", "items": {"type": "object"}, "description": "Advanced: raw Recruitee filter objects, e.g. [{\"field\":\"education\",\"contains\":[\"PhD\"]}]. Merged with the named filters."},
                "sort_by": {"type": "string", "enum": _SORT_OPTIONS, "description": "Sort order (default created_at_desc)."},
                "limit": {"type": "integer", "description": "Max results (default 30, max 100)."},
                "page": {"type": "integer", "description": "Page number for pagination (default 1)."},
            },
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    def _build_filters(self, params: dict[str, Any]) -> list[dict]:
        filters: list[dict] = []
        if params.get("query"):
            filters.append({"field": "all", "query": params["query"]})
        if params.get("offer_id"):
            filters.append({"field": "jobs", "has_any": [params["offer_id"]]})
        if params.get("stage"):
            filters.append({"field": "stage", "in": [params["stage"]]})
        if params.get("status"):
            filters.append({"field": "status", "in": [params["status"]]})
        if params.get("tags"):
            filters.append({"field": "tags", "has_any": params["tags"]})
        if params.get("sources"):
            filters.append({"field": "sources", "has_any": params["sources"]})
        if params.get("min_rating") is not None:
            filters.append({"field": "average_score", "gte": params["min_rating"]})
        if params.get("talent_pool_id"):
            filters.append({"field": "talent_pools", "has_any": [params["talent_pool_id"]]})
        if params.get("department_id"):
            filters.append({"field": "departments", "has_any": [params["department_id"]]})
        if params.get("admin_id"):
            filters.append({"field": "admins", "has_any": [params["admin_id"]]})
        created: dict[str, Any] = {}
        ca = _iso_to_unix(params.get("created_after", ""))
        cb = _iso_to_unix(params.get("created_before", ""))
        if ca is not None:
            created["gte"] = ca
        if cb is not None:
            created["lte"] = cb
        if created:
            filters.append({"field": "created_at", **created})
        if params.get("raw_filters"):
            filters.extend(params["raw_filters"])
        return filters

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)

        query_params: dict[str, Any] = {
            "limit": min(params.get("limit", 30), 100),
            "page": max(params.get("page", 1), 1),
            "sort_by": params.get("sort_by", "created_at_desc"),
        }
        filters = self._build_filters(params)
        if filters:
            query_params["filters_json"] = json.dumps(filters)

        data = await _recruitee_get(api_key, company_id, "/search/new/candidates", query_params)
        if "error" in data:
            return data

        results = []
        for c in data.get("hits", []):
            placements = c.get("placements", []) or []
            cur = placements[0] if placements else {}
            results.append({
                "id": c.get("id"),
                "name": c.get("name", ""),
                "emails": c.get("emails", []),
                "source": c.get("source", ""),
                "rating": c.get("rating") or c.get("average_score"),
                "current_job": (cur.get("offer", {}) or {}).get("title", ""),
                "current_stage": (cur.get("stage", {}) or {}).get("name", ""),
                "disqualified": cur.get("disqualified", False),
                "created_at": c.get("created_at", ""),
            })

        return {"candidates": results, "count": len(results), "total": data.get("total", 0)}


# --------------------------------------------------------------------------- #
# Candidate reads
# --------------------------------------------------------------------------- #

class GetCandidate(BaseTool):
    @property
    def name(self) -> str:
        return "get_recruitee_candidate"

    @property
    def description(self) -> str:
        return "Get full detail on a Recruitee candidate by ID: profile, emails/phones, source, rating, cover letter, placements (job + stage + disqualification), social links, custom fields, and admin URL."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"}}, "required": ["candidate_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, f"/candidates/{params['candidate_id']}")
        if "error" in data:
            return data
        c = data.get("candidate", data)
        placements = [{
            "placement_id": p.get("id"),
            "job": (p.get("offer", {}) or {}).get("title", ""),
            "offer_id": (p.get("offer", {}) or {}).get("id"),
            "stage": (p.get("stage", {}) or {}).get("name", ""),
            "stage_id": (p.get("stage", {}) or {}).get("id"),
            "disqualified": p.get("disqualified", False),
        } for p in (c.get("placements", []) or [])]
        return {
            "id": c.get("id"),
            "name": c.get("name", ""),
            "emails": c.get("emails", []),
            "phones": c.get("phones", []),
            "source": c.get("source", ""),
            "rating": c.get("rating"),
            "cover_letter": (c.get("cover_letter") or "")[:1500],
            "tags": c.get("tags", []),
            "placements": placements,
            "social_links": c.get("social_links", []),
            "custom_fields": c.get("fields", []),
            "created_at": c.get("created_at", ""),
            "admin_url": c.get("adminapp_url", ""),
        }


class ListCandidateNotes(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_candidate_notes"

    @property
    def description(self) -> str:
        return "List the recruiter notes on a Recruitee candidate (most recent first): note body, author, and timestamp."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"}}, "required": ["candidate_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, f"/candidates/{params['candidate_id']}/notes")
        if "error" in data:
            return data
        notes = [{
            "id": n.get("id"),
            "body": (n.get("body") or n.get("note") or "")[:2000],
            "author": (n.get("admin", {}) or {}).get("name", ""),
            "created_at": n.get("created_at", ""),
        } for n in (data.get("notes", []) or [])]
        return {"notes": notes, "count": len(notes)}


# NOTE: a per-candidate activity/timeline tool was intentionally dropped —
# Recruitee only exposes an account-wide CSV `/timeline`, not per-candidate
# JSON, so there's no clean tool to build. Stage/note history is visible via
# get_recruitee_candidate + list_recruitee_candidate_notes.


class ListCandidateMessages(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_candidate_messages"

    @property
    def description(self) -> str:
        return "List email/mailbox messages exchanged with a Recruitee candidate: subject, direction, snippet, and timestamp."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"}}, "required": ["candidate_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Messages come back inline on the candidate via ?with_messages=true —
        # there is no standalone /mailbox/messages?candidate_id collection.
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(
            api_key, company_id, f"/candidates/{params['candidate_id']}", {"with_messages": "true"}
        )
        if "error" in data:
            return data
        c = data.get("candidate", data)
        msgs = [{
            "subject": m.get("subject", ""),
            "direction": "inbound" if m.get("inbound") else "outbound",
            "snippet": (m.get("body_text") or m.get("body") or m.get("snippet") or "")[:500],
            "created_at": m.get("created_at", ""),
        } for m in (c.get("messages", []) or [])]
        return {"messages": msgs, "count": len(msgs)}


# --------------------------------------------------------------------------- #
# Offers / jobs / pipelines
# --------------------------------------------------------------------------- #

class ListJobs(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_jobs"

    @property
    def description(self) -> str:
        return "List job openings (offers) in Recruitee. Filter by scope and department. Returns title, status, department, location, and candidate count."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["active", "closed", "draft", "archived", "all"], "description": "Status scope (default active)."},
                "kind": {"type": "string", "enum": ["job", "talent_pool"], "description": "Offer kind (default job)."},
                "department_id": {"type": "integer", "description": "Only offers in this department."},
            },
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        qp: dict[str, Any] = {"view_mode": "brief"}
        scope = params.get("scope", "active")
        if scope != "all":
            qp["scope"] = scope
        if params.get("kind"):
            qp["kind"] = params["kind"]
        if params.get("department_id"):
            qp["department_id"] = params["department_id"]
        data = await _recruitee_get(api_key, company_id, "/offers", qp)
        if "error" in data:
            return data
        jobs = [{
            "id": o.get("id"),
            "title": o.get("title", ""),
            "status": o.get("status", ""),
            "kind": o.get("kind", ""),
            "department": o.get("department", ""),
            "location": o.get("location", ""),
            "candidates_count": o.get("candidates_count", 0),
            "created_at": o.get("created_at", ""),
        } for o in (data.get("offers", []) or [])]
        return {"jobs": jobs, "count": len(jobs)}


class GetJob(BaseTool):
    @property
    def name(self) -> str:
        return "get_recruitee_job"

    @property
    def description(self) -> str:
        return "Get a Recruitee job/offer by ID: description, full pipeline stages (with id + candidate count per stage), department, location, and stats."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"job_id": {"type": "integer", "description": "Offer/job Recruitee ID"}}, "required": ["job_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, f"/offers/{params['job_id']}")
        if "error" in data:
            return data
        o = data.get("offer", data)
        pipeline = [{
            "stage_id": s.get("id"),
            "name": s.get("name", ""),
            "category": s.get("category", ""),
            "candidates_count": s.get("candidates_count", 0),
        } for s in (o.get("pipeline", []) or [])]
        return {
            "id": o.get("id"),
            "title": o.get("title", ""),
            "status": o.get("status", ""),
            "kind": o.get("kind", ""),
            "department": o.get("department", ""),
            "location": o.get("location", ""),
            "description": (o.get("description") or "")[:2500],
            "pipeline": pipeline,
            "candidates_count": o.get("candidates_count", 0),
            "created_at": o.get("created_at", ""),
            "admin_url": o.get("adminapp_url", ""),
        }


class ListTalentPools(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_talent_pools"

    @property
    def description(self) -> str:
        return "List talent pools in Recruitee with candidate counts."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        # Talent pools are offers with kind=talent_pool.
        data = await _recruitee_get(api_key, company_id, "/offers", {"scope": "all", "kind": "talent_pool", "view_mode": "brief"})
        if "error" in data:
            return data
        pools = [{
            "id": o.get("id"),
            "title": o.get("title", ""),
            "candidates_count": o.get("candidates_count", 0),
        } for o in (data.get("offers", []) or [])]
        return {"talent_pools": pools, "count": len(pools)}


class ListPipelineTemplates(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_pipeline_templates"

    @property
    def description(self) -> str:
        return "List reusable pipeline templates and their stages (stage id + name + category), used when creating offers or moving candidates."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, "/pipeline_templates")
        if "error" in data:
            return data
        templates = [{
            "id": t.get("id"),
            "name": t.get("name", ""),
            "stages": [{"id": s.get("id"), "name": s.get("name", ""), "category": s.get("category", "")} for s in (t.get("stages", []) or [])],
        } for t in (data.get("pipeline_templates", []) or [])]
        return {"pipeline_templates": templates, "count": len(templates)}


# --------------------------------------------------------------------------- #
# Placements (candidate <-> offer pipeline membership)
# --------------------------------------------------------------------------- #

class ListPlacements(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_placements"

    @property
    def description(self) -> str:
        return "List placements (a candidate's position in an offer's pipeline). Filter by offer_id or candidate_id. Returns candidate, offer, current stage, and disqualification status."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Only placements on this offer."},
                "candidate_id": {"type": "integer", "description": "Only placements for this candidate."},
            },
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Recruitee has no GET /placements collection, so derive placements from
        # the candidate search index (proven path) filtered by offer/candidate.
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        offer_id = params.get("offer_id")
        candidate_id = params.get("candidate_id")
        qp: dict[str, Any] = {"limit": 100, "sort_by": "created_at_desc"}
        if offer_id:
            qp["filters_json"] = json.dumps([{"field": "jobs", "has_any": [offer_id]}])
        data = await _recruitee_get(api_key, company_id, "/search/new/candidates", qp)
        if "error" in data:
            return data
        placements = []
        for c in data.get("hits", []):
            if candidate_id and c.get("id") != candidate_id:
                continue
            for p in (c.get("placements", []) or []):
                p_offer = (p.get("offer", {}) or {}).get("id") or p.get("offer_id")
                if offer_id and p_offer != offer_id:
                    continue
                placements.append({
                    "placement_id": p.get("id"),
                    "candidate_id": c.get("id"),
                    "candidate_name": c.get("name", ""),
                    "offer_id": p_offer,
                    "stage": (p.get("stage", {}) or {}).get("name", ""),
                    "stage_id": (p.get("stage", {}) or {}).get("id") or p.get("stage_id"),
                    "disqualified": p.get("disqualified", False),
                })
        return {"placements": placements, "count": len(placements)}


# --------------------------------------------------------------------------- #
# Reference data (departments, admins, tags, sources, disqualify reasons,
# locations, requisitions, custom fields, tasks, interviews, evaluations)
# --------------------------------------------------------------------------- #

class _SimpleListTool(BaseTool):
    """Base for parameterless GET-and-map list endpoints."""
    _path: str = ""
    _root: str = ""
    _name: str = ""
    _desc: str = ""

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._desc

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {}}

    @property
    def provider(self) -> str:
        return "recruitee"

    def _map(self, item: dict) -> dict:
        return item

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, self._path)
        if "error" in data:
            return data
        items = data.get(self._root, []) or []
        mapped = [self._map(i) for i in items]
        return {self._root: mapped, "count": len(mapped)}


class ListDepartments(_SimpleListTool):
    _path, _root = "/departments", "departments"
    _name = "list_recruitee_departments"
    _desc = "List Recruitee departments (id, name, and job count)."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", ""), "offers_count": i.get("offers_count", 0)}


class ListAdmins(_SimpleListTool):
    _path, _root = "/admins", "admins"
    _name = "list_recruitee_admins"
    _desc = "List Recruitee team members (recruiters/admins): id, name, email, role. Use the id to filter candidates by assigned recruiter."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", ""), "email": i.get("email", ""), "role": i.get("role", "")}


class ListTags(_SimpleListTool):
    _path, _root = "/tags", "tags"
    _name = "list_recruitee_tags"
    _desc = "List all candidate tags defined in Recruitee (name + usage count)."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", ""), "taggings_count": i.get("taggings_count", 0)}


class ListSources(_SimpleListTool):
    _path, _root = "/sources", "sources"
    _name = "list_recruitee_sources"
    _desc = "List candidate sources (sourcing channels) configured in Recruitee."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", "")}


class ListDisqualifyReasons(_SimpleListTool):
    _path, _root = "/disqualify_reasons", "disqualify_reasons"
    _name = "list_recruitee_disqualify_reasons"
    _desc = "List disqualification reasons (id + name). Pass the id to disqualify_recruitee_candidate."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", "")}


class ListLocations(_SimpleListTool):
    _path, _root = "/locations", "locations"
    _name = "list_recruitee_locations"
    _desc = "List Recruitee office/job locations (id, name, city, country)."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "name": i.get("name", ""), "city": i.get("city", ""), "country": i.get("country_code", "")}


class ListRequisitions(_SimpleListTool):
    _path, _root = "/requisitions", "requisitions"
    _name = "list_recruitee_requisitions"
    _desc = "List hiring requisitions (id, title, status, approval state, department)."

    def _map(self, i: dict) -> dict:
        return {"id": i.get("id"), "title": i.get("title", ""), "status": i.get("status", ""), "department": (i.get("department", {}) or {}).get("name", "")}


# NOTE: a custom-field-definitions list tool was intentionally dropped — there
# is no GET for definitions in the public API (only POST to set values), and a
# candidate's custom-field VALUES already come back as `custom_fields` on
# get_recruitee_candidate.


class ListTasks(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_tasks"

    @property
    def description(self) -> str:
        return "List recruiting tasks/to-dos. Filter by completion and candidate. Returns title, due date, assignee, completed flag."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "completed": {"type": "boolean", "description": "Filter by completion state. Omit for all."},
                "candidate_id": {"type": "integer", "description": "Only tasks attached to this candidate."},
            },
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        qp: dict[str, Any] = {}
        if params.get("completed") is not None:
            qp["completed"] = str(bool(params["completed"])).lower()
        if params.get("candidate_id"):
            qp["candidate_id"] = params["candidate_id"]
        data = await _recruitee_get(api_key, company_id, "/tasks", qp or None)
        if "error" in data:
            return data
        tasks = [{
            "id": t.get("id"),
            "title": t.get("title") or t.get("name", ""),
            "due_date": t.get("due_date", ""),
            "completed": t.get("completed", False),
            "assignee": (t.get("admin", {}) or {}).get("name", ""),
            "candidate_id": t.get("candidate_id"),
        } for t in (data.get("tasks", []) or [])]
        return {"tasks": tasks, "count": len(tasks)}


class ListInterviewEvents(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_interview_events"

    @property
    def description(self) -> str:
        return "List scheduled interview events (upcoming and recent): title, start/end time, candidate, attendees, location/meeting link."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Only interviews for this candidate."},
                "upcoming_only": {"type": "boolean", "description": "If true, only future events (default false)."},
            },
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        qp: dict[str, Any] = {}
        if params.get("candidate_id"):
            qp["candidate_id"] = params["candidate_id"]
        data = await _recruitee_get(api_key, company_id, "/interview/events", qp or None)
        if "error" in data:
            return data
        now = datetime.now(timezone.utc)
        events = []
        for e in (data.get("events", []) or data.get("interview_events", []) or []):
            start = e.get("start_at") or e.get("starts_at", "")
            if params.get("upcoming_only"):
                su = _iso_to_unix(start)
                if su is not None and su < int(now.timestamp()):
                    continue
            events.append({
                "id": e.get("id"),
                "title": e.get("title", ""),
                "start_at": start,
                "end_at": e.get("end_at") or e.get("ends_at", ""),
                "candidate_id": e.get("candidate_id"),
                "location": e.get("location", "") or e.get("hangout_link", ""),
            })
        return {"interview_events": events, "count": len(events)}


class ListCandidateEvaluations(BaseTool):
    @property
    def name(self) -> str:
        return "list_recruitee_candidate_evaluations"

    @property
    def description(self) -> str:
        return "List interview evaluations/scorecards submitted for a Recruitee candidate: evaluator, score/recommendation, and notes."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"}}, "required": ["candidate_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _recruitee_get(api_key, company_id, "/interview/results", {"candidate_id": params["candidate_id"]})
        if "error" in data:
            return data
        evals = [{
            "evaluator": (r.get("admin", {}) or {}).get("name", ""),
            "score": r.get("score") or r.get("rating"),
            "recommendation": r.get("recommendation", ""),
            "notes": (r.get("note") or r.get("comment") or "")[:1000],
            "created_at": r.get("created_at", ""),
        } for r in (data.get("results", []) or data.get("interview_results", []) or [])]
        return {"evaluations": evals, "count": len(evals)}


# --------------------------------------------------------------------------- #
# Writes (mutations) — Rails-style {"<resource>": {...}} bodies.
# --------------------------------------------------------------------------- #

class CreateCandidate(BaseTool):
    @property
    def name(self) -> str:
        return "create_recruitee_candidate"

    @property
    def description(self) -> str:
        return "Add a new candidate to Recruitee. Optionally place them directly onto an offer's pipeline. Returns the new candidate id."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Candidate full name."},
                "emails": {"type": "array", "items": {"type": "string"}, "description": "Email address(es)."},
                "phones": {"type": "array", "items": {"type": "string"}, "description": "Phone number(s)."},
                "source": {"type": "string", "description": "Where the candidate came from."},
                "cover_letter": {"type": "string", "description": "Optional cover letter text."},
                "offer_id": {"type": "integer", "description": "If set, place the candidate onto this offer's pipeline."},
            },
            "required": ["name"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        candidate: dict[str, Any] = {"name": params["name"]}
        for k in ("emails", "phones", "source", "cover_letter"):
            if params.get(k) is not None:
                candidate[k] = params[k]
        body: dict[str, Any] = {"candidate": candidate}
        if params.get("offer_id"):
            body["offers"] = [params["offer_id"]]
        data = await _request("POST", api_key, company_id, "/candidates", json_body=body)
        if "error" in data:
            return data
        c = data.get("candidate", data)
        return {"success": True, "candidate_id": c.get("id"), "name": c.get("name", ""), "admin_url": c.get("adminapp_url", "")}


class UpdateCandidate(BaseTool):
    @property
    def name(self) -> str:
        return "update_recruitee_candidate"

    @property
    def description(self) -> str:
        return "Update a Recruitee candidate's profile fields (name, emails, phones, source, rating)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"},
                "name": {"type": "string"},
                "emails": {"type": "array", "items": {"type": "string"}},
                "phones": {"type": "array", "items": {"type": "string"}},
                "source": {"type": "string"},
                "rating": {"type": "number", "description": "Star rating 0-5."},
            },
            "required": ["candidate_id"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        fields = {k: params[k] for k in ("name", "emails", "phones", "source", "rating") if params.get(k) is not None}
        if not fields:
            return {"error": "No fields to update."}
        data = await _request("PATCH", api_key, company_id, f"/candidates/{params['candidate_id']}", json_body={"candidate": fields})
        if "error" in data:
            return data
        return {"success": True, "candidate_id": params["candidate_id"]}


class DeleteCandidate(BaseTool):
    @property
    def name(self) -> str:
        return "delete_recruitee_candidate"

    @property
    def description(self) -> str:
        return "Permanently delete a candidate from Recruitee. Destructive — confirm the candidate id with the user first."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"}}, "required": ["candidate_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _request("DELETE", api_key, company_id, f"/candidates/{params['candidate_id']}")
        if "error" in data:
            return data
        return {"success": True, "deleted_candidate_id": params["candidate_id"]}


# NOTE: an "add existing candidate to offer" tool (POST /placements) was
# intentionally dropped — Recruitee creates candidate↔offer associations through
# the candidate create/update endpoints (a standalone POST /placements returns
# 400 "Server internal error"), not a placement create. To place a candidate on
# an offer, use create_recruitee_candidate with `offer_id`. The move / disqualify
# / qualify / delete tools below operate on an EXISTING placement_id
# (PATCH/DELETE /placements/{id}), which is the supported path.


class MoveCandidateStage(BaseTool):
    @property
    def name(self) -> str:
        return "move_recruitee_candidate_stage"

    @property
    def description(self) -> str:
        return "Move a candidate to a different pipeline stage by updating their placement. Get placement_id from get_recruitee_candidate and stage_id from get_recruitee_job."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "placement_id": {"type": "integer", "description": "Placement id (candidate's position on an offer)."},
                "stage_id": {"type": "integer", "description": "Target pipeline stage id."},
            },
            "required": ["placement_id", "stage_id"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        # Stage change is a dedicated action endpoint (NOT PATCH /placements/{id},
        # which is delete-only); body is the bare {stage_id}, not wrapped.
        data = await _request(
            "PATCH", api_key, company_id, f"/placements/{params['placement_id']}/change_stage",
            json_body={"stage_id": params["stage_id"]},
        )
        if "error" in data:
            return data
        return {"success": True, "placement_id": params["placement_id"], "stage_id": params["stage_id"]}


class DisqualifyCandidate(BaseTool):
    @property
    def name(self) -> str:
        return "disqualify_recruitee_candidate"

    @property
    def description(self) -> str:
        return "Disqualify a candidate on an offer (by placement id), with a disqualify reason. Use list_recruitee_disqualify_reasons for reason ids."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "placement_id": {"type": "integer", "description": "Placement id."},
                "disqualify_reason_id": {"type": "integer", "description": "Reason id from list_recruitee_disqualify_reasons."},
            },
            "required": ["placement_id", "disqualify_reason_id"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        # Disqualify is a dedicated action endpoint; reason goes in the body.
        data = await _request(
            "PATCH", api_key, company_id,
            f"/placements/{params['placement_id']}/disqualify",
            json_body={"disqualify_reason_id": params["disqualify_reason_id"]},
        )
        if "error" in data:
            return data
        return {"success": True, "placement_id": params["placement_id"], "disqualified": True}


# NOTE: a re-qualify (revert disqualification) tool was intentionally dropped —
# none of the candidate revert endpoints resolved against the live API
# (`/placements/{id}/qualify`, `/revert_disqualify`, and `DELETE
# .../disqualify` all 404). Disqualify works; re-qualifying is rare and is done
# in the Recruitee UI until the endpoint is confirmed.


class DeletePlacement(BaseTool):
    @property
    def name(self) -> str:
        return "delete_recruitee_placement"

    @property
    def description(self) -> str:
        return "Remove a candidate from an offer's pipeline (delete a placement). Does NOT delete the candidate. Confirm with the user first."

    @property
    def input_schema(self) -> dict:
        return {"type": "object", "properties": {"placement_id": {"type": "integer", "description": "Placement id."}}, "required": ["placement_id"]}

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _request("DELETE", api_key, company_id, f"/placements/{params['placement_id']}")
        if "error" in data:
            return data
        return {"success": True, "deleted_placement_id": params["placement_id"]}


class AddCandidateNote(BaseTool):
    @property
    def name(self) -> str:
        return "add_recruitee_candidate_note"

    @property
    def description(self) -> str:
        return "Add a recruiter note to a Recruitee candidate."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"},
                "body": {"type": "string", "description": "Note text."},
            },
            "required": ["candidate_id", "body"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _request(
            "POST", api_key, company_id, f"/candidates/{params['candidate_id']}/notes",
            json_body={"note": {"body": params["body"]}},
        )
        if "error" in data:
            return data
        n = data.get("note", data)
        return {"success": True, "note_id": n.get("id"), "candidate_id": params["candidate_id"]}


class AddCandidateTags(BaseTool):
    @property
    def name(self) -> str:
        return "add_recruitee_candidate_tags"

    @property
    def description(self) -> str:
        return "Add one or more tags to a Recruitee candidate (tags are created if they don't exist)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"},
                "tags": {"type": "array", "items": {"type": "string"}, "description": "Tag names to add."},
            },
            "required": ["candidate_id", "tags"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        added = []
        for tag in params["tags"]:
            data = await _request(
                "POST", api_key, company_id, f"/candidates/{params['candidate_id']}/tags",
                json_body={"tag": tag},
            )
            if "error" not in data:
                added.append(tag)
        return {"success": True, "candidate_id": params["candidate_id"], "added_tags": added}


class RemoveCandidateTag(BaseTool):
    @property
    def name(self) -> str:
        return "remove_recruitee_candidate_tag"

    @property
    def description(self) -> str:
        return "Remove a tag from a Recruitee candidate by tag name."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "candidate_id": {"type": "integer", "description": "Candidate Recruitee ID"},
                "tag": {"type": "string", "description": "Tag name to remove."},
            },
            "required": ["candidate_id", "tag"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        data = await _request(
            "DELETE", api_key, company_id, f"/candidates/{params['candidate_id']}/tags",
            json_body={"tag": params["tag"]},
        )
        if "error" in data:
            return data
        return {"success": True, "candidate_id": params["candidate_id"], "removed_tag": params["tag"]}


class CreateTask(BaseTool):
    @property
    def name(self) -> str:
        return "create_recruitee_task"

    @property
    def description(self) -> str:
        return "Create a recruiting task/to-do, optionally attached to a candidate and assigned to an admin with a due date."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Task title."},
                "candidate_id": {"type": "integer", "description": "Optional candidate to attach to."},
                "admin_id": {"type": "integer", "description": "Optional assignee (admin id)."},
                "due_date": {"type": "string", "description": "Optional ISO date."},
            },
            "required": ["title"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        task = {"title": params["title"]}
        for src, dst in (("candidate_id", "candidate_id"), ("admin_id", "admin_id"), ("due_date", "due_date")):
            if params.get(src) is not None:
                task[dst] = params[src]
        data = await _request("POST", api_key, company_id, "/tasks", json_body={"task": task})
        if "error" in data:
            return data
        t = data.get("task", data)
        return {"success": True, "task_id": t.get("id"), "title": params["title"]}


class CompleteTask(BaseTool):
    @property
    def name(self) -> str:
        return "complete_recruitee_task"

    @property
    def description(self) -> str:
        return "Mark a Recruitee task as completed (or reopen it)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "task_id": {"type": "integer", "description": "Task id."},
                "completed": {"type": "boolean", "description": "True to complete (default), false to reopen."},
            },
            "required": ["task_id"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        completed = params.get("completed", True)
        data = await _request(
            "PATCH", api_key, company_id, f"/tasks/{params['task_id']}",
            json_body={"task": {"completed": completed}},
        )
        if "error" in data:
            return data
        return {"success": True, "task_id": params["task_id"], "completed": completed}


# NOTE: an interview-event CREATE tool was intentionally dropped — Recruitee
# interview scheduling is a multi-step subsystem (calendars + schedules +
# events + templates), not a single POST (`POST /interview/events` 404s). The
# READ side (list_recruitee_interview_events) stays; scheduling is better done
# via the user's Google Calendar tools.


class CreateOffer(BaseTool):
    @property
    def name(self) -> str:
        return "create_recruitee_offer"

    @property
    def description(self) -> str:
        return "Create a new job offer (or talent pool) in Recruitee with a title, optional description, department, and location."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Job title."},
                "description": {"type": "string", "description": "Optional job description (HTML/markdown)."},
                "department_id": {"type": "integer", "description": "Optional department id."},
                "kind": {"type": "string", "enum": ["job", "talent_pool"], "description": "Default 'job'."},
            },
            "required": ["title"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        offer = {"title": params["title"], "kind": params.get("kind", "job")}
        for src in ("description", "department_id"):
            if params.get(src) is not None:
                offer[src] = params[src]
        data = await _request("POST", api_key, company_id, "/offers", json_body={"offer": offer})
        if "error" in data:
            return data
        o = data.get("offer", data)
        return {"success": True, "offer_id": o.get("id"), "title": params["title"], "admin_url": o.get("adminapp_url", "")}


class UpdateOffer(BaseTool):
    @property
    def name(self) -> str:
        return "update_recruitee_offer"

    @property
    def description(self) -> str:
        return "Update a Recruitee offer's title, description, status (e.g. published/closed/draft), department, or location."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "offer_id": {"type": "integer", "description": "Offer Recruitee ID"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "status": {"type": "string", "enum": ["published", "draft", "closed", "archived"]},
                "department_id": {"type": "integer"},
            },
            "required": ["offer_id"],
        }

    @property
    def provider(self) -> str:
        return "recruitee"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        api_key = _get_api_key(access_token)
        company_id = _get_company_id(access_token)
        fields = {k: params[k] for k in ("title", "description", "status", "department_id") if params.get(k) is not None}
        if not fields:
            return {"error": "No fields to update."}
        data = await _request("PATCH", api_key, company_id, f"/offers/{params['offer_id']}", json_body={"offer": fields})
        if "error" in data:
            return data
        return {"success": True, "offer_id": params["offer_id"]}
