"""
Workflow Execution Service — runs a workflow's prompt with full tool access
via an autonomous agent loop, same as agent_service / trigger_agent_service.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from pydantic import BaseModel
from services.slack_client import get_slack_client
from sqlalchemy import select, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from config import ADVISOR_ENABLED, ADVISOR_TOOL, CODE_EXECUTION_TOOL, Models
from services.llm import get_spec, llm
from services.llm.client import is_transient_error
from services.llm.code_execution_bridge import EXECUTE_PYTHON_TOOL_SCHEMA
from services.llm.context_bounding import approx_request_tokens, bound_request
from services.llm.types import LLMResponse
from db.database import AsyncPostgresSessionGideon, TOOL_DB_SEMAPHORE
from db.models import Integration, Skill, User, Workflow, WorkflowRun
from services.encryption_service import decrypt
from services.events_service import track_error, track_workflow_triggered, track_llm_provider_fallback
from services.integration_alerts import alert_workflow_failed
from services.prompt_service import build_system_prompt, get_user_slack_id
from services.slack_format import markdown_to_slack_mrkdwn
from services.usage_service import log_usage
from services.user_cache_service import get_user_auth_token
from services.workflow_run_policy import MAX_WALL_CLOCK_SECONDS
from tools.slack_tool import SLACK_USER_TZ
from tools.tool_executor import execute_tool_call
from tools.tool_registry import get_tools_for_user

logger = structlog.get_logger()

# Round cap. Raised 50 -> 100 (2026-06): high-volume workflows (CRM
# re-engagement, network monitoring over many contacts) legitimately need more
# than 50 tool-rounds. We do NOT truncate or early-finalize to fit a cap —
# user work must not be silently dropped — so we extend the budget instead and
# let the wall-clock cap below be the real runaway backstop.
MAX_ROUNDS = 100

# Per-run cap on how many times a SINGLE tool may be called before the workflow
# loop refuses further calls of it (with a nudge to bulk/search/synthesize). A
# well-behaved workflow calls any one tool a handful of times; hundreds means an
# unbounded per-item fan-out that will exhaust MAX_ROUNDS. 40 is generous headroom
# over normal use while catching a runaway long before the round ceiling.
_WORKFLOW_PER_TOOL_CALL_CAP = 40

# Dynamic provider escalation: workflows run on cheap Kimi (256K window) by
# default, but a single run whose accumulated context outgrows that would 400.
# Rather than truncate (compromising user work), we escalate THAT run to
# Anthropic Sonnet's 1M window once it crosses this threshold. Only runs that
# actually get big pay Anthropic, and only from the crossing point on — most
# runs stay entirely on Kimi. Threshold sits below Kimi's 256K so we switch
# BEFORE it would overflow. ~4 chars/token approximation for the estimate.
CONTEXT_SWITCH_TOKENS = 150_000
# Sonnet 5 (was sonnet-4-6): same 1M window, cheaper-or-equal pricing, more
# capable on the hard big-context runs this fallback exists for. Adaptive
# thinking only + non-default sampling params 400 on Sonnet 5 — the executor's
# Anthropic path sends neither (see services/llm/anthropic_adapter.py), so it's
# a safe swap. Sonnet 5's ~30% denser tokenizer widens the ~4 chars/token
# estimate error, but the reactive context-shrink recovery below absorbs it.
# Env-overridable. Also used by the auto-pin path (chronic-overflow workflows
# start directly here). Uses the shared config.Models.SONNET_5 constant so this
# and CHAT_FALLBACK_MODEL can't drift when the fallback model changes (config is
# already imported above; it imports no services module, so there's no cycle).
WORKFLOW_FALLBACK_MODEL = os.environ.get("WORKFLOW_FALLBACK_MODEL", Models.SONNET_5)

# Auto-pin: a workflow that escalates Kimi -> Anthropic on >= _PIN_THRESHOLD of
# its last _PIN_WINDOW SCHEDULED runs gets pinned to start on Anthropic 1M
# directly (skipping the wasted Kimi attempt every run). Only AUTO-mode
# workflows (preferred_provider IS NULL) are auto-pinned; a manual 'kimi'/'anthropic'
# choice is always respected. Tunable via env for safe rollout.
_PIN_WINDOW = int(os.environ.get("WORKFLOW_PIN_WINDOW", "3"))
_PIN_THRESHOLD = int(os.environ.get("WORKFLOW_PIN_THRESHOLD", "2"))

# Soft wall-clock cap on a single workflow run. Anything taking longer is
# almost certainly stuck in a tool-loop (Exa keeps paginating, a Google
# Sheets fetch keeps retrying, etc.) and burning tokens for no progress.
# Caught when Harsh Anand's "Applications Due Today - Daily Sheet Sync"
# ran for 51 minutes on 2026-05-20 — single run cost ~$30-50 and was the
# headline anomaly behind that day's Anthropic invoice spike.
# We stop AT THE TOP of the next round (after current round's tool calls
# return) so we never abandon an in-flight tool call mid-stream.
#
# Raised 600 -> 1500 on 2026-06-03: the Kimi cutover (06-02) ~doubled per-round
# latency (reasoning), so legitimately long workflows that completed in ~18
# rounds on Sonnet now exceed 10 min and get aborted mid-converge (e.g. a heavy user's
# c0b519e1: 18 rounds on Sonnet 05-27 → capped at 13 rounds/784s on Kimi 06-03).
# Kimi is ~70% cheaper, so even an hour-long capped run is well below the cost
# that motivated this guardrail; the cap still kills a truly stuck loop.
# The value itself (5400s / 90 min, raised from 60 with the round-cap bump so
# genuinely long, high-volume workflows can finish; still bounds a stuck loop)
# lives in services/workflow_run_policy.py — imported at the top of this file —
# so routers/workflows_router.py can share it for the manual-run concurrent-run
# guard without a circular import.

# Workflow/command-management tools a RUN must never see. These are chat-agent
# tools; inside an execution they let a workflow create/activate/trigger/edit
# workflows or create slash commands — self-duplication (a PR-summary run once
# cloned itself), run_workflow recursion, and other meta-actions a scheduled run
# has no business doing. Read-only `list_workflows` is intentionally NOT blocked.
_WORKFLOW_MGMT_TOOLS_BLOCKED_IN_RUN = {
    "create_workflow",
    "activate_workflow",
    "run_workflow",
    "update_workflow",
    "deactivate_workflow",
    "create_custom_command",
    "update_custom_command",
}

# Run ids this pod is actively executing right now. Populated at loop entry,
# discarded on exit. The lifespan shutdown handler reads this to gracefully
# mark in-flight runs `interrupted` (resumable) on SIGTERM, instead of letting
# them die as orphaned `running` rows. Pod-local by design.
_active_run_ids: set[UUID] = set()

# Max times a single run may be resumed before we give up and fail it — guards
# against a "poison" run that crashes the pod every time it resumes.
MAX_RESUME_ATTEMPTS = 3

# Transient-error retry (429 / rate-limit / "engine overloaded" / 5xx / timeout)
# is owned SOLELY by the LLM wrapper — services/llm/client.py::_with_llm_retry,
# which honors Retry-After and covers every provider + caller. We must NOT add a
# second retry loop here: a bespoke 429 retry around stream_collect stacked on
# the wrapper's, turning one Anthropic 429 into up to 4x4=16 calls and making the
# inner attempts ignore Retry-After (and it only matched anthropic.RateLimitError
# anyway, so it was dead for Kimi). This stays a thin forwarder — kept for the
# call-site's readability and the 900s workflow timeout.
# Workflows are non-interactive (no user waiting), so they ride out a transient
# provider overload in-call with a more patient budget than chat's default 4
# attempts / 30s — ~1 min of backoff (1+2+4+8+16+32s across 7 attempts) before
# the error bubbles to the pause-and-resume handler. Short blips never reach the
# resume hop; longer outages still fall through to checkpoint-resume.
_WORKFLOW_RETRY_MAX_ATTEMPTS = 7
_WORKFLOW_RETRY_MAX_DELAY_S = 60.0
# Wall-clock budget across all retries of a single round. Bounds the worst case
# so retries can't blow the run's MAX_WALL_CLOCK_SECONDS (90 min) cap: with the
# 900s per-request timeout, 7 attempts could otherwise burn ~105 min on repeated
# timeouts. 600s sits above the ~63s of overload backoff (fast-failing 429s still
# get all 7 attempts) but below the 900s timeout, so one timed-out attempt ends
# the retries and the run pauses for checkpoint-resume instead of looping.
_WORKFLOW_RETRY_MAX_TOTAL_S = 600.0


async def _llm_stream_with_retry(
    spec,
    messages: list[dict],
    *,
    system,
    tools,
    timeout: float = 900.0,
) -> LLMResponse:
    return await llm.stream_collect(
        spec, messages=messages, system=system, tools=tools, timeout=timeout,
        retry_max_attempts=_WORKFLOW_RETRY_MAX_ATTEMPTS,
        retry_max_delay=_WORKFLOW_RETRY_MAX_DELAY_S,
        retry_max_total_seconds=_WORKFLOW_RETRY_MAX_TOTAL_S,
    )

GIDEON_APP_URL = os.environ.get("GIDEON_APP_URL", "http://localhost:3000")
# Slack @mention target on workflow failures -- forwards RCA requests to
# Set RCA_SLACK_USER_ID to the Slack user who should receive run RCAs.
RCA_SLACK_USER_ID = os.environ.get("RCA_SLACK_USER_ID", "")

# Strong refs for fire-and-forget DM tasks so they aren't GC'd mid-flight.
_notification_tasks: set[asyncio.Task] = set()


def _should_alert(alert_on: str, status: str) -> bool:
    """Gate the completion DM on the workflow's per-run-outcome preference.
    `alert_on`: "none" | "failure" | "success" | "both" (default "failure")."""
    mode = (alert_on or "failure").lower()
    if mode == "none":
        return False
    if mode == "both":
        return True
    if mode == "success":
        return status == "completed"
    # default / "failure"
    return status == "failed"


def _schedule_completion_dm(
    user_id: UUID,
    workflow_id: UUID,
    run_id: UUID,
    workflow_name: str,
    status: str,
    error: str | None = None,
    primary_artifact: dict | None = None,
    alert_on: str = "failure",
) -> None:
    """Fire-and-forget Slack DM on workflow completion/failure, subject to the
    workflow's `alert_on` preference (no-op when the outcome isn't opted in)."""
    if not _should_alert(alert_on, status):
        return
    task = asyncio.create_task(
        _send_workflow_completion_dm(
            user_id, workflow_id, run_id, workflow_name, status, error, primary_artifact
        )
    )
    _notification_tasks.add(task)
    task.add_done_callback(_notification_tasks.discard)


async def _send_workflow_completion_dm(
    user_id: UUID,
    workflow_id: UUID,
    run_id: UUID,
    workflow_name: str,
    status: str,
    error: str | None,
    primary_artifact: dict | None,
) -> None:
    """Send the user a Slack DM summarizing the workflow run outcome.

    Success DMs lead with the **primary artifact URL** (a Doc/Sheet/Notion page
    the model created via `mark_artifact`) — that way one click on the DM lands
    on the actual deliverable, not the workflow config. If the model didn't
    mark any artifact, we fall back to the Gideon-hosted run report page,
    which always exists.
    """
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            result = await session.execute(
                select(Integration).where(
                    Integration.user_id == user_id,
                    Integration.provider == "slack",
                    Integration.is_active == True,
                )
            )
            integ = result.scalar_one_or_none()
            if not integ or not integ.bot_token:
                return
            extra = integ.extra_data or {}
            slack_user_id = extra.get("user_id")
            if not slack_user_id:
                return
            bot_token = decrypt(integ.bot_token)
        # Session closed before external Slack calls.

        client = get_slack_client(token=bot_token)
        open_resp = await client.conversations_open(users=slack_user_id)
        channel_id = open_resp["channel"]["id"]

        run_link = f"{GIDEON_APP_URL}/workflows/{workflow_id}/runs/{run_id}"
        rca_mention = f"<@{RCA_SLACK_USER_ID}>"

        if status == "failed":
            header = f":warning: Workflow *{workflow_name}* failed."
            detail = (f"\n> {error[:500]}" if error else "")
            body = (
                f"{header}{detail}\n"
                f"<{run_link}|View the run>\n"
                f"_If you'd like an RCA or a fix, forward this to {rca_mention}._"
            )
        else:
            header = f":white_check_mark: Workflow *{workflow_name}* completed."
            if primary_artifact and primary_artifact.get("url"):
                title = primary_artifact.get("title") or "Open output"
                # Slack mrkdwn link form `<url|label>` is the only safe way to
                # surface a URL — bare URLs auto-link, but adjacent characters
                # (italic `_`, brackets, periods) can be absorbed into the
                # link target. That's how an earlier `_…{run_link}_` line was
                # rendering with a trailing `_` in the resolved URL.
                body = (
                    f"{header}\n"
                    f"<{primary_artifact['url']}|Open: {title}>\n"
                    f"<{run_link}|View run in Gideon>"
                )
            else:
                body = (
                    f"{header}\n"
                    f"<{run_link}|Open report>"
                )

        await client.chat_postMessage(channel=channel_id, text=markdown_to_slack_mrkdwn(body))
    except Exception as e:
        logger.warning(
            "Workflow completion DM failed",
            user_id=str(user_id),
            workflow_id=str(workflow_id),
            run_id=str(run_id),
            error=str(e),
        )


async def _execute_workflow_tool_parallel(
    block_name: str, block_input: dict, user_id: UUID, auth_token: str = ""
) -> dict:
    """Execute a single tool call with its own DB session. Safe for asyncio.gather.

    `auth_token` is the user's last-known bearer token (cached in Redis
    on every chat send by chat_router). Tools that gate on RBAC permissions —
    notably the analytics SQL tools — resolve permissions with this token to
    decide which tables the user can read. Without it they return "no access",
    which is why scheduled workflows used to fail on analytics queries even
    when the same prompt worked in chat.
    """
    try:
        # Bound concurrent tool-session checkouts per pod (shared with chat) so
        # a wide workflow fan-out can't exhaust the DB pool. Session opened only
        # after the semaphore is acquired — never hold a connection while queued.
        async with TOOL_DB_SEMAPHORE:
            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as tool_session:
                return await execute_tool_call(block_name, block_input, user_id, tool_session, auth_token=auth_token)
    except Exception as tool_err:
        logger.error("Workflow tool crashed", tool=block_name, error=str(tool_err))
        return {"error": f"Tool execution failed: {type(tool_err).__name__}"}


# `mark_artifact` is an internal-only tool wired ONLY for workflow runs. It
# doesn't dispatch through tool_executor — the loop intercepts it, validates
# the payload, and appends to the run's artifact list. The model is
# instructed (in WORKFLOW_SYSTEM_ADDON) to call this at the end of every run
# with whatever tangible output it produced (a Doc, Sheet, Notion page,
# Linear issue, posted Slack message, etc.).
ARTIFACT_KINDS = {
    "gdoc", "gsheet", "gslide", "gdrive_file",
    "notion_page", "notion_database",
    "linear_issue", "linear_project",
    "github_issue", "github_pr",
    "slack_message", "email", "figma_file",
    "report", "link",
}

# Higher rank wins as the primary artifact when multiple were marked. Anything
# not listed gets rank 0 (falls below everything explicit).
_KIND_PRIORITY = {
    "gdoc": 100, "gsheet": 90, "gslide": 80, "notion_page": 75,
    "notion_database": 70, "gdrive_file": 60, "figma_file": 55,
    "linear_project": 50, "linear_issue": 45,
    "github_pr": 40, "github_issue": 35,
    "slack_message": 25, "email": 20, "link": 10, "report": 5,
}

MARK_ARTIFACT_TOOL = {
    "name": "mark_artifact",
    "description": (
        "Record a tangible deliverable you produced during this workflow run "
        "(e.g. the Google Doc, Sheet, Notion page, Linear issue, or Slack "
        "message permalink that holds the report or final output). Call this "
        "at the END of the run for the primary deliverable. You may call it "
        "multiple times if you produced more than one; the first one with the "
        "highest priority kind becomes the link the user clicks from Slack."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Absolute URL to the artifact (https://...).",
            },
            "kind": {
                "type": "string",
                "enum": sorted(ARTIFACT_KINDS),
                "description": "Type of artifact. Pick the most specific match.",
            },
            "title": {
                "type": "string",
                "description": "Short human-readable title shown to the user.",
            },
        },
        "required": ["url", "kind", "title"],
    },
}


def _validate_artifact(payload: dict) -> dict | None:
    """Return a normalized artifact dict, or None if the payload is invalid.
    Defensive: the model can pass anything, so we sanity-check before storing."""
    url = (payload.get("url") or "").strip()
    kind = (payload.get("kind") or "").strip().lower()
    title = (payload.get("title") or "").strip()
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    if kind not in ARTIFACT_KINDS:
        kind = "link"
    return {
        "url": url[:2000],
        "kind": kind,
        "title": (title or url)[:300],
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _pick_primary_artifact(artifacts: list[dict]) -> dict | None:
    """Highest-priority kind wins; ties broken by insertion order."""
    if not artifacts:
        return None
    return max(artifacts, key=lambda a: _KIND_PRIORITY.get(a.get("kind", ""), 0))


def _serialize_messages_for_replay(messages: list[dict]) -> list[dict]:
    """Convert the in-memory message list (a mix of Anthropic SDK content
    block objects and plain dicts) into pure-dict JSON-safe form for
    persistence into `workflow_runs.replay_messages`.

    Each message is `{"role": ..., "content": ...}`. `content` is either a
    string (plain user text) or a list of blocks. Blocks are SDK objects
    whenever they came directly from `response.content`, and dicts when we
    constructed them locally (tool_results, max_tokens nudges, etc.).
    """
    out: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            # Defensive: unexpected shape — store as-is, JSON encoder may fail
            # later but at least we keep something.
            out.append({"role": role, "content": content})
            continue
        serialized_blocks: list[dict] = []
        for block in content:
            if isinstance(block, dict):
                serialized_blocks.append(block)
                continue
            # Try Pydantic model_dump first (current SDK), fall back to dict()
            try:
                serialized_blocks.append(block.model_dump(mode="json"))
                continue
            except Exception:
                pass
            try:
                serialized_blocks.append(dict(block))
                continue
            except Exception:
                pass
            # Last resort: only the type marker so the entry isn't lost.
            serialized_blocks.append({
                "type": getattr(block, "type", "unknown"),
                "_unserialized": repr(block)[:500],
            })
        out.append({"role": role, "content": serialized_blocks})
    return out

WORKFLOW_SYSTEM_ADDON = """
## Workflow Mode -- Autonomous Scheduled Execution

