"""Survey response analysis — Gideon's qualitative read on a survey's submissions.

Generates three things in one Sonnet call:
  - Themes from open-text answers (with representative quotes, anonymized)
  - Per-question stats (likert distributions, multi-select percentages)
  - Usage correlation (joins submitting users to `usage_log` and tells us
    what high-scorers do differently from low-scorers)

Output is cached in `surveys.analysis` (JSONB). Regeneration is triggered when
question schema changes (cleared in PATCH /admin/{slug}) or via ?refresh=true.
"""

import json
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.llm import get_spec, llm
from db.models import Survey, SurveyResponse, UsageLog, User

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Pure stats (no LLM)
# ---------------------------------------------------------------------------


def _per_question_stats(questions: list[dict], responses: list[SurveyResponse]) -> dict:
    """Return per-question aggregates that don't need an LLM:

    - likert_1_5: distribution dict + average
    - single_select / multi_select: option count + percent
    - text: count of non-empty answers + average length

    Skipped questions count as omissions.
    """
    out: dict[str, dict] = {}
    submitted = [r for r in responses if r.status == "submitted"]
    n_submitted = len(submitted)

    for q in questions:
        qid = q.get("id")
        qtype = q.get("type")
        answers = [(r.answers or {}).get(qid) for r in submitted]
        non_null = [a for a in answers if a is not None and a != "" and a != []]

        entry: dict[str, Any] = {
            "question_id": qid,
            "question_type": qtype,
            "answered": len(non_null),
            "skipped": n_submitted - len(non_null),
        }

        if qtype == "likert_1_5":
            dist: Counter = Counter()
            scores: list[int] = []
            for v in non_null:
                if isinstance(v, int) and 1 <= v <= 5:
                    dist[str(v)] += 1
                    scores.append(v)
            entry["distribution"] = {str(k): dist.get(str(k), 0) for k in (1, 2, 3, 4, 5)}
            entry["average"] = round(sum(scores) / len(scores), 2) if scores else None

        elif qtype == "single_select":
            counts: Counter = Counter(str(v) for v in non_null if isinstance(v, str))
            total = sum(counts.values()) or 1
            entry["counts"] = dict(counts)
            entry["percentages"] = {k: round(c * 100 / total, 1) for k, c in counts.items()}

        elif qtype == "multi_select":
            counts = Counter()
            for v in non_null:
                if isinstance(v, list):
                    for opt in v:
                        if isinstance(opt, str):
                            counts[opt] += 1
            base = max(len(non_null), 1)
            entry["counts"] = dict(counts)
            entry["percentages"] = {k: round(c * 100 / base, 1) for k, c in counts.items()}

        elif qtype == "text":
            lengths = [len(str(v)) for v in non_null]
            entry["average_length"] = round(sum(lengths) / len(lengths), 1) if lengths else 0
            entry["sample_count"] = len(lengths)

        out[qid] = entry

    return out


# ---------------------------------------------------------------------------
# Usage correlation (joins submitters to usage_log)
# ---------------------------------------------------------------------------


async def _usage_correlation(
    session: AsyncSession,
    submitter_ids: list[UUID],
) -> dict:
    """Group submitters by use-case usage in the last 30 days.

    Returns {use_case: {users_with_any_use, total_calls, avg_calls_per_user_with_any}}.
    Empty dict if nobody submitted yet.
    """
    if not submitter_ids:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    rows = await session.execute(
        select(
            UsageLog.user_id,
            UsageLog.use_case,
            func.count(UsageLog.id),
        )
        .where(UsageLog.user_id.in_(submitter_ids))
        .where(UsageLog.created_at >= cutoff)
        .group_by(UsageLog.user_id, UsageLog.use_case)
    )

    by_use_case: dict[str, dict[UUID, int]] = defaultdict(dict)
    for uid, use_case, n in rows.all():
        by_use_case[use_case][uid] = n

    result: dict[str, dict] = {}
    for use_case, per_user in by_use_case.items():
        total = sum(per_user.values())
        users = len(per_user)
        result[use_case] = {
            "users_with_any_use": users,
            "submitters_total": len(submitter_ids),
            "users_pct": round(users * 100 / len(submitter_ids), 1),
            "total_calls": total,
            "avg_calls_per_user_with_any": round(total / users, 1) if users else 0,
        }
    return result


# ---------------------------------------------------------------------------
# LLM themes
# ---------------------------------------------------------------------------


