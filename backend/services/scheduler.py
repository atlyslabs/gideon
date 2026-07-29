"""
Background schedulers for Gideon service.

Loops started in the FastAPI lifespan:
1. Data scheduler   — every 10 min, runs activity ingestion + suggestions (parallel, bounded)
2. Workflow scheduler — every 60s, executes due scheduled workflows
3. Daily usage scheduler — aggregates per-user token usage once a day
"""

import asyncio
import time
from datetime import date, datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from config import IS_PRODUCTION
from db.database import session_manager
from db.models import Integration, User, Workflow, WorkflowRun
from db.redis import get_redis
from services.activity_ingestion_service import ingest_user_activities
from services.delegation_service import run_due_reminders
from services.grafana_rbac_sync_service import (
    check_grafana_token_health,
    detect_and_handle_drift,
    sync_grafana_rbac_if_due,
)
from services.suggestion_service import generate_suggestions
from services.workflow_execution_service import (
    MAX_RESUME_ATTEMPTS,
    execute_workflow,
    get_active_run_ids,
    resume_workflow_run,
)
from services.meeting_prep_service import auto_generate_upcoming_preps
from services.email_auto_draft_service import auto_generate_email_drafts
from services.events_service import send_daily_usage_batch
from services.workflow_run_policy import STUCK_HEARTBEAT_SECONDS

FALLBACK_LOOKBACK_HOURS = 3  # suggestion/email-draft fallback when last_scheduler_run is null

# Stagger window: spread N due workflows uniformly across this many seconds.
# Combined with `_is_workflow_due`'s -2/+1 minute match window, the effective
# launch window is roughly [-2 min, +2 min] around each workflow's scheduled
# time — "acceptable" per product. Per-workflow gap is `STAGGER_WINDOW_S / N`,
# so small bursts get generous spacing (N=9 → 26s gap) and large bursts get
# compressed (N=60 → 4s gap). Combined with the retry layer in
# workflow_execution_service, this keeps us well inside the Anthropic org TPM
# cap (2M input tok/min on Sonnet 4.6, shared across all Gideon users).
STAGGER_WINDOW_S = 240.0

logger = structlog.get_logger()

# Strong-ref set for fire-and-forget workflow tasks. asyncio only holds weak
# refs to tasks, so without this they'd be eligible for GC mid-execution
# (ASYNC300). The done-callback discards entries so the set stays bounded.
_running_workflow_tasks: set[asyncio.Task] = set()

# In-flight workflow IDs, keyed on workflow_id (not task), to prevent the
# scheduler from re-queueing the same workflow before its first run is
# persisted. The ±1-min `_is_workflow_due` window can match three consecutive
# 60s ticks (08:59 / 09:00 / 09:01), and `_was_run_today` only flips True
# AFTER `execute_workflow` flushes the new WorkflowRun row — so without
# de-dup, a slow workflow can be queued 2-3 times for the same day.
_inflight_workflow_ids: set[UUID] = set()

# --- Crash-resilience: reap abandoned runs + resume interrupted ones ---------
# A run heartbeats (`last_progress_at`) at the top of every round. If a run is
# `running` but its heartbeat is older than this, the owning process is gone
# (pod crash / OOM / SIGKILL) — no live execution is updating it. The threshold
# sits comfortably above the longest plausible single round (the 900s LLM
# timeout + tool execution) so a slow-but-live round is never falsely reaped,
# and well below the 60-min wall-clock cap so detection is still timely. This is
# also why reaping keys on the heartbeat, not a blanket "all running" sweep:
# during a rolling deploy two pods overlap, and a live run on either pod keeps
# heartbeating, so it's never clobbered.
# The value itself (1800s / 30 min) lives in services/workflow_run_policy.py —
# imported at the top of this file — so routers/workflows_router.py can share
# it for the manual-run concurrent-run guard without a circular import.

# Don't resume a checkpoint older than this — the data it fetched is stale, so a
# fresh scheduled run is better than replaying hours-old context.
RESUME_MAX_AGE_SECONDS = 7200  # 2 h

# Day-of-week mapping: schedule_day_of_week column values -> Python weekday()
_DOW_MAP = {
    "MO": 0,
    "TU": 1,
    "WE": 2,
    "TH": 3,
    "FR": 4,
    "SA": 5,
    "SU": 6,
}

