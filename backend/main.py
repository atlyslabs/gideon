import os

if __name__ == "__main__":
    import dotenv

    dotenv.load_dotenv(".env")

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable
from uuid import uuid4

import sentry_sdk
import structlog
from prometheus_fastapi_instrumentator import Instrumentator
from config import (
    BASE_URL,
    MAINTENANCE_ETA,
    MAINTENANCE_MESSAGE,
    MAINTENANCE_MODE,
    SENTRY_URL,
    SERVICE_NAME,
)
from db.redis import connect_redis, disconnect_redis
from fastapi import FastAPI
from sentry_sdk import set_tag
from sentry_sdk.integrations.logging import LoggingIntegration
from services.scheduler import (
    daily_usage_scheduler_loop,
    data_scheduler_loop,
    workflow_scheduler_loop,
)
from services.workflow_execution_service import interrupt_active_runs
from services.trigger_listener import trigger_listener_loop
from services.gideon_inbox_service import gideon_inbox_poll_loop
from services.weather_service import weather_poll_loop
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = structlog.get_logger()

# Initialize Sentry
if SENTRY_URL:
    sentry_sdk.init(
        dsn=SENTRY_URL,
        traces_sample_rate=0.1,
        integrations=[
            LoggingIntegration(event_level=logging.CRITICAL),
        ],
        profiles_sample_rate=0.01,
        with_locals=False,
        send_default_pii=False,
        max_value_length=1024,
    )

set_tag("service", SERVICE_NAME)


def _log_llm_registry() -> None:
    """Print every LLM feature spec at startup so the routing is visible in
    pod logs without grepping env vars. Catches misconfiguration early —
    e.g. `PROVIDER_CHAT=openia` (typo) would silently fall back to anthropic;
    seeing the table in logs makes it obvious."""
    try:
        from config.llm_registry import all_specs
        import os
        specs = all_specs()
        logger.info("LLM registry loaded", feature_count=len(specs))
        for name, spec in specs.items():
            logger.info(
                "llm_spec",
                feature=name,
                provider=spec.provider,
                model=spec.model,
                effort=spec.effort,
                max_tokens=spec.max_tokens,
                use_cache=spec.use_cache,
                use_advisor=spec.use_advisor,
                use_code_interpreter=spec.use_code_interpreter,
                use_reasoning_summary=spec.use_reasoning_summary,
            )
        # Confirm which provider keys are *present* (booleans only — never log
        # the values themselves). Helpful for "why does Kimi 401" debugging.
        # We log only whether KIMI_BASE_URL is overridden (bool), not the URL
        # itself: a custom/internal base URL is sensitive infra detail that
        # shouldn't land in pod logs.
        logger.info(
            "llm_api_keys_present",
            anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
            openai=bool(os.environ.get("OPENAI_API_KEY")),
            kimi=bool(os.environ.get("KIMI_API_KEY")),
            kimi_base_url_overridden=bool(os.environ.get("KIMI_BASE_URL")),
        )
        # Surface any provider env-var overrides set in the environment so the
        # table above is interpretable next to "what someone set." Only the
        # keys, not values (no model id leak risk, but keeps log volume bounded).
        override_keys = sorted(
            k for k in os.environ.keys()
            if k.startswith((
                "PROVIDER_", "MODEL_", "EFFORT_", "MAX_TOKENS_",
                "USE_ADVISOR_", "USE_CACHE_",
                "USE_CODE_INTERPRETER_", "USE_REASONING_SUMMARY_",
            ))
            and any(k.endswith(suffix) for suffix in (
                "_CHAT", "_AGENT", "_WORKFLOW", "_ADVISOR", "_TRIGGER_AGENT",
                "_MEETING_PREP", "_EMAIL_DRAFT", "_SURVEY_ANALYSIS",
                "_SMART_ONBOARDING", "_SKILL_LEARNING", "_WORKFLOW_PREVIEW",
                "_WORKFLOW_SUGGESTIONS", "_TITLE_GEN", "_SUGGESTIONS",
            ))
        )
        if override_keys:
            logger.info("llm_env_overrides_active", keys=override_keys)
    except Exception as e:
        logger.error("LLM registry startup logging failed", error=str(e))


async def _wait_for_db_ready(max_wait_s: float = 30.0) -> bool:
    """Block until Postgres answers a trivial query, or `max_wait_s` elapses.

    On pod boot the background schedulers used to fire immediately and hit
    `connection refused` for the first ~minute while the DB connection /
    network path was still establishing — a burst of error-level noise on
    every deploy. Gating scheduler startup on a real `SELECT 1` removes that:
    the loops only start once the DB is actually reachable. Bounded so a
    genuinely-down DB doesn't wedge startup forever — we start the schedulers
    anyway after the timeout (their own try/except still handles failures),
    so this only smooths the normal boot race, it doesn't add a hard dep.
    """
    from sqlalchemy import text
    from db.database import ENGINE_ASYNC

    delay = 0.5
    waited = 0.0
    while waited < max_wait_s:
        try:
            async with ENGINE_ASYNC.connect() as conn:
                await conn.execute(text("SELECT 1"))
            if waited > 0:
                logger.info("DB ready after boot wait", waited_s=round(waited, 1))
            return True
        except Exception as e:
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * 2, 5.0)  # 0.5,1,2,4,5,5... capped
            logger.debug("DB not ready yet, retrying", waited_s=round(waited, 1), err=str(e)[:120])
    logger.warning(
        "DB not ready after boot wait — starting schedulers anyway",
        max_wait_s=max_wait_s,
    )
    return False


