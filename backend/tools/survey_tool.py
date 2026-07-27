"""Survey-builder tools — Gideon uses these to help admins design + run surveys.

All survey tools are admin-only (see tools/tool_executor.py — checked against
the SURVEY_ADMINS allowlist before execution). The same operations are also
available via /v1/surveys/admin/* HTTP endpoints; the tools are thin wrappers
so Gideon can take builder actions inside the chat panel.
"""

import json
import os
import pathlib
import re
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select

from db.models import Survey, SurveyResponse, User
from tools.internal_tools import InternalTool

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
_QTYPES = {"likert_1_5", "text", "single_select", "multi_select"}


def _validate_questions(questions: list[dict]) -> tuple[bool, str]:
    if not isinstance(questions, list):
        return False, "questions must be a list"
    seen_ids: set[str] = set()
    for i, q in enumerate(questions):
        if not isinstance(q, dict):
            return False, f"question {i} is not an object"
        qid = q.get("id")
        if not qid or not isinstance(qid, str):
            return False, f"question {i} missing 'id'"
        if qid in seen_ids:
            return False, f"duplicate question id '{qid}'"
        seen_ids.add(qid)
        qtype = q.get("type")
        if qtype not in _QTYPES:
            return False, f"question '{qid}' has invalid type '{qtype}' (must be one of {sorted(_QTYPES)})"
        if not q.get("prompt"):
            return False, f"question '{qid}' missing 'prompt'"
        if qtype in {"single_select", "multi_select"}:
            opts = q.get("options")
            if not isinstance(opts, list) or not opts:
                return False, f"question '{qid}' (type={qtype}) requires non-empty 'options' list"
    return True, ""