You are executing an automated workflow the user set up. They are NOT watching in real-time.

### How to Execute

1. **Read the instructions carefully.** The workflow prompt tells you exactly what to do.
2. **Use your tools.** You have access to all the user's connected integrations (Gmail, Slack, Calendar, Linear, Notion, GitHub, etc.). Use them to gather data and take actions.
3. **Take action.** Unlike chat mode, you should proactively complete the task: send Slack messages, create Linear issues, draft emails, update Notion pages -- whatever the workflow requires.
4. **Report results.** Your final text output is stored as the workflow run result. Make it a clear, structured markdown summary of what you did.

### Producing a Tangible Artifact (REQUIRED)

The user clicks one link from a Slack DM after the run completes. That link
must land them on the actual deliverable, not on the workflow config.

- **Default to creating a real artifact** the user can open: a Google Doc for
  reports, a Google Sheet for tabular data, a Notion page for docs that live
  alongside their team's notes, a Linear issue for tracked work, a Slack
  message for a shared update. Pick the format that fits the workflow's
  intent — when the prompt says "create a report", that means a Doc; when it
  says "weekly metrics", a Sheet usually fits better.
- **Even if the prompt is vague** ("send me a summary of X"), still create a
  Doc the user can open, then mark that. A summary that lives only in your
  text response is hard to share or revisit.
- **At the END of the run, call `mark_artifact`** with the absolute URL,
  kind (`gdoc`, `gsheet`, `gslide`, `notion_page`, `linear_issue`,
  `slack_message`, `email`, etc.), and a short title. If you produced more
  than one deliverable, mark each one — the system picks the most important
  as the link in the user's Slack DM.
- If you genuinely produced nothing tangible (e.g. you only did internal
  reasoning), it's fine to skip `mark_artifact` — the system will fall back
  to a Gideon-hosted page rendering your final text response.

### Safety Rules

- **Email**: Use `draft_email` unless the workflow explicitly says to send. The user can review drafts.
- **Destructive actions**: Don't delete or overwrite unless the workflow explicitly instructs it.
- **Scope**: Stay on task. Complete the workflow prompt, nothing more.

### Quality Bar