@asynccontextmanager
async def setup_and_teardown(app: FastAPI) -> AsyncIterator[None]:
    """Lifespan context manager for startup and shutdown events."""
    # Startup
    await connect_redis()
    logger.info("Gideon service started")

    # Log the LLM registry + API-key presence at startup so routing is
    # visible in pod logs without grepping the env or hitting /healthz.
    _log_llm_registry()

    # Gate scheduler startup on DB readiness so the loops don't error-spam
    # `connection refused` during the boot window before the pool is up.
    await _wait_for_db_ready()

    # Start background schedulers
    data_task = asyncio.create_task(data_scheduler_loop())
    workflow_task = asyncio.create_task(workflow_scheduler_loop())
    trigger_task = asyncio.create_task(trigger_listener_loop())
    daily_usage_task = asyncio.create_task(daily_usage_scheduler_loop())
    inbox_task = asyncio.create_task(gideon_inbox_poll_loop())
    weather_task = asyncio.create_task(weather_poll_loop())
    logger.info("Background schedulers started")

    # Connect to Graphiti (self-hosted memory graph on Neo4j).
    # Logged explicitly so startup failures are visible in pod logs — the
    # service layer itself fails soft, and that can silently hide a bad config.
    async def _connect_graphiti():
        try:
            from services.graphiti_service import _get_graphiti
            from config import NEO4J_URI
            client = await _get_graphiti()
            if client is None:
                logger.warning("Graphiti not available — memory writes will no-op", uri=NEO4J_URI)
            else:
                logger.info("Graphiti connected", uri=NEO4J_URI)
        except Exception as e:
            logger.error("Graphiti connect raised unexpectedly", error=str(e))
    asyncio.create_task(_connect_graphiti())

    # Warm Slack member cache on startup (SSOT for collaborators)
    async def _warm_slack_members():
        try:
            from services.workspace_member_service import _get_cached_or_fetch, _fetch_slack_members, SLACK_MEMBERS_KEY
            from db.database import AsyncPostgresSessionGideon
            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as session:
                members = await _get_cached_or_fetch(session, "slack", SLACK_MEMBERS_KEY, _fetch_slack_members)
                logger.info("Warmed Slack member cache", count=len(members))
        except Exception as e:
            logger.warning("Slack member cache warm failed", error=str(e))
    asyncio.create_task(_warm_slack_members())

    yield

    # Graceful drain (SIGTERM / deploy): mark this pod's in-flight workflow runs
    # `interrupted` BEFORE cancelling the schedulers, so the next pod resumes them
    # from their last checkpoint instead of leaving orphaned `running` rows. Fast
    # + best-effort, well inside the k8s termination grace window.
    try:
        drained = await interrupt_active_runs()
        if drained:
            logger.info("Graceful drain: in-flight workflow runs marked interrupted", count=drained)
    except Exception as e:
        logger.error("Graceful drain failed", error=str(e))

    # Shutdown — cancel schedulers
    data_task.cancel()
    workflow_task.cancel()
    trigger_task.cancel()
    inbox_task.cancel()
    weather_task.cancel()
    for task in (data_task, workflow_task, trigger_task, inbox_task, weather_task):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("Background task failed during shutdown", task=task.get_name(), error=str(e))
    logger.info("Background schedulers stopped")

    await disconnect_redis()
    logger.info("Gideon service stopped")


# Initialize FastAPI app
app = FastAPI(
    title="Gideon - AI Work Assistant",
    description="Personal AI assistant that connects to your work tools",
    version="0.1.0",
    lifespan=setup_and_teardown,
    docs_url=f"{BASE_URL}/swagger",
    redoc_url=f"{BASE_URL}/redoc",
    openapi_url=f"{BASE_URL}/openapi.json",
)


# Exposes Prometheus metrics at /metrics (latency, request counts, status codes).
instrumentor = Instrumentator().instrument(app).expose(app, endpoint="/metrics")


# Maintenance middleware — short-circuits every request with a 503 when
# MAINTENANCE_MODE is on, except the k8s healthz probe (so the pod stays
# up) and CORS preflights (so the browser can still read our response).
# Registered BEFORE logging so maintenance 503s still show up in logs.
_MAINTENANCE_ALLOW_PATHS = {f"{BASE_URL}/healthz"}


@app.middleware("http")
async def maintenance_middleware(request: Request, call_next: Callable) -> Response:
    if not MAINTENANCE_MODE:
        return await call_next(request)
    if request.method == "OPTIONS" or request.url.path in _MAINTENANCE_ALLOW_PATHS:
        return await call_next(request)
    return JSONResponse(
        status_code=503,
        content={
            "maintenance": True,
            "message": MAINTENANCE_MESSAGE,
            "eta": MAINTENANCE_ETA or None,
        },
        headers={"Retry-After": "60"},
    )


