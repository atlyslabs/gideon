"""
ClickHouse Analytics Tool — lets Claude query the analytics database.

Three tools:
1. list_analytics_tables — discover available tables
2. get_table_schema — get full schema + business rules + examples for a table
3. query_analytics — validate and execute SQL against ClickHouse

Tables are RBAC-gated by role. Users only see tables their role allows.
"""

import json
import os
import re
from typing import Any

import structlog
from services.auth_service import AUTH_MODE, single_user, verify_headers
from tools.clickhouse_client import run_query
from tools.internal_tools import InternalTool

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# RBAC — permission-to-table mapping
# ---------------------------------------------------------------------------

# Maps a permission string (as returned by your auth provider) to the logical
# tables it grants. Configure with ANALYTICS_PERMISSION_MAP, a JSON object:
#
#   {"analytics.ops:reader": ["orders"], "analytics.finance:reader": ["revenue"]}
#
# Empty by default: with no mapping configured, only holders of
# BROAD_READ_PERMISSION can reach the warehouse.
def _load_permission_map() -> dict[str, list[str]]:
    raw = os.environ.get("ANALYTICS_PERMISSION_MAP", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("ANALYTICS_PERMISSION_MAP must be a JSON object")
        return {str(k): [str(t) for t in v] for k, v in data.items()}
    except Exception as e:
        logger.error("ANALYTICS_PERMISSION_MAP failed to parse", error=str(e))
        return {}


PERMISSION_TABLE_ACCESS: dict[str, list[str]] = _load_permission_map()

# Broad warehouse read. A user holding this permission can discover and SELECT
# from ANY table in the configured ClickHouse database (live SHOW TABLES /
# DESCRIBE), not just curated ones. Still read-only: the SELECT-only guard and
# auto-LIMIT apply unchanged, and results render in a table card rather than
# being fed back to the model.
BROAD_READ_PERMISSION = os.environ.get("ANALYTICS_BROAD_READ_PERMISSION", "clickhouse:reader")

# Returned (never raised) when the bearer token is missing or expired. Without
# this guard an empty token looks identical to "no permissions" and users get a
# misleading access-denied message.
SESSION_EXPIRED_ERROR = (
    "Your session has expired. Sign in again and re-run this to refresh the "
    "session used for analytics queries."
)

# Only simple identifiers (optionally db-qualified) may be interpolated into a
# DESCRIBE — everything else (the actual SELECTs) goes through the SELECT-only
# guard, never string interpolation.
_TABLE_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+(?:\.[A-Za-z0-9_]+)?$")

# Cap on tables returned by broad discovery so a warehouse with thousands of
# tables doesn't blow the tool output.
_MAX_LIVE_TABLES = 1000

# Maps a physical ClickHouse table name to the logical catalog key that governs
# access to it — for when one logical entry (e.g. "funnel") spans several
# underlying event tables. Configure with ANALYTICS_TABLE_ALIASES, a JSON object
# of {"physical_table": "logical_key"}. Empty by default.
def _load_table_aliases() -> dict[str, str]:
    raw = os.environ.get("ANALYTICS_TABLE_ALIASES", "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("ANALYTICS_TABLE_ALIASES must be a JSON object")
        return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.error("ANALYTICS_TABLE_ALIASES failed to parse", error=str(e))
        return {}


_ACTUAL_TABLE_TO_LOGICAL: dict[str, str] = _load_table_aliases()

# All actual table names we know about (for RBAC scanning) — populated lazily after TABLES is defined
_ALL_ACTUAL_TABLES: list[str] = []


async def _get_user_permissions_ex(auth_token: str) -> tuple[list[str], bool]:
    """Resolve the caller's permission strings. Returns (permissions, expired):
    `expired` is True when the credentials were rejected, so callers can surface
    a re-auth prompt rather than a no-access message. A transport failure
    returns ([], False) — degrade to a permissions error, not a misleading
    "your session expired".

    Permissions are the union of the identity's `roles` and `access` lists as
    supplied by the configured auth backend (see `services/auth_service.py`).
    In single_user mode the local identity is returned directly, so a
    self-hosted instance needs no token here.
    """
    if AUTH_MODE == "single_user":
        user = single_user()
        return list(user.roles) + list(user.access), False

    if not auth_token:
        return [], True
    auth_header = auth_token if auth_token.startswith("Bearer ") else f"Bearer {auth_token}"
    try:
        user = await verify_headers(auth_header)
    except Exception as e:
        logger.warning("Failed to resolve permissions", error=str(e))
        return [], False
    if not user:
        return [], True
    return list(user.roles) + list(user.access), False


async def _resolve_auth(auth_token: str) -> tuple[list[str] | None, dict | None]:
    """Single entry point for the analytics tools' auth: validate the session and
    resolve permissions in one call. Returns (permissions, None) on success, or
    (None, {"error": SESSION_EXPIRED_ERROR}) when the credentials are missing or
    invalid — so a stale session tells the user to re-authenticate instead of
    "contact your admin for access"."""
    permissions, expired = await _get_user_permissions_ex((auth_token or "").strip())
    if expired:
        return None, {"error": SESSION_EXPIRED_ERROR}
    return permissions, None


def _tables_from_permissions(permissions: list[str]) -> set[str]:
    """Return the set of logical table names the user can access based on their permissions."""
    tables: set[str] = set()
    for perm, table_list in PERMISSION_TABLE_ACCESS.items():
        if perm in permissions:
            tables.update(table_list)
    return tables


def _has_broad_read(permissions: list[str]) -> bool:
    """True if the user holds the data-team broad warehouse-read permission."""
    return BROAD_READ_PERMISSION in permissions


async def _list_live_tables() -> list[dict]:
    """All tables/views in the `analytics` database (broad-read discovery)."""
    rows = await run_query(
        "SELECT name, engine FROM system.tables WHERE database = 'analytics' "
        f"ORDER BY name LIMIT {_MAX_LIVE_TABLES}"
    )
    return [{"name": r.get("name"), "engine": r.get("engine")} for r in rows]


async def _describe_live_table(table_name: str) -> list[dict]:
    """Live column list for a table in the `analytics` database (broad-read schema).

    `table_name` MUST already be validated against `_TABLE_IDENT_RE` — it is
    interpolated into the DESCRIBE, so anything but a bare identifier is a bug.
    Scope is `analytics` ONLY: a bare name is qualified as `analytics.<name>`, and
    a db-qualified name is rejected unless its prefix is `analytics`. Otherwise a
    broad-read user could introspect tables outside the advertised analytics scope
    (e.g. `system.*` / another database) via a db-qualified name. Defensive — the
    caller also enforces this and returns a friendlier message.
    """
    if "." in table_name:
        db_prefix, _, bare = table_name.partition(".")
        if db_prefix != "analytics":
            raise ValueError("Only the 'analytics' database can be introspected.")
        qualified = f"analytics.{bare}"
    else:
        qualified = f"analytics.{table_name}"
    rows = await run_query(f"DESCRIBE TABLE {qualified}")
    return [
        {"name": r.get("name"), "type": r.get("type"), "comment": r.get("comment") or None}
        for r in rows
    ]


def _check_sql_table_access(sql: str, allowed_logical: set[str]) -> str | None:
    """Check if SQL references any table the user doesn't have access to. Returns error message or None."""
    sql_upper = sql.upper()
    for actual_table in _ALL_ACTUAL_TABLES:
        if actual_table.upper() in sql_upper:
            logical = _ACTUAL_TABLE_TO_LOGICAL.get(actual_table, actual_table)
            if logical not in allowed_logical:
                return f"You don't have access to table '{actual_table}'."
    return None


# ---------------------------------------------------------------------------
# Disallowed SQL patterns (safety)
# ---------------------------------------------------------------------------

_DISALLOWED_PATTERNS = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE)\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Table catalog
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Table catalog
# ---------------------------------------------------------------------------
#
# A "curated" table is one you describe by hand so the model gets real column
# semantics, business rules, and worked examples instead of a bare DESCRIBE.
# This ships EMPTY on purpose: the catalog is deployment-specific, so point
# ANALYTICS_TABLES_FILE at a JSON or YAML file describing your own warehouse.
#
# Format — a mapping of logical table name -> entry:
#
#   my_table:
#     description: "One line shown when listing tables."
#     schema: |
#       Column reference the model reads before writing SQL.
#       - id: primary key
#       - created_at: row creation time (UTC)
#     business_rules: |
#       Domain rules the model must respect (dedupe, filters, caveats).
#     examples: |
#       -- Rows created yesterday
#       SELECT count() FROM my_table WHERE created_at >= today() - 1
#
# With no catalog configured, the tools still work for anyone holding the broad
# read permission: discovery falls back to a live SHOW TABLES / DESCRIBE against
# the `analytics` database, which works on any ClickHouse instance.
ANALYTICS_TABLES_FILE = os.environ.get("ANALYTICS_TABLES_FILE", "")

_REQUIRED_TABLE_KEYS = ("description", "schema", "business_rules", "examples")


def _load_table_catalog() -> dict[str, dict]:
    """Load the curated table catalog from ANALYTICS_TABLES_FILE.

    Returns {} when unset, missing, unparseable, or malformed — the tools then
    operate in live-discovery mode. Never raises: a bad catalog must not stop
    the service from booting.
    """
    path = ANALYTICS_TABLES_FILE.strip()
    if not path:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        if path.endswith((".yaml", ".yml")):
            import yaml
            data = yaml.safe_load(raw)
        else:
            data = json.loads(raw)
    except FileNotFoundError:
        logger.warning("Analytics table catalog not found", path=path)
        return {}
    except Exception as e:
        logger.error("Analytics table catalog failed to parse", path=path, error=str(e))
        return {}

    if not isinstance(data, dict):
        logger.error("Analytics table catalog must be a mapping of table -> entry", path=path)
        return {}

    catalog: dict[str, dict] = {}
    for name, entry in data.items():
        if not isinstance(entry, dict):
            logger.warning("Skipping malformed catalog entry", table=name)
            continue
        # Fill missing optional keys so consumers can index them unconditionally.
        catalog[str(name)] = {k: str(entry.get(k, "") or "") for k in _REQUIRED_TABLE_KEYS}
    logger.info("Analytics table catalog loaded", path=path, tables=len(catalog))
    return catalog


TABLES: dict[str, dict] = _load_table_catalog()

# Populate after TABLES is defined
_ALL_ACTUAL_TABLES.extend(list(TABLES.keys()) + list(_ACTUAL_TABLE_TO_LOGICAL.keys()))


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


class ListAnalyticsTables(InternalTool):
    name = "list_analytics_tables"
    description = (
        "List available analytics tables in ClickHouse. "
        "Call this first when the user asks a data/analytics question to find the right table. "
        "Returns table names and short descriptions."
    )
    input_schema = {
        "type": "object",
        "properties": {},
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        permissions, auth_err = await _resolve_auth(kwargs.get("auth_token", ""))
        if auth_err:
            return auth_err
        allowed = _tables_from_permissions(permissions)
        broad = _has_broad_read(permissions)
        if not allowed and not broad:
            return {
                "error": "You don't have access to analytics tables. Contact your admin to get access."
            }
        # Broad-read users see every curated table (rich schemas) regardless of
        # which curated perm they hold.
        curated = [
            {"name": name, "description": t["description"]}
            for name, t in TABLES.items()
            if name in allowed or broad
        ]
        result: dict[str, Any] = {
            "tables": curated,
            "usage": "Call get_table_schema with the table name to see full column details, business rules, and example queries before writing SQL.",
        }
        if broad:
            try:
                live = await _list_live_tables()
                curated_names = {c["name"] for c in curated}
                result["all_analytics_tables"] = [t for t in live if t["name"] not in curated_names]
                result["note"] = (
                    "You have broad analytics read. The curated tables above carry rich schemas + "
                    "business rules; tables under all_analytics_tables are raw — call get_table_schema "
                    "on any of them for live columns (no business rules). Everything is read-only."
                )
            except Exception as e:
                logger.warning("Broad table listing failed", error=str(e))
                result["all_analytics_tables_error"] = f"Could not list live tables: {e}"
        return result


class GetTableSchema(InternalTool):
    name = "get_table_schema"
    description = (
        "Get the full schema, business rules, and example queries for a ClickHouse analytics table. "
        "Call this after list_analytics_tables to get column details before writing SQL."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "table_name": {
                "type": "string",
                "description": "Table name as returned by list_analytics_tables",
            },
        },
        "required": ["table_name"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        table_name = params["table_name"]
        permissions, auth_err = await _resolve_auth(kwargs.get("auth_token", ""))
        if auth_err:
            return auth_err
        allowed = _tables_from_permissions(permissions)
        broad = _has_broad_read(permissions)

        table = TABLES.get(table_name)
        if table:
            if table_name not in allowed and not broad:
                return {"error": f"You don't have access to table '{table_name}'."}
            return {
                "table_name": table_name,
                "schema": table["schema"],
                "business_rules": table["business_rules"],
                "example_queries": table["examples"],
            }

        # Non-curated table: only broad-read users may introspect it, and only
        # via a live DESCRIBE (no hand-written business rules exist for it).
        if not broad:
            return {
                "error": f"Unknown table: {table_name}",
                "available_tables": sorted(n for n in TABLES if n in allowed),
            }
        if not _TABLE_IDENT_RE.match(table_name):
            return {"error": "Invalid table name. Use a bare identifier like 'my_table'."}
        # Analytics-only scope: discovery (list + describe) is limited to the
        # `analytics` database. Reject any other db qualifier so a broad-read user
        # can't introspect tables outside it (e.g. system.* or another database)
        # via a db-qualified name.
        if "." in table_name and table_name.split(".", 1)[0] != "analytics":
            return {"error": "Only tables in the 'analytics' database can be described."}
        try:
            columns = await _describe_live_table(table_name)
        except Exception as e:
            logger.warning("Live DESCRIBE failed", table=table_name, error=str(e)[:300])
            return {"error": f"Could not describe '{table_name}'."}
        if not columns:
            return {"error": f"Table '{table_name}' not found in the analytics database."}
        return {
            "table_name": table_name,
            "columns": columns,
            "note": (
                "Raw schema from ClickHouse (no curated business rules). Read-only — use the exact "
                "column names shown and add a LIMIT."
            ),
        }


class QueryAnalytics(InternalTool):
    name = "query_analytics"
    description = (
        "Execute a read-only SQL query against ClickHouse analytics database. "
        "IMPORTANT: Call get_table_schema first to see column names and example queries before writing SQL. "
        "Only SELECT queries are allowed. Results are returned as columns + rows. "
        "The results are automatically displayed as an interactive table in the UI. "
        "Do NOT repeat the data as a markdown table in your response. "
        "Just summarize the key findings or answer the user's question in text."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "sql": {
                "type": "string",
                "description": "The SELECT SQL query to execute against ClickHouse.",
            },
            "explanation": {
                "type": "string",
                "description": "Brief explanation of what this query does (for audit trail).",
            },
        },
        "required": ["sql"],
    }

    async def execute_internal(self, params, user_id, session, **kwargs):
        sql = params["sql"].strip().rstrip(";")
        explanation = params.get("explanation", "")

        # Safety: reject non-SELECT statements
        if _DISALLOWED_PATTERNS.search(sql):
            return {
                "error": "Only SELECT queries are allowed. INSERT/UPDATE/DELETE/DROP are not permitted."
            }

        # RBAC: check user has access to all tables referenced in the query
        permissions, auth_err = await _resolve_auth(kwargs.get("auth_token", ""))
        if auth_err:
            return auth_err
        allowed = _tables_from_permissions(permissions)
        broad = _has_broad_read(permissions)
        if not allowed and not broad:
            return {"error": "You don't have access to analytics tables."}
        # Broad-read (data team) may SELECT from any analytics table, so skip the
        # curated-table blocklist. The SELECT-only guard above + auto-LIMIT below
        # still apply, so it stays strictly read-only.
        if not broad:
            access_error = _check_sql_table_access(sql, allowed)
            if access_error:
                return {"error": access_error}

        # Safety: add LIMIT if not present
        if not re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
            sql = sql + " LIMIT 500"

        logger.info(
            "Executing analytics query",
            user_id=str(user_id),
            explanation=explanation,
            sql_preview=sql[:200],
        )

        try:
            rows = await run_query(sql)
            columns = list(rows[0].keys()) if rows else []

            return {
                "columns": columns,
                "rows": rows[:500],
                "row_count": len(rows),
                "truncated": len(rows) >= 500,
                "sql": sql,
            }
        except Exception as e:
            error_msg = str(e)
            logger.warning(
                "Analytics query failed", error=error_msg[:500], sql_preview=sql[:200]
            )
            # Don't return the raw exception (it can carry connection/host details
            # for network failures). Surface ONLY the ClickHouse query-level
            # diagnostic — e.g. "Unknown identifier 'foo'" — which is what the
            # model needs to fix its SQL and contains no infra/credentials. For
            # anything else (connection/timeout/etc.) return a generic message.
            # Full detail stays in the log above.
            if "DB::Exception:" in error_msg:
                detail = error_msg.split("DB::Exception:", 1)[1].strip()[:300]
                return {"error": f"Query failed: {detail}"}
            return {
                "error": "Query failed — the SQL could not be executed. "
                "Check table/column names and syntax, then retry."
            }