- Be thorough: read full threads, not just subjects.
- Be specific: include names, dates, links, IDs in your report.
- Be concise: the user should scan your output in 30 seconds.
"""


# ---------------------------------------------------------------------------
# Main execution orchestrator
# ---------------------------------------------------------------------------

async def execute_workflow(
    workflow_id: UUID,
    user_id: UUID,
    session: AsyncSession,
) -> None:
    """
    Execute a workflow run using the full agent loop.
    Creates its own WorkflowRun record (used by the scheduler).
    """
    # Load the workflow
    result = await session.execute(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.user_id == user_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        logger.error("Workflow not found for execution", workflow_id=str(workflow_id))
        return

    # Create the run
    run = WorkflowRun(
        workflow_id=workflow_id,
        user_id=user_id,
        status="running",
        source="scheduled",
    )
    session.add(run)
    await session.flush()
    await session.refresh(run)
    run_id = run.id
    # Commit the run row immediately so it's durable from the start. If we left
    # it uncommitted until the first in-loop commit, a failure during setup
    # would trigger _fail_run's rollback, which would discard this INSERT — the
    # failed run would then vanish entirely (its UPDATE matches no row) instead
    # of being recorded as failed.
    await session.commit()
    track_workflow_triggered(str(user_id), str(workflow_id), trigger_type="scheduled")

    await _execute_workflow_loop(workflow, user_id, run_id, session)


async def execute_workflow_for_run(
    workflow_id: UUID,
    user_id: UUID,
    run_id: UUID,
    session: AsyncSession,
) -> None:
    """
    Execute a workflow using a pre-created WorkflowRun record (used by manual "Run Now").
    """
    result = await session.execute(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.user_id == user_id)
    )
    workflow = result.scalar_one_or_none()
    if not workflow:
        logger.error("Workflow not found for execution", workflow_id=str(workflow_id))
        await _fail_run(session, run_id, "Workflow not found")
        return

    await _execute_workflow_loop(workflow, user_id, run_id, session)


# Anthropic's 1M context is a HARD cap — exceeding it 400s the whole run
# ("prompt is too long: N > 1000000"). Ceiling sits below 1M to leave headroom
# for the system prompt, tool schemas, and the 32K max output budget. The
# token-budgeting / field-truncation / drop-oldest machinery lives in the shared
# `services.llm.context_bounding` module (chat uses it too); this wrapper only
# supplies the workflow-specific ceiling + orphan-server-tool strip, and logs.
_ANTHROPIC_CONTEXT_CEILING_TOKENS = 920_000
# Kimi's hard context window is 262_144; sit below it to leave headroom for the
# 32K output budget + tool schemas. Unlike Anthropic (1M beta), Kimi previously
# had NO proactive bound before sending — a big round (e.g. carrying a full-day
# read_slack_channel result) just crawled/hung instead of erroring, wedging the
# run. Now all providers are bounded before every request.
_KIMI_CONTEXT_CEILING_TOKENS = 224_000
# gpt-5-class: 400K window but 272K input cap → sit at 250K (mirrors chat's
# CHAT_CONTEXT_CEILING_TOKENS["openai"]). Distinct from Kimi's 224K — routing
# OpenAI through Kimi's ceiling would truncate ~26K tokens of history early.
_OPENAI_CONTEXT_CEILING_TOKENS = 250_000

# Per-provider request ceiling. Workflow-specific: Anthropic runs the 1M-context
# beta here (unlike native chat, which caps at 160K), so it gets a far higher
# ceiling. Unknown/new provider → the most conservative ceiling (Kimi's 224K).
_WORKFLOW_CONTEXT_CEILING_TOKENS = {
    "anthropic": _ANTHROPIC_CONTEXT_CEILING_TOKENS,
    "openai": _OPENAI_CONTEXT_CEILING_TOKENS,
    "kimi": _KIMI_CONTEXT_CEILING_TOKENS,
}
_WORKFLOW_CONTEXT_CEILING_DEFAULT = _KIMI_CONTEXT_CEILING_TOKENS

# Reactive context-shrink recovery. Our pre-send bound uses a ~4 chars/token
# estimate; dense payloads (Grafana/ClickHouse row dumps, Sheets exports, long
# non-English text) tokenize closer to ~3 chars/token, so a request we bounded
# under the 920K Anthropic ceiling can STILL real-count past the 1M hard cap and
# 400 ("prompt is too long: N > 1000000"). Before this, that hard-failed the run
# with no recovery even though we were already on the top provider (Anthropic
# 1M) with nowhere left to escalate — Saksham's "Ping Panic 30-Day" + Richa's
# "Daily Call & Chat" did this almost daily. Now we read the REAL token count the
# provider reports, derive this payload's true chars/token density, re-bound
# under a corrected ceiling, and retry the round. Bounded so it can't loop:
# at most _MAX_CONTEXT_SHRINKS corrections, each strictly tighter, never below
# the floor (which stays comfortably inside every top provider's window).
_MAX_CONTEXT_SHRINKS = 4
_CONTEXT_SHRINK_FLOOR_TOKENS = 200_000



def _bound_request_context(
    messages: list,
    system_prompt: str,
    tools: list,
    ceiling: int = _ANTHROPIC_CONTEXT_CEILING_TOKENS,
) -> list:
    """Return a request-safe copy of `messages` guaranteed under `ceiling` (the
    provider's context window — 1M-beta for Anthropic, ~262K for Kimi).

    Truncates oversized STRING contents (never drops messages in the common path,
    so tool_use/tool_result pairing + thinking-block ordering stay intact); halves
    the field cap down to a floor; only then drops oldest messages, stripping
    orphan server-tool blocks from the survivors. No-op when already under the
    ceiling; the caller keeps the original `messages` untouched (full fidelity for
    checkpoint / resume)."""
    outcome = bound_request(
        messages, system_prompt, tools, ceiling,
        transform=_strip_orphan_server_tools,
    )
    if outcome.method == "field_truncate":
        logger.warning(
            "Workflow request exceeded context ceiling — truncated oversized contents",
            field_cap_chars=outcome.field_cap,
            ceiling=ceiling,
        )
    elif outcome.method == "drop_oldest":
        logger.warning(
            "Workflow request over ceiling after flooring fields — dropped oldest messages",
            approx_tokens=outcome.after_tokens,
            ceiling=ceiling,
            kept_messages=outcome.kept_messages,
        )
    return outcome.messages


def _is_server_tool_result_type(btype: Optional[str]) -> bool:
    """True for a server-side tool RESULT block.

    Anthropic emits one result block per server tool, each with its own type:
    `code_execution_tool_result`, `bash_code_execution_tool_result`,
    `text_editor_code_execution_tool_result`, `web_search_tool_result`,
    `advisor_tool_result`, `mcp_tool_result`, etc. They all share the
    `_tool_result` suffix and carry a `tool_use_id`. The plain CLIENT block is
    exactly `tool_result` (no prefix) — exclude it; client `tool_use`/`tool_result`
    pairing is handled by the separate 3-pass repair, not here. Matching by suffix
    (rather than a hardcoded set) means a new server tool type — including
    `advisor_tool_result`, which an earlier hardcoded set omitted and so caused the
    advisor's `server_tool_use` to be dropped as a false orphan — pairs correctly.
    """
    return isinstance(btype, str) and btype.endswith("_tool_result") and btype != "tool_result"


def _block_fields(block: object) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Return `(type, id, tool_use_id)` for a content block that may be a plain
    dict OR a raw Anthropic SDK block object (pydantic `BaseModel`).

    Assistant turns replayed verbatim from `response.raw.content` on the Anthropic
    path hold SDK objects, not dicts. A dict-only inspection silently skipped them,
    so an orphan `server_tool_use` SDK object survived the strip and reached the
    request (run d1ba4ced: a `bash_code_execution` cut off by max_tokens — no
    result block — 400'd the next round). Reading the three pairing fields via
    `model_dump` (scoped to those keys so a huge tool `input` isn't serialized)
    lets the orphan-strip see SDK blocks too. Any other object type → all-None.
    """
    if isinstance(block, dict):
        return block.get("type"), block.get("id"), block.get("tool_use_id")
    if isinstance(block, BaseModel):
        d = block.model_dump(mode="json", include={"type", "id", "tool_use_id"})
        return d.get("type"), d.get("id"), d.get("tool_use_id")
    return None, None, None


def _strip_orphan_server_tools(
    messages: list[dict], preserve_trailing_server_use: bool = False
) -> list[dict]:
    """Drop server-side tool blocks that aren't a matched pair.

    Anthropic 400s if a `server_tool_use` (e.g. `bash_code_execution`) appears in
    the replayed history without its corresponding `*_tool_result` block, or vice
    versa. These can get split across messages by a `pause_turn` continuation, an
    escalation switch mid-run, a checkpoint taken between the use and its result,
    or — the case that kept recurring — a `max_tokens` round that emits a
    `server_tool_use` but is cut off before the server result, after which the
    recovery code appends a user turn right behind the unpaired use. The
    client-side `tool_use`/`tool_result` 3-pass repair doesn't touch server blocks
    (different types), so this is the server-tool equivalent: keep only
    `server_tool_use` whose id has a matching result somewhere, and only results
    whose `tool_use_id` has a matching `server_tool_use`. Drop the rest. An
    assistant turn emptied — or reduced to only thinking — by the strip gets a
    text placeholder so content is always a valid standalone assistant turn.

    Block-type/id inspection goes through `_block_fields`, which reads BOTH plain
    dicts AND raw Anthropic SDK block objects. The Anthropic path replays
    `response.raw.content` verbatim (SDK objects); an earlier dict-only inspection
    skipped those, so an orphan `server_tool_use` SDK object slipped through every
    strip and 400'd the request (run d1ba4ced).

    `preserve_trailing_server_use`: when True, a `server_tool_use` that lives in
    the LAST list-content message is exempt from the orphan drop even with no
    result yet. That is the legitimate `pause_turn` replay state — the server
    already started the tool and is waiting for us to replay so it can continue;
    stripping it there would break the continuation. Set True when sanitizing
    right before sending a live round; leave False for escalation-conversion and
    resume seeds, where the tail is always a user/tool_result turn anyway.
    """
    result_ids: set[str] = set()
    use_ids: set[str] = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            btype, bid, btuid = _block_fields(b)
            if btype == "server_tool_use" and bid:
                use_ids.add(bid)
            elif _is_server_tool_result_type(btype) and btuid:
                result_ids.add(btuid)

    # Exempt the trailing pause_turn server_tool_use(s) from the orphan drop.
    # A genuine pause_turn leaves the unpaired use in the VERY LAST message, which
    # is always an assistant turn with list content — the server is mid-execution
    # and we replay so it can continue. If anything follows the use (e.g. a user
    # nudge appended after a max_tokens cutoff), it is NOT pause_turn and stays
    # eligible for the drop. So only the final message qualifies — do not skip
    # back past a trailing non-assistant/non-list turn.
    exempt_use_ids: set[str] = set()
    if preserve_trailing_server_use and messages:
        last = messages[-1]
        last_content = last.get("content")
        if last.get("role") == "assistant" and isinstance(last_content, list):
            for b in last_content:
                btype, bid, _btuid = _block_fields(b)
                if btype == "server_tool_use" and bid:
                    exempt_use_ids.add(bid)

    out: list[dict] = []
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        kept: list = []
        for b in content:
            btype, bid, btuid = _block_fields(b)
            if (
                btype == "server_tool_use"
                and bid not in result_ids
                and bid not in exempt_use_ids
            ):
                continue  # orphan server tool_use -> drop
            if _is_server_tool_result_type(btype) and btuid not in use_ids:
                continue  # orphan server tool_result -> drop
            kept.append(b)  # keep the ORIGINAL block (dict OR SDK object) for fidelity
        if m.get("role") == "assistant" and not any(
            _block_fields(b)[0] not in ("thinking", "redacted_thinking") for b in kept
        ):
            # Emptied, OR reduced to only thinking after dropping an orphan
            # server_tool_use (a max_tokens-truncated step whose result never
            # arrived): a thinking-only turn is not a valid standalone assistant
            # message to replay, so collapse it to a text placeholder.
            kept = [{"type": "text", "text": "(continuing)"}]
        out.append({**m, "content": kept})
    return out


def _make_resume_safe(messages: list[dict]) -> list[dict]:
    """Defensive guard before replaying a resume checkpoint.

    Checkpoints are taken at the TOP of a round, where `messages` already ends on
    the user/tool_result turn (assistant speaks next) — a valid replay state. This
    only protects against an unexpected tail: if the last message is an assistant
    turn carrying tool_use blocks with no following tool_result, drop it so we
    never replay a dangling tool_use (which Anthropic 400s on).
    """
    msgs = list(messages or [])
    while msgs and isinstance(msgs[-1], dict) and msgs[-1].get("role") == "assistant":
        content = msgs[-1].get("content")
        has_tool_use = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        )
        if has_tool_use:
            msgs.pop()
        else:
            break
    # Also drop any orphan server-side code-exec blocks (a checkpoint can land
    # between a bash_code_execution use and its result), which would 400 on replay.
    return _strip_orphan_server_tools(msgs)


