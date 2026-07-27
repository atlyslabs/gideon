"""
Tool Executor — given a Claude tool_use block, look up the tool,
fetch the user's decrypted OAuth token, execute, return result.
"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
import structlog

from db.models import Integration, User
from routers.surveys_router import is_survey_admin
from services.admin_alerts import schedule_admin_alert
from services.encryption_service import decrypt
from services.events_service import track_error, track_tool_used
from services.integration_health import record_integration_error
from tools.tool_registry import get_tool_by_name

logger = structlog.get_logger()


async def execute_tool_call(
    tool_name: str,
    tool_input: dict,
    user_id: UUID,
    session: AsyncSession,
    auth_token: str = "",
    conversation_id: str | None = None,
) -> dict:
    """
    Execute a tool call from Claude.
    Returns the result dict to send back as tool_result.
    """
    # Special-case: execute_python is the cross-provider code-execution
    # bridge. It's not in the static tool registry — it's only present in
    # the tools list when a non-Anthropic provider needs Python execution
    # (routed to Anthropic's hosted sandbox via services.llm.code_execution_bridge).
    # State (container_id) is persisted on the conversation row so files
    # persist across turns within the same chat.
    if tool_name == "execute_python":
        from services.llm.code_execution_bridge import execute_python as _bridge_exec
        from db.models import Conversation

        # SECURITY: read container state, COMMIT to release the DB transaction,
        # THEN make the external Anthropic API call. The bridge takes 1-10s
        # (sandbox spin-up + code exec); holding an open transaction over
        # that easily trips Postgres's `idle_in_transaction_session_timeout`
        # (30s) — and even within that, it pins a pool connection for
        # nothing. Same pattern as the main chat path: commit before any
        # network call to a slow upstream.
        from datetime import datetime, timezone

        container_id: str | None = None
        if conversation_id:
            conv_row = await session.execute(
                select(
                    Conversation.code_exec_container_id,
                    Conversation.code_exec_container_expires_at,
                ).where(Conversation.id == conversation_id)
            )
            row = conv_row.first()
            if row:
                container_id, expires_at = row[0], row[1]
                # Don't reuse a container we already know is expired — Anthropic
                # sandboxes idle-timeout (~20 min). Sending a stale id costs a
                # round-trip + 400 before the bridge's own retry kicks in; skip
                # it proactively. (The bridge still retries-without-container as
                # a backstop for the race where it expires mid-flight.)
                if expires_at and expires_at <= datetime.now(timezone.utc):
                    container_id = None
        await session.commit()

        code = (tool_input or {}).get("code", "") or ""
        result = await _bridge_exec(code, container_id=container_id)
        # Persist the (possibly new) container id + expiry so the next call
        # reuses it and the staleness check above works. Clear the id if the
        # bridge came back without one (e.g. hard failure) so we don't keep
        # sending a dead id.
        if conversation_id:
            new_expires = None
            raw_expires = result.get("container_expires_at")
            if raw_expires:
                try:
                    new_expires = datetime.fromisoformat(str(raw_expires).replace("Z", "+00:00"))
                except (ValueError, TypeError):
                    new_expires = None
            await session.execute(
                update(Conversation)
                .where(Conversation.id == conversation_id)
                .values(
                    code_exec_container_id=result.get("container_id") or None,
                    code_exec_container_expires_at=new_expires,
                )
            )
            await session.commit()
        track_tool_used(str(user_id), tool_name="execute_python", provider="anthropic_bridge")
        return result

    tool = get_tool_by_name(tool_name)
    if not tool:
        return {"error": f"Unknown tool: {tool_name}"}

    # Admin-only tools (survey builder) refuse to execute for non-admins.
    # Claude has the schemas in its system prompt but a non-admin user is
    # blocked here so a malicious prompt-inject can't use them.
    if getattr(tool, "admin_only", False):
        email_row = await session.execute(select(User.email).where(User.id == user_id))
        email = email_row.scalar_one_or_none()
        if not is_survey_admin(email):
            return {"error": f"Tool '{tool_name}' is admin-only."}

    # Internal + Grafana tools use DB session directly, no OAuth token needed
    if tool.provider in ("internal", "grafana"):
        try:
            from tools.internal_tools import InternalTool
            assert isinstance(tool, InternalTool)
            result = await tool.execute_internal(tool_input, user_id, session, auth_token=auth_token, conversation_id=conversation_id)
            logger.info("Tool executed", tool=tool_name, provider=tool.provider, user_id=str(user_id))
            track_tool_used(str(user_id), tool_name=tool_name, provider=tool.provider)
            return result
        except Exception as e:
            logger.error("Tool failed", tool=tool_name, provider=tool.provider, error=str(e))
            track_error(str(user_id), "tool_execution_failed", provider=tool.provider, tool_name=tool_name, detail=str(e))
            return {"error": f"Tool execution failed: {str(e)}"}

    # Fetch the user's token for this tool's provider
    result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == tool.provider,
            Integration.is_active == True,
        )
    )
    integration = result.scalar_one_or_none()

    if not integration:
        return {
            "error": f"No connected {tool.provider} integration. Please connect it first.",
            "reconnect": tool.provider,
        }

    # Decrypt the appropriate token
    # For Slack: prefer user token (access_token) so messages come from the user, not the bot
    # Fall back to bot_token only if access_token is missing
    if tool.provider == "slack":
        if integration.access_token:
            token = decrypt(integration.access_token)
        elif integration.bot_token:
            token = decrypt(integration.bot_token)
        else:
            return {"error": "No Slack token available. Please reconnect Slack."}
    else:
        token = decrypt(integration.access_token)

    # Proactive refresh for providers that support it. The shared helper below
    # coordinates parallel callers through a Redis lock so single-use refresh
    # tokens (Notion MCP, Granola MCP) don't get spent by two races on the
    # same RT. Works for every provider with a refresh endpoint.
    # NOTE: meta_ads is intentionally absent — the integration is disabled
    # (see comingSoon in providerConfig.ts + commented META_ADS_TOOL_INSTANCES
    # in tool_registry.py). Re-add when the tool is uncommented.
    _REFRESHABLE = ("notion", "granola", "posthog", "figma", "semrush", "ramp", "linear", "google")
    if tool.provider in _REFRESHABLE and integration.token_expiry:
        from datetime import datetime, timezone
        if datetime.now(timezone.utc) >= integration.token_expiry:
            from services.oauth.token_refresh import get_valid_access_token
            new_token = await get_valid_access_token(tool.provider, user_id)
            if new_token:
                token = new_token
                # Re-read our local integration row so downstream code sees the
                # freshly-persisted access_token / expiry.
                await session.refresh(integration)
            else:
                # Refresh permanently failed — surface reconnect.
                track_error(
                    str(user_id), "token_refresh_failed",
                    provider=tool.provider, tool_name=tool_name,
                    detail="refresh returned no token",
                )
                return {
                    "error": f"{tool.provider.title()} token expired and refresh failed. Please reconnect.",
                    "reconnect": tool.provider,
                }

    # Inject user name/email into Gmail draft/reply tools for proper From header
    # Also resolve recipient names from workspace member cache for proper To/CC display
    if tool_name in ("draft_email", "update_draft", "reply_to_email", "send_draft"):
        from db.models import User as UserModel
        user_row = await session.execute(select(UserModel.name, UserModel.email).where(UserModel.id == user_id))
        user_info = user_row.first()
        if user_info:
            tool_input["_from_name"] = user_info[0] or ""
            tool_input["_from_email"] = user_info[1] or ""

        # Resolve recipient display names: Google Directory -> Slack cache -> raw email
        if tool_name in ("draft_email", "update_draft", "reply_to_email"):
            from services.google_directory_service import resolve_email_to_name
            from services.workspace_member_service import get_all_members

            # Fetch Slack cache separately so its failure doesn't disable Google resolution
            slack_email_to_name: dict[str, str] = {}
            try:
                members = await get_all_members(session)
                slack_email_to_name = {m["email"].lower(): m["name"] for m in members if m.get("email") and m.get("name")}
            except Exception as e:
                logger.warning("Slack member cache fetch failed; falling back to Google Directory only", error=str(e))

            for field in ("to", "cc", "bcc"):
                raw = tool_input.get(field, "")
                if not raw:
                    continue
                resolved = []
                for addr in raw.split(","):
                    addr = addr.strip()
                    if not addr:
                        continue
                    if "<" in addr:
                        resolved.append(addr)
                        continue
                    name = ""
                    try:
                        name = await resolve_email_to_name(addr, token) or ""
                    except Exception as e:
                        logger.warning("Google Directory resolution failed", email=addr, error=str(e))
                    if not name:
                        name = slack_email_to_name.get(addr.lower(), "")
                    resolved.append(f"{name} <{addr}>" if name else addr)
                tool_input[field] = ", ".join(resolved)

    try:
        result = await tool.execute(tool_input, token)
        logger.info("Tool executed", tool=tool_name, user_id=str(user_id))
        track_tool_used(str(user_id), tool_name=tool_name, provider=tool.provider)
        return result
    except Exception as e:
        # Reactive refresh on 401 — goes through the same locked helper so parallel
        # callers hitting 401 simultaneously don't stampede Notion's token endpoint.
        # force_refresh=True: a 401 means the provider rejected the token even though
        # our stored token_expiry may still say it's valid (revoked server-side), so
        # we must refresh rather than hand back the same rejected token (401 loop).
        if tool.provider in ("notion", "granola", "posthog", "figma", "semrush") and integration.refresh_token and "401" in str(e):
            from services.oauth.token_refresh import get_valid_access_token
            new_token = await get_valid_access_token(tool.provider, user_id, force_refresh=True)
            if new_token:
                await session.refresh(integration)
                logger.info("MCP token refreshed on 401", provider=tool.provider, user_id=str(user_id))
                try:
                    return await tool.execute(tool_input, new_token)
                except Exception as retry_err:
                    logger.error("Tool retry after refresh still failed", provider=tool.provider, error=str(retry_err))
                    track_error(str(user_id), "token_refresh_failed", provider=tool.provider, tool_name=tool_name, detail=f"post-refresh retry: {retry_err}")
                    return {"error": f"{tool.provider.title()} token refreshed but the tool call still failed.", "reconnect": tool.provider}
            logger.error("MCP token refresh failed on 401", provider=tool.provider, user_id=str(user_id))
            track_error(str(user_id), "token_refresh_failed", provider=tool.provider, tool_name=tool_name, detail="401 retry: refresh returned no token")
            return {"error": f"{tool.provider.title()} token expired.", "reconnect": tool.provider}

        # Detect expired/revoked tokens (401/403) and deactivate integration
        err_str = str(e).lower()
        is_auth_error = any(phrase in err_str for phrase in (
            "401",
            "403",
            "not authenticated",
            "authentication required",
            "token expired",
            "token revoked",
        ))
        # Linear: try refresh on 401 before deactivating
        if is_auth_error and tool.provider == "linear" and integration.refresh_token:
            try:
                from services.oauth.linear_oauth import LinearOAuthProvider
                from services.encryption_service import encrypt
                new_tokens = await LinearOAuthProvider().refresh_access_token(
                    decrypt(integration.refresh_token)
                )
                integration.access_token = encrypt(new_tokens.access_token)
                if new_tokens.refresh_token:
                    integration.refresh_token = encrypt(new_tokens.refresh_token)
                integration.token_expiry = new_tokens.token_expiry
                await session.flush()
                # Retry the tool call with the new token
                result = await tool.execute(tool_input, new_tokens.access_token)
                return result
            except Exception as linear_err:
                logger.warning("Linear refresh failed on 401, deactivating")
                track_error(str(user_id), "integration_deactivated", provider="linear", tool_name=tool_name, detail=f"401 refresh failed: {linear_err}")
                await session.execute(
                    update(Integration).where(Integration.id == integration.id).values(is_active=False)
                )
                await session.flush()
                await record_integration_error(session, integration.id, "Token expired and refresh failed")
                return {"error": "Linear token expired and refresh failed.", "reconnect": "linear"}

        # Only deactivate providers without their own refresh logic
        if is_auth_error and tool.provider not in ("notion", "granola", "posthog", "figma", "semrush", "google"):
            # Deactivate so user is prompted to reconnect
            await session.execute(
                update(Integration)
                .where(Integration.id == integration.id)
                .values(is_active=False)
            )
            await session.flush()
            await record_integration_error(
                session, integration.id, f"Token expired or revoked during {tool_name}"
            )
            logger.warning("Integration deactivated due to expired token",
                           provider=tool.provider, user_id=str(user_id))
            track_error(str(user_id), "integration_deactivated", provider=tool.provider, tool_name=tool_name, detail=str(e))

            # GitHub-specific: a 401 on a just-reconnected token is almost
            # always the GitHub org pulling the Gideon OAuth app approval
            # (SAML SSO). Ping the admin once per 24h so someone can click
            # approve in the org settings — no user can self-serve this.
            if tool.provider == "github":
                schedule_admin_alert(
                    alert_key="github_sso_revoked",
                    message=(
                        ":rotating_light: *GitHub OAuth app access was revoked*\n\n"
                        "A user's GitHub token just 401'd — most likely the org pulled the "
                        "Gideon OAuth app approval. Every Gideon user's GitHub tools are "
                        "broken until this is re-approved.\n\n"
                        "*Fix (one click):* "
                        "https://github.com/settings/connections/applications\n"
                        "Find Gideon under pending requests → Approve.\n\n"
                        "_This alert is deduped — you'll only get one per 24h._"
                    ),
                )
            return {
                "error": f"{tool.provider.title()} token expired or revoked.",
                "reconnect": tool.provider,
            }

        logger.error("Tool execution failed", tool=tool_name, error=str(e))
        track_error(str(user_id), "tool_execution_failed", provider=tool.provider, tool_name=tool_name, detail=str(e))
        return {"error": f"Tool execution failed: {str(e)}"}
