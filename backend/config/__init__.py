import base64
import json
import logging
import os
import urllib.request

from dotenv import load_dotenv

load_dotenv()

SERVICE_NAME = "gideon"
IS_PRODUCTION = os.environ.get("ENV", "").lower() == "production"
BASE_URL = "/api/gideon"

_logger = logging.getLogger(__name__)

# Google Secret Manager — OPTIONAL. If you run on GCP and prefer to keep secrets
# out of the environment, set GCP_PROJECT_ID and they are read from Secret
# Manager instead. Unset (the default) means plain env vars are used.
# Uses the REST API with google-auth (already a dependency) to avoid
# pulling in the heavy google-cloud-secret-manager SDK + grpcio.
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "")
_gsm_credentials = None


def get_gsm_secret(secret_id: str, fallback_env: str = "", default: str = "") -> str:
    """Fetch a secret from Google Secret Manager via REST API.

    In production, tries GSM first and falls back to the env var / default.
    Locally (non-production), skips GSM entirely and just reads the env var.
    """
    env_value = os.environ.get(fallback_env, default) if fallback_env else default
    if not GCP_PROJECT_ID or not IS_PRODUCTION:
        return env_value
    global _gsm_credentials
    try:
        import google.auth
        import google.auth.transport.requests

        if _gsm_credentials is None:
            _gsm_credentials, _ = google.auth.default(
                scopes=["https://www.googleapis.com/auth/cloud-platform"],
            )
        if not _gsm_credentials.valid:
            _gsm_credentials.refresh(google.auth.transport.requests.Request())
        url = (
            f"https://secretmanager.googleapis.com/v1/"
            f"projects/{GCP_PROJECT_ID}/secrets/{secret_id}/versions/latest:access"
        )
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {_gsm_credentials.token}",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return base64.b64decode(data["payload"]["data"]).decode("UTF-8")
    except Exception:
        _logger.warning(
            "GSM fetch failed for %s, falling back to env var", secret_id, exc_info=True
        )
        return env_value


# Sentry
SENTRY_URL = os.environ.get("SENTRY_URL")

# Maintenance mode — set MAINTENANCE_MODE=1 (or "true") to return 503 on every
# API request except the healthz probe. Used during planned DB migrations so
# clients see a branded maintenance page instead of random 500s.
MAINTENANCE_MODE = os.environ.get("MAINTENANCE_MODE", "").lower() in (
    "1",
    "true",
    "yes",
)
MAINTENANCE_MESSAGE = os.environ.get(
    "MAINTENANCE_MESSAGE",
    "Gideon is down for a quick migration.",
)
MAINTENANCE_ETA = os.environ.get("MAINTENANCE_ETA", "")  # e.g. "2026-04-19T18:00:00Z"

# PostgreSQL — supports either a full URI or individual host/port/user/password env vars
_pg_uri = os.environ.get("POSTGRES_URI_GIDEON")
if _pg_uri:
    POSTGRES_URI = _pg_uri
else:
    _pg_host = os.environ.get("POSTGRES_HOST", "localhost")
    _pg_port = os.environ.get("POSTGRES_PORT", "5432")
    _pg_user = os.environ.get("POSTGRES_USER", "postgres")
    _pg_pass = get_gsm_secret(
        "GIDEON_PG_PASS", fallback_env="POSTGRES_PASSWORD", default="local"
    )
    _pg_db = os.environ.get("POSTGRES_DB", "gideon")
    POSTGRES_URI = f"postgresql+psycopg_async://{_pg_user}:{_pg_pass}@{_pg_host}:{_pg_port}/{_pg_db}"
POSTGRES_SCHEMA = os.environ.get("GIDEON_POSTGRES_SCHEMA", "public")

# Redis
REDIS_HOST = os.environ.get("REDIS_HOST", "localhost")
REDIS_PORT = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_DB = int(os.environ.get("REDIS_DB", "0"))

# Anthropic (Claude)
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")


_anthropic_client = None


def get_anthropic_client(timeout: float = 300.0):
    """Return the shared AsyncAnthropic client.

    NOTE: ``timeout`` is the *constructor default* and only takes effect on the
    very first call (the client is cached as a module global). Subsequent calls
    with a different timeout are ignored. To override per-request, use
    ``client.with_options(timeout=X).beta.messages.stream(...)``.
    """
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        from anthropic import DefaultAioHttpClient

        _anthropic_client = anthropic.AsyncAnthropic(
            api_key=ANTHROPIC_API_KEY,
            http_client=DefaultAioHttpClient(),
            timeout=timeout,
        )
    return _anthropic_client