async def resume_workflow_run(run_id: UUID, user_id: UUID, session: AsyncSession) -> None:
    """Resume an interrupted run from its checkpoint instead of re-running it.

    The caller (scheduler resumer) has already atomically claimed the run
    (status interrupted -> running, resume_count incremented). We rehydrate the
    checkpointed message state + next round and continue the agent loop. If the
    checkpoint is missing/empty we fall back to a clean run from round 0.
    """
    run = (
        await session.execute(
            select(WorkflowRun).where(WorkflowRun.id == run_id, WorkflowRun.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not run:
        logger.error("Resume: run not found", run_id=str(run_id))
        return

    workflow = (
        await session.execute(
            select(Workflow).where(Workflow.id == run.workflow_id, Workflow.user_id == user_id)
        )
    ).scalar_one_or_none()
    if not workflow:
        await _fail_run(session, run_id, "Workflow not found on resume")
        return

    seed = _make_resume_safe(run.replay_messages or [])
    resume_from = run.last_round or 0
    if not seed:
        seed, resume_from = None, 0  # nothing usable — run fresh

    logger.info(
        "Resuming interrupted workflow run",
        run_id=str(run_id), workflow_id=str(run.workflow_id),
        resume_from=resume_from, resume_count=run.resume_count,
    )
    await _execute_workflow_loop(
        workflow, user_id, run_id, session,
        resume_from=resume_from, seed_messages=seed,
        force_anthropic=bool(run.escalated_to_anthropic),
    )


def get_active_run_ids() -> set[UUID]:
    """Snapshot of run ids this pod is actively executing right now. The reaper
    uses it as a belt-and-suspenders guard so a live-but-slow run on THIS pod
    (e.g. a long single round) is never reaped even if its heartbeat looks stale.
    Returns a copy — callers must not mutate the internal set."""
    return set(_active_run_ids)


async def interrupt_active_runs() -> int:
    """Graceful-shutdown hook: mark this pod's in-flight runs `interrupted` so the
    next pod resumes them from checkpoint, instead of leaving orphaned `running`
    rows. Best-effort and fast (called from the lifespan shutdown inside the k8s
    termination grace window). Returns the count of run ids it attempted.

    Only flips rows still in `running` — a run that completed between the snapshot
    and this UPDATE keeps its terminal status (the WHERE guards it).
    """
    run_ids = list(_active_run_ids)
    if not run_ids:
        return 0
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as s:
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id.in_(run_ids), WorkflowRun.status == "running")
                .values(status="interrupted", last_progress_at=datetime.now(timezone.utc))
            )
        logger.info("Graceful drain: marked in-flight workflow runs interrupted", count=len(run_ids))
    except Exception as e:
        logger.error("Graceful drain failed to mark runs interrupted", error=str(e)[:160])
    return len(run_ids)


async def _execute_workflow_loop(
    workflow: Workflow,
    user_id: UUID,
    run_id: UUID,
    session: AsyncSession,
    *,
    resume_from: int = 0,
    seed_messages: Optional[list[dict]] = None,
    force_anthropic: bool = False,
) -> None:
    """Core agent loop shared by scheduled and manual runs.

    `resume_from` / `seed_messages` support resuming an interrupted run: the
    resumer rehydrates the checkpointed message state (`seed_messages`) and the
    next round (`resume_from`) so execution continues instead of restarting from
    scratch. Both default to a fresh run (round 0, prompt-only messages).

    `force_anthropic` resumes a run that had escalated Kimi -> Anthropic 1M
    (persisted on the run) straight on Anthropic, so it doesn't re-overflow Kimi
    before the in-loop escalation re-fires.
    """
    workflow_id = workflow.id
    # Mark this run as actively executing in THIS pod so the shutdown handler can
    # gracefully interrupt it on SIGTERM. Always discarded in the finally below.
    _active_run_ids.add(run_id)

    try:
        # Load user
        user_result = await session.execute(select(User).where(User.id == user_id))
        user = user_result.scalar_one_or_none()
        if not user:
            logger.error("Workflow: user not found", user_id=str(user_id))
            await _fail_run(session, run_id, "User not found")
            return

        # Scope the user's timezone for this run so Slack read tools resolve
        # human date / relative-word windows in the user's local time.
        SLACK_USER_TZ.set(user.timezone)

        # Cached bearer token for this user. Stored in Redis by
        # chat_router on every chat send (24h TTL, Fernet-encrypted). Empty
        # string is fine — RBAC tools will return their normal "no access"
        # error, same as for an unauthenticated user.
        auth_token = await get_user_auth_token(str(user_id))
        if not auth_token:
            logger.warning(
                "Workflow: no cached auth token; RBAC-gated tools (analytics) will be denied",
                user_id=str(user_id), workflow_id=str(workflow_id),
            )

        # Load tools + server-side tools (web search, code execution)
        tools = await get_tools_for_user(user_id, session)
        # A workflow run must NOT be able to create/activate/trigger workflows.
        # These management tools are for the chat agent only; handing them to the
        # executor lets a workflow spawn or fire workflows — which is how a daily
        # "summarize my PRs" run once called create_workflow mid-execution and
        # cloned itself into a duplicate (run 06-18, wf f67128c5). It's also a
        # recursion/runaway risk (run_workflow). list_workflows is read-only and
        # left in. Strip the state-changing ones from the executor's tool set so
        # the model can't even see them.
        tools = [t for t in tools if t.name not in _WORKFLOW_MGMT_TOOLS_BLOCKED_IN_RUN]
        tool_schemas = [t.to_claude_schema() for t in tools]
        # Internal-only "save what you made" tool. Dispatched in-loop, not via
        # tool_executor (no provider, no token).
        tool_schemas.append(MARK_ARTIFACT_TOOL)
        # Exa web search is registered as a regular tool in tool_registry

        # Artifact accumulator for this run. The loop appends every valid
        # `mark_artifact` invocation; we persist + pick a primary on completion.
        captured_artifacts: list[dict] = []

        # Load skill memories
        skill_memories = []
        try:
            skill_result = await session.execute(
                select(Skill).where(
                    Skill.user_id == user_id,
                    Skill.status == "completed",
                    Skill.memory_content.isnot(None),
                )
            )
            for skill in skill_result.scalars().all():
                skill_memories.append({
                    "name": skill.name,
                    "memory_content": skill.memory_content,
                })
        except Exception as e:
            logger.warning("Workflow: failed to load skill memories", error=str(e))

        slack_user_id = await get_user_slack_id(session, user_id)

        # Build system prompt. Workflows are automatic by design: every run
        # executes autonomously in auto mode ("draft it, then send it -- the
        # user trusts you to act"), so a scheduled run sends emails/Slack when
        # its prompt says to, with no human confirmation step. (Previously
        # auto_mode defaulted to False, injecting a contradictory "draft first,
        # do NOT send until the user confirms" instruction that made sends
        # unreliable -- some workflows sent, others silently left drafts.)
        system_prompt = build_system_prompt(
            persona_text=user.persona_text,
            user_name=user.name,
            user_email=user.email,
            slack_user_id=slack_user_id,
            connected_tools=tools,
            skill_memories=skill_memories if skill_memories else None,
            background_info=user.background_info,
            user_timezone=user.timezone,
            preferred_communication=user.preferred_communication,
            auto_mode=True,
        )
        system_prompt += "\n" + WORKFLOW_SYSTEM_ADDON

        # Build messages — or rehydrate a checkpoint when resuming. `seed_messages`
        # is sanitized by the resumer (orphan tool_use/tool_result repair) before
        # it reaches here, so it's a valid replay state.
        if seed_messages:
            messages = list(seed_messages)
            logger.info(
                "Workflow resuming from checkpoint",
                workflow_id=str(workflow_id), run_id=str(run_id),
                resume_from_round=resume_from, message_count=len(messages),
            )
        else:
            messages = [{"role": "user", "content": f"## Workflow Instructions\n\n{workflow.prompt}"}]

        # Workflows are background tasks with adaptive thinking + server-side tools
        # (advisor, code execution) that can have long stretches with no SSE bytes.
        # Spec timeout below is 900s (bumped from 300s) to ride out gaps between
        # SSE events; the wrapper streams under the hood to avoid Anthropic's
        # long-request timeout.
        from dataclasses import replace as _replace
        # 32768 max_tokens is double the prior 16384. With adaptive thinking
        # enabled, 16384 was tight enough that complex workflows (many parallel
        # tool results, long synthesis) had thinking consume the entire budget,
        # silently producing empty rounds (runs bbc41178 / 70016b85 on 2026-05-04).
        # Sonnet 4.6 supports up to 64K output tokens, so 32K leaves plenty of
        # headroom while keeping cost bounded.
        # Request the 1M-token context window (Anthropic beta) so heavy workflows
        # keep ALL their tool data instead of overflowing the 256K default and
        # 400'ing. Env-gated (WORKFLOW_1M_CONTEXT, default on) and only applies on
        # the anthropic provider; the adapter ignores the flag otherwise.
        _use_1m = os.environ.get("WORKFLOW_1M_CONTEXT", "true").lower() == "true"
        workflow_spec = _replace(get_spec("workflow"), max_tokens=32768, use_1m_context=_use_1m)
        # Start directly on Anthropic 1M — skipping the doomed Kimi attempt +
        # reactive switch — when EITHER:
        #   - this run is RESUMING one that already escalated (force_anthropic), or
        #   - the workflow is PINNED to Anthropic (workflow.preferred_provider),
        #     learned from chronic escalation (_maybe_pin_provider) or set manually
        #     in the editor. A workflow whose full-day context overflows Kimi every
        #     run would otherwise pay a wasted Kimi attempt before escalating.
        pinned_anthropic = (workflow.preferred_provider or "").lower() == "anthropic"
        if force_anthropic or pinned_anthropic:
            if workflow_spec.provider != "anthropic":
                workflow_spec = _replace(
                    workflow_spec, provider="anthropic",
                    model=WORKFLOW_FALLBACK_MODEL, use_1m_context=True,
                )
            # ALWAYS sanitize the message history — independent of the current
            # provider. A resumed escalated run's checkpoint can still hold
            # Kimi-origin unsigned thinking blocks (e.g. a reactive switch whose
            # latest checkpoint predates the sanitize), which Anthropic 400s on.
            # For a fresh pinned run the messages are prompt-only, so this is a
            # harmless no-op.
            messages = _to_anthropic_safe(messages)
            logger.info(
                "Workflow starting on Anthropic 1M",
                run_id=str(run_id),
                reason="resume_escalated" if force_anthropic else "pinned_provider",
            )
        all_text_parts: list[str] = []
        total_usage = None

        # Server-side tools the anthropic adapter doesn't add for us.
        # ADVISOR_TOOL is intentionally NOT appended here — the workflow spec's
        # `use_advisor=True` makes the anthropic adapter add it automatically
        # via `_build_tools_param`. Appending it again here produced a
        # duplicate tool definition (same name twice) which Anthropic 400'd
        # with "tool names must be unique".
        # Code execution is provider-specific. Anthropic runs it as a native
        # SERVER-side tool (CODE_EXECUTION_TOOL). Kimi/OpenAI CANNOT run that —
        # so we offer the `execute_python` function tool, which tool_executor
        # routes through Anthropic's hosted sandbox via code_execution_bridge
        # (identical to how chat handles code exec on Kimi). Without this,
        # PROVIDER_WORKFLOW=kimi runs got "Unknown tool" the moment the model
        # tried to compute anything, then produced garbage (Divyanshi's report
        # hand-computed the wrong epoch window -> empty sheet). Decided by the
        # INITIAL provider and left fixed for the run: a Kimi run that later
        # escalates to Anthropic keeps execute_python (the bridge works on
        # either provider), so the tool set never shifts mid-run.
        if workflow_spec.provider == "anthropic":
            server_tools: list[dict] = [CODE_EXECUTION_TOOL]
        else:
            server_tools = [EXECUTE_PYTHON_TOOL_SCHEMA]

        run_started_at = datetime.now(timezone.utc)

        # Fixed per-request overhead that the size estimate must include, or the
        # Kimi->Anthropic escalation undercounts and 400s before it triggers: the
        # system prompt + ALL tool schemas (241 tools is tens of thousands of
        # tokens) ride on every request alongside the message history.
        _base_context_chars = (
            len(system_prompt)
            + sum(len(json.dumps(t)) for t in (tool_schemas or []))
            + sum(len(json.dumps(t)) for t in server_tools)
        )

        # Per-run tool-call governance. Chat caps runaway tools per turn; workflows
        # had no equivalent, so a per-item fan-out (e.g. read_slack_channel once per
        # channel, read_slack_thread once per thread) silently burned all MAX_ROUNDS
        # then hard-failed (Harshil's 470-call scanner, Divyanshi's 158-thread
        # report). Once a single tool exceeds the cap, further calls are refused with
        # a nudge to use a bulk/search tool or synthesize — so the run FINISHES
        # best-effort instead of dying at the round ceiling. Counts are per tool NAME
        # (diverse parallel tool use is unaffected) and per run (reset on resume).
        tool_call_counts: dict[str, int] = {}
        capped_tools_logged: set[str] = set()

        # Reactive context-shrink recovery state (see _MAX_CONTEXT_SHRINKS). When
        # the top provider still 400s on context after proactive bounding, we
        # tighten this override and retry; `min`'d against the provider ceiling so
        # it only ever narrows. Reset per run (a resumed run re-derives it).
        context_ceiling_override: Optional[int] = None
        context_overflow_shrinks = 0

        # Resume continues from the checkpointed round; a fresh run starts at 0.
        for round_num in range(resume_from, MAX_ROUNDS):
            # Wall-clock soft-cap. Checked at the top of each round so we never
            # abort mid-tool-call. A stuck round (paginating Exa, retrying
            # Sheets, etc.) burns tokens proportional to its length; bailing at
            # 10 minutes caps the blast radius without affecting normal runs
            # which complete in ~3 minutes. (Clock resets per resume attempt;
            # MAX_RESUME_ATTEMPTS bounds total work across resumes.)
            elapsed = (datetime.now(timezone.utc) - run_started_at).total_seconds()
            if elapsed > MAX_WALL_CLOCK_SECONDS:
                logger.warning(
                    "Workflow exceeded wall-clock cap; aborting before next round",
                    workflow_id=str(workflow_id),
                    run_id=str(run_id),
                    elapsed_seconds=int(elapsed),
                    cap_seconds=MAX_WALL_CLOCK_SECONDS,
                    rounds_completed=round_num,
                )
                track_error(
                    str(user_id),
                    "workflow_execution_timeout",
                    detail=f"Exceeded {MAX_WALL_CLOCK_SECONDS}s wall-clock cap after {round_num} round(s)",
                )
                await _fail_run(
                    session,
                    run_id,
                    f"Exceeded {MAX_WALL_CLOCK_SECONDS}s wall-clock cap after {round_num} round(s) — likely a stuck tool loop.",
                    partial_output="\n\n".join(all_text_parts) if all_text_parts else None,
                )
                return

            # Escalate to Anthropic's 1M window once this run's full request size
            # approaches Kimi's 256K cap. Scoped to KIMI only — we don't override
            # an explicitly-configured openai/anthropic provider (openai has its
            # own large window and the operator chose it deliberately). The
            # estimate counts the WHOLE request — fixed overhead (system prompt +
            # tool schemas) PLUS the message history — so we switch before Kimi
            # would 400 on the real payload, not just on the history alone.
            # One-way, and the history is sanitized so Anthropic accepts the replay.
            if workflow_spec.provider == "kimi":
                msg_chars = sum(len(json.dumps(m)) for m in messages)
                approx_tokens = (_base_context_chars + msg_chars) // 4
                if approx_tokens > CONTEXT_SWITCH_TOKENS:
                    logger.info(
                        "Workflow request approaching Kimi window; escalating to Anthropic 1M",
                        approx_tokens=approx_tokens,
                        threshold=CONTEXT_SWITCH_TOKENS,
                        round=round_num + 1,
                        from_model=workflow_spec.model,
                        run_id=str(run_id),
                    )
                    workflow_spec = _replace(
                        workflow_spec,
                        provider="anthropic",
                        model=WORKFLOW_FALLBACK_MODEL,
                        use_1m_context=True,
                    )
                    messages = _to_anthropic_safe(messages)
                    await _mark_run_escalated(run_id)  # sticky: resume starts on Anthropic
                    # Preemptive reroute (avoided the overflow) — reason distinguishes
                    # it from the reactive on-failure escalation below.
                    track_llm_provider_fallback(
                        str(user_id), feature="workflow", from_provider="kimi",
                        reason="context_proactive", round_num=round_num + 1,
                    )

            logger.info(
                "Workflow agent round",
                round=round_num + 1,
                workflow_id=str(workflow_id),
                run_id=str(run_id),
            )

            round_tools = (tool_schemas or []) + server_tools

            # Drop any open implicit transaction on `session` BEFORE awaiting
            # the LLM round-trip. Workflow rounds routinely run minutes (heavy
            # tool use, 32K output budget, multiple rounds); Postgres's
            # `idle_in_transaction_session_timeout = 30s` (see the project
            # CLAUDE.md DB rules) will otherwise kill the connection and the
            # `_fail_run` / final WorkflowRun UPDATE below would roll back
            # with the "PendingRollbackError ... idle-in-transaction" cascade
            # we observed in Loki on 2026-05-28 around 04:30 UTC. After commit,
            # the next `session.execute()` transparently opens a fresh txn.
            await session.commit()

            # Final server-tool sanitize before the request. A previous round can
            # leave an UNPAIRED `server_tool_use` mid-history: e.g. a `max_tokens`
            # cutoff emits a `bash_code_execution` / `text_editor_code_execution`
            # use with no result, then the recovery path appends a user turn right
            # behind it (runs de9d550a / 761e535b / f6fb4a25 / 4e54b65a). That
            # orphan 400s the next request. Run UNCONDITIONALLY (not gated on
            # provider) — it's a no-op for Kimi/OpenAI history (no server_tool_use
            # blocks), and gating on provider was the one way an orphan could slip
            # through if provider state was momentarily off on a resumed/escalated
            # run (4e54b65a recurred with the gate in place). Preserve a
            # legitimately-trailing `pause_turn` use (server mid-execution, replayed
            # to continue). Cleaning before the checkpoint also keeps the resume
            # seed protocol-safe.
            messages = _strip_orphan_server_tools(
                messages, preserve_trailing_server_use=True
            )

            # Checkpoint + heartbeat for crash-resilience, on a fresh isolated
            # session. `messages` here is a valid replay state (assistant's turn
            # next), so resuming at `round_num` is always protocol-safe.
            await _checkpoint_run(run_id, messages, round_num)

            try:
                # stream_collect avoids Anthropic's long-request timeout on
                # non-streaming calls. _llm_stream_with_retry handles 429 with
                # Retry-After / exp-backoff; all other exceptions fall through
                # to the existing fail path.
                # Bound the request under Anthropic's 1M HARD cap. Escalation only
                # swaps the model; it never trims, so a workflow that pulls a huge
                # tool result (a 169K-token search_emails/read_email dump blew the
                # request to 1.1M on run 2ecedbde) 400s with "prompt is too long".
                # Truncate oversized block contents for the REQUEST only — the
                # in-memory `messages` (and the checkpoint) keep full fidelity for
                # resume; we re-bound each round. No-op under the ceiling and for
                # non-Anthropic providers (their own context handling applies).
                # Bound the request under the provider's window BEFORE sending —
                # ALL providers now, each with its own ceiling. Kimi previously had
                # no proactive bound, so a big round (carrying a full-day
                # read_slack_channel result) just crawled/hung on Kimi instead of
                # erroring, wedging the run. OpenAI must NOT inherit Kimi's 224K
                # ceiling (its input window is larger — 250K) or history truncates
                # early.
                # Only Kimi rounds are fail-fast: a Kimi round that exceeds ~5 min
                # is a hang, so time out → the retry/pause-resume path (or reactive
                # escalation) kicks in instead of wedging. Anthropic 1M rounds and
                # OpenAI o-series/gpt-5 reasoning rounds can legitimately run long,
                # so they keep the 900s budget.
                _req_timeout = 300.0 if workflow_spec.provider == "kimi" else 900.0
                # Inner retry loop: a TOP-provider context-overflow 400 (our
                # ~4 chars/token estimate undershot a dense payload) is recovered
                # IN PLACE — re-bound tighter and re-send WITHOUT consuming an outer
                # round-budget slot (the conversation hasn't advanced, so burning a
                # `round_num` on it would push MAX_ROUNDS earlier for no reason).
                # Bounded by _MAX_CONTEXT_SHRINKS + a strictly-decreasing ceiling so
                # it can't loop. EVERY other error (Kimi escalation, transient,
                # fatal) re-raises to the outer handler with its existing behavior;
                # Kimi context errors escalate the provider there, not here.
                while True:
                    _ceiling = _WORKFLOW_CONTEXT_CEILING_TOKENS.get(
                        workflow_spec.provider, _WORKFLOW_CONTEXT_CEILING_DEFAULT
                    )
                    # A prior shrink this run may have tightened the ceiling. Only
                    # ever narrows.
                    if context_ceiling_override is not None:
                        _ceiling = min(_ceiling, context_ceiling_override)
                    request_messages = _bound_request_context(
                        messages, system_prompt, round_tools, ceiling=_ceiling
                    )
                    try:
                        response = await _llm_stream_with_retry(
                            workflow_spec,
                            request_messages,
                            system=system_prompt,
                            tools=round_tools,
                            timeout=_req_timeout,
                        )
                        break
                    except Exception as call_err:
                        if not (
                            workflow_spec.provider != "kimi"
                            and _is_context_limit_error(call_err)
                            and context_overflow_shrinks < _MAX_CONTEXT_SHRINKS
                        ):
                            raise  # Kimi escalate / transient / fatal → outer except
                        # Read the REAL count the error reports, derive this
                        # payload's true density, and size a corrected ceiling so
                        # the re-estimated request lands under 90% of the provider's
                        # real max. Fall back to a multiplicative ratchet when the
                        # error carries no parseable count.
                        new_ceiling = int(_ceiling * 0.80)
                        overflow = _parse_context_overflow(call_err)
                        if overflow:
                            actual, hard_max = overflow
                            try:
                                est = approx_request_tokens(
                                    request_messages, system_prompt, round_tools
                                )
                            except Exception:
                                est = 0
                            if est > 0 and actual > 0:
                                density = actual / est
                                new_ceiling = int((hard_max * 0.90) / density)
                        # Guarantee forward progress (>=15% tighter each attempt);
                        # never collapse below the floor.
                        new_ceiling = min(new_ceiling, int(_ceiling * 0.85))
                        new_ceiling = max(new_ceiling, _CONTEXT_SHRINK_FLOOR_TOKENS)
                        context_overflow_shrinks += 1
                        context_ceiling_override = new_ceiling
                        logger.warning(
                            "Workflow hit provider context limit on top provider — "
                            "re-bounding tighter and retrying round in place",
                            round=round_num + 1, run_id=str(run_id),
                            prev_ceiling=_ceiling, new_ceiling=new_ceiling,
                            shrink_attempt=context_overflow_shrinks,
                            error=str(call_err)[:160],
                        )
                        track_error(
                            str(user_id), "workflow_context_rebound",
                            detail=f"round={round_num + 1}: ceiling {_ceiling}->{new_ceiling}",
                        )
                        # loop: re-bound + re-send this same round, tighter
            except Exception as e:
                # Reactive escalation safety net: if Kimi 400s on a context-limit
                # error (proactive estimate undershot, or one round jumped the
                # window), switch to Anthropic 1M and RETRY this round instead of
                # failing the run. Kimi-only + one-way, so it can't loop — once on
                # Anthropic, any repeat error falls through to the fail path below.
                if workflow_spec.provider == "kimi" and _is_context_limit_error(e):
                    logger.warning(
                        "Kimi hit context limit; escalating to Anthropic 1M and retrying round",
                        round=round_num + 1, run_id=str(run_id), error=str(e)[:160],
                    )
                    workflow_spec = _replace(
                        workflow_spec,
                        provider="anthropic",
                        model=WORKFLOW_FALLBACK_MODEL,
                        use_1m_context=True,
                    )
                    messages = _to_anthropic_safe(messages)
                    await _mark_run_escalated(run_id)  # sticky: resume starts on Anthropic
                    # Reactive reroute — Kimi actually failed on context this round.
                    track_llm_provider_fallback(
                        str(user_id), feature="workflow", from_provider="kimi",
                        reason="context", round_num=round_num + 1,
                    )
                    continue  # re-attempt this round on Anthropic (messages unchanged)
                # (Top-provider context-overflow recovery is handled in place by
                # the inner retry loop above, so it never reaches here — it either
                # succeeds after re-bounding or, once _MAX_CONTEXT_SHRINKS is
                # exhausted, re-raises and falls through to the fail path below.)
                # Transient provider failure (engine overload / 429 / 5xx) that
                # survived the wrapper's in-call retries: DON'T hard-fail and throw
                # away the completed rounds. The round was checkpointed at its top,
                # so mark the run `interrupted` and let the scheduler's reaper
                # resume it from that checkpoint next tick — riding out the blip and
                # recovering the work. The reaper bounds this via MAX_RESUME_ATTEMPTS
                # and gives up (→ failed) once the checkpoint goes stale, so it
                # can't loop. Engine-overload runs (686b05fa @ round 3, 4b929d06 @
                # round 12) used to discard 8–20 min of work this way.
                if is_transient_error(e) and await _mark_run_interrupted(run_id):
                    logger.warning(
                        "Workflow round hit a transient provider error after retries — "
                        "paused for reaper resume instead of failing",
                        round=round_num + 1, run_id=str(run_id), error=str(e)[:160],
                    )
                    track_error(
                        str(user_id), "workflow_transient_paused",
                        detail=f"round={round_num + 1}: {e}"[:300],
                    )
                    return
                logger.error("Workflow: Claude API error", error=str(e), round=round_num + 1)
                await _fail_run(session, run_id, f"Claude API error: {e}")
                track_error(str(user_id), "workflow_execution_failed", detail=f"round={round_num + 1}: {e}")
                _schedule_completion_dm(
                    user_id=user_id,
                    workflow_id=workflow_id,
                    run_id=run_id,
                    workflow_name=workflow.name or "Untitled workflow",
                    status="failed",
                    error=f"Claude API error (round {round_num + 1}): {e}",
                    alert_on=workflow.alert_on or "failure",
                )
                return

            # Log usage
            if response.usage:
                log_usage(user_id, workflow_spec.model, "workflow", response.usage)

            # Process response (neutral LLMResponse shape).
            tool_use_blocks = list(response.tool_calls)
            has_tool_use = bool(tool_use_blocks)
            round_text = response.text or ""
            round_text_chars = len(round_text)
            if round_text:
                all_text_parts.append(round_text)
            thinking_text = response.thinking or ""

            # Always log how the round resolved — without stop_reason + content
            # mix, silent failures (e.g. max_tokens consumed entirely by
            # thinking, leaving no text and no tool_use) are invisible.
            logger.info(
                "Workflow agent round resolved",
                round=round_num + 1,
                stop_reason=response.stop_reason,
                has_tool_use=has_tool_use,
                tool_use_blocks=len(tool_use_blocks),
                text_chars=round_text_chars,
                thinking_chars=len(thinking_text),
                workflow_id=str(workflow_id),
                run_id=str(run_id),
            )

            # Build the assistant message for next-round history.
            #
            # IMPORTANT: when running on Anthropic AND the response has raw
            # SDK blocks, we replay those VERBATIM. The neutral rebuild
            # (thinking/text/tool_use only) drops server-side blocks that
            # workflows depend on:
            #   - server_tool_use (advisor + code_execution invocations)
            #   - advisor_tool_result (advisor's reply, must be present on
            #     replay or Anthropic 400s on the next round)
            #   - code_execution_tool_result (sandbox output blocks, ditto)
            # On `pause_turn` specifically, the server already executed the
            # tool and the model is waiting for us to replay the conversation
            # so it can continue from the result. Losing these blocks meant
            # pause_turn rounds appeared to succeed but the next round saw
            # an inconsistent conversation. `_serialize_messages_for_replay`
            # handles both SDK objects and dicts for the persisted JSONB
            # column, so we don't lose fidelity in storage either.
            raw_content = getattr(response.raw, "content", None) if response.raw else None
            if raw_content:
                messages.append({"role": "assistant", "content": list(raw_content)})
            else:
                # Non-Anthropic or empty raw — fall back to neutral rebuild.
                assistant_blocks: list[dict] = []
                if thinking_text:
                    thinking_block: dict = {"type": "thinking", "thinking": thinking_text}
                    # Preserve OpenAI Responses reasoning id for round-trip.
                    if getattr(response, "reasoning_id", None):
                        thinking_block["_reasoning_id"] = response.reasoning_id
                    assistant_blocks.append(thinking_block)
                if round_text:
                    assistant_blocks.append({"type": "text", "text": round_text})
                for tc in tool_use_blocks:
                    assistant_blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.input,
                    })
                if assistant_blocks:
                    messages.append({"role": "assistant", "content": assistant_blocks})

            # pause_turn = server-side tool executed (web search / code execution),
            # continue the loop so Claude can process the results
            if response.stop_reason == "pause_turn":
                continue

            # max_tokens with no tool_use AND no text — the round was cut off
            # mid-response (typically: thinking budget consumed the entire
            # max_tokens window, leaving nothing emitted). Silently treating
            # this as "completed" produced a `bbc41178…` style run: status
            # completed, output 153 chars (just earlier rounds' narrative
            # text), no real report. Recover by nudging Claude to produce the
            # synthesis on the next round — `max_tokens` is per-response, not
            # cumulative, so the next round has a fresh budget.
            if (
                response.stop_reason == "max_tokens"
                and not has_tool_use
                and round_text_chars == 0
            ):
                logger.warning(
                    "Workflow round truncated by max_tokens with no output — nudging for synthesis",
                    round=round_num + 1,
                    workflow_id=str(workflow_id),
                    run_id=str(run_id),
                )
                messages.append({
                    "role": "user",
                    "content": (
                        "Your previous response was truncated before producing any output. "
                        "Please produce the final markdown summary of what you found now — "
                        "no further tool calls needed unless data is genuinely missing."
                    ),
                })
                continue

            # Done — model either explicitly ended its turn, or returned text
            # without tool_use (which we treat as terminal output).
            if not has_tool_use or response.stop_reason == "end_turn":
                logger.info(
                    "Workflow agent completed",
                    rounds=round_num + 1,
                    stop_reason=response.stop_reason,
                    output_chars=sum(len(t) for t in all_text_parts),
                    workflow_id=str(workflow_id),
                    run_id=str(run_id),
                )
                break

            # Split mark_artifact (internal, in-process) from real tool calls
            # (dispatched via tool_executor with their own DB sessions).
            artifact_blocks = [b for b in tool_use_blocks if b.name == "mark_artifact"]
            external_blocks = [b for b in tool_use_blocks if b.name != "mark_artifact"]

            # Capture artifacts directly. Synthesize a tool_result so Claude's
            # tool_use_id mapping stays intact for the next round.
            artifact_results: dict[str, dict] = {}
            for block in artifact_blocks:
                normalized = _validate_artifact(block.input or {})
                if normalized:
                    normalized["tool_call_id"] = block.id
                    captured_artifacts.append(normalized)
                    artifact_results[block.id] = {"ok": True, "kind": normalized["kind"]}
                else:
                    artifact_results[block.id] = {
                        "error": "Invalid artifact: provide an absolute https:// url, a known kind, and a title."
                    }

            # Per-run tool-call governance: refuse a tool once it's over its cap,
            # returning a nudge (toward a bulk/search tool or synthesis) instead of
            # executing — so an unbounded per-item fan-out finishes best-effort
            # rather than burning to the round ceiling. Diverse parallel tool use is
            # unaffected (cap is per tool NAME).
            to_execute = []
            capped_results: dict[str, dict] = {}
            for b in external_blocks:
                n = tool_call_counts.get(b.name, 0)
                if n >= _WORKFLOW_PER_TOOL_CALL_CAP:
                    if b.name not in capped_tools_logged:
                        capped_tools_logged.add(b.name)
                        logger.warning(
                            "Workflow tool-call budget reached — refusing further calls, "
                            "nudging model to bulk/search/synthesize",
                            tool=b.name, count=n, cap=_WORKFLOW_PER_TOOL_CALL_CAP,
                            run_id=str(run_id),
                        )
                        track_error(
                            str(user_id), "workflow_tool_budget_capped",
                            tool_name=b.name,
                            detail=f"{b.name} exceeded {_WORKFLOW_PER_TOOL_CALL_CAP} calls",
                        )
                    capped_results[b.id] = {
                        "error": (
                            f"Tool-call budget reached: '{b.name}' has been called "
                            f"{n} times in this run and will not be run again. Calling it "
                            "once per item exhausts the round budget before the task "
                            "finishes. Use a BULK tool instead — 'read_slack_threads' reads "
                            "many threads in one call, 'search_slack_messages' searches ALL "
                            "channels at once — or synthesize your result / take your final "
                            "action from the data you already have."
                        ),
                    }
                else:
                    tool_call_counts[b.name] = n + 1
                    to_execute.append(b)

            external_results_raw = await asyncio.gather(
                *[_execute_workflow_tool_parallel(b.name, b.input, user_id, auth_token) for b in to_execute]
            ) if to_execute else []
            external_results = dict(zip([b.id for b in to_execute], external_results_raw))
            external_results.update(capped_results)

            tool_results = []
            for block in tool_use_blocks:
                result = artifact_results.get(block.id) or external_results.get(block.id) or {}
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    # NOT per-result capped: workflows do COMPREHENSIVE reads (a
                    # full-day read_slack_channel, a 50-thread read_slack_threads)
                    # where every message matters — a blanket cut would drop
                    # panics/tickets and undercount the report. Request size is
                    # bounded instead by `_bound_request_context` each round, which
                    # only truncates when actually over the provider ceiling and
                    # preserves message count. A single pathological result is
                    # caught there too (its content field gets field-truncated).
                    "content": json.dumps(result),
                })

            messages.append({"role": "user", "content": tool_results})
        else:
            logger.warning(
                "Workflow agent exhausted max rounds",
                rounds=MAX_ROUNDS,
                workflow_id=str(workflow_id),
                run_id=str(run_id),
            )
            track_error(str(user_id), "workflow_execution_failed", detail=f"Exceeded {MAX_ROUNDS} rounds without completion")
            await _fail_run(
                session, run_id, f"Exceeded maximum {MAX_ROUNDS} rounds",
                partial_output="\n\n".join(all_text_parts) if all_text_parts else None,
            )
            _schedule_completion_dm(
                user_id=user_id,
                workflow_id=workflow_id,
                run_id=run_id,
                workflow_name=workflow.name or "Untitled workflow",
                status="failed",
                error=f"Exceeded maximum {MAX_ROUNDS} rounds without completion",
                alert_on=workflow.alert_on or "failure",
            )
            return

        # Build final output
        output = "\n\n".join(all_text_parts) if all_text_parts else "Workflow completed but produced no output."

        # Always store the model's final text as the canonical run report —
        # this is what `/workflows/{id}/runs/{run_id}` renders, and what the
        # Slack DM falls back to when the model didn't mark any artifact.
        report_markdown = output

        # Serialize the in-memory message list (Anthropic SDK objects + dicts)
        # to a pure-dict JSON-safe form so it can be persisted into the
        # encrypted `replay_messages` column. Used by the "Follow up in chat"
        # button on the run detail page to seed a Conversation with the
        # exact context Claude saw — thinking, tool_use, tool_result, advisor
        # blocks. Stored verbatim (no truncation): users will follow up to
        # iterate on what the model produced, and full fidelity matters.
        replay_messages = _serialize_messages_for_replay(messages)

        # Determine final status. Workflows are automatic by design -- there is
        # no manual approval gate, so every run completes autonomously.
        now = datetime.now(timezone.utc)
        final_status = "completed"
        completed_at = now

        await session.execute(
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id)
            .values(
                status=final_status,
                output=output,
                report_markdown=report_markdown,
                artifacts=captured_artifacts or None,
                replay_messages=replay_messages,
                completed_at=completed_at,
            )
        )
        await session.commit()

        # Learn the provider preference from escalation history: if this workflow
        # has been chronically overflowing Kimi, pin it to start on Anthropic 1M
        # next time. Runs AFTER the commit (fresh txn), best-effort, and only for
        # AUTO-mode workflows — never breaks the completed run.
        await _maybe_pin_provider(session, workflow_id)

        logger.info(
            "Workflow execution finished",
            workflow_id=str(workflow_id),
            run_id=str(run_id),
            status=final_status,
            artifacts=len(captured_artifacts),
        )

        # DM on completed runs (workflows always complete autonomously).
        if final_status == "completed":
            primary = _pick_primary_artifact(captured_artifacts)
            _schedule_completion_dm(
                user_id=user_id,
                workflow_id=workflow_id,
                run_id=run_id,
                workflow_name=workflow.name or "Untitled workflow",
                status="completed",
                primary_artifact=primary,
                alert_on=workflow.alert_on or "failure",
            )

    except Exception as e:
        # A transient DB connectivity blip (DNS EAI_AGAIN, connection reset,
        # server-closed) is infra, not a workflow defect — pause and let the
        # reaper resume from the last checkpoint (bounded by MAX_RESUME_ATTEMPTS),
        # same as a transient provider error in the round loop. Narrow gate
        # (connection-level OperationalError only) so a real query/logic error
        # still fails fast. `_mark_run_interrupted` opens its own session, so a
        # poisoned `session` here doesn't block it; it returns False if the DB is
        # still unreachable, in which case we fall through to the normal fail.
        if _is_transient_db_error(e) and await _mark_run_interrupted(run_id):
            logger.warning(
                "Workflow hit a transient DB connectivity error — paused for "
                "reaper resume instead of failing",
                workflow_id=str(workflow_id),
                run_id=str(run_id),
                error=str(e)[:160],
            )
            track_error(str(user_id), "workflow_transient_db_paused", detail=str(e)[:300])
            return
        logger.error(
            "Workflow execution failed",
            workflow_id=str(workflow_id),
            run_id=str(run_id),
            error=str(e),
        )
        track_error(str(user_id), "workflow_execution_failed", detail=str(e))
        await _fail_run(session, run_id, str(e))
        _schedule_completion_dm(
            user_id=user_id,
            workflow_id=workflow_id,
            run_id=run_id,
            workflow_name=(workflow.name if workflow else "Untitled workflow"),
            status="failed",
            error=str(e),
            alert_on=(workflow.alert_on if workflow else "failure"),
        )
    finally:
        _active_run_ids.discard(run_id)