# HTTP middleware for logging requests/responses
@app.middleware("http")
async def http_middleware(request: Request, call_next: Callable) -> Response:
    start_time = time.perf_counter_ns()
    try:
        trace_id = str(uuid4())
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(logging_trace_id=trace_id)
        set_tag("endpoint", request.url.path.split("/")[-1])

        response: Response = await call_next(request)

        end_time = time.perf_counter_ns()
        duration = round((end_time - start_time) / 1_000_000_000, 2)

        log_level = (
            "request success" if 200 <= response.status_code < 300 else "request failed"
        )
        logger.info(
            log_level,
            request=_log_request(request),
            response={"status_code": response.status_code, "duration_sec": duration},
        )
    except Exception as e:
        logger.exception(
            "Internal server error",
            request=_log_request(request),
            response={"error_details": str(e)},
            exc_info=True,
        )
        raise

    return response


def _log_request(request: Request) -> dict:
    """Log request details with masked authorization header."""
    return {
        "method": request.method,
        "path": request.url.path,
        "headers": {
            key: "masked" if key.lower() == "authorization" else value
            for key, value in request.headers.items()
        },
        "query_params": dict(request.query_params),
    }


# CORS middleware
# Comma-separated exact origins allowed to call the API, e.g.
# "https://gideon.example.com,http://localhost:3000". Defaults to local dev.
CORS_ALLOWED_ORIGINS = [
    o.strip() for o in os.environ.get(
        "CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
    ).split(",") if o.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS", "DELETE"],
    allow_headers=["*"],
    allow_origins=CORS_ALLOWED_ORIGINS,
)


# Routers
from routers import (
    chat_router,
    commands_router,
    dev_console_router,
    hub_router,
    integrations_router,
    memory_router,
    mention_router,
    poc_router,
    skills_router,
    surveys_router,
    users_router,
    webhooks_router,
    whatsapp_router,
    workflows_router,
)

app.include_router(
    users_router.router,
    prefix=f"{BASE_URL}/v1/users",
    tags=["Users"],
)

app.include_router(
    integrations_router.router,
    prefix=f"{BASE_URL}/v1/integrations",
    tags=["Integrations"],
)

app.include_router(
    chat_router.router,
    prefix=f"{BASE_URL}/v1/chat",
    tags=["Chat"],
)

app.include_router(
    commands_router.router,
    prefix=f"{BASE_URL}/v1/commands",
    tags=["Commands"],
)

app.include_router(
    hub_router.router,
    prefix=f"{BASE_URL}/v1/hub",
    tags=["Hub"],
)

app.include_router(
    skills_router.router,
    prefix=f"{BASE_URL}/v1/skills",
    tags=["Skills"],
)

app.include_router(
    workflows_router.router,
    prefix=f"{BASE_URL}/v1/workflows",
    tags=["Workflows"],
)

app.include_router(
    memory_router.router,
    prefix=f"{BASE_URL}/v1/memories",
    tags=["Memories"],
)

app.include_router(
    mention_router.router,
    prefix=f"{BASE_URL}/v1/mentions",
    tags=["Mentions"],
)

app.include_router(
    webhooks_router.router,
    prefix=f"{BASE_URL}/v1/webhooks",
    tags=["Webhooks"],
)

app.include_router(
    whatsapp_router.router,
    prefix=f"{BASE_URL}/v1/whatsapp",
    tags=["WhatsApp"],
)

app.include_router(
    poc_router.router,
    prefix=f"{BASE_URL}/v1/poc",
    tags=["POC"],
)

app.include_router(
    surveys_router.router,
    prefix=f"{BASE_URL}/v1/surveys",
    tags=["Surveys"],
)

app.include_router(
    dev_console_router.router,
    prefix=f"{BASE_URL}/v1/dev-console",
    tags=["DevConsole"],
)

# Health check
@app.get(f"{BASE_URL}/healthz", include_in_schema=False)
async def healthz():
    return {"status": "ok"}


# Build identifier — captured at pod startup. The frontend polls /version every
# 60s and shows an "update available" banner when this changes vs the value it
# saw on first load. Pod restart (i.e. deploy) → new value → banner. Uses time
# of import so all workers in the same pod return the same value; rolling
# deploys may briefly flap between two values, which is harmless (the banner
# only triggers a no-op refresh).
#
# Auth-required: only logged-in users need this, and it's polled from inside
# the authenticated Layout. Keeps the unauthenticated surface limited to
# /healthz (which k8s probes need).
import time as _time
from fastapi import Depends as _Depends
from middleware.auth import get_current_user as _get_current_user

_BUILD_VERSION = str(int(_time.time()))


@app.get(f"{BASE_URL}/v1/version", include_in_schema=False)
async def version(_auth_user=_Depends(_get_current_user)):
    return {"version": _BUILD_VERSION}


if __name__ == "__main__":
    import uvicorn

    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=workers,
    )
if __name__ == "__main__":
    import uvicorn

    workers = int(os.getenv("UVICORN_WORKERS", "1"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        workers=workers,
    )