class Models:
    """Available Claude model IDs."""

    OPUS = "claude-opus-4-6"
    # Opus 4.7 only supports adaptive thinking (manual budget_tokens returns
    # 400). It also defaults `thinking.display` to "omitted" — services that
    # render thinking in the UI should set display: "summarized" explicitly.
    OPUS_4_7 = "claude-opus-4-7"
    SONNET = "claude-sonnet-4-6"
    # Sonnet 5: same 1M context as 4.6, cheaper through 2026-08-31 ($2/$0.20/$10
    # intro, then $3/$0.30/$15 = parity with 4.6), and more capable (near-Opus on
    # coding/agentic + high-res vision). Adaptive thinking only + non-default
    # sampling params 400 — our Anthropic path sends neither, so it's a safe swap.
    # New tokenizer counts ~30% denser than 4.6; the workflow context-shrink
    # recovery absorbs the larger pre-send estimate error.
    SONNET_5 = "claude-sonnet-5"
    HAIKU = "claude-haiku-4-5-20251001"


# Advisor tool — Sonnet executor + Opus 4.7 advisor for near-Opus quality at
# lower cost (Anthropic's recommended pairing for coding tasks at medium effort).
# Set ADVISOR_ENABLED=false to revert to direct executor model.
ADVISOR_ENABLED = os.environ.get("ADVISOR_ENABLED", "true").lower() == "true"
ADVISOR_BETA = "advisor-tool-2026-03-01"
# Per Anthropic's compatibility matrix, Opus 4.7 is currently the only valid
# advisor model. Opus 4.6 as advisor returns 400 invalid_request_error.
MODEL_ADVISOR = os.environ.get("MODEL_ADVISOR", Models.OPUS_4_7)

# `max_uses=3` caps advisor calls per request — guards against runaway loops
# (e.g., a chat turn calling the advisor 10x in one round).
# `caching` enables advisor-side prompt caching; per Anthropic this breaks
# even at ~3 advisor calls per conversation, valuable for long chat sessions.
ADVISOR_TOOL = {
    "type": "advisor_20260301",
    "name": "advisor",
    "model": MODEL_ADVISOR,
    "max_uses": 3,
    "caching": {"type": "ephemeral", "ttl": "5m"},
}

# Anthropic's native code execution tool (Python sandbox).
# Runs server-side, no container_id tracking needed for single-turn use —
# Claude gets a fresh container per API call and can chain code steps within
# that call. Cross-turn continuity is not preserved (next user turn starts clean).
CODE_EXECUTION_BETA = "code-execution-2025-08-25"

# Anthropic 1M-token context window (beta). Lets heavy workflows keep ALL their
# tool data in context instead of overflowing the 256K default and 400'ing —
# we extend the limit rather than truncate user work. Env-overridable in case
# the header differs by model generation. >200K input is billed at a premium
# tier; acceptable for low-volume internal workflows.
CONTEXT_1M_BETA = os.environ.get("CONTEXT_1M_BETA", "context-1m-2025-08-07")
CODE_EXECUTION_TOOL = {
    "type": "code_execution_20250825",
    "name": "code_execution",
}

# Per-use-case model assignments — defaults depend on ADVISOR_ENABLED.
# Advisor on: Sonnet executor + Opus advisor. Advisor off: direct Opus.
_default_executor = Models.SONNET if ADVISOR_ENABLED else Models.OPUS
MODEL_CHAT = os.environ.get("MODEL_CHAT", _default_executor)

# Anthropic model the wrapper chat path falls back to when the primary provider
# (Kimi/OpenAI) fails on an overload/transient/context error mid-turn. Mirrors
# WORKFLOW_FALLBACK_MODEL — a Moonshot 429 blip then degrades chat to "runs on
# Claude" instead of dying. Sonnet 5 (see Models.SONNET_5): same 1M window as
# 4.6, cheaper-or-equal, more capable — and the fallback is where capability
# matters most (it only fires on an outage). Env-overridable.
CHAT_FALLBACK_MODEL = os.environ.get("CHAT_FALLBACK_MODEL", Models.SONNET_5)


# Adaptive-thinking effort. Steers how much extended thinking Claude uses on
# Opus 4.6 / 4.7 / Sonnet 4.6. Valid values per Anthropic docs:
#   - "max"    — always thinks deeply, no constraints
#   - "xhigh"  — extended exploration (Opus 4.7 ONLY; 400s on other models)
#   - "high"   — always thinks (Anthropic default)
#   - "medium" — moderate thinking, may skip simple queries
#   - "low"    — minimal thinking, skips on simple tasks (lowest latency)
# Defaulting to "medium" keeps first-token latency reasonable for interactive
# UX while still letting Claude think on harder prompts.
_VALID_EFFORTS = {"max", "xhigh", "high", "medium", "low"}