# Matches the "<actual> tokens > <max>" pair inside a provider context-overflow
# message, e.g. Anthropic's "prompt is too long: 1144891 tokens > 1000000
# maximum". Tolerant of thousands separators.
_CONTEXT_OVERFLOW_RE = re.compile(r"(\d[\d,]*)\s*tokens?\s*>\s*(\d[\d,]*)")


def _parse_context_overflow(exc: BaseException) -> Optional[tuple[int, int]]:
    """Extract `(actual_tokens, max_tokens)` from a context-overflow error, or
    None when the message carries no such pair. Lets the reactive recovery derive
    the payload's true token density (real actual / our estimate) and re-bound
    under a ceiling scaled to it, instead of blindly guessing a smaller cap."""
    m = _CONTEXT_OVERFLOW_RE.search(str(exc))
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))
    except ValueError:
        return None


def _is_context_limit_error(exc: BaseException) -> bool:
    """True if an LLM error is a context/token-window overflow (vs auth, rate
    limit, etc.). Used to reactively escalate Kimi -> Anthropic 1M when our
    proactive size estimate undershot and Kimi 400s anyway."""
    msg = str(exc).lower()
    return any(
        sig in msg
        for sig in (
            "model token limit",      # Kimi: "exceeded model token limit: 262144"
            "262144",
            "context length",
            "context_length",
            "maximum context",
            "request exceeded",
            "prompt is too long",
            "too many tokens",
        )
    )


