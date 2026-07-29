# Gideon

An open-source AI work assistant. Gideon connects to the tools you already use
(Slack, Gmail, Calendar, Notion, Linear, GitHub, Drive and more), remembers what
matters, and does real work: drafting replies, preparing you for meetings,
running scheduled workflows, and answering questions about your own data.

You can talk to it in a web app, from Slack, over email, or on WhatsApp.

**Provider neutral.** Every AI feature routes independently to Anthropic,
OpenAI, or any OpenAI-compatible endpoint. Switch the chat model to Claude, run
the background agent on something cheaper, and change your mind later with an
environment variable. No rewrite, no lock-in.

## Quick start

You need Docker, and an API key from at least one LLM provider.

```bash
git clone https://github.com/atlyslabs/gideon.git
cd gideon

cp backend/.env.example backend/.env
# Set ANTHROPIC_API_KEY and ENCRYPTION_KEY. Generate the latter with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

docker compose up
```

Open http://localhost:3000.

New here? [**AGENTS.md**](AGENTS.md) is the setup guide: every feature, which
ones need credentials, and exactly which credentials.

Gideon starts in `single_user` mode: no login, no identity provider, everything
belongs to you. That is the intended setup for personal and self-hosted use.
Integrations are all optional, so an instance with only an API key still gives
you chat, memory, workflows, and file understanding. Add OAuth credentials for
a service and its tools appear.

## Running without Docker

```bash
# backend  (Python 3.13)
cd backend
poetry install
poetry run uvicorn main:app --reload --port 8080

# frontend (Node 20)
cd frontend
npm install
npm run dev
```

Postgres and Redis must be reachable at the values in `backend/.env`.

## Configuration

Everything is environment driven and documented inline in
[`backend/.env.example`](backend/.env.example). The parts worth knowing up front:

| Variable | What it does |
|---|---|
| `ANTHROPIC_API_KEY` | Default provider for every AI feature. |
| `ENCRYPTION_KEY` | Encrypts OAuth tokens and message content at rest. Required. |
| `AUTH_MODE` | `single_user` (default) or `oidc`. |
| `PROVIDER_<FEATURE>` / `MODEL_<FEATURE>` | Route one feature to a different provider or model. |
| `ANALYTICS_BACKEND` | Product analytics. `none` by default: Gideon never phones home. |

Frontend build-time variables (`NEXT_PUBLIC_*`) are read by Next.js at build:
`NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_APP_URL`, `NEXT_PUBLIC_ADMIN_EMAILS`,
`NEXT_PUBLIC_WEATHER_VIDEO_BASE` (weather backdrop videos; a CSS gradient is used
when unset), and `NEXT_PUBLIC_HEAD_MODEL_URL` (the 3D model behind the holographic
particle effect; defaults to the bundled `orb.obj`). `NEXT_PUBLIC_NOTIFICATION_SOUND_URL`
enables a completion chime (no audio file ships with the project).

In-chat React previews are optional and need a separate compile service: set
`PREVIEW_BUILDER_URL` (a server-side variable on the frontend) to something
exposing `POST /compile`. Unset, that route returns 501 and everything else
works normally.

### Authentication

- **`single_user`** (default) resolves every request to one local identity from
  `SINGLE_USER_*`. Do not expose an instance in this mode to a network you do
  not control.
- **`oidc`** validates bearer tokens against any OpenID Connect provider
  (Google, Okta, Auth0, Keycloak, Entra) via its userinfo endpoint. Set
  `OIDC_USERINFO_URL`.

Both live behind a single function, `services/auth_service.verify_headers`. To
support a different identity system, add a branch there. Nothing else changes,
because every route depends only on `middleware.auth.get_current_user`.

## Architecture

```
backend/            FastAPI, Python 3.13, async SQLAlchemy
  routers/          HTTP surface (chat, hub, workflows, webhooks, integrations)
  services/         Business logic
    llm/            Provider-neutral LLM layer (Anthropic / OpenAI-compatible)
    auth_service    Pluggable identity
  tools/            One module per integration, registered in tool_registry.py
  migrations/       init.sql, applied on first boot
frontend/           Next.js 15, React 19, Tailwind v4
```

**Storage.** Postgres for durable state, Redis for caching, streaming, and
locks. Neo4j is optional and powers the long-term memory graph; without it
memory falls back to Postgres.

**The LLM layer** (`backend/services/llm/`) is the piece worth reading first.
It presents one interface (`complete`, `stream`, `stream_collect`) over
different provider APIs, and normalizes the parts that usually leak: tool
calling, extended thinking, image blocks, streaming events, and token
accounting.

## Adding a tool

Tools are how Gideon does anything. Each is a class in `backend/tools/`.

For a tool that needs no third-party OAuth, extend `InternalTool` and implement
`execute_internal`, which receives the user id and a DB session:

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

For a tool backed by a connected integration, extend `BaseTool` instead, set
`provider` to the integration name (`"google"`, `"slack"`, `"notion"`, ...) and
implement `execute(params, access_token)`. Gideon decrypts and passes that
user's token, refreshing it first when needed.

Either way, register the class in `backend/tools/tool_registry.py` and it
becomes available to chat, workflows, and every trigger surface at once.

## Analytics over your own data

Gideon can answer questions by writing SQL against a ClickHouse warehouse.
Queries are SELECT-only, automatically limited, and permission gated.

Point `ANALYTICS_TABLES_FILE` at a YAML or JSON file describing your tables and
the model gets real column semantics and business rules instead of guessing:

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

Without a catalog, Gideon falls back to live `SHOW TABLES` / `DESCRIBE`
discovery, which works against any ClickHouse instance.

## Contributing

Issues and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## Security

Please do not open a public issue for security problems. See
[SECURITY.md](SECURITY.md).

## Credits

Created by [Abhishek Banerjee](https://github.com/abhiban9). See [AUTHORS](AUTHORS) for the full list of contributors.

## License

[Apache License 2.0](LICENSE). Copyright 2026 Atlys Inc.