def _resolve_effort(env_var: str, default: str = "medium") -> str:
    val = os.environ.get(env_var, default).strip().lower()
    return val if val in _VALID_EFFORTS else default


CHAT_EFFORT = _resolve_effort("CHAT_EFFORT")
AGENT_EFFORT = _resolve_effort("AGENT_EFFORT")
WORKFLOW_EFFORT = _resolve_effort("WORKFLOW_EFFORT")
MODEL_AGENT = os.environ.get("MODEL_AGENT", _default_executor)
MODEL_WORKFLOW_EXECUTION = os.environ.get("MODEL_WORKFLOW_EXECUTION", _default_executor)
MODEL_SKILL_LEARNING = os.environ.get("MODEL_SKILL_LEARNING", Models.SONNET)
MODEL_BACKGROUND_RESEARCH = os.environ.get("MODEL_BACKGROUND_RESEARCH", Models.SONNET)
MODEL_WORKFLOW_SUGGESTIONS = os.environ.get("MODEL_WORKFLOW_SUGGESTIONS", Models.SONNET)
MODEL_SMART_ONBOARDING = os.environ.get("MODEL_SMART_ONBOARDING", Models.SONNET)
MODEL_SUGGESTIONS = os.environ.get("MODEL_SUGGESTIONS", Models.HAIKU)
MODEL_TITLE_GENERATION = os.environ.get("MODEL_TITLE_GENERATION", Models.HAIKU)
MODEL_TRIGGER_AGENT = os.environ.get("MODEL_TRIGGER_AGENT", Models.SONNET)
MODEL_MEETING_PREP = os.environ.get("MODEL_MEETING_PREP", Models.SONNET)
MODEL_EMAIL_DRAFT = os.environ.get("MODEL_EMAIL_DRAFT", Models.SONNET)

# Legacy alias — prefer specific MODEL_* vars above
ANTHROPIC_MODEL = MODEL_CHAT

# Google OAuth (for Gmail, Calendar, Drive, Docs, Sheets, Slides)
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.environ.get(
    "GOOGLE_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/google/callback",
)

# Slack OAuth
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET", "")
SLACK_REDIRECT_URI = os.environ.get(
    "SLACK_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/slack/callback",
)
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET", "")

# Notion MCP OAuth (public client, no secret needed)
# Registered via POST https://mcp.notion.com/register with both local + prod redirect URIs
NOTION_MCP_CLIENT_ID = os.environ.get("NOTION_MCP_CLIENT_ID", "lorDgDgmqKpLHA9J")
NOTION_REDIRECT_URI = os.environ.get(
    "NOTION_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/notion/callback",
)

# Granola MCP OAuth (dynamic client registration -- client_id registered at runtime)
GRANOLA_REDIRECT_URI = os.environ.get(
    "GRANOLA_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/granola/callback",
)

# Figma OAuth (pre-registered app, confidential client + PKCE)
FIGMA_CLIENT_ID = os.environ.get("FIGMA_CLIENT_ID", "")
FIGMA_CLIENT_SECRET = os.environ.get("FIGMA_CLIENT_SECRET", "")
FIGMA_REDIRECT_URI = os.environ.get(
    "FIGMA_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/figma/callback",
)

# PostHog MCP OAuth (dynamic client registration -- client_id registered at runtime)
POSTHOG_REDIRECT_URI = os.environ.get(
    "POSTHOG_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/posthog/callback",
)

# Semrush MCP OAuth (dynamic client registration -- client_id registered at runtime)
SEMRUSH_REDIRECT_URI = os.environ.get(
    "SEMRUSH_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/semrush/callback",
)

# Meta Ads OAuth (Meta Graph API; we connect via per-user OAuth against a
# pre-registered Meta developer app — App ID in META_ADS_CLIENT_ID, optional
# secret in META_ADS_CLIENT_SECRET. The hosted MCP server at mcp.facebook.com
# is allowlist-gated to a partner set, so we hit Graph API directly.)
META_ADS_REDIRECT_URI = os.environ.get(
    "META_ADS_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/meta_ads/callback",
)

# Ramp OAuth
RAMP_CLIENT_ID = os.environ.get("RAMP_CLIENT_ID", "")
RAMP_CLIENT_SECRET = os.environ.get("RAMP_CLIENT_SECRET", "")
RAMP_REDIRECT_URI = os.environ.get(
    "RAMP_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/ramp/callback",
)

# Linear OAuth
LINEAR_CLIENT_ID = os.environ.get("LINEAR_CLIENT_ID", "")
LINEAR_CLIENT_SECRET = os.environ.get("LINEAR_CLIENT_SECRET", "")
LINEAR_REDIRECT_URI = os.environ.get(
    "LINEAR_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/linear/callback",
)