# Track last suggestion run hour per user to run suggestions ~hourly
_last_suggestion_hour: dict[UUID, int] = {}

# Parallel processing constants
SCHEDULER_CONCURRENCY = 20
SCHEDULER_LOCK_KEY = "gideon:scheduler:data_lock"
SCHEDULER_LOCK_TTL = 900  # 15 min safety valve
USER_TIMEOUT = 120  # max seconds per user before giving up

# Hard cap on how far back a single cycle will try to catch up. Without
# this, one timed-out cycle freezes `last_scheduler_run`; every subsequent
# cycle then attempts a larger window (1h → 1d → 1w → ...) and times out
# again, locking the user out permanently. The cap means each cycle does
# bounded work; some historical activity is dropped but the user resumes
# getting new data on the very next cycle.
#
# Intentionally smaller than activity_ingestion_service.DEFAULT_LOOKBACK_HOURS
# (currently 24h, used for FIRST-RUN new users). Recovery has a hard
# USER_TIMEOUT=120s constraint per user, and ingestion is sequential
# across providers (Gmail → Calendar → Slack → Linear → GitHub) — heavy
# users timed out even at 24h because a single cycle had to
# pull a full day of every provider in ≤120s. 6h fits comfortably; a
# previously-stuck user catches up to "now" in ~4 cycles (~40 min) at
# the 10-min scheduler cadence.
MAX_INGESTION_LOOKBACK_HOURS = 6


# ---------------------------------------------------------------------------
# Data Scheduler (Activity Ingestion + Suggestions — parallel)
# ---------------------------------------------------------------------------

async def _process_single_user(
    uid: UUID,
    user_tz: str | None,
    feature_action_items: bool,
    last_scheduler_run: datetime | None,
    semaphore: asyncio.Semaphore,
) -> None:
    """Process one user's activity ingestion + suggestions under semaphore."""
    async with semaphore:
        user_start = time.monotonic()
        cycle_start_ts = datetime.now(timezone.utc)
        ingestion_ok = False
        # When ingestion is clamped (recovering from a long stall), we stamp
        # `last_scheduler_run` forward by only one window, not to `now`. Walking
        # forward one window per cycle lets historical catch-up actually happen
        # — stamping `cycle_start_ts` after a clamp would advance the cursor
        # past the unfetched gap and silently drop it forever.
        stamp_ts = cycle_start_ts

        # 'since' for suggestions/email-drafts (3h fallback for first run)
        # Activity ingestion gets None on first run so it uses its own 24h fallback
        if last_scheduler_run:
            suggestion_since = last_scheduler_run - timedelta(minutes=2)  # 2 min overlap buffer
            ingestion_since = last_scheduler_run - timedelta(minutes=2)
            # Death-spiral guard: clamp INGESTION lookback only — see the
            # constant's comment for the failure mode this prevents. We do
            # NOT clamp `suggestion_since`: suggestion generation only
            # reads activity_log (a cheap indexed SELECT, no API timeout
            # risk), and clamping would silently skip older pending
            # messages once `last_scheduler_run` advances past them.
            lookback_floor = cycle_start_ts - timedelta(hours=MAX_INGESTION_LOOKBACK_HOURS)
            if ingestion_since < lookback_floor:
                ingestion_since = lookback_floor
                stamp_ts = min(
                    cycle_start_ts,
                    last_scheduler_run + timedelta(hours=MAX_INGESTION_LOOKBACK_HOURS),
                )
        else:
            suggestion_since = cycle_start_ts - timedelta(hours=FALLBACK_LOOKBACK_HOURS)
            ingestion_since = None  # let ingestion use its own DEFAULT_LOOKBACK_HOURS (24h)

        # --- Activity Ingestion (prod only, manages own sessions internally) ---
        if IS_PRODUCTION:
            try:
                count = await ingest_user_activities(uid, since=ingestion_since)
                if count > 0:
                    logger.info("Activity ingestion complete", user_id=str(uid), new_activities=count)
                ingestion_ok = True
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("Activity ingestion failed for user", user_id=str(uid), error=str(e))
        else:
            ingestion_ok = True  # non-prod: skip ingestion but allow stamping

        # --- Suggestions (hourly, 8AM-9PM, prod only, if feature enabled) ---
        if not IS_PRODUCTION or not feature_action_items:
            if ingestion_ok:
                await _stamp_last_run(uid, stamp_ts)
            return

        try:
            tz = ZoneInfo(user_tz) if user_tz else ZoneInfo("UTC")
        except Exception:
            tz = ZoneInfo("UTC")
        user_hour = datetime.now(tz).hour

        if not (8 <= user_hour <= 21):
            if ingestion_ok:
                await _stamp_last_run(uid, stamp_ts)
            return

        # Only run once per hour per user
        last_hour = _last_suggestion_hour.get(uid)
        if last_hour == user_hour:
            if ingestion_ok:
                await _stamp_last_run(uid, stamp_ts)
            return

        try:
            # generate_suggestions manages its own short-lived sessions so no
            # pool connection is held across its LLM call (up to 150s).
            await generate_suggestions(uid, since=suggestion_since)
            _last_suggestion_hour[uid] = user_hour
            logger.info("Suggestions generated", user_id=str(uid))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Suggestion generation failed for user", user_id=str(uid), error=str(e))

        if ingestion_ok:
            await _stamp_last_run(uid, stamp_ts)

        logger.debug(
            "User processing complete",
            user_id=str(uid),
            duration_sec=round(time.monotonic() - user_start, 1),
        )