# Anthropic requires tool_use / tool_result ids to match this pattern. Kimi and
# OpenAI mint ids like `call_abc123` / `chatcmpl-tool-...` (underscores, dots,
# colons), which Anthropic 400s on ("String should match pattern '^[a-zA-Z0-9-]+$'").
_ANTHROPIC_TOOL_ID_RE = re.compile(r"^[a-zA-Z0-9-]+$")


def _anthropic_safe_tool_id(original: str, mapping: dict[str, str]) -> str:
    """Map an arbitrary provider tool-id to an Anthropic-pattern-safe id.

    Keyed on the ORIGINAL id so a tool_use and its paired tool_result always
    resolve to the same rewritten id regardless of which is seen first. Ids that
    already satisfy the pattern pass through unchanged (minimal churn); invalid
    ones have disallowed chars replaced with '-' and are disambiguated against
    ids already assigned, so two distinct originals can never collide.
    """
    if original in mapping:
        return mapping[original]
    if original and _ANTHROPIC_TOOL_ID_RE.match(original):
        mapping[original] = original
        return original
    base = re.sub(r"[^a-zA-Z0-9-]", "-", original or "").strip("-") or "tool"
    candidate = base
    used = set(mapping.values())
    i = 1
    while candidate in used:
        candidate = f"{base}-{i}"
        i += 1
    mapping[original] = candidate
    return candidate