# GitHub OAuth
GITHUB_CLIENT_ID = os.environ.get("GITHUB_CLIENT_ID", "")
GITHUB_CLIENT_SECRET = os.environ.get("GITHUB_CLIENT_SECRET", "")
GITHUB_REDIRECT_URI = os.environ.get(
    "GITHUB_REDIRECT_URI",
    "http://localhost:8000/api/gideon/v1/integrations/github/callback",
)

# Grafana (global Admin SA token + per-user RBAC via permission lookup)
GRAFANA_URL = os.environ.get("GRAFANA_URL", "")
GRAFANA_TOKEN = os.environ.get("GRAFANA_TOKEN", "")

# ClickHouse (read-only analytics DB)
CLICKHOUSE_HOST = os.environ.get("CLICKHOUSE_HOST", "")
CLICKHOUSE_PORT = int(os.environ.get("CLICKHOUSE_PORT", "8443"))
CLICKHOUSE_USER = os.environ.get("CLICKHOUSE_USER", "")
CLICKHOUSE_PASSWORD = os.environ.get("CLICKHOUSE_PASSWORD", "")
CLICKHOUSE_SECURE = os.environ.get("CLICKHOUSE_SECURE", "true").lower() == "true"

# Encryption key for OAuth tokens + encrypted columns at rest (Fernet).
# Fetched from GCP Secret Manager (GIDEON_ENCRYPTION_KEY) in production — guarded
# by an IAM deny policy so only the gideon workload identity can read it.
# Locally falls back to the ENCRYPTION_KEY env var for dev convenience.
ENCRYPTION_KEY = get_gsm_secret(
    "GIDEON_ENCRYPTION_KEY", fallback_env="ENCRYPTION_KEY", default=""
)

# OpenAI (used by Graphiti for entity extraction + embeddings)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")

# Graphiti / Neo4j — self-hosted knowledge graph.
# Prod wires the combined NEO4J_AUTH="user/password" env var from the
# gideon-neo4j-auth k8s secret (single source controlled by the Neo4j Helm
# chart). Locally, set NEO4J_USER / NEO4J_PASSWORD directly.
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
_neo4j_auth = os.environ.get("NEO4J_AUTH", "")
if _neo4j_auth and "/" in _neo4j_auth:
    _u, _p = _neo4j_auth.split("/", 1)
    NEO4J_USER = _u
    NEO4J_PASSWORD = _p
else:
    NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
    NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "testpassword")

# Exa (web search)
EXA_API_KEY = os.environ.get("EXA_API_KEY", "")


# Semrush — shared org API key against api.semrush.com REST endpoints.
# The hosted Semrush MCP at mcp.semrush.com is Anthropic-partner-gated and
# rejects self-issued tokens, so we hit REST directly with the standard
# Semrush key (?key=<API_KEY>). One key for all users.
SEMRUSH_API_KEY = os.environ.get("SEMRUSH_API_KEY", "")
SEMRUSH_API_URL = os.environ.get("SEMRUSH_API_URL", "https://api.semrush.com/")


# Gideon's own Slack bot token (xoxb-...). This is the identity Gideon posts and
# DMs as — distinct from the per-user OAuth Slack tokens stored in `integrations`.
GIDEON_BOT_TOKEN = os.environ.get("GIDEON_BOT_TOKEN", "")

# Who receives Gideon's admin alerts (e.g. a dead OAuth token needing reconnect).
GIDEON_ADMIN_ALERT_EMAIL = os.environ.get("GIDEON_ADMIN_ALERT_EMAIL", "")

# --- WhatsApp entry point (Meta WhatsApp Business Cloud API) -----------------
# Gideon's OWN dedicated number on the WhatsApp Business Account (separate from any
# line). All four must be set in prod for the WhatsApp lane to activate; when
# any is empty the webhook + send paths short-circuit (feature stays inert).
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_ACCESS_TOKEN = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
# Echoed back during Meta's GET webhook verification handshake.
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "")
# Meta App Secret — used to validate the X-Hub-Signature-256 on inbound webhooks
# so only Meta can drive the lane.
WHATSAPP_APP_SECRET = os.environ.get("WHATSAPP_APP_SECRET", "")
# Pre-approved authentication-category template used to deliver the link OTP to a
# cold number (Meta requires a template outside the 24h customer-service window).
WHATSAPP_OTP_TEMPLATE = os.environ.get("WHATSAPP_OTP_TEMPLATE", "gideon_link_otp")
WHATSAPP_GRAPH_VERSION = os.environ.get("WHATSAPP_GRAPH_VERSION", "v18.0")