def _coerce_required(value: Any) -> bool:
    """Treat only real-bool True or the strings 'true'/'1'/'yes' as required.
    A bare `bool(value)` flips strings like 'false'/'no' to True — wrong."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    if isinstance(value, (int, float)):
        return value != 0
    return True  # default = required, matches schema's expectation


def _normalize_questions(questions: list[dict]) -> list[dict]:
    """Drop unknown fields, default 'required' to True."""
    out = []
    for q in questions:
        entry = {
            "id": q["id"],
            "type": q["type"],
            "prompt": q["prompt"],
            "required": _coerce_required(q.get("required", True)),
        }
        if q.get("options") is not None:
            entry["options"] = list(q["options"])
        if q.get("placeholder"):
            entry["placeholder"] = q["placeholder"]
        out.append(entry)
    return out


def _validate_eligibility(value: Any) -> tuple[bool, str, dict]:
    """Ensure eligibility is a dict with expected shape. Returns (ok, error, normalized)."""
    if value is None:
        return True, "", {}
    if not isinstance(value, dict):
        return False, "eligibility must be an object (got " + type(value).__name__ + ")", {}
    normalized: dict = {}
    for key in ("departments", "role_keywords", "emails"):
        if key in value:
            v = value[key]
            if v is None:
                continue
            if not isinstance(v, list):
                return False, f"eligibility.{key} must be a list", {}
            normalized[key] = [str(x) for x in v if x is not None]
    return True, "", normalized


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _load_cohort_presets() -> list[dict]:
    """Org-specific shorthand → audience mappings, e.g. "ops PMs" → the exact
    `departments` + `role_keywords` that select those people.

    Empty by default: every organization's department and role names differ,
    so there is no sensible built-in list. Set SURVEY_COHORT_PRESETS to a JSON
    array (or a path to a .json file) to teach Gideon your own shorthand:

        [
          {
            "key": "eng_leads",
            "aliases": ["eng leads", "engineering leads"],
            "description": "Engineering leadership (~8 people).",
            "departments": ["Engineering"],
            "role_keywords": ["Lead", "Head", "Director"]
          }
        ]

    With no presets configured, Gideon falls back to confirming the target
    audience with the admin before drafting, which is the safe default.
    """
    raw = os.environ.get("SURVEY_COHORT_PRESETS", "").strip()
    if not raw:
        return []
    try:
        if not raw.startswith("["):
            raw = pathlib.Path(raw).read_text()
        presets = json.loads(raw)
    except (OSError, ValueError) as e:
        logger.error("Failed to load SURVEY_COHORT_PRESETS", error=str(e))
        return []
    if not isinstance(presets, list):
        logger.error("SURVEY_COHORT_PRESETS must be a JSON array")
        return []
    return [p for p in presets if isinstance(p, dict)]


# IMPORTANT (for the model): when an admin uses one of a preset's `aliases`,
# use that preset's exact `departments` + `role_keywords`. Do NOT invent your
# own mapping for a phrase a preset already covers.
_COHORT_PRESETS: list[dict] = _load_cohort_presets()


class GetOrgBreakdown(InternalTool):
    admin_only = True
    name = "get_org_breakdown"
    description = (
        "List distinct departments and roles across all Gideon users with user counts, "
        "AND return a list of canonical 'cohort_presets' for common shorthand "
        "(e.g. 'ops PMs', 'PMs'). Always call this BEFORE draft_survey if the admin "
        "used any shorthand for who to target — use the matching preset's exact "
        "`departments` + `role_keywords` instead of inventing your own."
    )
    input_schema = {"type": "object", "properties": {}}

    async def execute_internal(self, params, user_id, session, **kwargs):
        dept_rows = await session.execute(
            select(User.department, func.count(User.id))
            .where(User.department.isnot(None))
            .group_by(User.department)
            .order_by(func.count(User.id).desc())
        )
        role_rows = await session.execute(
            select(User.role, func.count(User.id))
            .where(User.role.isnot(None))
            .group_by(User.role)
            .order_by(func.count(User.id).desc())
        )
        total = (await session.execute(select(func.count(User.id)))).scalar_one()
        return {
            "total_users": total,
            "departments": [{"name": d, "count": c} for d, c in dept_rows.all() if d],
            "roles": [{"name": r, "count": c} for r, c in role_rows.all() if r],
            "cohort_presets": _COHORT_PRESETS,
        }


class DraftSurvey(InternalTool):
    admin_only = True
    name = "draft_survey"
    description = (
        "Create a new survey in 'draft' status. The survey is NOT live until activate_survey is called. "
        "Use this once you and the admin have agreed on a slug, title, target audience, and at least one question.\n\n"
        "TARGETING: If the admin used shorthand like 'ops PMs', 'PMs', 'ops managers', etc., call "
        "get_org_breakdown FIRST and use the matching `cohort_presets` entry's `departments` + "
        "`role_keywords` VERBATIM. Do not invent your own department or role-keyword lists when a "
        "preset matches — these are the canonical mappings agreed with the admin team. If no preset "
        "matches what the admin asked for, confirm the target audience with them before drafting."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "slug": {
                "type": "string",
                "description": "URL-safe identifier (a-z, 0-9, hyphens). Used as the API key. e.g. 'gideon-impact-2026-q2'.",
            },
            "title": {"type": "string", "description": "Short title shown to respondents."},
            "intro_text": {"type": "string", "description": "Optional 1-2 sentence intro under the title."},
            "eligibility": {
                "type": "object",
                "description": (
                    "Targeting filter. A user is eligible if their email is in 'emails' OR "
                    "(their department matches one of 'departments' AND their role contains "
                    "any 'role_keywords' substring). Empty filters = broadcast to everyone."
                ),
                "properties": {
                    "departments": {"type": "array", "items": {"type": "string"}},
                    "role_keywords": {"type": "array", "items": {"type": "string"}},
                    "emails": {"type": "array", "items": {"type": "string"}},
                },
            },
            "questions": {
                "type": "array",
                "description": "List of questions. Each: {id, type (likert_1_5|text|single_select|multi_select), prompt, options?, required?, placeholder?}.",
                "items": {"type": "object"},
            },
        },
        "required": ["slug", "title", "questions"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        slug = (params.get("slug") or "").strip().lower()
        if not _SLUG_RE.match(slug):
            return {"error": "slug must be lowercase a-z/0-9/hyphens, 3-64 chars, and not start/end with a hyphen"}
        title = (params.get("title") or "").strip()
        if not title:
            return {"error": "title is required"}

        questions = params.get("questions") or []
        ok, msg = _validate_questions(questions)
        if not ok:
            return {"error": msg}

        existing = await session.execute(select(Survey).where(Survey.slug == slug))
        if existing.scalar_one_or_none():
            return {"error": f"A survey with slug '{slug}' already exists. Pick a different slug or call update_survey_draft."}

        elig_ok, elig_err, eligibility = _validate_eligibility(params.get("eligibility"))
        if not elig_ok:
            return {"error": elig_err}

        survey = Survey(
            slug=slug,
            title=title,
            intro_text=params.get("intro_text"),
            status="draft",
            eligibility=eligibility,
            questions=_normalize_questions(questions),
            created_by=user_id,
        )
        session.add(survey)
        await session.flush()

        return {
            "ok": True,
            "slug": slug,
            "id": str(survey.id),
            "status": "draft",
            "message": f"Draft '{slug}' created with {len(questions)} questions. Review the form preview, then call activate_survey when ready to ship.",
        }


class UpdateSurveyDraft(InternalTool):
    admin_only = True
    name = "update_survey_draft"
    description = (
        "Update title / intro / eligibility / questions on an existing survey. "
        "Allowed on draft AND active surveys. Closed surveys are immutable. "
        "Pass only the fields you want to change — others are left untouched. "
        "Replacing the questions list invalidates any cached analysis."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "title": {"type": "string"},
            "intro_text": {"type": "string"},
            "eligibility": {"type": "object"},
            "questions": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["slug"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        slug = (params.get("slug") or "").strip().lower()
        survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
        survey = survey_row.scalar_one_or_none()
        if not survey:
            return {"error": f"No survey with slug '{slug}'."}
        if survey.status == "closed":
            return {"error": "Closed surveys are immutable."}

        changed: list[str] = []
        if "title" in params and params["title"] is not None:
            t = (params["title"] or "").strip()
            if not t:
                return {"error": "title cannot be empty"}
            survey.title = t
            changed.append("title")
        if "intro_text" in params:
            survey.intro_text = params["intro_text"]
            changed.append("intro_text")
        if "eligibility" in params and params["eligibility"] is not None:
            elig_ok, elig_err, eligibility = _validate_eligibility(params["eligibility"])
            if not elig_ok:
                return {"error": elig_err}
            survey.eligibility = eligibility
            changed.append("eligibility")
        if "questions" in params and params["questions"] is not None:
            ok, msg = _validate_questions(params["questions"])
            if not ok:
                return {"error": msg}
            survey.questions = _normalize_questions(params["questions"])
            survey.analysis = None
            survey.analysis_generated_at = None
            changed.append("questions")

        if not changed:
            return {"ok": True, "slug": slug, "message": "No fields to update."}

        await session.flush()
        return {"ok": True, "slug": slug, "status": survey.status, "changed": changed}


class ActivateSurvey(InternalTool):
    admin_only = True
    name = "activate_survey"
    description = (
        "Flip a draft survey to 'active' so eligible users start seeing the blocking modal "
        "on their next page load. Confirm with the admin before calling — once active, "
        "users will be interrupted."
    )
    input_schema = {
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        slug = (params.get("slug") or "").strip().lower()
        survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
        survey = survey_row.scalar_one_or_none()
        if not survey:
            return {"error": f"No survey with slug '{slug}'."}
        if survey.status == "closed":
            return {"error": "Closed surveys cannot be reactivated. Create a new survey instead."}
        if survey.status == "active":
            return {"ok": True, "slug": slug, "status": "active", "message": "Already active."}
        if not survey.questions:
            return {"error": "Cannot activate a survey with zero questions."}
        survey.status = "active"
        await session.flush()
        return {
            "ok": True,
            "slug": slug,
            "status": "active",
            "message": f"Survey '{slug}' is now live for eligible users.",
        }


class CloseSurvey(InternalTool):
    admin_only = True
    name = "close_survey"
    description = (
        "Stop a survey from showing up to new respondents. Existing dismissals/submissions "
        "are kept. Closed surveys are immutable and cannot be reactivated."
    )
    input_schema = {
        "type": "object",
        "properties": {"slug": {"type": "string"}},
        "required": ["slug"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        slug = (params.get("slug") or "").strip().lower()
        survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
        survey = survey_row.scalar_one_or_none()
        if not survey:
            return {"error": f"No survey with slug '{slug}'."}
        if survey.status == "closed":
            return {"ok": True, "slug": slug, "status": "closed", "message": "Already closed."}
        survey.status = "closed"
        survey.closed_at = datetime.now(timezone.utc)
        await session.flush()

        # Quick stats so Gideon can summarize for the admin
        count_rows = await session.execute(
            select(SurveyResponse.status, func.count(SurveyResponse.id))
            .where(SurveyResponse.survey_id == survey.id)
            .group_by(SurveyResponse.status)
        )
        counts = {s: n for s, n in count_rows.all()}
        return {
            "ok": True,
            "slug": slug,
            "status": "closed",
            "submitted": counts.get("submitted", 0),
            "dismissed": counts.get("dismissed", 0),
        }


class AnalyzeSurveyResponses(InternalTool):
    admin_only = True
    name = "analyze_survey_responses"
    description = (
        "Generate (or return cached) themes, score distributions, and usage correlations "
        "for a survey. Pass refresh=true to force regeneration. Useful right after closing "
        "a survey or whenever the admin wants the verdict."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "slug": {"type": "string"},
            "refresh": {"type": "boolean", "description": "Force regeneration even if cached."},
        },
        "required": ["slug"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        from services.survey_analysis_service import generate_analysis

        slug = (params.get("slug") or "").strip().lower()
        survey_row = await session.execute(select(Survey).where(Survey.slug == slug))
        survey = survey_row.scalar_one_or_none()
        if not survey:
            return {"error": f"No survey with slug '{slug}'."}

        if not params.get("refresh") and survey.analysis:
            return {
                "ok": True,
                "slug": slug,
                "cached": True,
                "generated_at": survey.analysis_generated_at.isoformat() if survey.analysis_generated_at else None,
                "analysis": survey.analysis,
            }

        analysis = await generate_analysis(survey.id, session)
        survey.analysis = analysis
        survey.analysis_generated_at = datetime.now(timezone.utc)
        await session.flush()
        return {
            "ok": True,
            "slug": slug,
            "cached": False,
            "generated_at": survey.analysis_generated_at.isoformat(),
            "analysis": analysis,
        }