async def _stamp_last_run(uid: UUID, ts: datetime) -> None:
    """Update user's last_scheduler_run timestamp."""
    try:
        async with session_manager.get_session() as session:
            await session.execute(
                update(User).where(User.id == uid).values(last_scheduler_run=ts)
            )
    except Exception as e:
        logger.debug("Failed to stamp last_scheduler_run", user_id=str(uid), error=str(e))


async def _release_lock(redis_client, lock_key: str, lock_value: str) -> None:
    """Release Redis lock only if we still own it (atomic compare-and-delete)."""
    lua = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
    try:
        await redis_client.eval(lua, 1, lock_key, lock_value)
    except Exception as e:
        logger.warning("Failed to release scheduler lock", error=str(e))


async def data_scheduler_loop() -> None:
    """Run every 10 minutes. For each user with active integrations:
    - Always: ingest activities (Gmail, Slack, Calendar, Linear, GitHub)
    - Hourly (8AM-9PM in user's timezone): generate AI suggestions
    Users are processed in parallel, bounded by SCHEDULER_CONCURRENCY.
    """
    logger.info("Data scheduler started")

    while True:
        lock_value = str(uuid4())
        redis_client = get_redis()

        try:
            # --- Overlap guard: acquire Redis lock with unique value ---
            acquired = await redis_client.set(
                SCHEDULER_LOCK_KEY, lock_value, nx=True, ex=SCHEDULER_LOCK_TTL
            )
            if not acquired:
                logger.warning("Data scheduler: previous cycle still running, skipping")
                await asyncio.sleep(600)
                continue

            cycle_start = time.monotonic()

            # Collect all users with at least one active integration + their timezone/feature flags
            users: list[tuple[UUID, str | None, bool, datetime | None]] = []
            async with session_manager.get_session() as session:
                result = await session.execute(
                    select(User.id, User.timezone, User.feature_action_items, User.last_scheduler_run).where(
                        User.id.in_(
                            select(Integration.user_id).where(Integration.is_active == True).distinct()
                        )
                    )
                )
                users = [(row[0], row[1], row[2], row[3]) for row in result.all()]

            logger.info("Data scheduler: eligible users", count=len(users))

            # --- Parallel user processing with per-user timeout ---
            semaphore = asyncio.Semaphore(SCHEDULER_CONCURRENCY)
            tasks = [
                asyncio.wait_for(
                    _process_single_user(uid, tz, fai, last_run, semaphore),
                    timeout=USER_TIMEOUT,
                )
                for uid, tz, fai, last_run in users
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Log any unexpected errors (not caught by _process_single_user)
            for i, result in enumerate(results):
                if isinstance(result, asyncio.TimeoutError):
                    logger.warning("User processing timed out", user_id=str(users[i][0]))
                elif isinstance(result, Exception):
                    logger.error(
                        "Unexpected user processing error",
                        user_id=str(users[i][0]),
                        error=str(result),
                    )

            # --- Delegation reminders (every cycle, all envs) ---
            try:
                sent = await run_due_reminders()
                if sent:
                    logger.info("Delegation reminders sent", count=sent)
            except Exception as e:
                logger.error("Delegation reminder pass failed", error=str(e))

            # --- Grafana RBAC: daily full sync + per-cycle drift detector ---
            # Roles + team memberships rarely change, so the full
            # fan-out (org_users + teams + N team-members) only runs once a
            # day. Every 10 min we do a cheap O(1) hash check on /api/org/users
            # and trigger an immediate resync ONLY if anything actually changed.
            try:
                synced = await sync_grafana_rbac_if_due()
                if synced:
                    logger.info("Grafana RBAC daily sync ok", users=synced)
                else:
                    drifted = await detect_and_handle_drift()
                    if drifted:
                        logger.info("Grafana RBAC drift sync ok")
            except Exception as e:
                logger.error(
                    "Grafana RBAC sync pass failed",
                    error_type=type(e).__name__, error=repr(e),
                )

            # --- Grafana token health (idempotent within a UTC day) ---
            try:
                await check_grafana_token_health()
            except Exception as e:
                logger.error(
                    "Grafana token healthcheck failed",
                    error_type=type(e).__name__, error=repr(e),
                )

            # --- Meeting Prep + Email Auto-Drafts (prod only) ---
            if IS_PRODUCTION:
                try:
                    await auto_generate_upcoming_preps()
                except Exception as e:
                    logger.error("Meeting prep generation failed", error=str(e))

                # Pass pre-stamp cursors so email drafts use the original last_scheduler_run
                pre_stamp_cursors = {uid: last_run for uid, _tz, _fai, last_run in users}
                try:
                    await auto_generate_email_drafts(pre_stamp_cursors=pre_stamp_cursors)
                except Exception as e:
                    logger.error("Email auto-draft generation failed", error=str(e))

            cycle_duration = time.monotonic() - cycle_start
            logger.info(
                "Data scheduler cycle complete",
                duration_sec=round(cycle_duration, 1),
                users=len(users),
            )

        except Exception as e:
            logger.error("Data scheduler loop error", error=str(e))
        finally:
            await _release_lock(redis_client, SCHEDULER_LOCK_KEY, lock_value)

        await asyncio.sleep(600)  # 10 minutes


# ---------------------------------------------------------------------------
# Workflow Scheduler helpers
# ---------------------------------------------------------------------------

def _is_workflow_due(workflow: Workflow, now_utc: datetime, user_tz: str | None) -> bool:
    """Return True if *workflow* should execute at the current moment.

    schedule_time, schedule_start_date, and schedule_day_of_week are interpreted
    in the user's timezone (``user_tz``), falling back to UTC if unset.
    """
    try:
        tz = ZoneInfo(user_tz) if user_tz else ZoneInfo("UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    now_local = now_utc.astimezone(tz)
    today = now_local.date()
    interval: int = workflow.schedule_interval or 1
    unit: str = workflow.schedule_unit or "day"

    # --- start-date gating (user-local), shared by all units ---
    start_date: date | None = workflow.schedule_start_date
    if start_date and today < start_date:
        return False

    # Default start_date to workflow creation date (user-local) so interval math works
    # (using today would make days_since_start=0 every day, breaking bi-weekly etc.)
    if start_date is None:
        if workflow.created_at:
            created_local = workflow.created_at
            if created_local.tzinfo is None:
                created_local = created_local.replace(tzinfo=timezone.utc)
            start_date = created_local.astimezone(tz).date()
        else:
            start_date = today

    # --- multi-weekday filter (overlay, applies to ANY unit) ---
    # When `schedule_days_of_week` is set + non-empty, today's weekday must be
    # in it. Lets users say "every 30 minutes, but only Mon-Fri" without
    # splitting into separate workflows. Applied before the unit branch so it
    # gates interval (minute/hour) schedules too.
    days_filter = workflow.schedule_days_of_week
    if days_filter:
        allowed = {_DOW_MAP.get(d) for d in days_filter if isinstance(d, str)}
        allowed.discard(None)
        if allowed and today.weekday() not in allowed:
            return False

    # --- sub-daily interval units (every N minutes / hours) ---
    # These have no time-of-day anchor: the cadence is enforced by the
    # elapsed-since-last-run check in the scheduler loop (via the batched
    # `_last_scheduled_runs`), not by an HH:MM match window. Here we only confirm the
    # shared start-date + weekday gating above passed; if it did, the workflow
    # is eligible to run this tick (subject to the elapsed check).
    if unit in ("minute", "hour"):
        return True

    # --- time-of-day window (day / week units only) ---
    schedule_time_str: str = workflow.schedule_time or "00:30"
    try:
        sched_hour, sched_minute = map(int, schedule_time_str.split(":"))
    except (ValueError, AttributeError):
        return False
    if not (0 <= sched_hour <= 23 and 0 <= sched_minute <= 59):
        return False

    # Match window: up to 2 min BEFORE the scheduled time (so we can start
    # workflows early and stagger them across the [-2, +2] minute window to
    # avoid bursting the Anthropic org TPM cap), and up to 1 min AFTER (for
    # scheduler drift). Asymmetric on purpose. Wrap around midnight so 23:59
    # and 00:00 are 1 minute apart. _was_run_today() prevents double execution.
    now_total_min = now_local.hour * 60 + now_local.minute
    sched_total_min = sched_hour * 60 + sched_minute
    # Signed minutes-until-scheduled, midnight-wrapped to the shortest path.
    raw = sched_total_min - now_total_min
    if raw > 720:
        raw -= 1440
    elif raw < -720:
        raw += 1440
    # raw > 0 means scheduled is in the future, raw < 0 means past.
    if raw > 2 or raw < -1:
        return False

    days_since_start = (today - start_date).days

    if unit == "day":
        if days_since_start % interval != 0:
            return False

    elif unit == "week":
        dow = workflow.schedule_day_of_week  # e.g. "MO"
        expected_weekday = _DOW_MAP.get(dow)
        if expected_weekday is None:
            return False
        if today.weekday() != expected_weekday:
            return False
        weeks_since_start = days_since_start // 7
        if weeks_since_start % interval != 0:
            return False

    return True


def _interval_seconds(workflow: Workflow) -> int | None:
    """Cadence in seconds for sub-daily (minute/hour) workflows, else None."""
    unit = workflow.schedule_unit or "day"
    interval = max(1, workflow.schedule_interval or 1)
    if unit == "minute":
        return interval * 60
    if unit == "hour":
        return interval * 3600
    return None


async def _last_scheduled_runs(
    workflow_ids: list[UUID],
    session: AsyncSession,
) -> dict[UUID, datetime]:
    """Most-recent *scheduled* run start (`triggered_at`) per workflow, for the
    given ids, in ONE grouped query.

    Used to dedup sub-daily (minute/hour) interval workflows: a run still in
    progress has a recent row, so the cadence check waits and overlap is
    prevented. Manual runs (source != 'scheduled') are ignored, mirroring
    `_was_run_today`, so a Run Now test doesn't reset the interval clock.

    Batched on purpose — one query for all interval candidates per tick instead
    of a COUNT per workflow, so the scheduler stays flat as interval workflows
    grow.
    """
    if not workflow_ids:
        return {}
    result = await session.execute(
        select(
            WorkflowRun.workflow_id,
            func.max(WorkflowRun.triggered_at),
        )
        .where(
            WorkflowRun.workflow_id.in_(workflow_ids),
            WorkflowRun.source == "scheduled",
        )
        .group_by(WorkflowRun.workflow_id)
    )
    return {wf_id: last for wf_id, last in result.all() if last is not None}


async def _was_run_today(
    workflow_id: UUID,
    session: AsyncSession,
    now_utc: datetime,
    user_tz: str | None,
) -> bool:
    """Return True if the workflow already ran *on schedule* today in the
    user's local day. Manual runs (source != 'scheduled') are intentionally
    NOT counted — testing a workflow with Run Now shouldn't consume the
    day's automatic run.
    """
    try:
        tz = ZoneInfo(user_tz) if user_tz else ZoneInfo("UTC")
    except Exception:
        tz = ZoneInfo("UTC")
    today_local = now_utc.astimezone(tz).date()
    start_local = datetime.combine(today_local, datetime.min.time(), tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)

    result = await session.execute(
        select(func.count(WorkflowRun.id)).where(
            WorkflowRun.workflow_id == workflow_id,
            WorkflowRun.triggered_at >= start_utc,
            WorkflowRun.triggered_at < end_utc,
            WorkflowRun.source == "scheduled",
        )
    )
    return (result.scalar() or 0) > 0


# ---------------------------------------------------------------------------
# Workflow Scheduler
# ---------------------------------------------------------------------------

def _has_fresh_checkpoint(run: WorkflowRun, now: datetime) -> bool:
    """True if `run` carries a resumable checkpoint that isn't too stale."""
    if not run.replay_messages or run.last_round is None:
        return False
    hb = run.last_progress_at or run.triggered_at
    if hb is None:
        return False
    return (now - hb).total_seconds() <= RESUME_MAX_AGE_SECONDS


async def _claim_interrupted_run(run_id: UUID) -> bool:
    """Atomically claim an interrupted run for resume: interrupted -> running,
    resume_count += 1. The status guard means only one claimer wins, so a run is
    never resumed twice concurrently. Returns True iff this caller claimed it."""
    try:
        async with session_manager.get_session() as session:
            res = await session.execute(
                update(WorkflowRun)
                .where(WorkflowRun.id == run_id, WorkflowRun.status == "interrupted")
                .values(
                    status="running",
                    resume_count=WorkflowRun.resume_count + 1,
                    last_progress_at=datetime.now(timezone.utc),
                )
            )
        return (res.rowcount or 0) == 1
    except Exception as e:
        logger.error("Failed to claim interrupted run", run_id=str(run_id), error=str(e)[:160])
        return False


async def _resume_run_task(run_id: UUID, workflow_id: UUID, user_id: UUID) -> None:
    """Run a claimed resume in its own session. Errors are logged, never raised."""
    try:
        async with session_manager.get_session() as session:
            await resume_workflow_run(run_id, user_id, session)
        logger.info("Resumed workflow run finished", run_id=str(run_id), workflow_id=str(workflow_id))
    except Exception as e:
        logger.error("Resume task failed", run_id=str(run_id), error=str(e))


async def _reap_resume_workflows(now: datetime) -> set[UUID]:
    """Recover runs left non-terminal by a dead pod, and resume eligible ones.

    - `running` + stale heartbeat -> abandoned: `interrupted` if it has a fresh
      checkpoint and resume budget, else `failed`.
    - `interrupted` + fresh checkpoint + budget -> claimed and resumed; otherwise
      `failed`.

    Returns the set of workflow_ids with a live/queued run so the scheduler skips
    starting a duplicate scheduled run for them this tick (overlap guard).
    """
    busy: set[UUID] = set()
    to_resume: list[tuple[UUID, UUID, UUID]] = []  # (run_id, workflow_id, user_id)
    active = get_active_run_ids()
    try:
        async with session_manager.get_session() as session:
            rows = (
                await session.execute(
                    select(WorkflowRun).where(
                        WorkflowRun.status.in_(["running", "interrupted"])
                    )
                )
            ).scalars().all()
            for r in rows:
                # This pod is actively executing it — definitely live, never reap.
                if r.id in active or r.workflow_id in _inflight_workflow_ids:
                    busy.add(r.workflow_id)
                    continue

                if r.status == "running":
                    hb = r.last_progress_at or r.triggered_at
                    if hb is not None and (now - hb).total_seconds() < STUCK_HEARTBEAT_SECONDS:
                        busy.add(r.workflow_id)  # heartbeating elsewhere — alive
                        continue
                    # Stale -> the owning process is gone.
                    if _has_fresh_checkpoint(r, now) and (r.resume_count or 0) < MAX_RESUME_ATTEMPTS:
                        r.status = "interrupted"
                        busy.add(r.workflow_id)
                        logger.warning("Reaper: abandoned running run -> interrupted", run_id=str(r.id))
                    else:
                        r.status = "failed"
                        r.error = "Run abandoned (no live process — likely a pod crash) and not resumable."
                        r.completed_at = now
                        logger.warning("Reaper: abandoned running run -> failed", run_id=str(r.id))
                    continue

                # status == "interrupted"
                if _has_fresh_checkpoint(r, now) and (r.resume_count or 0) < MAX_RESUME_ATTEMPTS:
                    busy.add(r.workflow_id)
                    to_resume.append((r.id, r.workflow_id, r.user_id))
                else:
                    r.status = "failed"
                    r.error = "Interrupted run not resumable (stale checkpoint or resume attempts exhausted)."
                    r.completed_at = now
                    logger.warning("Reaper: interrupted run -> failed (not resumable)", run_id=str(r.id))
            await session.commit()
    except Exception as e:
        logger.error("Reaper/resumer scan failed", error=str(e)[:160])
        return busy

    # Claim + launch resumes outside the scan session. Each claim is atomic, and
    # we reuse the same inflight guard scheduled runs use so nothing double-runs.
    for run_id, workflow_id, user_id in to_resume:
        if workflow_id in _inflight_workflow_ids:
            continue
        if not await _claim_interrupted_run(run_id):
            continue
        _inflight_workflow_ids.add(workflow_id)
        task = asyncio.create_task(_resume_run_task(run_id, workflow_id, user_id))
        _running_workflow_tasks.add(task)
        task.add_done_callback(_running_workflow_tasks.discard)
        task.add_done_callback(lambda _t, w=workflow_id: _inflight_workflow_ids.discard(w))
    return busy


async def workflow_scheduler_loop() -> None:
    """Run every 60 seconds. Find active scheduled workflows that are due
    and execute them (one session per workflow)."""
    logger.info("Workflow scheduler started")

    while True:
        try:
            now = datetime.now(timezone.utc)

            # Recover runs left non-terminal by a dead pod (reap stale `running`,
            # resume eligible `interrupted`). Returns workflow_ids with a live or
            # queued run so we don't start a duplicate scheduled run below.
            busy_workflow_ids = await _reap_resume_workflows(now)

            # Collect due workflow ids + user ids in a read-only session
            due_workflows: list[tuple[UUID, UUID]] = []
            async with session_manager.get_session() as session:
                # Auto-deactivate workflows whose `active_until` has passed.
                await session.execute(
                    update(Workflow)
                    .where(
                        Workflow.status == "active",
                        Workflow.active_until.isnot(None),
                        Workflow.active_until <= now,
                    )
                    .values(status="inactive")
                )

                result = await session.execute(
                    select(Workflow, User.timezone)
                    .join(User, User.id == Workflow.user_id)
                    .where(
                        Workflow.status == "active",
                        Workflow.when_type == "schedule",
                    )
                )
                interval_candidates: list[tuple[Workflow, int]] = []
                for workflow, user_tz in result.all():
                    if not _is_workflow_due(workflow, now, user_tz):
                        continue
                    # Sub-daily (minute/hour) workflows dedup on elapsed time
                    # since the last scheduled run; daily/weekly dedup on
                    # "already ran in today's local day".
                    interval_secs = _interval_seconds(workflow)
                    if interval_secs is not None:
                        # Defer: deduped in ONE batched query below, not a COUNT
                        # per workflow per tick.
                        interval_candidates.append((workflow, interval_secs))
                    elif not await _was_run_today(workflow.id, session, now, user_tz):
                        due_workflows.append((workflow.id, workflow.user_id))

                # Batch the interval dedup: a single query for the most recent
                # scheduled run of every interval candidate, compared in Python.
                # Avoids N COUNT round-trips when many minute/hour workflows are
                # active.
                if interval_candidates:
                    last_runs = await _last_scheduled_runs(
                        [w.id for w, _ in interval_candidates], session
                    )
                    for workflow, interval_secs in interval_candidates:
                        last = last_runs.get(workflow.id)
                        # Due if it has never run on schedule, or the last
                        # scheduled run started more than (interval - tolerance)
                        # ago. 30s tolerance ~= half the 60s tick.
                        if last is None or (now - last).total_seconds() >= max(0, interval_secs - 30):
                            due_workflows.append((workflow.id, workflow.user_id))

            # Overlap guard: never start a fresh scheduled run for a workflow that
            # already has a live/interrupted/just-resumed run this tick.
            if busy_workflow_ids:
                due_workflows = [(w, u) for (w, u) in due_workflows if w not in busy_workflow_ids]

            if due_workflows:
                logger.info("Workflow scheduler: due workflows", count=len(due_workflows))

            # Fan out: each workflow runs as its own task so the loop can sleep
            # 60s on schedule regardless of how long any single workflow takes.
            # Previously we awaited each execution serially, which on busy
            # mornings (20+ due workflows × ~30-90s each) blocked the loop for
            # 10-30 min — workflows scheduled inside that window then failed
            # the ±1-min `_is_workflow_due` check on the next tick and were
            # silently skipped for the day.
            #
            # Per-workflow start delay: see WORKFLOW_START_STAGGER_S. We pre-
            # decrement the inflight set in the launcher so a workflow that
            # never actually starts (e.g. process killed during the stagger
            # sleep) doesn't stay marked inflight forever.
            # Pre-filter to the workflows we'll actually launch this tick so
            # the stagger spacing is computed from the launch count, not the
            # nominal due count (otherwise inflight-skipped rows compress the
            # remaining launches into a smaller slice of the window).
            to_launch = [(w, u) for (w, u) in due_workflows if w not in _inflight_workflow_ids]
            n = len(to_launch)
            stagger_gap = STAGGER_WINDOW_S / n if n > 0 else 0.0
            for i, (wf_id, user_id) in enumerate(to_launch):
                _inflight_workflow_ids.add(wf_id)
                start_delay = i * stagger_gap
                task = asyncio.create_task(
                    _run_scheduled_workflow_with_delay(wf_id, user_id, start_delay)
                )
                _running_workflow_tasks.add(task)
                task.add_done_callback(_running_workflow_tasks.discard)
                task.add_done_callback(lambda _t, w=wf_id: _inflight_workflow_ids.discard(w))

        except Exception as e:
            logger.error("Workflow scheduler loop error", error=str(e))

        await asyncio.sleep(60)


async def _run_scheduled_workflow_with_delay(
    wf_id: UUID, user_id: UUID, delay_seconds: float,
) -> None:
    """Sleep `delay_seconds`, then run the workflow. Used by the scheduler to
    spread same-tick workflow launches across the org TPM window."""
    if delay_seconds > 0:
        try:
            await asyncio.sleep(delay_seconds)
        except asyncio.CancelledError:
            return
    await _run_scheduled_workflow(wf_id, user_id)


async def _run_scheduled_workflow(wf_id: UUID, user_id: UUID) -> None:
    """Execute one scheduled workflow with its own DB session. Errors are
    logged but never re-raised — a failing workflow must not poison the
    scheduler loop or any sibling task."""
    try:
        async with session_manager.get_session() as session:
            await execute_workflow(wf_id, user_id, session)
        logger.info(
            "Scheduled workflow executed",
            workflow_id=str(wf_id),
            user_id=str(user_id),
        )
    except Exception as e:
        logger.error(
            "Scheduled workflow execution failed",
            workflow_id=str(wf_id),
            user_id=str(user_id),
            error=str(e),
        )


# ---------------------------------------------------------------------------
# Daily Usage Batch Scheduler
# ---------------------------------------------------------------------------

DAILY_USAGE_LOCK_KEY = "gideon:scheduler:daily_usage_lock"


async def daily_usage_scheduler_loop() -> None:
    """Run every 10 minutes. At ~00:30 UTC each day, send yesterday's
    per-user usage aggregates to Pulse as ``gideon_daily_usage`` events.

    Uses a Redis lock (24h TTL) so the batch runs exactly once per day
    even with multiple pods.
    """
    logger.info("Daily usage scheduler started")

    while True:
        try:
            now = datetime.now(timezone.utc)
            # Only attempt between 00:15 and 00:45 UTC
            if now.hour == 0 and 15 <= now.minute <= 45:
                redis_client = get_redis()
                lock_key = f"{DAILY_USAGE_LOCK_KEY}:{now.date()}"
                acquired = await redis_client.set(lock_key, "1", nx=True, ex=86400)
                if acquired:
                    logger.info("Daily usage batch starting", date=str(now.date()))
                    await send_daily_usage_batch()
        except Exception as e:
            logger.error("Daily usage scheduler error", error=str(e))

        await asyncio.sleep(600)  # check every 10 minutes
