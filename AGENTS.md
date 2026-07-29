# AGENTS.md

Setup and orientation guide for Gideon. Written for whoever (or whatever) is
standing this up for the first time: what the features are, which ones need
credentials, and exactly which credentials.

The short version: **three environment variables get you a working instance.**
Everything else is optional, and every integration you leave blank simply does
not appear as a tool. Nothing breaks, the tool just is not offered to the model.

---

## 1. Fastest path to a running instance

```bash
git clone https://github.com/atlyslabs/gideon.git
cd gideon
cp backend/.env.example backend/.env
```

Edit `backend/.env` and set three things:

| Variable | How to get it |
|---|---|
| `ANTHROPIC_API_KEY` | console.anthropic.com. Or use `OPENAI_API_KEY` / `KIMI_API_KEY` instead, see section 5. |
| `ENCRYPTION_KEY` | `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `SINGLE_USER_EMAIL` | Your email. Used as the local identity. |

Then:

```bash
docker compose up
```

Open http://localhost:3000. Postgres, Redis, the API and the frontend all come
up together, and the schema is applied on first boot from
`backend/migrations/init.sql`.

You are now logged in as one user with no password. That is `AUTH_MODE=single_user`,
the default, and it is the intended setup for personal and self-hosted use.
**Do not expose an instance in this mode to a network you do not control**,
because there is no authentication in front of it.

### Without Docker

```bash
# backend (Python 3.13)
cd backend && poetry install && poetry run uvicorn main:app --reload --port 8080