def _build_llm_input(
    survey: Survey,
    questions: list[dict],
    submitted: list[tuple[SurveyResponse, User]],
    stats: dict,
    usage: dict,
) -> str:
    """Assemble a compact prompt for Sonnet.

    Open-text answers are passed verbatim (with redacted user names → just
    role/department) so Sonnet can quote them. Likert + multi-select are
    summarized via stats — no need to repeat raw values.
    """
    text_questions = [q for q in questions if q.get("type") == "text"]

    text_blocks = []
    for q in text_questions:
        qid = q["id"]
        prompt = q["prompt"]
        rows = []
        for resp, user in submitted:
            ans = (resp.answers or {}).get(qid)
            if not ans or not str(ans).strip():
                continue
            tag = f"[{user.role or 'unknown role'} / {user.department or 'unknown dept'}]"
            rows.append(f"  - {tag} {str(ans).strip()[:400]}")
        text_blocks.append({
            "question_id": qid,
            "prompt": prompt,
            "answers": rows[:60],  # cap per question
        })

    return json.dumps({
        "survey_title": survey.title,
        "intro_text": survey.intro_text,
        "n_submitted": len(submitted),
        "per_question_stats": stats,
        "usage_correlation": usage,
        "text_responses": text_blocks,
    }, indent=2)


async def _generate_themes(input_blob: str) -> dict:
    """Single Sonnet call. Returns parsed JSON or empty fallback on failure.

    Wraps the Anthropic call so transient failures (timeout, 5xx, rate limit)
    surface as the empty fallback rather than 500-ing the request. We deliberately
    do NOT log model output — it contains respondent free-text quotes (PII).
    """
    try:
        system = (
            "You analyze user survey responses for an internal AI assistant called Gideon. "
            "Your output is shown to a leadership team — be specific, quote concrete examples, "
            "and lead with the most actionable findings. Anonymize quotes by attaching role / "
            "department tags only (already done for you in the input).\n\n"
            "Return ONLY a JSON object of this exact shape, no markdown, no preamble:\n"
            "{\n"
            '  "headline": "one-sentence summary of the overall sentiment",\n'
            '  "themes": [\n'
            '    {"name": "...", "weight": 1-5, "description": "...", "supporting_quotes": ["...", "..."]}\n'
            "  ],\n"
            '  "wins": ["specific things people praised"],\n'
            '  "gaps": ["specific things people said are missing or broken"],\n'
            '  "recommendations": ["concrete next steps for the team"]\n'
            "}\n\n"
            "Aim for 3-6 themes. weight=5 means many people raised it OR it's high-impact. "
            "Quotes must come from the input verbatim — do not paraphrase or invent."
        )

        response = await llm.complete(
            get_spec("survey_analysis"),
            messages=[{
                "role": "user",
                "content": f"Survey data follows. Generate the analysis JSON.\n\n{input_blob}",
            }],
            system=system,
            timeout=240.0,  # was 120s — 4096-token analysis on Kimi reasoning
        )
    except Exception as e:
        logger.error("Survey analysis LLM call failed", error=str(e), error_type=type(e).__name__)
        return _empty_themes()

    raw = response.text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return _empty_themes()
        return parsed
    except json.JSONDecodeError as e:
        # Don't log the raw output — it contains respondent quotes (PII).
        logger.warning("Survey analysis JSON parse failed", error=str(e), raw_len=len(raw))
        return _empty_themes()


def _empty_themes() -> dict:
    return {
        "headline": "Could not generate analysis (LLM parse failed).",
        "themes": [],
        "wins": [],
        "gaps": [],
        "recommendations": [],
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def generate_analysis(survey_id: UUID, session: AsyncSession) -> dict:
    """Generate full analysis blob for a survey. Returns the JSON dict that
    will be stored in `surveys.analysis`. The caller is responsible for
    persisting and updating `analysis_generated_at`.
    """
    survey_row = await session.execute(select(Survey).where(Survey.id == survey_id))
    survey = survey_row.scalar_one_or_none()
    if not survey:
        return _empty_themes() | {"headline": "Survey not found"}

    questions = survey.questions or []
    rows = await session.execute(
        select(SurveyResponse, User)
        .join(User, User.id == SurveyResponse.user_id)
        .where(SurveyResponse.survey_id == survey.id)
    )
    all_pairs = rows.all()
    submitted_pairs = [(r, u) for r, u in all_pairs if r.status == "submitted"]
    responses_only = [r for r, _ in all_pairs]

    stats = _per_question_stats(questions, responses_only)
    submitter_ids = [u.id for _, u in submitted_pairs]
    usage = await _usage_correlation(session, submitter_ids)

    if not submitted_pairs:
        return {
            "headline": "No submitted responses yet.",
            "themes": [],
            "wins": [],
            "gaps": [],
            "recommendations": [],
            "stats": stats,
            "usage_correlation": usage,
            "n_submitted": 0,
            "n_dismissed": sum(1 for r in responses_only if r.status == "dismissed"),
        }

    blob = _build_llm_input(survey, questions, submitted_pairs, stats, usage)
    themes = await _generate_themes(blob)

    return {
        **themes,
        "stats": stats,
        "usage_correlation": usage,
        "n_submitted": len(submitted_pairs),
        "n_dismissed": sum(1 for r in responses_only if r.status == "dismissed"),
    }