def _to_anthropic_safe(messages: list[dict]) -> list[dict]:
    """Make a Kimi/OpenAI-built history replayable on Anthropic for a mid-run
    provider switch. Two transforms:

    1. Strips `thinking` blocks — Kimi's are unsigned and carry a non-standard
       `_reasoning_id`, which Anthropic 400s on. One-way: after the switch we only
       ever produce Anthropic blocks, so dropping the earlier reasoning is safe.
    2. Rewrites every `tool_use.id` and `tool_result.tool_use_id` to an
       Anthropic-pattern-safe id via a shared mapping, so Kimi/OpenAI-shaped ids
       (`call_...` etc.) don't get rejected. The mapping keeps each tool_use
       paired with its result.

    Text / tool_use / tool_result content is otherwise preserved. An assistant
    turn left empty after stripping gets a placeholder so content is never empty.
    """
    id_map: dict[str, str] = {}
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        new_blocks: list = []
        for b in content:
            if not isinstance(b, dict):
                new_blocks.append(b)
                continue
            btype = b.get("type")
            if btype == "thinking":
                continue
            if btype == "tool_use" and b.get("id"):
                new_blocks.append({**b, "id": _anthropic_safe_tool_id(b["id"], id_map)})
            elif btype == "tool_result" and b.get("tool_use_id"):
                new_blocks.append({**b, "tool_use_id": _anthropic_safe_tool_id(b["tool_use_id"], id_map)})
            else:
                new_blocks.append(b)
        if role == "assistant" and not new_blocks:
            new_blocks = [{"type": "text", "text": "(continuing)"}]
        out.append({"role": role, "content": new_blocks})
    # Server-side code-exec blocks (bash_code_execution + its result) can be split
    # by a pause_turn / mid-run escalation; drop any unpaired ones so the switch to
    # Anthropic doesn't 400 on an orphan server_tool_use.
    return _strip_orphan_server_tools(out)