# frontend (Node 20)
cd frontend && npm install && npm run dev
```

Postgres and Redis still have to be reachable at whatever `backend/.env` points at.

---

## 2. Features, and what each one costs you in setup

"Needs nothing" means it works on a fresh instance with only the three required
variables above.

| Feature | What it does | What it needs |
|---|---|---|
| **Chat** | Streaming chat, extended thinking, tool use, file understanding | Needs nothing |
| **Memory** | Remembers durable facts about you, recalls them in later chats | Needs nothing. `docker compose` runs the Neo4j that powers the graph view; without it memory still works, backed by Postgres |
| **Todos and delegation** | Todos, plus delegating one to people over a Slack group DM with recurring nudges | Todos need nothing. Delegation needs Slack |
| **Workflows** | Natural-language automations on a schedule (every N minutes to weekly) | Needs nothing, but a workflow is only as useful as the integrations it can reach |
| **Skills** | Reusable instructions Gideon learns and applies | Needs nothing |
| **Custom slash commands** | Your own `/<slug>` prompts | Needs nothing |
| **Hub** | Daily briefing: meetings, todos, suggestions | Needs nothing. Meetings need Google, weather needs `WEATHER_API_KEY` |
| **Usage and cost** | Per-model token and USD accounting | Needs nothing |
| **In-chat React previews** | Gideon writes a component, you see it rendered | Needs a separate compile service, see `PREVIEW_BUILDER_URL` in section 7. Returns 501 unset |
| **Meeting prep** | Auto-generated prep notes before a meeting | Google (Calendar) |
| **Email auto-draft** | Drafts replies in your voice | Google (Gmail) |
| **Slack trigger** | @-mention Gideon in Slack, it replies in thread | `GIDEON_BOT_TOKEN` |
| **Email trigger** | CC a mailbox to kick off a run | `GIDEON_INBOX_EMAIL` + Google |
| **WhatsApp trigger** | Message Gideon on WhatsApp | `WHATSAPP_*` (Meta Cloud API) |
| **Analytics over your warehouse** | Gideon writes SELECT-only SQL against ClickHouse | `CLICKHOUSE_*`, see section 6 |
| **Surveys** | Admin-built in-app surveys with cohort targeting | `SURVEY_ADMINS` (comma-separated emails) |
| **Web search** | Exa-backed research | `EXA_API_KEY` |

---

## 3. Integrations and their exact credentials

Three connection models, and it matters which one you are dealing with:

- **Per-user OAuth**: you register an OAuth app once, then each person clicks
  Connect in the UI and their own token is stored (encrypted).
- **Dynamic registration**: no app registration at all. The provider hands out
  a client at connect time. Zero configuration on your side.
- **Shared key**: one credential in the environment, used for everybody.

| Integration | Model | Environment variables | What it unlocks |
|---|---|---|---|
| **Google** | per-user OAuth | `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI` | Gmail, Calendar, Drive, Sheets, Docs, Search Console. One connection covers all of them |
| **Slack** | bot token + per-user OAuth | `GIDEON_BOT_TOKEN`, `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET`, `SLACK_SIGNING_SECRET` | Messages, channels, threads, canvases, and the @-mention trigger |
| **Notion** | per-user OAuth (MCP) | `NOTION_MCP_CLIENT_ID` | Pages, databases, page creation |
| **Linear** | per-user OAuth | `LINEAR_CLIENT_ID`, `LINEAR_CLIENT_SECRET` | Issues, projects, cycles, teams |
| **GitHub** | per-user OAuth | `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET` | Repos, issues, PRs, code search, reviews |
| **Ramp** | per-user OAuth | `RAMP_CLIENT_ID`, `RAMP_CLIENT_SECRET` | Transactions, cards, spend |
| **Figma** | per-user OAuth | `FIGMA_CLIENT_ID`, `FIGMA_CLIENT_SECRET` | Design context, variables, screenshots |
| **Granola** | dynamic registration | **none** | Meeting notes and transcripts |
| **PostHog** | dynamic registration | **none** | Product analytics queries |
| **Recruitee** | per-user API key, entered in the UI | **none** | Full ATS surface (candidates, jobs, pipelines) |
| **Grafana** | shared key | `GRAFANA_URL`, `GRAFANA_TOKEN` | Dashboards, panel queries, panel rendering |
| **Semrush** | shared key | `SEMRUSH_API_KEY` | Domain and keyword SEO research |
| **Freshdesk** | shared key | `FRESHDESK_API_URL`, `FRESHDESK_API_KEY` | Support tickets (read-only) |
| **Freshchat** | shared key | `FRESHCHAT_INSTANCE_URL`, `FRESHCHAT_API_KEY` | Customer conversations (read-only) |
| **Exa** | shared key | `EXA_API_KEY` | Web search and research |
| **WhatsApp** | Meta Cloud API | `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_VERIFY_TOKEN`, `WHATSAPP_APP_SECRET` | Inbound message trigger |
| **Weather** | shared key | `WEATHER_API_KEY` | Hub weather panel (OpenWeather) |
| **Meta Ads** | per-user OAuth | `META_ADS_CLIENT_ID`, `META_ADS_CLIENT_SECRET` | Built but **disabled**. See the comment in `tools/tool_registry.py` to re-enable |

**Start with Google and Slack.** Between them they cover most of what makes
Gideon useful day to day, and every other integration is additive.

The registry that decides which integrations exist at all is
`backend/config/integration_registry.py`. The UI copy for each card lives in
`backend/config/integration_cards.py`.

---

## 4. Authentication

| Mode | When to use it | Configuration |
|---|---|---|
| `single_user` (default) | Personal or self-hosted. No login, one identity, everything belongs to you | `SINGLE_USER_EMAIL`, `SINGLE_USER_NAME`, `SINGLE_USER_ID` |
| `oidc` | A team. Validates bearer tokens against any OpenID Connect provider | `OIDC_USERINFO_URL`, plus optional `OIDC_ID_CLAIM` / `OIDC_EMAIL_CLAIM` / `OIDC_ROLES_CLAIM` |

Both live behind one function, `services/auth_service.verify_headers`. To support
a different identity system, add a branch there. Nothing else changes, because
every route handler depends only on `middleware.auth.get_current_user`.

`ALLOWED_EMAIL_DOMAINS` restricts sign-in to a comma-separated list of bare
domains. Empty accepts any authenticated email.

---

## 5. LLM routing

Anthropic is the default for every feature. You only need one provider key, but
**each feature routes independently**, so you can run chat on Claude and a
background job on something cheaper.

The pattern is `PROVIDER_<FEATURE>` and `MODEL_<FEATURE>`:

```bash
PROVIDER_CHAT=anthropic
MODEL_CHAT=claude-opus-4-8

