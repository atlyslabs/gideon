# Contributing to Gideon

Thanks for taking the time. Issues and pull requests are both welcome.

## Getting set up

See the Quick start in the [README](README.md). The short version:

```bash
cp backend/.env.example backend/.env   # set ANTHROPIC_API_KEY and ENCRYPTION_KEY
docker compose up
```

For iterating on code, run the services directly instead so you get reload:

```bash
cd backend  && poetry install && poetry run uvicorn main:app --reload --port 8080
cd frontend && npm install && npm run dev
```

## Before opening a pull request

```bash
cd backend  && poetry run pytest && poetry run flake8
cd frontend && npm run lint && npm run type-check
```

Keep the change focused. A PR that fixes one thing gets reviewed quickly; a PR
that fixes one thing and reformats four files does not.

## Conventions

These are the rules the existing code follows. Matching them keeps review short.

**Python**

- Python 3.13, FastAPI, async SQLAlchemy.
- Imports at the top of the file. No imports inside functions, except to break
  a genuine circular import or to make a heavy dependency optional. Both cases
  deserve a comment saying which.
- No nested function definitions. Extract module-level helpers, prefixed with
  `_` when internal.
- Do not use `getattr` / `setattr` / `hasattr`, including the three-argument
  form. Use direct attribute access or an explicit `None` check.
- Do not mix sync and async. No `requests`, blocking file IO, or `time.sleep`
  inside `async def`. Use `aiohttp` / `httpx`, and `asyncio.to_thread` for
  CPU-bound work such as document parsing.
- Keep a strong reference to every `asyncio.create_task` result, via a
  module-level set plus `add_done_callback(discard)`. Tasks are weakly
  referenced and will otherwise vanish mid-flight.
- Log through `structlog` with structured fields, never `print`.

**Database**

- Hold sessions for as short a time as possible, and never across an external
  HTTP call. A hung API call must not hold a pooled connection.
- Wrap related reads and writes in one transaction.
- Always set explicit timeouts on outbound HTTP calls.

**Frontend**

- TypeScript, no `any` in new code.
- Keep components to roughly 400 to 500 lines. Split beyond that.

**Everything**

- Never log secrets, tokens, or personal data. Not even partially, and not
  "just the first few characters".
- Error messages returned to users should be actionable and must not contain
  stack traces, internal paths, or personal data.
- Use obviously fake values in examples, tests, and docs
  (`user@example.com`, `sk-fake-key-123`).

## Adding an integration

1. Create `backend/tools/your_tool.py`. Extend `BaseTool` for an OAuth-backed
   service, or `InternalTool` when no third-party token is involved.
2. Register the class in `backend/tools/tool_registry.py`.
3. If it needs OAuth, add a provider under `backend/services/oauth/`.
4. Document every new environment variable in `backend/.env.example`.
5. Make it degrade cleanly: with no credentials configured, the tool should be
   absent rather than erroring.

## Reporting bugs

Include what you expected, what happened, and the smallest set of steps that
reproduces it. Relevant logs help, with any secrets removed.

For security issues, do not open a public issue. See [SECURITY.md](SECURITY.md).

## License

Contributions are accepted under the [Apache License 2.0](LICENSE), the same
license as the project.