async def _mark_run_escalated(run_id: UUID) -> None:
    """Persist that this run escalated Kimi -> Anthropic 1M, so a resume starts
    on Anthropic directly instead of re-overflowing Kimi. Fresh isolated session,
    best-effort (a missed write just means resume re-derives the switch in-loop)."""
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as s:
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(escalated_to_anthropic=True)
            )
    except Exception as e:
        logger.warning("Failed to persist workflow escalation flag", run_id=str(run_id), error=str(e)[:160])


def _is_transient_db_error(exc: BaseException) -> bool:
    """True for a transient DB CONNECTIVITY failure (DNS blip, connection
    refused/reset, server-closed) — safe to pause-and-resume via the reaper
    rather than hard-fail, exactly like a transient provider error. Deliberately
    narrow: gated on a SQLAlchemy ``OperationalError`` AND a connection-level
    message, so a query / constraint / logic error (which would just loop until
    MAX_RESUME_ATTEMPTS) still fails fast. Root case: run 34e390fc failed on
    `failed to resolve host 'postgres-b2c-rw': [Errno -3] Try again` (EAI_AGAIN —
    a passing DNS blip)."""
    if not isinstance(exc, OperationalError):
        return False
    s = str(exc).lower()
    markers = (
        "failed to resolve host",
        "could not translate host name",
        "[errno -3]",  # EAI_AGAIN "Try again"
        "could not connect to server",
        "connection refused",
        "connection reset",
        "server closed the connection",
        "could not receive data from server",
        "terminating connection",
    )
    return any(m in s for m in markers)


async def _mark_run_interrupted(run_id: UUID) -> bool:
    """Mark a `running` run `interrupted` so the scheduler's reaper resumes it from
    its last checkpoint next tick. Used when the LLM round fails on a TRANSIENT
    provider error (engine overload / 429 / 5xx) after in-call retries are spent:
    a capacity blip shouldn't hard-fail a workflow that's minutes deep with a
    valid checkpoint — pausing recovers the completed rounds and retries when the
    provider recovers. The reaper bounds total resumes via MAX_RESUME_ATTEMPTS and
    drops to `failed` once the checkpoint goes stale (RESUME_MAX_AGE_SECONDS) or
    attempts are exhausted, so this can't loop forever. Returns True if it flipped
    a still-`running` row (guarded so a completed/failed run isn't revived)."""
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as s:
            res = await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.status == "running")
                .values(status="interrupted", last_progress_at=datetime.now(timezone.utc))
            )
        return (res.rowcount or 0) == 1
    except Exception as e:
        logger.warning("Failed to mark run interrupted", run_id=str(run_id), error=str(e)[:160])
        return False


async def _checkpoint_run(run_id: UUID, messages: list[dict], next_round: int) -> None:
    """Persist a per-round resume checkpoint + heartbeat for `run_id`.

    Writes the current message state into `replay_messages`, the next round to
    execute into `last_round`, and bumps `last_progress_at` (the heartbeat the
    reaper uses to tell a live run from an abandoned one).

    Uses a FRESH short-lived session so a checkpoint failure can never poison the
    long-lived loop session, and is fully best-effort: if the write fails we log
    and continue. The only cost of a missed checkpoint is that a crash in the
    very next round resumes from one round earlier — never a lost run.

    Checkpoint is taken at the TOP of a round, where `messages` is always in a
    valid "assistant's turn next" state (initial prompt, or a prior round's
    tool_result/user message) — so a rehydrate never lands on a dangling
    tool_use. `next_round` is therefore the round we are about to run.
    """
    try:
        serialized = _serialize_messages_for_replay(messages)
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as s:
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(
                    replay_messages=serialized,
                    last_round=next_round,
                    last_progress_at=datetime.now(timezone.utc),
                )
            )
    except Exception as e:
        logger.warning(
            "Workflow checkpoint write failed (run continues; resumability falls back one round)",
            run_id=str(run_id),
            next_round=next_round,
            error=str(e)[:160],
        )


async def _maybe_pin_provider(session: AsyncSession, workflow_id: UUID) -> None:
    """Auto-pin a workflow to Anthropic when it CHRONICALLY escalates from Kimi.

    A workflow whose big context overflows Kimi every run pays a wasted Kimi
    attempt before the reactive Kimi->Anthropic switch every single time. Once
    >= _PIN_THRESHOLD of the last _PIN_WINDOW SCHEDULED runs escalated, pin it so
    future runs start on Anthropic 1M directly (honored by the run-start block in
    _execute_workflow_loop).

    Only touches workflows in AUTO mode: the pin is written with an ATOMIC
    conditional `UPDATE ... WHERE preferred_provider IS NULL`, so a manual choice
    ('anthropic' or 'kimi', set in the editor) is always respected — even one made
    mid-run — and there is no read-modify-write race. (Reading then writing a
    Workflow instance would be unsafe here: the session runs with
    expire_on_commit=False, so a `session.get` after the completion commit could
    return the stale run-start instance with a now-outdated preferred_provider.)
    Best-effort — any failure is logged and swallowed so it can never break a run
    that already finished."""
    try:
        rows = (
            await session.execute(
                select(WorkflowRun.escalated_to_anthropic)
                .where(
                    WorkflowRun.workflow_id == workflow_id,
                    WorkflowRun.source == "scheduled",
                )
                .order_by(WorkflowRun.triggered_at.desc())
                .limit(_PIN_WINDOW)
            )
        ).scalars().all()
        escalated = sum(1 for r in rows if r)
        if len(rows) >= _PIN_WINDOW and escalated >= _PIN_THRESHOLD:
            # Conditional on IS NULL → only pins a workflow still in AUTO mode;
            # a manual pin/opt-out set anytime (including during this run) wins.
            result = await session.execute(
                update(Workflow)
                .where(
                    Workflow.id == workflow_id,
                    Workflow.preferred_provider.is_(None),
                )
                .values(preferred_provider="anthropic")
            )
            await session.commit()
            if result.rowcount:
                logger.info(
                    "Auto-pinned workflow to Anthropic 1M (chronic Kimi escalation)",
                    workflow_id=str(workflow_id),
                    escalated=escalated,
                    window=_PIN_WINDOW,
                )
    except Exception as e:
        logger.warning(
            "Provider auto-pin check failed (ignored)",
            workflow_id=str(workflow_id),
            error=str(e)[:160],
        )


async def _fail_run(
    session: AsyncSession,
    run_id: UUID,
    error: str,
    *,
    partial_output: Optional[str] = None,
) -> None:
    """Mark a workflow run as failed, using a FRESH short-lived session.

    By the time a run fails, the long-lived workflow `session` is often already
    poisoned — e.g. its connection was killed by Postgres's idle-in-transaction
    timeout during a slow LLM round, leaving it in PendingRollbackError. Writing
    the failure on that session would itself raise, so the run never records as
    failed and shows up as "stuck / never ran" in the UI. We roll back the
    poisoned session (best-effort, to return its connection to the pool cleanly)
    and write the status via a brand-new session so the failure always lands.

    `partial_output` (optional): narrative the model produced before the run was
    cut short (wall-clock / round cap). Persisted to `output` + `report_markdown`
    so the run detail page + "Follow up in chat" show the salvaged work instead
    of an empty failure. Status stays `failed` — the owner is still alerted that
    it didn't finish; we only rescue the work, not hide the failure.
    """
    try:
        await session.rollback()
    except Exception as rollback_err:
        # The poisoned session may be unrecoverable; the fresh session below
        # still records the failure, so this is non-fatal — but log it so the
        # underlying connection issue isn't invisible.
        logger.warning(
            "Failed to roll back poisoned workflow session before marking failure",
            run_id=str(run_id),
            error=str(rollback_err),
        )
    try:
        salvage = (
            {"output": partial_output, "report_markdown": partial_output}
            if partial_output
            else {}
        )
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as s:
            await s.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id)
                .values(
                    status="failed",
                    error=error,
                    completed_at=datetime.now(timezone.utc),
                    **salvage,
                )
            )
    except Exception as e:
        logger.error(
            "Failed to record workflow run failure",
            run_id=str(run_id),
            error=str(e),
        )
    # Notify the admin of ANY workflow failure (fire-and-forget, as Gideon).
    # Single chokepoint — every terminal failure routes through _fail_run.
    alert_workflow_failed(run_id, error)