PROVIDER_TRIGGER_AGENT=kimi        # route one feature elsewhere
MODEL_TRIGGER_AGENT=kimi-k2.6
```

Provider is `anthropic`, `openai`, or `kimi`. The 14 feature keys:

`CHAT`, `AGENT`, `WORKFLOW`, `ADVISOR`, `TRIGGER_AGENT`, `MEETING_PREP`,
`EMAIL_DRAFT`, `SURVEY_ANALYSIS`, `SMART_ONBOARDING`, `SKILL_LEARNING`,
`WORKFLOW_PREVIEW`, `WORKFLOW_SUGGESTIONS`, `TITLE_GEN`, `SUGGESTIONS`

The same suffix works for `EFFORT_<FEATURE>`, `MAX_TOKENS_<FEATURE>`,
`USE_ADVISOR_<FEATURE>` and `USE_CACHE_<FEATURE>`. Defaults and tiering live in
`backend/config/llm_registry.py`, which is the single source of truth.

**Any OpenAI-compatible endpoint** works via `OPENAI_API_KEY` plus
`OPENAI_BASE_URL` (vLLM, Ollama, OpenRouter, Together, LiteLLM and so on). Set
`PROVIDER_<FEATURE>=openai` and point `MODEL_<FEATURE>` at whatever that server
serves.

The provider-neutral layer is `backend/services/llm/`. It normalizes the things
that usually leak between providers: tool calling, extended thinking, image
blocks, streaming events and token accounting.

---

## 6. Analytics over your own warehouse

Gideon can answer questions by writing SQL against ClickHouse. Queries are
SELECT-only, automatically limited, and permission gated.

```bash
CLICKHOUSE_HOST=
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=
CLICKHOUSE_PASSWORD=
CLICKHOUSE_SECURE=true
```

Without a catalog, Gideon discovers tables live via `SHOW TABLES` and `DESCRIBE`,
which works against any instance. Point `ANALYTICS_TABLES_FILE` at a YAML or
JSON file and the model gets real column semantics and business rules instead
of guessing:

```yaml
orders:
  description: "One row per customer order."
  schema: |
    - id: order id
    - created_at: order creation time (UTC)
    - total_cents: order total in cents
  business_rules: |
    Exclude rows where status = 'test'.
  examples: |
    -- Revenue yesterday
    SELECT sum(total_cents)/100 FROM orders WHERE created_at >= today() - 1
```

`ANALYTICS_BROAD_READ_PERMISSION` names the permission that grants unrestricted
read. `ANALYTICS_PERMISSION_MAP` maps narrower permissions to the tables they
grant, as JSON. All queries are scoped to the `analytics` database.

---

## 7. Frontend configuration

See [`frontend/.env.example`](frontend/.env.example). Nothing here is required
to boot.

`NEXT_PUBLIC_*` variables are inlined by Next.js **at build time**, so changing
one means rebuilding, not just restarting. They are also visible to anyone who
loads the page, so never put a secret in one.

| Variable | Effect |
|---|---|
| `NEXT_PUBLIC_API_URL` | Where the backend lives |
| `NEXT_PUBLIC_APP_URL` | Public URL of the frontend |
| `NEXT_PUBLIC_AUTH_API_URL` | Base URL for login calls. Same origin when unset |
| `NEXT_PUBLIC_ADMIN_EMAILS` | Comma-separated, unlocks admin-only UI |
| `NEXT_PUBLIC_GOOGLE_CLIENT_ID` | Sign-in button client id. Only for `AUTH_MODE=oidc` with Google |
| `NEXT_PUBLIC_WEATHER_VIDEO_BASE` | Base URL for weather backdrop videos. Unset falls back to a CSS gradient |
| `NEXT_PUBLIC_HEAD_MODEL_URL` | The 3D model behind the particle effect. Defaults to the bundled `orb.obj` |
| `NEXT_PUBLIC_NOTIFICATION_SOUND_URL` | Completion chime. No audio file ships with the project |
| `NEXT_PUBLIC_STREAM_VARIANT` | How streamed text animates. 10 options, defaults to `character-ripple` |
| `NEXT_PUBLIC_GIT_SHA` | Build id on the About page. Usually set by CI |
| `PREVIEW_BUILDER_URL` | **Server-side, not public.** Enables in-chat React previews. Must expose `POST /compile`. Unset returns 501 |
| `WEATHER_API_KEY` | **Server-side.** OpenWeather key for the hub's city search route |

---

## 8. What runs in the background

Six loops start with the app (`backend/main.py`) and stop with it. There is no
separate worker process and no external scheduler to configure.

| Loop | Job |
|---|---|
| `workflow_scheduler_loop` | Fires due workflows, resumes interrupted runs |
| `data_scheduler_loop` | Per-user background ingestion |
| `trigger_listener_loop` | Slack mentions and events |
| `gideon_inbox_poll_loop` | Polls the trigger mailbox |
| `daily_usage_scheduler_loop` | Rolls up token and cost accounting |
| `weather_poll_loop` | Refreshes the hub weather panel |

Workflow runs take 30 to 90 seconds, so sub-5-minute schedules are clamped up to
a floor to stop runs overlapping.

---

## 9. Repo layout

```
backend/                 FastAPI, Python 3.13, async SQLAlchemy
  routers/               HTTP surface, one module per area
  services/              Business logic
    llm/                 Provider-neutral LLM layer
    auth_service.py      Pluggable identity, the only file auth modes touch
  tools/                 One module per integration
    tool_registry.py     ALL_TOOLS, grouped by provider. Register here
  config/
    llm_registry.py      Per-feature model and provider specs
    integration_registry.py  Which integrations exist
    integration_cards.py     UI copy for each integration
  migrations/init.sql    Applied on first boot
frontend/                Next.js 15, React 19, Tailwind v4
```

API routes are all under `/v1/*`: `users`, `integrations`, `chat`, `commands`,
`hub`, `skills`, `workflows`, `memories`, `mentions`, `webhooks`, `whatsapp`,
`surveys`, `dev-console`.

Storage: Postgres for durable state, Redis for caching, streaming and locks,
Neo4j for the memory graph. All three run from `docker compose up`. Neo4j is
the only optional one — without it, memory falls back to Postgres.

---

## 10. Adding a tool

Tools are how Gideon does anything. Each is a class in `backend/tools/`.

No third-party OAuth needed, extend `InternalTool`:

```python
from typing import Any
from uuid import UUID
from sqlalchemy.ext.asyncio import AsyncSession
from tools.internal_tools import InternalTool

class CountTodos(InternalTool):
    name = "count_todos"
    description = "Count how many open todos the user has."
    input_schema = {"type": "object", "properties": {}}

    async def execute_internal(
        self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs
    ) -> dict[str, Any]:
        return {"open": 3}
```

Backed by a connected integration, extend `BaseTool`, set `provider` to the
integration name (`"google"`, `"slack"`, `"notion"`), and implement
`execute(params, access_token)`. Gideon decrypts and passes that user's token,
refreshing it first when needed.

Either way, register the class in `backend/tools/tool_registry.py` and it becomes
available to chat, workflows and every trigger surface at once.

---

## 11. Conventions

Python:

- No private cross-module imports. Do not import `_name` from another module.
- No nested imports, no nested functions.
- No `getattr` / `setattr` / `hasattr`, including the three-argument form.
- Do not mix sync and async. No `requests` or `time.sleep()` inside `async def`.
- Keep a strong reference to `asyncio.create_task(...)` results, or the task can
  be garbage-collected mid-flight. Use a module-level set plus
  `add_done_callback(discard)`.
- Structured logging only (`structlog`). Pass fields, do not concatenate values
  into the message.

Frontend:

- Keep files around 400 to 500 lines. Split rather than grow.

Both:

- Never log secrets, tokens or PII, not even partially.
- Set an explicit timeout on every outbound HTTP call.

---

## 12. Verifying a change

```bash
# backend: every module imports cleanly
cd backend && poetry run python -c "
import pathlib, importlib, sys; sys.path.insert(0,'.')
for p in sorted(pathlib.Path('.').rglob('*.py')):
    if any(x in p.parts for x in ('__pycache__','tests','scripts')): continue
    importlib.import_module(str(p.with_suffix('')).replace('/','.').removesuffix('.__init__'))
print('ok')"

# backend: unit tests (the rest are live-integration and need real tokens)
poetry run pytest tests/test_new_tool_surface.py \
                  tests/test_workflow_interval_schedule.py \
                  tests/test_workflow_orphan_server_tool.py

# frontend
cd frontend && npx tsc --noEmit && npm run build
```

The import check is worth more than it looks. It catches missing imports and
bad module references that a syntax check cannot, because those only fail at
runtime.

---

## 13. Troubleshooting

| Symptom | Cause |
|---|---|
| Previously saved integrations stop working | `ENCRYPTION_KEY` changed. Encrypted rows are unreadable with a different key |
| A tool never gets offered to the model | Its credentials are blank. That is by design, check the table in section 3 |
| Chat 400s with "model is required" | `MODEL_<FEATURE>=` was set to empty rather than left unset |
| In-chat previews return 501 | `PREVIEW_BUILDER_URL` is unset. Expected, see section 7 |
| Everything 503s | `MAINTENANCE_MODE` is set |
| Workflows never fire | Check the scheduler loops started, and that the schedule is above the 5-minute floor |

Every variable is documented inline in
[`backend/.env.example`](backend/.env.example), which is the reference when this
file and the code disagree.
