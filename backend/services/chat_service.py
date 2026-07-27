"""
Chat Service — core chat loop with Claude.
Handles: conversation management, message storage, streaming with tool_use loops.
"""

import asyncio
import base64
import io
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator
from uuid import UUID, uuid4

import anthropic
import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select as sa_select

from config import ANTHROPIC_API_KEY, MODEL_CHAT, CHAT_FALLBACK_MODEL, ADVISOR_ENABLED, ADVISOR_BETA, ADVISOR_TOOL, CODE_EXECUTION_BETA, CODE_EXECUTION_TOOL, CHAT_EFFORT, get_anthropic_client
from db.database import AsyncPostgresSessionGideon, TOOL_DB_SEMAPHORE
from db.models import ActivityLog, Conversation, Memory, Message, Skill, UploadedFile, User
from services.llm.context_bounding import bound_request, cap_tool_result
from services.prompt_service import build_system_prompt, get_user_slack_id
from services.slash_commands import expand_slash_command
from services.tool_use_repair import (
    drop_unfulfilled_tool_uses,
    repair_orphan_tool_uses,
    strip_orphan_tool_results,
)
from tools.calendar_tool import RECURRING_MASTER_CACHE
from tools.slack_tool import SLACK_USER_TZ
from tools.tool_registry import get_tools_for_user
from tools.tool_executor import execute_tool_call
from services.events_service import track_error, track_chat_sent, track_llm_provider_fallback
from services.chat_stream_redis import (
    clear_abort_flag,
    finalize_stream,
    frame_sentinel_type,
    heartbeat_loop,
    init_stream,
    is_aborted,
    is_producer_alive,
    publish_frame,
    set_turn_meta,
    tail_stream,
)

logger = structlog.get_logger()

# SSE heartbeat during long Claude stream waits (pre-first-token thinking on
# large contexts can run 2-3 min with zero bytes downstream, which hits
# proxy/browser idle timeouts and silently drops the SSE connection).
STREAM_HEARTBEAT_INTERVAL_SEC = 15.0
_STREAM_HEARTBEAT = object()


async def _events_with_heartbeat(stream, interval: float = STREAM_HEARTBEAT_INTERVAL_SEC):
    """Wrap a Claude stream iterator and emit `_STREAM_HEARTBEAT` sentinels
    during idle periods. A background pump reads from the stream into a queue;
    the consumer reads from the queue with a timeout — a timeout means no
    real event arrived, so we yield a heartbeat to keep the SSE connection
    warm (proxies treat any downstream byte as activity).

    Exceptions raised by the stream are re-raised from the iterator.
    """
    queue: asyncio.Queue = asyncio.Queue()
    _END = object()
    err: list[BaseException] = []

    async def _pump():
        try:
            async for ev in stream:
                await queue.put(ev)
        except BaseException as e:  # noqa: BLE001
            err.append(e)
        finally:
            await queue.put(_END)

    pump_task = asyncio.create_task(_pump())
    try:
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=interval)
            except asyncio.TimeoutError:
                yield _STREAM_HEARTBEAT
                continue
            if item is _END:
                if err:
                    raise err[0]
                return
            yield item
    finally:
        if not pump_task.done():
            pump_task.cancel()
            try:
                await pump_task
            except (asyncio.CancelledError, Exception):
                pass


# History window loaded per turn. Older messages get folded into `conversations.summary`.
MAX_CONTEXT_MESSAGES = 50
# Post-turn compaction: if the just-finished response consumed more than this many
# input tokens, fold older messages into the rolling summary so the NEXT turn fits.
# Sonnet's context window is 200K; 120K (60%) leaves headroom for the next user
# message, system prompt, tools, and any tool results.
COMPACT_INPUT_TOKEN_THRESHOLD = 120_000
# Pre-turn safety net: if the unsummarized tail exceeds this many decrypted chars
# (~4 chars/token, so ~55k tokens of history — on top of the system prompt + 241
# tool schemas), fold it down SYNCHRONOUSLY before building the request. This is
# what keeps turns fast and bounded regardless of provider: the post-turn trigger
# below only fires when a turn COMPLETES, so a chat on the Kimi wrapper path (which
# had no post-turn compaction) or one whose prior turn stalled would otherwise grow
# unbounded and every subsequent turn would reload the whole fat tail. Observed on
# One long conversation: 50 post-summary messages had grown to ~315k chars and
# every turn re-sent ~100k tokens to Kimi → multi-minute waits / stalls.
COMPACT_PRETURN_CHAR_BUDGET = 220_000
# When compacting, keep this many of the most recent messages verbatim.
KEEP_RECENT_MESSAGES = 10
# Max tokens per Claude response (Sonnet 4.6 supports up to 64K output).
CHAT_MAX_OUTPUT_TOKENS = 32768

# ---------------------------------------------------------------------------
# Request-size guards (context-overflow protection)
#
# Production chat on Kimi (kimi-k2.6, hard context limit 262_144 tokens) was
# failing ~42x/week with `Error code: 400 ... exceeded model token limit:
# 262144 (requested: 348618)` — some at round 1 (history too big), most
# mid-turn as verbatim tool results accumulated across up to MAX_TOOL_ROUNDS
# rounds. Two layers of defense, BOTH request-only (the DB rows are persisted
# in full BEFORE these caps apply — see the `_store_message` calls that
# precede each append):
#   1. TOOL_RESULT_REQUEST_MAX_CHARS — per-tool-result cap at append time.
#   2. _bound_chat_request_messages  — whole-request bound before every model
#      call (chat-local port of the workflow bounding helpers in
#      services/workflow_execution_service.py; repo rules forbid importing
#      its _-prefixed helpers cross-module, so chat keeps its own copies).
# ---------------------------------------------------------------------------

# Per-tool-result cap (chars) on the copy forwarded to the model. 60K chars
# ≈ 15K tokens: generous enough for real payloads (emails, Notion pages, Exa
# results) while guaranteeing that one fat result can't eat a double-digit
# share of Kimi's 262K window, let alone 40 of them.
TOOL_RESULT_REQUEST_MAX_CHARS = 60_000
TOOL_RESULT_TRUNC_SUFFIX = (
    f"\n\n[... tool result truncated at {TOOL_RESULT_REQUEST_MAX_CHARS} chars — "
    "ask for a narrower query if you need the rest]"
)

# Per-provider approx-token ceilings for the FULL request (messages + system
# prompt + tool schemas, estimated at ~4 chars/token). Each sits below the
# model's hard limit to leave headroom for the 32K `max_tokens` output budget
# (Moonshot counts prompt + max_tokens against its 262_144 limit) and for the
# chars/4 heuristic's underestimate on dense JSON.
#   kimi      — 262_144 hard cap − 32_768 output − ~5K margin
#   openai    — gpt-5-class: 400K window but 272K input cap → 250K
#   anthropic — native chat does NOT request the 1M-context beta (unlike
#               workflows), so Sonnet's window is 200K → 160K after output
#               headroom. Workflows use 920_000 because they run on the beta.
CHAT_CONTEXT_CEILING_TOKENS = {
    "kimi": 224_000,
    "openai": 250_000,
    "anthropic": 160_000,
}
# Unknown/new provider → most conservative ceiling.
CHAT_CONTEXT_CEILING_DEFAULT = 224_000


def _cap_tool_result_for_request(payload: str) -> str:
    """Cap a serialized tool result for the model request. The full result is
    already persisted to the messages table by the time this runs — this bounds
    only the copy appended to the in-request message list. Shares the cap logic
    with the workflow executor via `context_bounding.cap_tool_result`."""
    return cap_tool_result(payload, TOOL_RESULT_REQUEST_MAX_CHARS, TOOL_RESULT_TRUNC_SUFFIX)


def _bound_chat_request_messages(
    messages: list,
    system_prompt: str,
    tools: list,
    provider: str,
    tool_round: int,
) -> list:
    """Return a request-safe COPY of `messages` bounded under the provider's
    context ceiling (no-op — returns the original list — when already under).

    The token-budgeting / field-truncation / drop-oldest machinery lives in the
    shared `services.llm.context_bounding` module (workflows use it too). This
    wrapper supplies the per-provider ceiling + chat's `prefer_newest_turn`
    fallback (keep the newest turn over a bare stale anchor), and logs. The
    caller keeps the original `messages` untouched — canonical in-memory history
    and DB rows retain full fidelity."""
    ceiling = CHAT_CONTEXT_CEILING_TOKENS.get(provider, CHAT_CONTEXT_CEILING_DEFAULT)
    outcome = bound_request(
        messages, system_prompt, tools, ceiling, prefer_newest_turn=True,
    )
    if outcome.method == "field_truncate":
        logger.warning(
            "chat request exceeded context ceiling — truncated oversized message fields",
            provider=provider,
            round=tool_round,
            approx_tokens_before=outcome.before_tokens,
            approx_tokens_after=outcome.after_tokens,
            ceiling=ceiling,
            field_cap_chars=outcome.field_cap,
        )
    elif outcome.method == "drop_oldest":
        logger.warning(
            "chat request over ceiling after field flooring — dropped oldest messages",
            provider=provider,
            round=tool_round,
            approx_tokens_before=outcome.before_tokens,
            approx_tokens_after=outcome.after_tokens,
            ceiling=ceiling,
            kept_messages=outcome.kept_messages,
        )
    return outcome.messages


# SINGLE SOURCE OF TRUTH for the non-Anthropic chat path
# (`_run_chat_turn_via_wrapper`) reactive-fallback errors: reason bucket →
# substrings. On any of these, a Kimi/OpenAI blip should degrade chat to "runs
# on Claude" rather than dying (mirrors the workflow executor's escalation). The
# flattened union drives the fallback DECISION; the ordered buckets drive the
# `reason` dimension + user message. Keeping both off one table means adding a
# marker can't silently drift the reason to 'other'. Order matters: the first
# matching bucket wins (overload before context, etc.).
_FALLBACK_REASON_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("overload", ("429", "overload", "engine_overloaded", "overloaded_error",
                  "rate limit", "rate_limit", "too many requests")),
    # `timeout` MUST precede `context`: a timeout whose text also says "too long"
    # ("...took too long...timed out") is a retry situation, not a start-a-new-chat
    # one, so match it as timeout first. `context` keeps "too long" because that
    # is the ONLY marker for Anthropic's own overflow phrasing ("prompt is too
    # long: N > M") — Kimi says "exceeded model token limit" (other markers).
    ("timeout", ("timeout", "timed out")),
    ("context", ("context", "context_length", "token limit", "exceeded model", "too long")),
    ("server_error", ("500", "502", "503", "504", "server_error")),
)


def _fallback_reason(exc: Exception) -> str:
    """Bucket a fallback-worthy error for the `gideon_llm_provider_fallback`
    event `reason` dimension + the user-facing message. Returns the first
    matching bucket (see `_FALLBACK_REASON_MARKERS` ordering) or 'other'."""
    s = str(exc).lower()
    for reason, markers in _FALLBACK_REASON_MARKERS:
        if any(m in s for m in markers):
            return reason
    return "other"


def _should_fallback_to_anthropic(exc: Exception) -> bool:
    """True if `exc` is a provider overload / transient / context error worth
    retrying on Anthropic. Deliberately broad on the transient/capacity family
    (the frequent Moonshot `429 engine_overloaded_error`); a deterministic bad
    request won't match and correctly won't trigger a (doomed, costly) retry.
    Shares `_FALLBACK_REASON_MARKERS` with `_fallback_reason` — a fallback-worthy
    error is exactly one that buckets to something other than 'other'."""
    return _fallback_reason(exc) != "other"


def _strip_thinking_for_anthropic(messages: list) -> list:
    """Return a COPY of `messages` with thinking/reasoning blocks removed so a
    Kimi/OpenAI history can be safely replayed to Anthropic. Kimi/OpenAI carry
    unsigned `thinking` blocks (round-tripped as reasoning_content); Anthropic
    400s on thinking blocks without a valid signature. Thinking is optional
    context — dropping it for the fallback attempt is safe. An assistant message
    left empty after stripping is dropped so no empty-content turn is sent."""
    out: list = []
    for m in messages:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            blocks = [
                b for b in m["content"]
                if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
            ]
            if not blocks and m.get("role") == "assistant":
                continue  # was thinking-only — drop the now-empty turn
            out.append({**m, "content": blocks})
        else:
            out.append(m)
    return out


def _tool_result_event_extras(tc_name: str, result: dict) -> dict:
    """Rich SSE fields the FE needs to render a tool result LIVE: a preview/
    wireframe card, an analytics data table, a Grafana chart, a lottie, an email
    draft card, or a reconnect prompt. Shared by BOTH chat paths so the Kimi/
    OpenAI wrapper path surfaces these during streaming, not only after a page
    reload (the wrapper path used to emit a bare tool_result, which is why
    previews/tables/charts only appeared on refresh). Mirrors exactly what the FE
    `useChatStream` tool_result handler reads.
    """
    extras: dict = {}
    if not isinstance(result, dict):
        return extras
    if result.get("reconnect"):
        extras["reconnect"] = result["reconnect"]
    if tc_name in ("draft_email", "reply_to_email", "update_draft") and result.get("draft_id"):
        extras["draft"] = {
            "draft_id": result["draft_id"],
            "to": result.get("to", ""),
            "subject": result.get("subject", ""),
            "body": result.get("body", ""),
            "cc": result.get("cc", ""),
            "bcc": result.get("bcc", ""),
        }
    if tc_name == "query_analytics" and result.get("columns") and result.get("rows"):
        all_rows = result["rows"]
        extras["data_table"] = {
            "columns": result["columns"],
            "rows": all_rows,
            "row_count": result.get("row_count", len(all_rows)),
            "display_limit": 100,
            "sql": result.get("sql", ""),
        }
    if tc_name == "generate_preview" and result.get("type") == "preview":
        extras["preview"] = {
            "framework": result.get("framework", "react"),
            "code": result.get("code", ""),
            "description": result.get("description", ""),
        }
    if tc_name == "render_lottie" and result.get("type") == "lottie":
        extras["preview"] = {
            "framework": "lottie",
            "code": json.dumps(result.get("lottie_json", {}), separators=(",", ":")),
            "description": result.get("description", "") or result.get("title", ""),
        }
        extras["lottie"] = {
            "title": result.get("title", ""),
            "size_bytes": result.get("size_bytes", 0),
        }
    if tc_name == "query_grafana_panel" and result.get("query_results"):
        for _ref_id, frame in result["query_results"].items():
            fields = frame.get("fields", [])
            rows = frame.get("rows", [])
            if len(fields) >= 2 and rows:
                extras["chart_data"] = {
                    "title": result.get("panel_title", ""),
                    "fields": fields,
                    "rows": rows[:500],
                    "time_range": result.get("time_range"),
                    "active_filters": result.get("active_filters", {}),
                }
                break  # use first frame
    return extras


async def _execute_tool_parallel(
    tc_item: dict,
    user_id: UUID,
    auth_token: str = "",
    conversation_id: str | None = None,
) -> dict:
    """Execute a single tool call with its own DB session. Safe for asyncio.gather.

    The TOOL_DB_SEMAPHORE bounds how many of these run concurrently per pod so a
    wide tool fan-out (or several overlapping turns) can't exhaust the DB pool.
    The session is only opened AFTER acquiring the semaphore, so we never hold a
    pooled connection while queued.
    """
    try:
        async with TOOL_DB_SEMAPHORE:
            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as tool_session:
                return await execute_tool_call(
                    tc_item["name"], tc_item["input"], user_id, tool_session,
                    auth_token=auth_token, conversation_id=conversation_id,
                )
    except Exception as tool_err:
        logger.error("Tool execution crashed", tool=tc_item["name"], error=str(tool_err))
        return {"error": f"Tool execution failed: {type(tool_err).__name__}"}


# Images returned by code_execution are streamed inline as base64 (no Drive
# round-trip). Other files (xlsx, csv, pdf, docx, etc.) are uploaded to the
# user's Drive under "Gideon/Code Outputs/" and surfaced as download chips —
# the sandbox FS is ephemeral so without this the file is lost the moment
# the container expires, even though the model thinks the file is "ready".
_IMAGE_EXTENSIONS = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}
_FILE_MIME_FOR_EXT = {
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls":  "application/vnd.ms-excel",
    ".csv":  "text/csv",
    ".tsv":  "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".pdf":  "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc":  "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt":  "application/vnd.ms-powerpoint",
    ".zip":  "application/zip",
    ".tar":  "application/x-tar",
    ".gz":   "application/gzip",
    ".txt":  "text/plain",
    ".html": "text/html",
    ".md":   "text/markdown",
    ".xml":  "application/xml",
    ".parquet": "application/x-parquet",
}


def _upload_code_output_to_drive_sync(access_token: str, file_bytes: bytes, filename: str, mime_type: str) -> dict[str, str]:
    """Upload bytes generated inside the code_execution sandbox to the user's
    Drive at `Gideon/Code Outputs/<filename>`. Returns `{file_id, url}`. Sync,
    call via `asyncio.to_thread`."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload

    creds = Credentials(token=access_token)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    def _find_or_create_folder(name: str, parent_id: str | None) -> str:
        query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        # Always scope to a specific parent. Without `'root' in parents` the
        # top-level lookup matches a folder named "Gideon" anywhere in the
        # user's Drive (including nested under another folder), which would
        # cause uploads to land somewhere unexpected.
        query += f" and '{parent_id or 'root'}' in parents"
        results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
        existing = results.get("files", [])
        if existing:
            return existing[0]["id"]
        meta: dict[str, Any] = {
            "name": name,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id or "root"],
        }
        return service.files().create(body=meta, fields="id").execute()["id"]

    gideon_folder = _find_or_create_folder("Gideon", None)
    outputs_folder = _find_or_create_folder("Code Outputs", gideon_folder)

    media = MediaIoBaseUpload(io.BytesIO(file_bytes), mimetype=mime_type, resumable=False)
    created = service.files().create(
        body={"name": filename, "parents": [outputs_folder]},
        media_body=media,
        fields="id, webViewLink",
    ).execute()
    return {"file_id": created["id"], "url": created.get("webViewLink", "")}


async def _download_code_exec_artifacts(file_ids: list[str], user_id: UUID) -> tuple[list[dict], list[dict]]:
    """Fetch every file the code_execution sandbox produced. Images are
    returned base64-inline (rendered in chat). Non-image files are uploaded to
    the user's Drive (`Gideon/Code Outputs/`) and a chip is returned with a
    viewable URL. Returns `(images, files)`.

    If the user has no Google integration, non-image files are skipped — better
    than silently discarding bytes, but degrading to "no chip" is the only
    sensible fallback because we have nowhere to put them.
    """
    if not file_ids:
        return [], []
    images: list[dict] = []
    files: list[dict] = []
    client = get_anthropic_client()
    drive_token: str | None = None  # lazy: only resolved if a non-image shows up
    drive_token_resolved = False
    from services.oauth.token_refresh import get_valid_access_token

    for file_id in file_ids:
        try:
            metadata = await client.beta.files.retrieve_metadata(file_id)
            filename = getattr(metadata, "filename", "") or ""
            ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

            if ext in _IMAGE_EXTENSIONS:
                file_response = await client.beta.files.download(file_id)
                raw_bytes = await file_response.aread() if hasattr(file_response, "aread") else file_response.read()
                images.append({
                    "data": base64.b64encode(raw_bytes).decode("ascii"),
                    "mime": _IMAGE_EXTENSIONS[ext],
                    "filename": filename,
                })
                continue

            mime = _FILE_MIME_FOR_EXT.get(ext, "application/octet-stream")
            if not drive_token_resolved:
                drive_token = await get_valid_access_token("google", user_id)
                drive_token_resolved = True
            if not drive_token:
                logger.info("code_exec produced a file but user has no Google integration; skipping upload",
                            file_id=file_id, filename=filename)
                continue

            file_response = await client.beta.files.download(file_id)
            raw_bytes = await file_response.aread() if hasattr(file_response, "aread") else file_response.read()
            try:
                upload = await asyncio.to_thread(
                    _upload_code_output_to_drive_sync, drive_token, raw_bytes, filename or f"file-{file_id}", mime
                )
            except Exception as up_err:
                logger.warning("code_exec Drive upload failed", file_id=file_id, filename=filename, error=str(up_err))
                continue
            files.append({
                "filename": filename,
                "mime": mime,
                "drive_url": upload["url"],
                "drive_file_id": upload["file_id"],
                "size_bytes": len(raw_bytes),
            })
        except Exception as e:
            logger.debug("Failed to download code execution file", file_id=file_id, error=str(e))
    return images, files


def _extract_file_ids_from_content_block(content_block) -> list[str]:
    """Extract file_ids from a code_execution or bash_code_execution tool result content block."""
    file_ids = []
    content = getattr(content_block, "content", None)
    if content is None:
        return file_ids
    # content is either an error or a result block with .content list of output blocks
    inner_content = getattr(content, "content", None)
    if isinstance(inner_content, list):
        for item in inner_content:
            fid = getattr(item, "file_id", None)
            if fid:
                file_ids.append(fid)
    return file_ids
MAX_TOOL_ROUNDS = 40

# Track in-flight background research tasks to prevent duplicates
_background_research_in_flight: set[UUID] = set()

# Strong refs for fire-and-forget tasks — asyncio holds only weak refs,
# so an unreferenced task can be GC'd mid-execution. See CLAUDE.md ASYNC300.
_background_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


async def get_or_create_conversation(
    session: AsyncSession, user_id: UUID, conversation_id: str | None = None
) -> Conversation:
    """Get existing conversation or create a new one."""
    if conversation_id:
        result = await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
            )
        )
        conv = result.scalar_one_or_none()
        if conv:
            return conv

    conv = Conversation(user_id=user_id)
    session.add(conv)
    await session.flush()
    await session.refresh(conv)
    return conv


async def _load_history(session: AsyncSession, conversation_id: UUID) -> list[dict]:
    """Load recent messages as Claude API format.

    Starts after `conversations.summary_last_message_id` if a summary exists, and
    includes the most recent MAX_CONTEXT_MESSAGES (not the oldest).
    """
    conv_row = await session.execute(
        select(Conversation.summary, Conversation.summary_last_message_id)
        .where(Conversation.id == conversation_id)
    )
    conv = conv_row.one_or_none()
    summary_text: str | None = conv[0] if conv else None
    summary_last_id: UUID | None = conv[1] if conv else None

    # Resolve the cutoff timestamp for "messages after summary".
    after_created_at = None
    if summary_last_id is not None:
        ts_row = await session.execute(
            select(Message.created_at).where(Message.id == summary_last_id)
        )
        after_created_at = ts_row.scalar_one_or_none()

    query = select(Message).where(Message.conversation_id == conversation_id)
    if after_created_at is not None:
        # Messages strictly after the summary cutoff (id tiebreak for same-ts rows).
        query = query.where(
            (Message.created_at > after_created_at)
            | ((Message.created_at == after_created_at) & (Message.id > summary_last_id))
        )

    # Take the newest N, then reverse back to chronological order.
    result = await session.execute(
        query.order_by(Message.created_at.desc(), Message.id.desc()).limit(MAX_CONTEXT_MESSAGES)
    )
    db_messages = list(reversed(result.scalars().all()))

    claude_messages: list[dict] = []
    if summary_text:
        # Represent the compacted prefix as a synthetic user/assistant pair so Claude
        # sees it without confusing the tool-use protocol.
        claude_messages.append({
            "role": "user",
            "content": f"[Earlier conversation — continue from here]\n\n{summary_text}",
        })
        claude_messages.append({
            "role": "assistant",
            "content": [{"type": "text", "text": "Understood. Continuing."}],
        })

    pending_tool_results = []

    for msg in db_messages:
        if msg.role == "user":
            # Flush any pending tool results first
            if pending_tool_results:
                claude_messages.append({"role": "user", "content": pending_tool_results})
                pending_tool_results = []
            claude_messages.append({"role": "user", "content": msg.content or ""})
        elif msg.role == "assistant":
            # Flush any pending tool results first
            if pending_tool_results:
                claude_messages.append({"role": "user", "content": pending_tool_results})
                pending_tool_results = []
            content_blocks = []
            # Text MUST come before tool_use (Claude API requirement)
            if msg.content:
                content_blocks.append({"type": "text", "text": msg.content})
            if msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_type = tc.get("type", "tool_use")
                    if tc_type == "code_artifact":
                        # FE-only: the code_execution sandbox produced a file
                        # which we uploaded to Drive. Anthropic has no concept
                        # of this block — drop it from the replayed history.
                        continue
                    if tc_type == "server_tool_use":
                        # advisor: always replay (pair with advisor_tool_result in same msg).
                        # code_execution: replay + rely on container_id being passed in
                        # create_params; the pre-request check strips these if container
                        # is expired to avoid 400s.
                        if tc.get("name") in ("advisor", "code_execution"):
                            content_blocks.append({"type": "server_tool_use", "id": tc["id"], "name": tc["name"], "input": tc.get("input", {})})
                        # else: skip — legacy server tools (e.g. native web_search)
                    elif tc_type == "advisor_tool_result":
                        # Anthropic requires `content.type` on replay. Older rows may
                        # have been persisted with `content: {}` (pre-fix), which 400s
                        # the whole conversation. Coerce to a valid-shape stub.
                        advisor_content = tc.get("content") or {}
                        if not isinstance(advisor_content, dict) or not advisor_content.get("type"):
                            advisor_content = {"type": "advisor_result", "text": ""}
                        content_blocks.append({"type": "advisor_tool_result", "tool_use_id": tc["tool_use_id"], "content": advisor_content})
                    elif tc_type in ("code_execution_tool_result", "bash_code_execution_tool_result"):
                        # Pass the full serialized block through — Anthropic expects
                        # the nested `content` (stdout/stderr/files) verbatim.
                        content_blocks.append(tc)
                    else:
                        content_blocks.append({"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]})
            if content_blocks:
                claude_messages.append({"role": "assistant", "content": content_blocks})
        elif msg.role == "tool":
            # Accumulate tool results — they must be in a single user message.
            # Capped for the request only — the DB row keeps the full result;
            # history is rebuilt from DB every turn, so nothing is lost. This
            # is what stops a fat historical result from blowing round 1.
            if msg.tool_results:
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": msg.tool_results.get("tool_use_id", ""),
                    "content": _cap_tool_result_for_request(
                        json.dumps(msg.tool_results.get("result", {}))
                    ),
                })

    # Flush remaining tool results
    if pending_tool_results:
        claude_messages.append({"role": "user", "content": pending_tool_results})

    # Repair any orphan tool_use blocks. Without this, a single stale turn
    # (e.g. stream cancelled mid-tool-execution) would permanently wedge the
    # conversation — every subsequent turn would 400 with "tool_use ids were
    # found without tool_result blocks immediately after".
    # Move client `tool_use` blocks to the end of each assistant message so
    # they come after any server-side blocks (advisor, code_execution + their
    # paired results). Older rows were persisted with `tool_use` first and
    # advisor blocks after, which trips an Anthropic 400 on replay even though
    # the matching tool_result IS in the very next user message. See the
    # `all_tool_data` ordering note in `_run_chat_turn` for the write-side fix.
    _reorder_assistant_tool_blocks(claude_messages)

    claude_messages = repair_orphan_tool_uses(claude_messages)
    claude_messages = strip_orphan_tool_results(claude_messages)
    # Final safety net: if any tool_use survived the repair (duplicates,
    # type-mismatch, edge cases the synthetic-result inserter missed), strip
    # those blocks outright. We'd rather lose context than wedge the chat.
    claude_messages = drop_unfulfilled_tool_uses(claude_messages)

    return claude_messages


def _reorder_assistant_tool_blocks(messages: list[dict]) -> None:
    """In-place: for each assistant message, push client `tool_use` blocks to
    the end of the content list, after any server-side `server_tool_use` /
    `*_tool_result` blocks. Anthropic's API requires the client tool_use to
    be the last tool-related block so its tool_result is "immediately after"
    in the next user message — older rows persisted blocks in the wrong order
    (`tool_use + advisor + advisor_tool_result`), which 400s on replay.
    """
    _SERVER_TYPES = {
        "server_tool_use",
        "advisor_tool_result",
        "code_execution_tool_result",
        "bash_code_execution_tool_result",
    }
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if not isinstance(content, list) or len(content) < 2:
            continue
        client_tool_uses: list[dict] = []
        others: list[dict] = []
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_use":
                client_tool_uses.append(b)
            else:
                others.append(b)
        if not client_tool_uses:
            continue
        # Skip if already in the correct order (no server blocks after a tool_use).
        seen_tool_use = False
        bad_order = False
        for b in content:
            if not isinstance(b, dict):
                continue
            btype = b.get("type")
            if btype == "tool_use":
                seen_tool_use = True
            elif seen_tool_use and btype in _SERVER_TYPES:
                bad_order = True
                break
        if bad_order:
            msg["content"] = others + client_tool_uses


# The tool_use ↔ tool_result sanitizers (repair_orphan_tool_uses,
# strip_orphan_tool_results, drop_unfulfilled_tool_uses) live in
# services.tool_use_repair — a single source of truth shared with the trigger/
# WhatsApp agent. Imported at the top of this module.


def _render_messages_for_summary(db_messages: list[Message]) -> str:
    """Render DB messages as plain text the summarizer can read."""
    lines: list[str] = []
    for msg in db_messages:
        if msg.role == "user" and msg.content:
            lines.append(f"USER: {msg.content}")
        elif msg.role == "assistant":
            if msg.content:
                lines.append(f"ASSISTANT: {msg.content}")
            if msg.tool_calls:
                tool_names = [tc.get("name", "") for tc in msg.tool_calls if tc.get("type") != "advisor_tool_result"]
                if tool_names:
                    lines.append(f"  (tool calls: {', '.join(n for n in tool_names if n)})")
        elif msg.role == "tool" and msg.tool_results:
            result = msg.tool_results.get("result", {})
            # Keep tool results short — we don't need full payloads in the summary prompt.
            summary = json.dumps(result)[:300]
            lines.append(f"  (tool result: {summary})")
    return "\n".join(lines)


def _message_chars(m: Message) -> int:
    """Approximate decrypted size of a message for context-budget heuristics."""
    total = 0
    for val in (m.content, m.thinking):
        if val:
            total += len(val)
    for val in (m.tool_calls, m.tool_results):
        if val:
            total += len(json.dumps(val, default=str))
    return total


async def _unsummarized_tail_chars(session: AsyncSession, conversation_id: UUID) -> int:
    """Total decrypted char size of messages NOT yet folded into the summary.

    Cheap proxy for "how big is the history this turn will reload". Used by the
    pre-turn guard to decide whether to compact synchronously before building
    the request. Mirrors the cutoff logic in `_load_history` / `_compact_conversation`.
    """
    row = (
        await session.execute(
            select(Conversation.summary_last_message_id).where(Conversation.id == conversation_id)
        )
    ).one_or_none()
    summary_last_id = row[0] if row else None

    after_created_at = None
    if summary_last_id is not None:
        after_created_at = (
            await session.execute(select(Message.created_at).where(Message.id == summary_last_id))
        ).scalar_one_or_none()

    query = select(Message).where(Message.conversation_id == conversation_id)
    if after_created_at is not None:
        query = query.where(
            (Message.created_at > after_created_at)
            | ((Message.created_at == after_created_at) & (Message.id > summary_last_id))
        )
    # Bound the scan to the same window the turn would actually load.
    query = query.order_by(Message.created_at.desc(), Message.id.desc()).limit(MAX_CONTEXT_MESSAGES)
    rows = (await session.execute(query)).scalars().all()
    return sum(_message_chars(m) for m in rows)


async def _compact_conversation(session: AsyncSession, conversation_id: UUID) -> bool:
    """Fold all but the last KEEP_RECENT_MESSAGES unsummarized messages into
    `conversations.summary` via Haiku. Extends any existing summary. Caller
    decides whether the conversation needs compaction (e.g. based on tokens).

    Returns True if a summary was written, False if there was nothing to do.
    """
    conv_result = await session.execute(
        select(Conversation.summary, Conversation.summary_last_message_id)
        .where(Conversation.id == conversation_id)
    )
    row = conv_result.one_or_none()
    existing_summary: str | None = row[0] if row else None
    summary_last_id: UUID | None = row[1] if row else None

    after_created_at = None
    if summary_last_id is not None:
        ts_row = await session.execute(
            select(Message.created_at).where(Message.id == summary_last_id)
        )
        after_created_at = ts_row.scalar_one_or_none()

    base_query = select(Message).where(Message.conversation_id == conversation_id)
    if after_created_at is not None:
        base_query = base_query.where(
            (Message.created_at > after_created_at)
            | ((Message.created_at == after_created_at) & (Message.id > summary_last_id))
        )

    ordered = base_query.order_by(Message.created_at.asc(), Message.id.asc())
    result = await session.execute(ordered)
    unsummarized = list(result.scalars().all())

    # Nothing to do if the unsummarized tail is already short.
    if len(unsummarized) <= KEEP_RECENT_MESSAGES:
        return False

    to_summarize = unsummarized[:-KEEP_RECENT_MESSAGES]
    if not to_summarize:
        return False

    transcript = _render_messages_for_summary(to_summarize)
    if not transcript.strip():
        return False

    prior = f"\n\nEXISTING SUMMARY (extend, do not restate):\n{existing_summary}" if existing_summary else ""
    prompt = (
        "Summarize the following chat transcript between a user and an AI assistant "
        "so the assistant can continue the conversation coherently without the full "
        "history. Preserve: user's goals, decisions made, data found, entities named "
        "(people, tickets, files, URLs), and any pending work. Omit filler and tool "
        "mechanics. Use 4-10 short bullets. No preamble."
        f"{prior}\n\nTRANSCRIPT:\n{transcript}"
    )

    # Don't hold an open implicit transaction across the Sonnet call. CLAUDE.md
    # rule #3 — Postgres `idle_in_transaction_session_timeout = 30s` will kill
    # the connection during the LLM round-trip and the UPDATE below would roll
    # back. The reads above are already self-consistent.
    await session.commit()

    try:
        from dataclasses import replace as _replace
        from services.llm import get_spec, llm
        from config import Models
        # Sonnet, not Haiku. On long histories (observed at ~1000 messages /
        # ~400k chars), Haiku roleplays as the assistant and "replies" to the
        # last message instead of summarizing the transcript. Sonnet has the
        # same 200k context but follows the instruction reliably.
        # MUST pin provider="anthropic" too — title_gen routes to Kimi in prod,
        # and a bare model=claude-sonnet-4-6 on the Kimi provider 400s (unknown
        # model). Compaction deliberately stays on Anthropic Sonnet regardless
        # of where the haiku tier is pointed.
        compact_spec = _replace(
            get_spec("title_gen"), provider="anthropic", model=Models.SONNET, max_tokens=2000
        )
        response = await llm.complete(
            compact_spec,
            messages=[{"role": "user", "content": prompt}],
            timeout=120.0,
        )
        new_summary = response.text.strip()
    except Exception as e:
        logger.warning("Conversation compaction failed", conversation_id=str(conversation_id), error=str(e))
        return False

    if not new_summary:
        return False

    new_last_id = to_summarize[-1].id
    await session.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(summary=new_summary, summary_last_message_id=new_last_id)
    )
    await session.flush()
    logger.info(
        "Conversation compacted",
        conversation_id=str(conversation_id),
        messages_summarized=len(to_summarize),
        kept_recent=KEEP_RECENT_MESSAGES,
    )
    return True


def _extract_code_exec_blocks(final_message) -> list[dict]:
    """Pull code_execution server_tool_use + result blocks out of a completed
    Claude response so we can persist them for replay on future turns.
    """
    blocks: list[dict] = []
    for block in getattr(final_message, "content", None) or []:
        btype = getattr(block, "type", None)
        name = getattr(block, "name", None)
        if btype == "server_tool_use" and name == "code_execution":
            try:
                blocks.append(block.model_dump(mode="json"))
            except Exception:  # noqa: BLE001
                blocks.append({"type": "server_tool_use", "id": getattr(block, "id", ""), "name": "code_execution", "input": getattr(block, "input", {}) or {}})
        elif btype in ("code_execution_tool_result", "bash_code_execution_tool_result"):
            try:
                blocks.append(block.model_dump(mode="json"))
            except Exception:  # noqa: BLE001
                # Best-effort fallback — keeps the tool_use_id pairing intact
                blocks.append({"type": btype, "tool_use_id": getattr(block, "tool_use_id", ""), "content": {}})
    return blocks


def _parse_anthropic_datetime(value) -> datetime | None:
    """Anthropic returns container.expires_at as either a datetime or ISO string."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            # Handle trailing 'Z' as UTC
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _strip_code_exec_blocks(history: list[dict]) -> None:
    """Remove code_execution server_tool_use / result blocks from history in-place.
    Used when the conversation's container has expired and we can't resume — the
    stale blocks would cause `container_id is required` 400s.
    """
    for msg in history:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        msg["content"] = [
            b for b in content
            if not (
                isinstance(b, dict) and (
                    (b.get("type") == "server_tool_use" and b.get("name") == "code_execution")
                    or b.get("type") in ("code_execution_tool_result", "bash_code_execution_tool_result")
                )
            )
        ]


async def _persist_tool_placeholders(conversation_id: UUID, tool_use_ids: list[str]) -> None:
    """Write placeholder tool_result rows for any tool_use IDs whose real result
    didn't persist (stream cancelled, exception mid-loop, DB error). Uses a fresh
    session so it works even if the primary session is broken. Must be called
    under `asyncio.shield` so cancellation doesn't interrupt the writes.
    """
    if not tool_use_ids:
        return
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as rescue_session:
            for tid in tool_use_ids:
                rescue_session.add(Message(
                    conversation_id=conversation_id,
                    role="tool",
                    tool_results={
                        "tool_use_id": tid,
                        "result": {
                            "error": "Tool execution interrupted before result was persisted.",
                            "interrupted": True,
                        },
                    },
                ))
            await rescue_session.commit()
        logger.warning(
            "Persisted placeholder tool_results for interrupted tool calls",
            conversation_id=str(conversation_id),
            count=len(tool_use_ids),
            tool_use_ids=tool_use_ids,
        )
    except Exception as e:
        logger.error(
            "Failed to persist placeholder tool_results — conversation may orphan",
            conversation_id=str(conversation_id),
            error=str(e),
            tool_use_ids=tool_use_ids,
        )


async def _compact_conversation_background(conversation_id: UUID) -> None:
    """Open its own DB session so it's safe to fire-and-forget after the turn ends."""
    try:
        sm = AsyncPostgresSessionGideon()
        async with sm.get_session() as session:
            await _compact_conversation(session, conversation_id)
    except Exception as e:
        logger.warning(
            "Background conversation compaction failed",
            conversation_id=str(conversation_id),
            error=str(e),
        )


async def _preturn_compact_frames(
    session: AsyncSession, conversation_id: UUID
) -> AsyncGenerator[str, None]:
    """Pre-turn safety net (both chat paths). If the unsummarized tail is oversized,
    compact it SYNCHRONOUSLY before the turn builds its request, and surface a
    transient "Compacting Memory" tool indicator to the user while it runs.

    Yields SSE frames (tool_start before the work, tool_result after) so the user
    sees why the first token is taking a beat. The frames are ephemeral — they are
    not persisted to `messages.tool_calls`, so they vanish on reload, which is what
    we want for a housekeeping step. Must be called AFTER the user message is
    committed and BEFORE `_load_history`, so the advanced summary pointer is picked
    up by the load on the same session.
    """
    try:
        tail_chars = await _unsummarized_tail_chars(session, conversation_id)
    except Exception as e:  # noqa: BLE001 — never let the guard block a turn
        logger.warning("Pre-turn tail measurement failed", conversation_id=str(conversation_id), error=str(e))
        return
    if tail_chars < COMPACT_PRETURN_CHAR_BUDGET:
        return

    call_id = f"compact-{uuid4().hex[:8]}"
    yield f"data: {json.dumps({'type': 'tool_start', 'tool': 'compacting_memory', 'tool_call_id': call_id})}\n\n"
    compacted = False
    try:
        compacted = await _compact_conversation(session, conversation_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Pre-turn compaction failed", conversation_id=str(conversation_id), error=str(e))
    summary = (
        "Compacted earlier messages to keep this chat fast."
        if compacted
        else "Optimized conversation memory."
    )
    yield f"data: {json.dumps({'type': 'tool_result', 'tool': 'compacting_memory', 'tool_call_id': call_id, 'summary': summary})}\n\n"


async def _store_message(
    session: AsyncSession,
    conversation_id: UUID,
    role: str,
    content: str | None = None,
    tool_calls: list | None = None,
    tool_results: dict | None = None,
    model: str | None = None,
    tokens_used: int | None = None,
    thinking: str | None = None,
    mentions: dict | list | None = None,
    status: str = "done",
) -> Message:
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        tool_calls=tool_calls,
        tool_results=tool_results,
        model=model,
        tokens_used=tokens_used,
        thinking=thinking,
        mentions=mentions,
        status=status,
    )
    session.add(msg)
    await session.flush()
    await session.refresh(msg)
    return msg


_REFUSAL_STEMS = (
    "i can't",
    "i cannot",
    "i'm unable",
    "i am unable",
    "i don't have access",
    "i do not have access",
    "i don't have the ability",
    "sorry, i",
    "i'm sorry",
    "i apologize",
    "unfortunately, i",
)


def _is_refusal_or_error_reply(reply: str | None) -> bool:
    """Heuristic: does this assistant reply look like a refusal/error we should exclude from title context?"""
    if not reply:
        return True
    stripped = reply.strip()
    if len(stripped) < 20:
        return True
    head = stripped[:100].lower()
    return any(head.startswith(stem) for stem in _REFUSAL_STEMS)


def _clean_title(raw: str) -> str | None:
    """Strip preamble junk and sanity-clamp length. Returns None if the result is implausibly long (malformed LLM output)."""
    title = raw.strip().strip("#").strip('"').strip("'").strip()
    title = re.sub(r'^(?:new\s+)?(?:title\s+)?(?:suggestions?\s+)?(?:for\s+this\s+conversation\s*)?[:]\s*', '', title, flags=re.IGNORECASE).strip()
    title = re.sub(r'^(?:title|suggestion)\s*[:]\s*', '', title, flags=re.IGNORECASE).strip()
    if not title:
        return None
    # Hard guards — a valid title is <= 80 chars and <= 12 words.
    # If Haiku drifted into prose, drop the result so we retry next turn.
    if len(title) > 80 or len(title.split()) > 12:
        logger.warning("Auto-title rejected (too long)", chars=len(title), preview=title[:60])
        return None
    return title


def _parse_title_json(raw: str) -> tuple[str, str | None]:
    """Parse Haiku's JSON response. Returns (action, title) where action is 'keep' or 'update'."""
    try:
        # Some models wrap JSON in prose or code fences — pull the first {...} block.
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return ("update", _clean_title(raw))
        data = json.loads(match.group(0))
        action = str(data.get("action", "update")).lower()
        title_raw = data.get("title") or ""
        return (action, _clean_title(title_raw) if title_raw else None)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ("update", _clean_title(raw))


async def _generate_title(latest_message: str, current_title: str | None = None, assistant_reply: str | None = None) -> str | None:
    """Generate a title via Haiku with structured JSON output.

    Rules:
    - An assistant reply that looks like a refusal/error is stripped before prompting — errors must never become titles.
    - Output is JSON {action, title}. Malformed or overlong outputs are rejected by _clean_title.
    """
    # Drop refusal/error replies — they must not inform the title.
    if _is_refusal_or_error_reply(assistant_reply):
        assistant_reply = None

    reply_context = f"\nAssistant reply: {assistant_reply[:300]}" if assistant_reply else ""

    try:
        if current_title:
            system = (
                "Decide if a conversation title needs updating. Reply with JSON only:\n"
                "{\"action\": \"keep\"} if the topic is unchanged, OR\n"
                "{\"action\": \"update\", \"title\": \"3-6 word title\"} if the topic shifted.\n"
                "Title must be 3-6 words, no quotes, no hashtags, no prefixes, no trailing punctuation."
            )
            prompt = f"Current title: {current_title}\nNew user message: {latest_message[:200]}{reply_context}"
        else:
            system = (
                "Generate a concise conversation title from the user's intent. Reply with JSON only:\n"
                "{\"action\": \"update\", \"title\": \"3-6 word title\"}\n"
                "Title must be 3-6 words, focused on what the user is asking about, no quotes, no hashtags, no prefixes."
            )
            prompt = f"User message: {latest_message[:200]}{reply_context}"

        from dataclasses import replace as _replace
        from services.llm import get_spec, llm
        title_spec = _replace(get_spec("title_gen"), max_tokens=80)
        response = await llm.complete(
            title_spec,
            messages=[{"role": "user", "content": prompt}],
            system=system,
            timeout=90.0,  # was 30s — Kimi reasoning adds latency to title gen
        )
        action, title = _parse_title_json(response.text)
        if action == "keep":
            return None
        return title  # may be None if overlong / malformed
    except anthropic.APITimeoutError:
        logger.warning("Auto-title timed out")
    except anthropic.RateLimitError:
        logger.warning("Auto-title rate limited")
    except Exception as e:
        logger.warning("Auto-title failed", error=str(e))
    return None


async def _save_title_background(conversation_id: UUID, title: str, expected_prev_title: str | None = None):
    """Save generated title only if the current title matches what we expected (avoids overwriting manual renames)."""
    from db.database import AsyncPostgresSessionGideon
    sm = AsyncPostgresSessionGideon()
    bg_session = sm._session_factory()
    try:
        # Never overwrite a user-renamed title — title_locked is set by the rename endpoint.
        stmt = update(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.title_locked == False,
        )
        if expected_prev_title is not None:
            stmt = stmt.where(Conversation.title == expected_prev_title)
        else:
            stmt = stmt.where(Conversation.title.is_(None))
        result = await bg_session.execute(stmt.values(title=title))
        if result.rowcount > 0:
            await bg_session.commit()
        else:
            await bg_session.rollback()
    except Exception as e:
        logger.warning("Failed to save title", error=str(e))
    finally:
        await bg_session.close()


async def _generate_and_save_title(
    conv_id: UUID, msg: str,
    current_title: str | None = None,
    assistant_reply: str | None = None,
) -> None:
    """Generate a title and save it. Used as a fire-and-forget background task."""
    # Skip the LLM call entirely if the user has manually locked the title.
    from db.database import AsyncPostgresSessionGideon
    sm = AsyncPostgresSessionGideon()
    async with sm.get_session() as session:
        result = await session.execute(
            select(Conversation.title_locked).where(Conversation.id == conv_id)
        )
        locked = result.scalar_one_or_none()
    if locked:
        return
    title = await _generate_title(msg, current_title=current_title, assistant_reply=assistant_reply)
    if title:
        await _save_title_background(conv_id, title, expected_prev_title=current_title)


async def generate_suggested_reply(
    user_message: str,
    assistant_reply: str,
    user_first_name: str | None = None,
    user_id: UUID | None = None,
) -> str | None:
    """Predict a short follow-up the user is likely to send next.

    Returns 2-10 words, lowercased natural phrasing — e.g.
    "yes send it to the team" / "make it shorter" / "show me the data".
    Returns None only when the inputs are truly empty or the model refuses.
    """
    if not assistant_reply or len(assistant_reply.strip()) < 4:
        return None
    if _is_refusal_or_error_reply(assistant_reply):
        return None

    system = (
        "You predict the very next short reply a user is likely to type in a chat with an AI assistant. "
        "Output JSON only: {\"reply\": \"...\"}\n"
        "Always produce a reply — there is essentially always SOMETHING a user could say next. "
        "Only return {\"reply\": null} if the prior turn is literally empty/garbled.\n"
        "Rules for the reply text:\n"
        "- 2-10 words, lowercase, no trailing punctuation, no quotes\n"
        "- written from the USER's perspective, addressing the assistant\n"
        "- concrete and natural — what a real person would type\n"
        "- if the assistant proposed an action → confirm (\"yes send it to the team\", \"go ahead\")\n"
        "- if the assistant produced a draft / list / answer → ask a refinement (\"make it shorter\", \"only the top 3\", \"add more detail\")\n"
        "- if the assistant asked an open question (\"anything I can help with?\") → pick a plausible work task (\"summarize my unread emails\", \"what's on my calendar tomorrow\")\n"
        "- if the assistant gave info → ask the next obvious follow-up (\"why\", \"show me an example\", \"how do i fix it\")"
    )
    you = f" (user's name: {user_first_name})" if user_first_name else ""
    prompt = (
        f"User message{you}:\n{user_message[:600]}\n\n"
        f"Assistant reply:\n{assistant_reply[:1200]}"
    )

    try:
        from dataclasses import replace as _replace
        from services.llm import get_spec, llm
        sug_spec = _replace(get_spec("suggestions"), max_tokens=60)
        response = await llm.complete(
            sug_spec,
            messages=[{"role": "user", "content": prompt}],
            system=system,
            timeout=90.0,  # was 15s — fire-and-forget, but 15s reliably timed out on Kimi reasoning
        )
        raw = response.text
        # Don't log raw — it can contain conversation-derived plaintext/PII.
        # Length is enough to debug "model returned nothing" cases.
        logger.info("Suggested reply received", chars=len(raw))
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        reply = data.get("reply")
        if not reply or not isinstance(reply, str):
            return None
        reply = reply.strip().strip('"').strip("'")
        reply = re.sub(r"[.!?]+$", "", reply).strip()
        # Enforce the prompt's 2-10 word contract. Char-length alone lets a
        # 79-char single-word URL through; word-count rejects those cleanly.
        word_count = len(reply.split())
        if len(reply) < 2 or len(reply) > 80 or word_count < 2 or word_count > 10:
            return None
        try:
            from services.usage_service import log_usage
            if user_id is not None:
                usage_obj = getattr(response.raw, "usage", None) or response.usage
                log_usage(user_id, sug_spec.model, "suggested_reply", usage_obj)
        except Exception:
            pass
        return reply
    except anthropic.APITimeoutError:
        logger.warning("Suggested reply timed out")
    except anthropic.RateLimitError:
        logger.warning("Suggested reply rate limited")
    except Exception as e:
        logger.warning("Suggested reply failed", error=str(e))
    return None


async def _load_user_memories(user_id: UUID, query: str, session: AsyncSession) -> list[dict]:
    """Search mem0 for relevant memories. Falls back to DB keyword search if mem0 is unavailable."""
    # Try Graphiti first
    try:
        from services.graphiti_service import search_memories
        memories = await search_memories(str(user_id), query, limit=15)
        if memories:
            return [{"content": m["content"], "category": "semantic"} for m in memories]
    except Exception as e:
        logger.warning("mem0 search failed, falling back to DB", error=str(e))

    # Fallback: load from DB (old keyword-based approach)
    try:
        result = await session.execute(
            select(Memory).where(
                Memory.user_id == user_id,
                Memory.is_active == True,
            ).order_by(Memory.updated_at.desc()).limit(50)
        )
        memories = result.scalars().all()
        return [{"content": m.content, "category": m.category} for m in memories]
    except Exception as e:
        logger.warning("Failed to load user memories from DB", error=str(e))
        return []


async def _load_recent_activities(user_id: UUID, session: AsyncSession) -> list[dict]:
    """Load the user's activity log entries from the last 24 hours for prompt context."""
    try:
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        result = await session.execute(
            select(ActivityLog).where(
                ActivityLog.user_id == user_id,
                ActivityLog.created_at >= cutoff,
            ).order_by(ActivityLog.created_at.desc()).limit(20)
        )
        activities = result.scalars().all()
        return [
            {"source": a.source, "title": a.title}
            for a in activities
        ]
    except Exception as e:
        logger.warning("Failed to load recent activities", error=str(e))
        return []


async def _extract_memories_background(
    user_id: UUID,
    user_message: str,
    assistant_message: str,
    user_name: str | None = None,
) -> None:
    """Auto-extract memories from the conversation turn (runs in background).
    Pass user_name so Graphiti can rewrite generic "User" subjects to the
    real name and steer the extractor toward typed entities."""
    try:
        from services.graphiti_service import add_memories_from_conversation
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": assistant_message},
        ]
        await add_memories_from_conversation(str(user_id), messages, user_name=user_name)
    except Exception as e:
        logger.warning("mem0 auto-extraction failed", error=str(e))


async def _build_file_context(session, user_id: UUID, file_ids: list[str]) -> tuple[str, list[dict]]:
    """Build file context for Claude. Returns (text_context, image_blocks).
    Images are returned as content blocks for inline vision. Text files as metadata."""
    try:
        result = await session.execute(
            select(UploadedFile).where(
                UploadedFile.id.in_(file_ids),
                UploadedFile.user_id == user_id,
            )
        )
        files = list(result.scalars().all())
        if not files:
            return "", []

        lines = []
        image_blocks: list[dict] = []
        for f in files:
            size_kb = f.size_bytes / 1024
            if f.is_image and f.image_base64:
                # Strip data URI prefix if present (e.g. "data:image/png;base64,")
                raw_b64 = f.image_base64
                if raw_b64.startswith("data:"):
                    raw_b64 = raw_b64.split(",", 1)[1] if "," in raw_b64 else raw_b64
                # Inject image directly into conversation for Claude vision
                image_blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": f.mime_type or "image/png", "data": raw_b64},
                })
                lines.append(f"- {f.filename} ({size_kb:.0f} KB, image) — included above for visual analysis.")
            elif f.is_image:
                lines.append(f"- {f.filename} ({size_kb:.0f} KB, image) — file_id: {f.id}. Use `read_uploaded_file` to view the image.")
            else:
                token_est = len(f.extracted_text) // 4 if f.extracted_text else 0
                lines.append(f"- {f.filename} ({size_kb:.0f} KB, ~{token_est:,} tokens) — file_id: {f.id}. Use `read_uploaded_file` to read the content.")
        text_context = "[Uploaded Files]\n" + "\n".join(lines) if lines else ""
        return text_context, image_blocks
    except Exception as e:
        logger.warning("Failed to build file context", error=str(e))
        return "", []


_VALID_MENTION_CATEGORIES = {
    "collaborator", "tool", "gmail", "linear", "notion", "google_calendar",
    "slack", "google_drive", "google_docs", "google_sheets", "google_slides",
}


def _sanitize_mention_text(text: str | None, max_len: int = 200) -> str:
    """Strip control characters and limit length to prevent prompt injection."""
    if not text:
        return ""
    # Remove characters that could break prompt formatting
    cleaned = text.replace("\n", " ").replace("\r", " ").replace("\t", " ")
    # Remove sequences that look like prompt delimiters
    for delimiter in ["##", "```", "---", "===", "<|", "|>"]:
        cleaned = cleaned.replace(delimiter, "")
    return cleaned[:max_len].strip()


def _format_mention_metadata(mentions: list[dict]) -> str:
    """Format mention metadata with tool hints so Claude knows how to fetch content."""
    if not mentions:
        return ""
    lines = []
    for m in mentions:
        category = m.get("category", "")
        if category not in _VALID_MENTION_CATEGORIES:
            continue  # Skip unknown categories

        label = _sanitize_mention_text(m.get("label", ""))
        mention_id = _sanitize_mention_text(m.get("id", ""), max_len=100)
        url = _sanitize_mention_text(m.get("url", ""), max_len=300)

        if not label and not mention_id:
            continue

        if category == "collaborator":
            lines.append(f"- **Person**: {label} (email: {mention_id})")
        elif category == "tool":
            lines.append(f"- **Use tool**: `{label}`")
        elif category == "gmail":
            lines.append(f"- **Email**: \"{label}\" — use `read_email` with email_id=\"{mention_id}\" to get the full content")
        elif category == "linear":
            lines.append(f"- **Linear Issue**: {label} — use `get_linear_issue` with issue_id=\"{mention_id}\" to get details")
        elif category == "notion":
            lines.append(f"- **Notion Page**: {label} — use `read_notion_page` with page_url=\"{url}\" to get content")
        elif category == "google_calendar":
            lines.append(f"- **Calendar Event**: {label} (id: {mention_id})")
        elif category == "slack":
            lines.append(f"- **Slack Message**: {label} — permalink: {url}")
        elif category.startswith("google_"):
            doc_type = category.replace("google_", "Google ").title()
            lines.append(f"- **{doc_type}**: {label} — use `get_drive_file` with file_id=\"{mention_id}\" to get details")
    return "\n".join(lines)


# Background producer tasks. Strong refs required so asyncio doesn't GC them
# mid-execution (see CLAUDE.md ASYNC300). Tasks self-remove via done callback.
_chat_producer_tasks: set[asyncio.Task] = set()


async def _drive_chat_producer(
    turn_id: str,
    conv_id: UUID,
    user_id: UUID,
    user: User,
    message: str,
    conversation_id: str,
    auth_token: str,
    mentions: list[dict] | None,
    resume: bool,
    file_ids: list[str] | None,
    auto_mode: bool,
) -> None:
    """Drive the inner turn generator, republish every frame into Redis, keep
    the alive/TTL heartbeat running, and clear the conversation's
    `active_turn_id` when finished. Runs as a detached task so the client
    disconnecting doesn't cancel it."""
    stop_event = asyncio.Event()
    hb_task = asyncio.create_task(heartbeat_loop(turn_id, stop_event))
    sent_sentinel = False
    try:
        async for frame in _run_chat_turn(
            user_id=user_id,
            user=user,
            message=message,
            conversation_id=conversation_id,
            auth_token=auth_token,
            mentions=mentions,
            resume=resume,
            file_ids=file_ids,
            auto_mode=auto_mode,
            turn_id=turn_id,
        ):
            await publish_frame(turn_id, frame)
            if frame_sentinel_type(frame):
                sent_sentinel = True
    except asyncio.CancelledError:
        logger.info("chat producer cancelled", turn_id=turn_id)
        await publish_frame(
            turn_id,
            f"data: {json.dumps({'type': 'aborted', 'message': 'Stream was cancelled.'})}\n\n",
        )
        sent_sentinel = True
        raise
    except Exception as e:
        logger.error("chat producer failed", turn_id=turn_id, err=str(e))
        await publish_frame(
            turn_id,
            f"data: {json.dumps({'type': 'error', 'message': 'Stream failed.'})}\n\n",
        )
        sent_sentinel = True
    finally:
        if not sent_sentinel:
            await publish_frame(
                turn_id,
                f"data: {json.dumps({'type': 'done', 'conversation_id': str(conv_id)})}\n\n",
            )
        stop_event.set()
        try:
            await hb_task
        except Exception:
            pass
        await finalize_stream(turn_id)
        await clear_abort_flag(turn_id)
        try:
            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as s:
                await s.execute(
                    update(Conversation)
                    .where(
                        Conversation.id == conv_id,
                        Conversation.active_turn_id == UUID(turn_id),
                    )
                    .values(active_turn_id=None)
                )
                await s.commit()
        except Exception as e:
            logger.warning(
                "failed to clear active_turn_id",
                conv_id=str(conv_id),
                turn_id=turn_id,
                err=str(e),
            )


async def stream_chat(
    user_id: UUID,
    user: User,
    message: str,
    conversation_id: str | None,
    auth_token: str = "",
    mentions: list[dict] | None = None,
    resume: bool = False,
    file_ids: list[str] | None = None,
    auto_mode: bool = False,
) -> AsyncGenerator[str, None]:
    """Public entry for `/chat/send`. Allocates a turn_id, spawns a background
    producer task, and returns a tailer over the Redis stream. The producer
    keeps running even if the HTTP connection is dropped — closing the tab
    mid-stream no longer aborts Claude. The client reattaches by calling
    `/chat/resume/{turn_id}` (phase 3)."""
    sm = AsyncPostgresSessionGideon()
    turn_id: str | None = None
    conv_id: UUID | None = None

    try:
        async with sm.get_session() as setup_session:
            conv = await get_or_create_conversation(setup_session, user_id, conversation_id)
            conv_id = conv.id

            # Concurrent-send guard (defense-in-depth; the UI already blocks
            # this). If there's a live producer, surface the active turn_id so
            # the client can attach instead of spawning a second turn.
            if conv.active_turn_id:
                existing_turn = str(conv.active_turn_id)
                if await is_producer_alive(existing_turn):
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "busy",
                                "turn_id": existing_turn,
                                "conversation_id": str(conv.id),
                                "message": "A turn is already running for this conversation.",
                            }
                        )
                        + "\n\n"
                    )
                    return
                # Stale flag (pod crashed / alive key expired) — clear and
                # proceed. Previous message rows stay as-is.
                conv.active_turn_id = None
                await setup_session.commit()

            turn_id = str(uuid4())
            conv.active_turn_id = UUID(turn_id)
            await setup_session.commit()
    except Exception as e:
        logger.error("stream_chat setup failed", err=str(e))
        yield f"data: {json.dumps({'type': 'error', 'message': 'Failed to start turn.'})}\n\n"
        return

    await init_stream(turn_id)
    await set_turn_meta(turn_id, str(user_id), str(conv_id))
    # First real frame tells the client which turn to attach to for abort /
    # multi-tab resume. Published before the producer task so even an
    # immediate reconnect sees it at entry id 1.
    await publish_frame(
        turn_id,
        f"data: {json.dumps({'type': 'turn_started', 'turn_id': turn_id, 'conversation_id': str(conv_id)})}\n\n",
    )

    producer = asyncio.create_task(
        _drive_chat_producer(
            turn_id=turn_id,
            conv_id=conv_id,
            user_id=user_id,
            user=user,
            message=message,
            conversation_id=str(conv_id),
            auth_token=auth_token,
            mentions=mentions,
            resume=resume,
            file_ids=file_ids,
            auto_mode=auto_mode,
        )
    )
    _chat_producer_tasks.add(producer)
    producer.add_done_callback(_chat_producer_tasks.discard)

    try:
        async for frame in tail_stream(turn_id, last_id="0"):
            yield frame
    except asyncio.CancelledError:
        # Client went away. Tailer stops; producer keeps running.
        logger.info("chat tailer cancelled (client disconnect)", turn_id=turn_id)
        raise


async def _run_chat_turn(
    user_id: UUID,
    user: User,
    message: str,
    conversation_id: str | None,
    auth_token: str = "",
    mentions: list[dict] | None = None,
    resume: bool = False,
    file_ids: list[str] | None = None,
    auto_mode: bool = False,
    turn_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """
    Main chat loop with SSE streaming.
    Manages its own DB session with explicit commits so data is visible
    to other endpoints immediately.

    Routes by the registered ``chat`` spec provider:
      - anthropic (default): native code path below, with advisor +
        code_execution + cache + thinking blocks.
      - openai / kimi: delegates to `_run_chat_turn_via_wrapper`, which
        streams via the provider-neutral wrapper (OpenAI Responses API for
        openai with code_interpreter+reasoning, Chat Completions for kimi
        with the Anthropic-sandbox bridge wired as `execute_python`).

    When resume=True, skips user message creation — responds to existing
    messages in the conversation (used by assign-to-Gideon flow).

    Yielded SSE frames are consumed by `_drive_chat_producer` and forwarded
    to Redis. Client disconnect no longer cancels this generator; only
    producer-level events (explicit abort, pod shutdown) do.
    """
    # Provider-aware routing: hand off to the wrapper-based path for
    # non-Anthropic providers. The Anthropic native path below is unchanged.
    from services.llm import get_spec as _get_llm_spec
    _chat_spec = _get_llm_spec("chat")
    if _chat_spec.provider != "anthropic":
        async for frame in _run_chat_turn_via_wrapper(
            user_id=user_id,
            user=user,
            message=message,
            conversation_id=conversation_id,
            auth_token=auth_token,
            mentions=mentions,
            resume=resume,
            file_ids=file_ids,
            auto_mode=auto_mode,
            turn_id=turn_id,
        ):
            yield frame
        return

    sm = AsyncPostgresSessionGideon()
    session = sm._session_factory()
    conv = None
    accumulated_text = ""
    accumulated_thinking = ""
    accumulated_tool_calls: list[dict] = []

    try:
        # Get or create conversation
        conv = await get_or_create_conversation(session, user_id, conversation_id)
        prev_conv_updated_at = None  # set in non-resume path before user message is stored

        if not resume:
            # Build mentions payload with file metadata if files attached
            msg_mentions = None
            if file_ids:
                file_result = await session.execute(
                    select(UploadedFile.id, UploadedFile.filename).where(
                        UploadedFile.id.in_(file_ids),
                        UploadedFile.user_id == user_id,
                    )
                )
                files_data = [
                    {"filename": row.filename, "ext": row.filename.rsplit(".", 1)[-1].upper() if "." in row.filename else ""}
                    for row in file_result.all()
                ]
                if files_data:
                    msg_mentions = {"files": files_data}

            # Capture the most recent message time (not conv.updated_at — that column
            # is never refreshed by new messages, so using it would treat every old
            # conversation as "resumed after a long pause" and retitle on every turn).
            last_msg_result = await session.execute(
                select(Message.created_at)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            prev_last_message_at = last_msg_result.scalar_one_or_none()

            # Store user message and commit immediately
            await _store_message(session, conv.id, "user", content=message, mentions=msg_mentions)
            await session.commit()

            track_chat_sent(
                str(user_id),
                conversation_id=str(conv.id),
                is_new=(conversation_id is None),
                has_mentions=bool(mentions),
                has_files=bool(file_ids),
            )

            # Count user messages in this conversation for title regeneration schedule
            user_msg_count_result = await session.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conv.id,
                    Message.role == "user",
                )
            )
            user_msg_count = user_msg_count_result.scalar() or 0

            # First message: generate title then yield conversation to frontend
            if user_msg_count == 1 and not conv.title:
                early_title = await _generate_title(message)
                if early_title:
                    await _save_title_background(conv.id, early_title, expected_prev_title=None)
                    conv.title = early_title
                yield f"data: {json.dumps({'type': 'conversation_created', 'conversation_id': str(conv.id), 'title': conv.title or message[:50]})}\n\n"

            # Auto-trigger smart onboarding if not yet done
            if not user.background_generated and user.id not in _background_research_in_flight:
                from services.smart_onboarding_service import run_smart_onboarding

                _background_research_in_flight.add(user.id)

                async def _run_smart_onboarding():
                    try:
                        await run_smart_onboarding(user.id)
                    finally:
                        _background_research_in_flight.discard(user.id)

                _fire_and_forget(_run_smart_onboarding())

        # When resuming, resolve the last user message + count for title schedule
        if resume:
            if not message:
                last_msg_result = await session.execute(
                    select(Message)
                    .where(Message.conversation_id == conv.id, Message.role == "user")
                    .order_by(Message.created_at.desc())
                    .limit(1)
                )
                last_msg = last_msg_result.scalar_one_or_none()
                if last_msg:
                    message = last_msg.content or ""
            count_result = await session.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conv.id,
                    Message.role == "user",
                )
            )
            user_msg_count = count_result.scalar() or 0

        # Load tools for this user
        tools = await get_tools_for_user(user_id, session)
        claude_tools = [t.to_claude_schema() for t in tools]

        # Exa web search is registered as a regular tool in tool_registry
        # (no server-side web_search needed)

        # Load completed skill memories for this user
        skill_memories = []
        try:
            skill_result = await session.execute(
                sa_select(Skill).where(
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
            logger.warning("Failed to load skill memories", error=str(e))

        # Load relevant memories via mem0 semantic search (falls back to DB)
        user_memories = await _load_user_memories(user_id, message, session)

        # Load recent activities for context injection
        recent_activity = await _load_recent_activities(user_id, session)

        # Build system prompt
        # Format mention metadata for system prompt (Claude will tool-call if it needs content)
        mention_context = _format_mention_metadata(mentions) if mentions else None

        slack_user_id = await get_user_slack_id(session, user_id)

        system_prompt = build_system_prompt(
            persona_text=user.persona_text,
            user_name=user.name,
            user_email=user.email,
            slack_user_id=slack_user_id,
            connected_tools=tools,
            skill_memories=skill_memories if skill_memories else None,
            memories=user_memories if user_memories else None,
            background_info=user.background_info,
            user_timezone=user.timezone,
            preferred_communication=user.preferred_communication,
            recent_activity=recent_activity if recent_activity else None,
            mention_context=mention_context,
            auto_mode=auto_mode,
        )

        # Slash command expansion. If this turn's user message starts with a
        # recognized `/cmd`, append the command's prompt template as a
        # turn-scoped system-prompt addon. User-defined commands (from
        # `custom_slash_commands`) override built-ins of the same slug. The
        # user's raw message is left untouched so the conversation history
        # shows what they invoked, and so existing skills (Email Tone etc.)
        # still apply on top.
        slash_addon = await expand_slash_command(message, user_id, session)
        if slash_addon:
            system_prompt = system_prompt + slash_addon

        # Survey-builder context. If this conversation is linked to a survey
        # via `surveys.builder_conversation_id`, we're inside the Surveys page
        # builder chat. Inject the active slug so Gideon edits THAT survey
        # instead of inventing a new one when the admin says "add a question"
        # or "activate it".
        from db.models import Survey as _Survey
        _builder_row = await session.execute(
            select(_Survey.slug, _Survey.status).where(_Survey.builder_conversation_id == conv.id)
        )
        _builder = _builder_row.first()
        if _builder:
            _builder_slug, _builder_status = _builder
            system_prompt = system_prompt + (
                "\n\n## Survey Builder Context\n"
                f"You are inside the Surveys page builder for the survey with slug=`{_builder_slug}` "
                f"(currently status=`{_builder_status}`). EVERY call to a survey tool "
                "(`draft_survey`, `update_survey_draft`, `activate_survey`, `close_survey`, "
                "`analyze_survey_responses`) in this conversation MUST use this exact slug. "
                "DO NOT call `draft_survey` again — the survey already exists; use `update_survey_draft` "
                "to add or change questions, eligibility, title, or intro. Only call `draft_survey` if "
                "the admin EXPLICITLY says they want to create a separate, additional survey.\n\n"
                "The admin can already see the survey in the Form / Targeting / Responses tabs of this "
                "page. Do NOT re-render the survey as markdown in chat after editing it — just confirm "
                "what changed and point to the Form tab if they want to see the rendered form, or the "
                "Preview button for the exact respondent view."
            )

        # Pre-turn safety net: fold an oversized tail down BEFORE loading, so this
        # turn fits and stays fast even if the previous turn's post-turn compaction
        # never ran (stalled turn, or the Kimi wrapper path which lacked it). Emits
        # a transient "Compacting Memory" indicator while it works.
        async for _frame in _preturn_compact_frames(session, conv.id):
            yield _frame

        # Load conversation history. Compaction (if needed) happens at END of the
        # PREVIOUS turn, so any summary is already in place by the time we load.
        history = await _load_history(session, conv.id)

        # Inject uploaded files into the last user message
        # Images are injected as vision content blocks (Claude sees them directly)
        # Text files are described as metadata (Claude reads via tool call)
        if file_ids:
            file_context, image_blocks = await _build_file_context(session, user_id, file_ids)
            if (file_context or image_blocks) and history:
                for i in range(len(history) - 1, -1, -1):
                    if history[i].get("role") == "user":
                        # Convert string content to content blocks if we have images
                        if image_blocks:
                            existing = history[i].get("content", "")
                            if isinstance(existing, str):
                                content_blocks = [{"type": "text", "text": existing}]
                            else:
                                content_blocks = list(existing)
                            content_blocks.extend(image_blocks)
                            if file_context:
                                content_blocks.append({"type": "text", "text": file_context})
                            history[i]["content"] = content_blocks
                        elif file_context and isinstance(history[i].get("content"), str):
                            history[i]["content"] += "\n\n" + file_context
                        break

        # The main chat streaming loop is intentionally Anthropic-only.
        # It relies on features the OpenAI/Kimi adapters cannot model
        # without lossy translation:
        #   - `server_tool_use` + `advisor_tool_result` event types (advisor)
        #   - `code_execution_tool_result` event type + container continuity
        #   - `cache_control: ephemeral` placement on system + tool blocks
        #   - `thinking: {type: adaptive, display: summarized}` enum form
        #   - File-artifact retrieval via client.beta.files.* (see
        #     _download_code_exec_artifacts above)
        # `services/llm` is used for every other feature (workflows, trigger
        # agents, suggestions, drafts, prep, onboarding, surveys, etc) and
        # exposes the same provider-spec abstraction — chat itself stays
        # native here so the advanced features keep working as-is.
        client = get_anthropic_client(timeout=180.0)

        # Turn-scoped cache for recurring-event master lookups in calendar_tool.
        # If `list_events` runs twice in this turn, both calls share one fetch
        # per master event ID. Setting the ContextVar here scopes the cache
        # to this single chat turn — the next chat turn gets a fresh dict.
        RECURRING_MASTER_CACHE.set({})
        # Scope the user's timezone to this turn so Slack read tools can resolve
        # human date/relative-word windows ('yesterday', '2026-07-10') correctly.
        SLACK_USER_TZ.set(user.timezone)

        tool_round = 0
        accumulated_text = ""
        accumulated_thinking = ""
        accumulated_tool_calls = []
        accumulated_advisor_blocks = []
        # code_execution blocks captured from final_message.content across tool rounds
        accumulated_code_exec_blocks: list[dict] = []
        # Drive-uploaded files generated by the code_execution sandbox this turn.
        # Persisted alongside tool_calls so the chip rehydrates on reload.
        accumulated_code_files: list[dict] = []

        # Per-/chat/send tool-call governance.
        # Caps the model from spamming raw analytics SQL after a Grafana panel
        # already returned data — past incidents had Claude run 26 query_analytics
        # calls in 4 minutes after one successful query_grafana_panel.
        _tool_call_counts: dict[str, int] = {}
        _grafana_panel_succeeded = False
        QUERY_ANALYTICS_HARD_CAP = 5
        QUERY_ANALYTICS_CAP_AFTER_GRAFANA = 1

        while tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1

            # Cooperative abort check: user (or another tab) hit /chat/abort.
            # Checked between rounds AND inside the streaming inner loop on a
            # short cadence — without the in-stream check, a long thinking
            # phase (no tool calls, just thinking_delta events) would ignore
            # the flag until thinking finished, leaving the producer running
            # for minutes after the UI showed "stopped".
            if turn_id and await is_aborted(turn_id):
                logger.info("chat turn aborted via flag", turn_id=turn_id, tool_round=tool_round)
                raise asyncio.CancelledError()

            # Container continuity: if the conv has a live container, pass it so
            # Claude resumes the existing Python sandbox (files, variables preserved).
            # If expired/absent, strip stale code_execution blocks from history —
            # replaying them without a container_id causes 400s.
            container_alive = (
                conv.code_exec_container_id
                and conv.code_exec_container_expires_at
                and conv.code_exec_container_expires_at > datetime.now(timezone.utc)
            )
            if not container_alive:
                _strip_code_exec_blocks(history)

            # Call Claude with streaming + prompt caching + extended thinking
            create_params = {
                "model": MODEL_CHAT,
                "max_tokens": CHAT_MAX_OUTPUT_TOKENS,
                "system": [
                    {
                        "type": "text",
                        "text": system_prompt,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                "messages": history,
                # `adaptive` is Anthropic's recommended thinking mode for Opus 4.6
                # / 4.7 / Sonnet 4.6 — fixed `budget_tokens` is deprecated/rejected
                # on those models. Latency is steered via `output_config.effort`.
                # `display: summarized` is Opus 4.6's default but Opus 4.7 defaults
                # to "omitted" (empty thinking text). Setting it explicitly keeps
                # the chat UI's thinking blocks readable across both models.
                "thinking": {"type": "adaptive", "display": "summarized"},
                "output_config": {"effort": CHAT_EFFORT},
            }
            if container_alive:
                create_params["container"] = {"id": conv.code_exec_container_id}

            # Compose the full tool list: user-connected tools + server-side tools
            # (advisor for Sonnet+Opus pairing, code_execution for the Python sandbox).
            server_tools: list[dict] = [CODE_EXECUTION_TOOL]
            if ADVISOR_ENABLED:
                server_tools.append(ADVISOR_TOOL)
            create_params["tools"] = claude_tools + server_tools if claude_tools else server_tools

            # Bound the request under Sonnet's context window (request-only
            # copy — `history` and the DB rows keep full fidelity). No-op
            # unless the approx size exceeds the anthropic ceiling.
            create_params["messages"] = _bound_chat_request_messages(
                history,
                system_prompt,
                create_params["tools"],
                provider="anthropic",
                tool_round=tool_round,
            )

            try:
                # code_execution always needs its beta. Advisor adds its own when enabled.
                betas = [CODE_EXECUTION_BETA]
                if ADVISOR_ENABLED:
                    betas.append(ADVISOR_BETA)
                # Close out any open implicit transaction on `session` BEFORE
                # awaiting Anthropic. With prompt caching warm + a long history
                # the stream can run >30s; Postgres's
                # `idle_in_transaction_session_timeout` (30s, set on every
                # connection — see CONTRIBUTING.md database conventions) will otherwise
                # terminate the connection mid-stream and the assistant +
                # tool_result writes that follow would roll back with no row
                # ever persisted. After commit, the next `session.execute()`
                # transparently opens a fresh transaction.
                # This is the chat_service half of CLAUDE.md rule #3 ("No
                # external integration points inside an open DB session").
                await session.commit()
                # The Anthropic client is cached, so the timeout from
                # `get_anthropic_client(...)` only applies to the first caller.
                # Extended-thinking turns + tool loops can run several minutes;
                # override per-request so the connection isn't dropped mid-stream.
                stream_ctx = client.with_options(timeout=900.0).beta.messages.stream(
                    **create_params, betas=betas,
                )

                async with stream_ctx as stream:
                    current_tool_use = None
                    tool_input_json = ""
                    last_abort_check = time.monotonic()

                    async for event in _events_with_heartbeat(stream):
                        # Mid-stream abort check (~every 2s) so a Stop click during
                        # the thinking phase actually halts the producer instead of
                        # waiting for thinking to finish + a between-rounds check.
                        if turn_id and (time.monotonic() - last_abort_check) >= 2.0:
                            last_abort_check = time.monotonic()
                            if await is_aborted(turn_id):
                                logger.info("chat turn aborted mid-stream", turn_id=turn_id, tool_round=tool_round)
                                raise asyncio.CancelledError()
                        if event is _STREAM_HEARTBEAT:
                            # SSE comment line — invisible to the frontend parser but
                            # counts as downstream bytes to proxies/browsers, keeping
                            # the connection alive during Claude's pre-token phase.
                            yield ": keepalive\n\n"
                            continue
                        if event.type == "content_block_start":
                            if event.content_block.type == "thinking":
                                yield f"data: {json.dumps({'type': 'thinking_start'})}\n\n"
                            elif event.content_block.type == "tool_use":
                                current_tool_use = {
                                    "id": event.content_block.id,
                                    "name": event.content_block.name,
                                    "input": {},
                                }
                                tool_input_json = ""
                                yield f"data: {json.dumps({'type': 'tool_start', 'tool': event.content_block.name, 'tool_call_id': event.content_block.id})}\n\n"
                            elif event.content_block.type == "server_tool_use":
                                if event.content_block.name == "advisor":
                                    accumulated_advisor_blocks.append({
                                        "type": "server_tool_use",
                                        "id": event.content_block.id,
                                        "name": "advisor",
                                        "input": {},
                                    })
                                    yield f"data: {json.dumps({'type': 'thinking_start'})}\n\n"
                                else:
                                    yield f"data: {json.dumps({'type': 'tool_start', 'tool': event.content_block.name, 'tool_call_id': event.content_block.id})}\n\n"
                            elif event.content_block.type == "advisor_tool_result":
                                # Content MUST carry a `type` field on replay or Anthropic
                                # rejects the next turn with a 400 and wedges the whole
                                # conversation. Fall back to a valid-shape stub if the
                                # stream emits an unrecognized/empty content object.
                                content_dict: dict[str, Any] = {"type": "advisor_result", "text": ""}
                                cb_content = event.content_block.content
                                try:
                                    if cb_content.type == "advisor_result":
                                        content_dict = {"type": "advisor_result", "text": cb_content.text or ""}
                                    elif cb_content.type == "advisor_redacted_result":
                                        content_dict = {"type": "advisor_redacted_result", "encrypted_content": cb_content.encrypted_content}
                                except AttributeError:
                                    pass
                                accumulated_advisor_blocks.append({
                                    "type": "advisor_tool_result",
                                    "tool_use_id": event.content_block.tool_use_id,
                                    "content": content_dict,
                                })
                            elif event.content_block.type == "web_search_tool_result":
                                yield f"data: {json.dumps({'type': 'tool_result', 'tool': 'web_search', 'tool_call_id': event.content_block.tool_use_id, 'summary': 'Search complete'})}\n\n"
                            elif event.content_block.type in ("code_execution_tool_result", "bash_code_execution_tool_result"):
                                file_ids = _extract_file_ids_from_content_block(event.content_block)
                                yield f"data: {json.dumps({'type': 'tool_result', 'tool': 'code_execution', 'tool_call_id': event.content_block.tool_use_id, 'summary': 'Code executed'})}\n\n"
                                if file_ids:
                                    images, code_files = await _download_code_exec_artifacts(file_ids, user_id)
                                    for img in images:
                                        yield f"data: {json.dumps({'type': 'code_image', 'data': img['data'], 'mime': img['mime'], 'filename': img['filename']})}\n\n"
                                    for cf in code_files:
                                        # Track for assistant-message persistence so the chip
                                        # rehydrates on reload via syncDbMessages.
                                        accumulated_code_files.append(cf)
                                        yield f"data: {json.dumps({'type': 'code_file', 'filename': cf['filename'], 'mime': cf['mime'], 'drive_url': cf['drive_url'], 'drive_file_id': cf['drive_file_id'], 'size_bytes': cf['size_bytes']})}\n\n"

                        elif event.type == "content_block_delta":
                            if event.delta.type == "thinking_delta":
                                accumulated_thinking += event.delta.thinking
                                yield f"data: {json.dumps({'type': 'thinking_delta', 'content': event.delta.thinking})}\n\n"
                            elif event.delta.type == "text_delta":
                                accumulated_text += event.delta.text
                                yield f"data: {json.dumps({'type': 'text_delta', 'content': event.delta.text})}\n\n"
                            elif event.delta.type == "input_json_delta":
                                tool_input_json += event.delta.partial_json

                        elif event.type == "content_block_stop":
                            if current_tool_use:
                                try:
                                    current_tool_use["input"] = json.loads(tool_input_json) if tool_input_json else {}
                                except json.JSONDecodeError:
                                    current_tool_use["input"] = {}
                                accumulated_tool_calls.append(current_tool_use)
                                current_tool_use = None
                                tool_input_json = ""

                    # Get final message for stop reason
                    final_message = await stream.get_final_message()

                # Capture the container id + expiry so the next turn can resume.
                # Anthropic returns `container` on any message where code_execution ran.
                container_obj = getattr(final_message, "container", None)
                if container_obj:
                    new_id = getattr(container_obj, "id", None)
                    new_expires = _parse_anthropic_datetime(getattr(container_obj, "expires_at", None))
                    if new_id:
                        conv.code_exec_container_id = new_id
                        conv.code_exec_container_expires_at = new_expires

                # Pull code_execution server_tool_use + result blocks from the
                # response so they're persisted in the assistant message below.
                # Needed so replay on future turns includes the full code trail.
                accumulated_code_exec_blocks.extend(_extract_code_exec_blocks(final_message))

            except anthropic.APITimeoutError:
                logger.error("Claude API timeout")
                track_error(str(user_id), "claude_timeout", user_email=user.email, detail=f"model={MODEL_CHAT}, tool_round={tool_round}")
                yield f"data: {json.dumps({'type': 'error', 'message': 'Response timed out. Please try again.'})}\n\n"
                return
            except anthropic.RateLimitError as e:
                logger.error("Claude rate limited", request_id=getattr(e, '_request_id', None))
                track_error(str(user_id), "claude_rate_limit", user_email=user.email, detail=str(e))
                yield f"data: {json.dumps({'type': 'error', 'message': 'Rate limited. Please wait a moment and try again.'})}\n\n"
                return
            except anthropic.APIStatusError as e:
                error_type = e.body.get("error", {}).get("type", "unknown") if isinstance(e.body, dict) else e.__class__.__name__
                error_message = e.body.get("error", {}).get("message", "") if isinstance(e.body, dict) else str(e)
                # Stale code_execution container: clear it, strip stale blocks from
                # history, retry this round once with a fresh container.
                if e.status_code == 400 and "container" in (error_message or "").lower() and container_alive:
                    logger.warning(
                        "Code execution container stale — clearing and retrying round",
                        conversation_id=str(conv.id),
                        error_message=error_message[:200],
                    )
                    conv.code_exec_container_id = None
                    conv.code_exec_container_expires_at = None
                    _strip_code_exec_blocks(history)
                    tool_round -= 1  # retry same round
                    continue
                logger.error("Claude API error", status=e.status_code, error_type=error_type, error_message=error_message, request_id=getattr(e.response, 'headers', {}).get('request-id'))
                track_error(str(user_id), "claude_api_error", user_email=user.email, detail=f"status={e.status_code}, type={error_type}")
                # Surface upstream provider outages explicitly. Anthropic returns
                # `529` + `error.type == "overloaded_error"` during incidents;
                # showing "something went wrong" makes it look like a Gideon bug
                # when it's an Anthropic-side problem the user can verify on
                # https://status.claude.com.
                is_overloaded = e.status_code == 529 or error_type == "overloaded_error"
                if is_overloaded:
                    yield f"data: {json.dumps({'type': 'error', 'code': 'provider_overloaded', 'message': 'Claude is overloaded right now. This is on Anthropic, not Gideon. Try again in a minute.'})}\n\n"
                    return
                msg = "Rate limit exceeded. Please wait a moment." if e.status_code == 429 else "Sorry, something went wrong. Please try again."
                yield f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
                return
            except Exception as e:
                logger.error("Claude API error", error=str(e))
                track_error(str(user_id), "claude_unknown_error", user_email=user.email, detail=str(e))
                # Catch overloaded that surfaced as a generic Exception (e.g.
                # raised from inside the streaming context with a stringified body).
                err_str = str(e).lower()
                if "overloaded_error" in err_str or "529" in err_str or "overloaded" in err_str:
                    yield f"data: {json.dumps({'type': 'error', 'code': 'provider_overloaded', 'message': 'Claude is overloaded right now. This is on Anthropic, not Gideon. Try again in a minute.'})}\n\n"
                    return
                yield f"data: {json.dumps({'type': 'error', 'message': 'Sorry, something went wrong. Please try again.'})}\n\n"
                return

            # pause_turn: server-side tool (advisor) executed — continue loop with full content
            if final_message.stop_reason == "pause_turn":
                history.append({"role": "assistant", "content": final_message.content})
                accumulated_text = ""
                accumulated_thinking = ""
                accumulated_tool_calls = []
                # Keep accumulated_advisor_blocks AND accumulated_code_files —
                # both need to be stored when the turn ends.
                continue

            # Merge advisor blocks into tool_calls for DB storage (backward-compatible).
            # Persist advisor + code_execution blocks alongside client tool_uses so
            # replay reconstructs the full assistant message Claude saw.
            #
            # ORDER MATTERS for Anthropic replay. Server-side blocks (advisor +
            # code_execution) must come BEFORE the client `tool_use`, mirroring
            # Claude's natural "think-then-act" stream order. If the client
            # tool_use is followed by `server_tool_use` / `advisor_tool_result`
            # in the same assistant message, Anthropic rejects the next replay
            # with `messages.X: tool_use ids were found without tool_result
            # blocks immediately after` — even though the tool_result IS in
            # the next user message. Putting server-side first keeps the
            # client tool_use as the last tool-related block, matching the
            # stream order the model actually produced.
            # `code_artifact` entries are FE-only metadata — they let the
            # frontend rebuild the download chip on reload via syncDbMessages.
            # `_load_history` filters them back out when replaying to Claude.
            code_artifact_blocks = [{"type": "code_artifact", **f} for f in accumulated_code_files]
            all_tool_data = accumulated_advisor_blocks + accumulated_code_exec_blocks + accumulated_tool_calls + code_artifact_blocks

            # If Claude stopped with end_turn (no tool calls), we're done
            if final_message.stop_reason == "end_turn" or not accumulated_tool_calls:
                stored_msg = await _store_message(
                    session, conv.id, "assistant",
                    content=accumulated_text or None,
                    tool_calls=all_tool_data or None,
                    model=MODEL_CHAT,
                    tokens_used=final_message.usage.output_tokens if final_message.usage else None,
                    thinking=accumulated_thinking or None,
                )
                await session.commit()

                # Yield done immediately so the frontend gets the link without waiting for title
                yield f"data: {json.dumps({'type': 'done', 'message_id': str(stored_msg.id), 'conversation_id': str(conv.id), 'title': conv.title})}\n\n"

                # Log usage (fire-and-forget)
                if final_message.usage:
                    from services.usage_service import log_usage
                    log_usage(user_id, MODEL_CHAT, "chat", final_message.usage)

                # Title schedule:
                # - Turn 1: early title already fired, now refine with reply
                # - Every 5 turns: check if topic shifted
                # - Session gap: conversation idle >24h → check if topic shifted on resume
                session_gap = False
                if prev_last_message_at is not None and user_msg_count > 1:
                    gap = datetime.now(timezone.utc) - prev_last_message_at
                    if gap.total_seconds() >= 24 * 3600:
                        session_gap = True
                should_regen_title = (
                    user_msg_count == 1
                    or (user_msg_count > 1 and user_msg_count % 5 == 0)
                    or session_gap
                )
                if should_regen_title:
                    # Never feed refusals/errors into the title — the regen helper also
                    # filters, but we do an early short-circuit so Haiku always sees
                    # stable signal (user intent + a successful reply, or user intent alone).
                    reply_for_title = None if _is_refusal_or_error_reply(accumulated_text) else accumulated_text
                    _fire_and_forget(_generate_and_save_title(conv.id, message, current_title=conv.title, assistant_reply=reply_for_title))

                if accumulated_text:
                    _fire_and_forget(_extract_memories_background(
                        user_id, message, accumulated_text, user_name=user.name or None,
                    ))

                # Post-turn compaction: if the turn we just finished already consumed a
                # large chunk of the context window, fold older messages into the rolling
                # summary before the NEXT turn starts. Runs in the background so the user
                # gets their response immediately.
                #
                # `input_tokens` is cache-MISS tokens only — once prompt caching is warm
                # it can be a few hundred even when the model processed 200k+ of cached
                # context. Use input + cache_read so the threshold reflects actual context
                # size; without this, compaction never fires on warm conversations and
                # history grows unbounded (a long conversation hit 1000 messages
                # with `summary` still NULL despite multiple turns at 180–240k true ctx).
                usage = final_message.usage
                total_ctx = (
                    (getattr(usage, "input_tokens", 0) or 0)
                    + (getattr(usage, "cache_read_input_tokens", 0) or 0)
                ) if usage else 0
                if total_ctx and total_ctx >= COMPACT_INPUT_TOKEN_THRESHOLD:
                    logger.info(
                        "Scheduling post-turn compaction",
                        conversation_id=str(conv.id),
                        total_ctx_tokens=total_ctx,
                    )
                    _fire_and_forget(_compact_conversation_background(conv.id))

                return

            # Tool calls need execution — pass full content (including thinking) for Claude's context
            history.append({"role": "assistant", "content": final_message.content})

            # Store assistant message FIRST and commit to get distinct timestamp
            await _store_message(
                session, conv.id, "assistant",
                content=accumulated_text or None,
                tool_calls=all_tool_data,
                model=MODEL_CHAT,
                thinking=accumulated_thinking or None,
            )
            await session.commit()

            # Track persistence per tool_use so we can write placeholder rows for any
            # that don't land (cancellation, exception, DB error). Without this, an
            # orphan tool_use permanently wedges the conversation.
            expected_tool_ids = [tc["id"] for tc in accumulated_tool_calls]
            persisted_tool_ids: set[str] = set()
            tool_results_for_claude: list[dict] = []
            try:
                # Per-/chat/send guard: short-circuit query_analytics calls that exceed
                # the cap, OR any query_analytics call after a successful Grafana panel
                # query. Returns a synthetic result so Claude sees the rejection and
                # synthesizes from existing data instead of looping more SQL.
                synthetic_results: dict[str, dict] = {}
                calls_to_dispatch: list[dict] = []
                for tc in accumulated_tool_calls:
                    name = tc.get("name", "")
                    if name == "query_analytics":
                        cap = (
                            QUERY_ANALYTICS_CAP_AFTER_GRAFANA
                            if _grafana_panel_succeeded
                            else QUERY_ANALYTICS_HARD_CAP
                        )
                        if _tool_call_counts.get(name, 0) >= cap:
                            reason = (
                                "You already have data from query_grafana_panel earlier this turn. "
                                "Use that result to answer the user. Do NOT issue more raw SQL queries to duplicate dashboard work."
                                if _grafana_panel_succeeded
                                else f"query_analytics call limit reached ({QUERY_ANALYTICS_HARD_CAP} per turn). Stop calling tools and synthesize a response from results so far."
                            )
                            synthetic_results[tc["id"]] = {"error": reason}
                            continue
                    calls_to_dispatch.append(tc)
                    _tool_call_counts[name] = _tool_call_counts.get(name, 0) + 1

                # Execute all tool calls in parallel (each gets its own DB session to avoid conflicts).
                # Emit SSE keepalive every STREAM_HEARTBEAT_INTERVAL_SEC so the upstream proxy doesn't
                # reap the connection while a slow tool (e.g. a big Notion fetch) is running.
                # asyncio.gather returns a Future that's already scheduling its coroutines, so
                # asyncio.shield can wrap it directly — no create_task needed.
                gather_future = asyncio.gather(
                    *[_execute_tool_parallel(tc, user_id, auth_token, conversation_id=conversation_id) for tc in calls_to_dispatch]
                )
                try:
                    while True:
                        try:
                            dispatched_results = await asyncio.wait_for(
                                asyncio.shield(gather_future), timeout=STREAM_HEARTBEAT_INTERVAL_SEC
                            )
                            break
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                except BaseException:
                    # Outer cancellation (client disconnect) or generator close: the shield
                    # above would otherwise leave gather_future running as an orphan, its
                    # tool coroutines still holding DB sessions and eventually raising
                    # unhandled exceptions. Cancel the gather (propagates to all children)
                    # and re-raise so normal cancellation/exception handling continues.
                    gather_future.cancel()
                    raise

                # Stitch dispatched results back into the original tool_call order,
                # interleaving any synthetic guard rejections.
                dispatched_iter = iter(dispatched_results)
                tool_results_raw = [
                    synthetic_results[tc["id"]] if tc["id"] in synthetic_results else next(dispatched_iter)
                    for tc in accumulated_tool_calls
                ]

                # Update Grafana-panel success flag for the next round's guard.
                for tc, result in zip(accumulated_tool_calls, tool_results_raw):
                    if (
                        tc.get("name") == "query_grafana_panel"
                        and isinstance(result, dict)
                        and not result.get("error")
                    ):
                        _grafana_panel_succeeded = True
                        break

                # Process results in order (store, build Claude payload, yield SSE events).
                # Commit per-tool BEFORE marking it persisted so the rescue
                # tracker reflects DB state, not just in-memory flush. Without
                # this, a cancellation between flush and the bulk commit at the
                # end of the round leaves an orphan tool_use in DB while the
                # tracker thinks all results are written.
                for tc, result in zip(accumulated_tool_calls, tool_results_raw):
                    await _store_message(
                        session, conv.id, "tool",
                        tool_results={"tool_use_id": tc["id"], "result": result},
                    )
                    await session.commit()
                    persisted_tool_ids.add(tc["id"])

                    # For analytics queries, don't send data to Claude -- frontend renders the table
                    if tc["name"] == "query_analytics" and result.get("rows"):
                        claude_result = {"status": "success", "row_count": result.get("row_count", len(result["rows"])), "note": "Data is displayed to the user. Do not add anything else."}
                    else:
                        claude_result = result

                    # Images: send as actual image content block so Claude vision can see them
                    if isinstance(claude_result, dict) and claude_result.get("type") == "image" and claude_result.get("image_data"):
                        raw_b64 = claude_result["image_data"]
                        if raw_b64.startswith("data:"):
                            raw_b64 = raw_b64.split(",", 1)[1] if "," in raw_b64 else raw_b64
                        tool_results_for_claude.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": [
                                {"type": "image", "source": {"type": "base64", "media_type": claude_result.get("mime_type", "image/png"), "data": raw_b64}},
                                {"type": "text", "text": f"Image: {claude_result.get('filename', 'uploaded image')}. Analyze it visually."},
                            ],
                        })
                    # Documents: send as a document content block so Claude can OCR natively
                    # (used for scanned / image-only PDFs where text extraction yielded nothing).
                    elif isinstance(claude_result, dict) and claude_result.get("type") == "document" and claude_result.get("document_data"):
                        tool_results_for_claude.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": [
                                {"type": "document", "source": {"type": "base64", "media_type": claude_result.get("mime_type", "application/pdf"), "data": claude_result["document_data"]}},
                                {"type": "text", "text": f"Document: {claude_result.get('filename', 'uploaded.pdf')}. Read it directly."},
                            ],
                        })
                    else:
                        # Request-only cap — the full result was persisted via
                        # _store_message above; only the model copy is bounded.
                        tool_results_for_claude.append({
                            "type": "tool_result",
                            "tool_use_id": tc["id"],
                            "content": _cap_tool_result_for_request(json.dumps(claude_result)),
                        })

                    if "row_count" in result:
                        summary = f"Found {result['row_count']} row{'s' if result['row_count'] != 1 else ''}"
                    elif "count" in result:
                        summary = f"Found {result['count']} results"
                    else:
                        summary = "Done"
                    if "error" in result:
                        summary = result["error"]
                        track_error(
                            str(user_id),
                            "integration_auth" if result.get("reconnect") else "tool_execution",
                            provider=result.get("reconnect", ""),
                            tool_name=tc["name"],
                            user_email=user.email,
                            detail=result["error"],
                        )
                    event: dict = {'type': 'tool_result', 'tool': tc['name'], 'tool_call_id': tc['id'], 'summary': summary}
                    # Rich render payloads (preview / data_table / chart / lottie /
                    # draft / reconnect) — shared with the wrapper path via the
                    # helper so both render live and never diverge.
                    event.update(_tool_result_event_extras(tc["name"], result))
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                # Rescue any tool_uses whose result didn't persist (cancel / exception /
                # DB error in the loop). Without this, the next turn would 400 forever.
                missing = [tid for tid in expected_tool_ids if tid not in persisted_tool_ids]
                if missing:
                    # Shield so asyncio cancellation doesn't interrupt the rescue writes.
                    await asyncio.shield(_persist_tool_placeholders(conv.id, missing))

            history.append({"role": "user", "content": tool_results_for_claude})
            # Tool result rows are already committed per-tool above; this commit
            # is a defensive no-op flush in case any other DB ops queued up
            # during result processing (track_error etc. don't, but keep the
            # commit so future additions stay durable).
            await session.commit()

            accumulated_text = ""
            accumulated_thinking = ""
            accumulated_tool_calls = []
            accumulated_advisor_blocks = []
            accumulated_code_exec_blocks = []

        # Max rounds exceeded
        track_error(str(user_id), "max_tool_rounds", user_email=user.email, detail=f"tool_round={tool_round}")
        yield f"data: {json.dumps({'type': 'error', 'message': 'Too many tool calls. Please try a simpler request.'})}\n\n"

    except asyncio.CancelledError:
        # Turn was cancelled. Post-Phase-2 the sources are narrower than the
        # legacy "client disconnected" path: explicit /chat/abort flag (user
        # hit Stop) or pod shutdown — client disconnect alone no longer
        # cancels this generator.
        #
        # We drop `accumulated_tool_calls` unconditionally — persisting a
        # tool_use without a matching tool_result would orphan the turn and
        # wedge every subsequent message on this conversation.
        logger.info(
            "chat turn cancelled",
            user_id=str(user_id),
            conversation_id=str(conv.id) if conv else None,
            turn_id=turn_id,
            tool_round=tool_round,
            had_partial_text=bool(accumulated_text),
        )
        # Rescue placeholder tool_results for any tool_use already committed
        # in this round but whose result didn't make it. The inner finally
        # block at line ~1948 handles cancellations that happen AFTER the
        # rescue try is entered, but cancellations between the assistant
        # tool_calls commit (line ~1770) and that try-block are handled here.
        # Without this rescue the orphan tool_use wedges every subsequent
        # message in the conversation with a Claude 400 invalid_request_error.
        if conv and accumulated_tool_calls:
            try:
                rescue_ids = [tc["id"] for tc in accumulated_tool_calls if tc.get("id")]
                if rescue_ids:
                    await asyncio.shield(_persist_tool_placeholders(conv.id, rescue_ids))
            except Exception as rescue_err:
                logger.warning(
                    "Failed to persist tool placeholders on cancel",
                    conversation_id=str(conv.id),
                    error=str(rescue_err),
                )
        if conv:
            try:
                if accumulated_text:
                    await _store_message(
                        session, conv.id, "assistant",
                        content=accumulated_text,
                        tool_calls=None,
                        model=MODEL_CHAT,
                        thinking=accumulated_thinking or None,
                        status="aborted",
                    )
                    await session.commit()
                    logger.info("Saved partial response on abort", conversation_id=str(conv.id))
                elif tool_round > 0:
                    await _store_message(
                        session, conv.id, "assistant",
                        content="⚠️ Response was interrupted mid-processing. Please retry.",
                        tool_calls=None,
                        model=MODEL_CHAT,
                        status="aborted",
                    )
                    await session.commit()
                    logger.info("Saved interruption marker on abort", conversation_id=str(conv.id))
            except Exception as save_err:
                logger.warning("Failed to save cancel marker on abort", error=str(save_err))
        # Signal tailers. `_drive_chat_producer` also synthesizes one if this
        # yield is skipped (e.g. generator re-raise), but emitting here gives
        # the frame a usable `conversation_id` payload.
        yield f"data: {json.dumps({'type': 'aborted', 'conversation_id': str(conv.id) if conv else None, 'turn_id': turn_id})}\n\n"
    except Exception as e:
        logger.error("stream_chat error", error=str(e))
        track_error(str(user_id), "chat_stream_error", user_email=user.email, detail=str(e))
        yield f"data: {json.dumps({'type': 'error', 'message': 'Sorry, something went wrong. Please try again.'})}\n\n"
    finally:
        await session.close()


# ===========================================================================
# Provider-neutral chat path (services.llm wrapper)
#
# Used when `PROVIDER_CHAT` routes chat off Anthropic to OpenAI or Kimi. The
# Anthropic native path above stays as-is — this is a parallel, simpler
# streaming loop that:
#
#   - Uses `llm.stream()` so events come back in the neutral shape.
#   - For OpenAI, the LLMSpec sets `use_code_interpreter=True` and
#     `use_reasoning_summary=True`, which routes through the Responses API
#     adapter (server-side Python sandbox + reasoning summary streaming).
#   - For Kimi (no native server-side code execution), injects
#     `EXECUTE_PYTHON_TOOL_SCHEMA` into the tool list so the model can call
#     `execute_python`; `tool_executor` routes that call through
#     `services.llm.code_execution_bridge` to Anthropic's hosted sandbox.
#   - Preserves the SSE event protocol the frontend already consumes.
#   - Skips Anthropic-only features the wrapper can't model: advisor (it's a
#     beta server-side tool), code_execution `container_id` continuity on the
#     OpenAI side (the Responses API has its own container lifecycle),
#     `cache_control` placement (caching is automatic on both other providers).
# ===========================================================================


async def _run_chat_turn_via_wrapper(
    user_id: UUID,
    user: User,
    message: str,
    conversation_id: str | None,
    auth_token: str = "",
    mentions: list[dict] | None = None,
    resume: bool = False,
    file_ids: list[str] | None = None,
    auto_mode: bool = False,
    turn_id: str | None = None,
) -> AsyncGenerator[str, None]:
    """Non-Anthropic chat turn. Mirrors `_run_chat_turn`'s contract but uses
    `services.llm.llm.stream()` for the streaming loop. Tool dispatch flows
    through the same `execute_tool_call` as the Anthropic path, and the SSE
    protocol yielded to the frontend is identical.
    """
    from dataclasses import replace as _spec_replace

    from services.llm import get_spec, llm
    from services.llm.code_execution_bridge import EXECUTE_PYTHON_TOOL_SCHEMA
    from services.llm.types import (
        MessageStop,
        TextDelta,
        ThinkingDelta,
        ToolCallEnd,
        ToolCallStart,
        Usage,
    )

    sm = AsyncPostgresSessionGideon()
    session = sm._session_factory()
    conv = None
    accumulated_text = ""
    accumulated_thinking = ""
    accumulated_tool_calls: list[dict] = []
    persisted_tool_ids: set[str] = set()

    # Scope the user's timezone to this turn so Slack read tools can resolve
    # human date/relative-word windows correctly (mirrors the native path).
    SLACK_USER_TZ.set(user.timezone)

    try:
        # ---------------- Setup (mirrors the Anthropic path) ----------------
        conv = await get_or_create_conversation(session, user_id, conversation_id)
        prev_last_message_at = None
        user_msg_count = 0

        if not resume:
            msg_mentions = None
            if file_ids:
                file_result = await session.execute(
                    select(UploadedFile.id, UploadedFile.filename).where(
                        UploadedFile.id.in_(file_ids),
                        UploadedFile.user_id == user_id,
                    )
                )
                files_data = [
                    {
                        "filename": row.filename,
                        "ext": row.filename.rsplit(".", 1)[-1].upper() if "." in row.filename else "",
                    }
                    for row in file_result.all()
                ]
                if files_data:
                    msg_mentions = {"files": files_data}

            last_msg_result = await session.execute(
                select(Message.created_at)
                .where(Message.conversation_id == conv.id)
                .order_by(Message.created_at.desc())
                .limit(1)
            )
            prev_last_message_at = last_msg_result.scalar_one_or_none()

            await _store_message(session, conv.id, "user", content=message, mentions=msg_mentions)
            await session.commit()

            track_chat_sent(
                str(user_id),
                conversation_id=str(conv.id),
                is_new=(conversation_id is None),
                has_mentions=bool(mentions),
                has_files=bool(file_ids),
            )

            user_msg_count_result = await session.execute(
                select(func.count(Message.id)).where(
                    Message.conversation_id == conv.id,
                    Message.role == "user",
                )
            )
            user_msg_count = user_msg_count_result.scalar() or 0

            if user_msg_count == 1 and not conv.title:
                early_title = await _generate_title(message)
                if early_title:
                    await _save_title_background(conv.id, early_title, expected_prev_title=None)
                    conv.title = early_title
                yield (
                    f"data: {json.dumps({'type': 'conversation_created', 'conversation_id': str(conv.id), 'title': conv.title or message[:50]})}\n\n"
                )

        # ---------------- History + tools + system prompt ----------------
        # Pre-turn safety net (see _preturn_compact_frames): this path runs on Kimi/
        # OpenAI, which had NO post-turn compaction trigger, so the tail could grow
        # unbounded. Fold it down before loading so the turn fits + stays fast.
        async for _frame in _preturn_compact_frames(session, conv.id):
            yield _frame

        history = await _load_history(session, conv.id)

        all_tools = await get_tools_for_user(user_id, session)
        # Convert BaseTool list to neutral dicts (Anthropic-shaped JSON Schema
        # carriers). The wrapper's adapters convert per-provider.
        chat_tools = [t.to_claude_schema() for t in all_tools]

        chat_spec = get_spec("chat")
        # On Kimi (no native server-side code execution), expose the bridge as
        # a callable function. tool_executor routes `execute_python` → the
        # Anthropic sandbox bridge automatically; conversation_id ties
        # container_id continuity to this chat row.
        # On OpenAI, the Responses adapter handles code_interpreter natively;
        # we still inject the function tool too so the model can fall back.
        if chat_spec.provider in ("kimi", "openai"):
            chat_tools.append(EXECUTE_PYTHON_TOOL_SCHEMA)

        # Skill memories
        skill_memories: list[dict] = []
        try:
            skill_result = await session.execute(
                select(Skill).where(
                    Skill.user_id == user_id,
                    Skill.status == "completed",
                    Skill.memory_content.isnot(None),
                )
            )
            for skill in skill_result.scalars().all():
                skill_memories.append({"name": skill.name, "memory_content": skill.memory_content})
        except Exception as e:
            logger.warning("wrapper-chat: failed to load skill memories", error=str(e))

        slack_user_id = await get_user_slack_id(session, user_id)
        system_prompt = build_system_prompt(
            persona_text=user.persona_text,
            user_name=user.name,
            user_email=user.email,
            slack_user_id=slack_user_id,
            connected_tools=all_tools,
            skill_memories=skill_memories if skill_memories else None,
            background_info=user.background_info,
            user_timezone=user.timezone,
            preferred_communication=user.preferred_communication,
        )

        # File context for the latest user message (matches native path).
        # Include BOTH the text context AND image content blocks. Dropping
        # image_blocks (the old behavior here) told the model "an image was
        # attached" via the text note while never sending the actual image —
        # so vision questions got answered blind. OpenAI (chat + responses)
        # and Kimi-k2.6 all accept base64 image blocks; the adapters convert
        # the neutral {"type":"image","source":{base64}} shape per-provider.
        if not resume and file_ids:
            try:
                file_context, image_blocks = await _build_file_context(session, user_id, file_ids)
                if (file_context or image_blocks) and history:
                    for i in range(len(history) - 1, -1, -1):
                        if history[i].get("role") == "user":
                            existing = history[i].get("content")
                            # Normalize to a content-block list so we can append
                            # image blocks + the text note uniformly.
                            if isinstance(existing, str):
                                blocks: list[dict] = [{"type": "text", "text": existing}] if existing else []
                            elif isinstance(existing, list):
                                blocks = existing
                            else:
                                blocks = []
                            blocks.extend(image_blocks)
                            if file_context:
                                blocks.append({"type": "text", "text": file_context})
                            history[i]["content"] = blocks
                            break
            except Exception as e:
                logger.warning("wrapper-chat: file context build failed", error=str(e))

        # Spec tweak: OpenAI gets Responses API features. Kimi just uses
        # Chat Completions (its natural fit). Both providers get caching for
        # free (automatic on the provider side).
        if chat_spec.provider == "openai":
            stream_spec = _spec_replace(
                chat_spec,
                use_code_interpreter=True,
                use_reasoning_summary=True,
            )
        else:
            stream_spec = chat_spec

        # ---------------- Streaming loop ----------------
        tool_round = 0
        last_usage: Optional[Usage] = None
        # One-way reactive fallback: flipped True the first time we escalate the
        # primary provider (Kimi/OpenAI) to Anthropic on an overload/transient
        # error, so a repeat failure on Anthropic can't loop.
        switched_provider = False
        while tool_round < MAX_TOOL_ROUNDS:
            tool_round += 1

            if turn_id and await is_aborted(turn_id):
                logger.info("wrapper-chat aborted between rounds", turn_id=turn_id, tool_round=tool_round)
                raise asyncio.CancelledError()

            current_tool: dict | None = None
            round_tool_calls: list[dict] = []
            round_text_chars = 0
            round_thinking_chars = 0
            round_reasoning_id: str | None = None  # OpenAI Responses reasoning item id
            stop_reason = "end_turn"

            # SECURITY: commit + release the implicit transaction BEFORE the
            # external LLM stream. Otherwise Postgres's
            # `idle_in_transaction_session_timeout` (30s) kills the connection
            # mid-stream and every subsequent query in this turn 500s. The
            # native Anthropic path does the same thing explicitly before its
            # streaming SDK call.
            await session.commit()

            # Bound the request under the provider's context ceiling (Kimi's
            # 262K hard cap is what produced the prod `exceeded model token
            # limit` 400s). Request-only copy — `history` and the DB rows keep
            # full fidelity; no-op when already under the ceiling.
            request_messages = _bound_chat_request_messages(
                history,
                system_prompt,
                chat_tools,
                provider=chat_spec.provider,
                tool_round=tool_round,
            )

            try:
                async for ev in llm.stream(
                    stream_spec,
                    messages=request_messages,
                    system=system_prompt,
                    tools=chat_tools,
                    timeout=900.0,
                ):
                    if turn_id and isinstance(ev, (TextDelta, ThinkingDelta)) and await is_aborted(turn_id):
                        logger.info("wrapper-chat aborted mid-stream", turn_id=turn_id)
                        raise asyncio.CancelledError()

                    if isinstance(ev, TextDelta):
                        accumulated_text += ev.text
                        round_text_chars += len(ev.text)
                        yield f"data: {json.dumps({'type': 'text_delta', 'content': ev.text})}\n\n"
                    elif isinstance(ev, ThinkingDelta):
                        accumulated_thinking += ev.thinking
                        round_thinking_chars += len(ev.thinking)
                        if ev.reasoning_id:
                            round_reasoning_id = ev.reasoning_id
                        yield f"data: {json.dumps({'type': 'thinking_delta', 'content': ev.thinking})}\n\n"
                    elif isinstance(ev, ToolCallStart):
                        current_tool = {"id": ev.id, "name": ev.name, "input": {}}
                        yield (
                            f"data: {json.dumps({'type': 'tool_start', 'tool': ev.name, 'tool_call_id': ev.id})}\n\n"
                        )
                    elif isinstance(ev, ToolCallEnd):
                        # Some providers don't emit ToolCallStart first
                        # (event ordering varies) — synthesize if needed.
                        if current_tool is None or current_tool.get("id") != ev.id:
                            yield (
                                f"data: {json.dumps({'type': 'tool_start', 'tool': ev.name, 'tool_call_id': ev.id})}\n\n"
                            )
                        round_tool_calls.append({"id": ev.id, "name": ev.name, "input": ev.input})
                        current_tool = None
                    elif isinstance(ev, Usage):
                        last_usage = ev
                    elif isinstance(ev, MessageStop):
                        stop_reason = ev.stop_reason
                    # StreamError is reserved-but-not-emitted (see
                    # services/llm/types.py). Adapter exceptions propagate
                    # to the outer try/except below.

            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Reactive provider fallback. If a non-Anthropic provider fails on
                # an overload/transient/context error and this round hasn't
                # streamed anything yet, switch to Anthropic and redo the round —
                # a Moonshot `429 engine_overloaded` blip degrades chat to "runs
                # on Claude" instead of dying. Mirrors the workflow escalation.
                # Guards: (1) once only (`switched_provider`) so it can't loop;
                # (2) only from a non-Anthropic provider; (3) only if nothing was
                # emitted this round, else re-running would duplicate streamed
                # text; (4) only for the transient/overload/context error family.
                # `current_tool is not None` catches an in-flight tool whose
                # `tool_start` SSE already went to the FE but whose ToolCallEnd
                # hasn't landed yet (so it isn't in round_tool_calls) — re-running
                # would emit a second tool_start and orphan the first in the UI.
                round_had_output = (
                    round_text_chars > 0
                    or round_thinking_chars > 0
                    or bool(round_tool_calls)
                    or current_tool is not None
                )
                if (
                    not switched_provider
                    and chat_spec.provider != "anthropic"
                    and not round_had_output
                    and _should_fallback_to_anthropic(e)
                ):
                    logger.warning(
                        "wrapper-chat: provider error — falling back to Anthropic",
                        from_provider=chat_spec.provider,
                        error=str(e)[:200],
                        round=tool_round,
                    )
                    # Dedicated reroute event (NOT track_error — a successful
                    # fallback is a recovery). Counts as one row in
                    # gideon_llm_provider_fallback for the observability dashboard.
                    track_llm_provider_fallback(
                        str(user_id),
                        feature="chat",
                        from_provider=chat_spec.provider,
                        to_provider="anthropic",
                        reason=_fallback_reason(e),
                        round_num=tool_round,
                    )
                    # Switch spec to Anthropic, drop the OpenAI Responses flags,
                    # and re-run THIS round. Strip Kimi/OpenAI thinking blocks so
                    # Anthropic doesn't 400 on unsigned thinking. The re-run's
                    # `_bound_chat_request_messages` re-bounds under Anthropic's
                    # own (smaller) ceiling, so a context-overflow fallback fits.
                    chat_spec = _spec_replace(
                        chat_spec,
                        provider="anthropic",
                        model=CHAT_FALLBACK_MODEL,
                        use_code_interpreter=False,
                        use_reasoning_summary=False,
                        use_advisor=False,  # native-path-only beta; not on the wrapper
                    )
                    stream_spec = chat_spec
                    history = _strip_thinking_for_anthropic(history)
                    switched_provider = True
                    tool_round -= 1  # redo this round on Anthropic
                    continue

                logger.error("wrapper-chat: LLM API error", error=str(e), round=tool_round)
                track_error(
                    str(user_id),
                    "chat_llm_error",
                    user_email=user.email,
                    detail=f"provider={chat_spec.provider}, round={tool_round}: {e}",
                )
                # Honest, reason-specific message (esp. after the Anthropic backup
                # ALSO failed) — keyed off the exact bucket, not the broad fallback
                # matcher, so a context/timeout error isn't mislabeled "overloaded".
                _CHAT_ERROR_MESSAGES = {
                    "overload": "The AI provider is overloaded right now. Please try again in a moment.",
                    "server_error": "The AI provider is temporarily unavailable. Please try again in a moment.",
                    "timeout": "That request took too long and timed out. Please try again.",
                    "context": "This conversation has gotten too long for the model. Please start a new chat to continue.",
                }
                msg = _CHAT_ERROR_MESSAGES.get(
                    _fallback_reason(e), "Sorry, something went wrong. Please try again."
                )
                yield (
                    f"data: {json.dumps({'type': 'error', 'message': msg})}\n\n"
                )
                return

            logger.info(
                "wrapper-chat round resolved",
                round=tool_round,
                provider=chat_spec.provider,
                stop_reason=stop_reason,
                text_chars=round_text_chars,
                thinking_chars=round_thinking_chars,
                tool_calls=len(round_tool_calls),
            )

            accumulated_tool_calls.extend(round_tool_calls)

            # Build assistant content blocks for history (next round's context).
            #
            # CRITICAL: Kimi-k2.6 (and OpenAI o-series) require reasoning_content
            # on EVERY assistant tool-call message in the replay, not just the
            # first. Without per-round thinking attached here, round 2+ 400s
            # with "thinking is enabled but reasoning_content is missing in
            # assistant tool call message at index N". The OpenAI-compatible
            # adapter converts thinking blocks → reasoning_content on the
            # assistant message; we just need to make sure every round
            # supplies one.
            assistant_blocks: list[dict] = []
            if round_thinking_chars > 0:
                this_round_thinking = accumulated_thinking[-round_thinking_chars:]
                if this_round_thinking.strip():
                    thinking_block: dict = {"type": "thinking", "thinking": this_round_thinking}
                    # OpenAI Responses needs the reasoning item id round-tripped
                    # so it can resume its chain of thought; the responses
                    # adapter reads `_reasoning_id` off the block on replay.
                    if round_reasoning_id:
                        thinking_block["_reasoning_id"] = round_reasoning_id
                    assistant_blocks.append(thinking_block)
            if round_text_chars > 0:
                # accumulated_text spans all rounds — but for the assistant
                # message at this round, only emit what was produced THIS round.
                this_round_text = accumulated_text[-round_text_chars:]
                if this_round_text.strip():
                    assistant_blocks.append({"type": "text", "text": this_round_text})
            for tc in round_tool_calls:
                assistant_blocks.append(
                    {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
                )

            if assistant_blocks:
                history.append({"role": "assistant", "content": assistant_blocks})

            # End-of-turn? No tool calls → we're done.
            if not round_tool_calls:
                # Store ONLY this final round's text/thinking. Earlier rounds
                # that called tools already persisted their own assistant
                # messages (above, before their tool rows), so re-storing the
                # cross-round `accumulated_*` here would duplicate them in the
                # rendered conversation. The final round has no tool calls by
                # definition, so tool_calls=None.
                final_text = (
                    accumulated_text[-round_text_chars:] if round_text_chars else None
                )
                final_thinking = (
                    accumulated_thinking[-round_thinking_chars:]
                    if round_thinking_chars else None
                )
                stored_msg = await _store_message(
                    session,
                    conv.id,
                    "assistant",
                    content=final_text,
                    tool_calls=None,
                    model=stream_spec.model,
                    tokens_used=last_usage.output_tokens if last_usage else None,
                    thinking=final_thinking,
                )
                await session.commit()

                yield (
                    f"data: {json.dumps({'type': 'done', 'message_id': str(stored_msg.id), 'conversation_id': str(conv.id), 'title': conv.title})}\n\n"
                )

                if last_usage:
                    from services.usage_service import log_usage as _log_usage
                    _log_usage(user_id, stream_spec.model, "chat", last_usage)

                    # Post-turn compaction (mirrors the native path). The neutral
                    # Usage exposes input_tokens + cache_read_input_tokens; their sum
                    # reflects true context size even when provider-side caching makes
                    # the billed input small. Without this, history grew unbounded on
                    # the Kimi path. The pre-turn guard is the hard safety net; this
                    # keeps well-behaved conversations from ever reaching it.
                    total_ctx = (last_usage.input_tokens or 0) + (last_usage.cache_read_input_tokens or 0)
                    if total_ctx >= COMPACT_INPUT_TOKEN_THRESHOLD:
                        logger.info(
                            "Scheduling post-turn compaction",
                            conversation_id=str(conv.id),
                            total_ctx_tokens=total_ctx,
                        )
                        _fire_and_forget(_compact_conversation_background(conv.id))

                # Title schedule (matches native path).
                if not resume:
                    session_gap = False
                    if prev_last_message_at is not None and user_msg_count > 1:
                        gap = datetime.now(timezone.utc) - prev_last_message_at
                        if gap.total_seconds() >= 24 * 3600:
                            session_gap = True
                    should_regen_title = (
                        user_msg_count == 1
                        or (user_msg_count > 1 and user_msg_count % 5 == 0)
                        or session_gap
                    )
                    if should_regen_title:
                        reply_for_title = (
                            None if _is_refusal_or_error_reply(accumulated_text) else accumulated_text
                        )
                        _fire_and_forget(
                            _generate_and_save_title(
                                conv.id,
                                message,
                                current_title=conv.title,
                                assistant_reply=reply_for_title,
                            )
                        )

                if accumulated_text:
                    _fire_and_forget(
                        _extract_memories_background(
                            user_id, message, accumulated_text, user_name=user.name or None,
                        )
                    )

                return

            # Persist the assistant message (with this round's tool_use blocks)
            # BEFORE running tools / writing tool-result rows. Otherwise the
            # role="tool" rows are orphaned on reload — the 3-pass sanitizer
            # strips tool_results that have no preceding assistant tool_use,
            # so replay history loses the whole round. Mirrors the native path
            # (which stores the assistant message first, then commits).
            this_round_thinking_persist = (
                accumulated_thinking[-round_thinking_chars:] if round_thinking_chars else None
            )
            this_round_text_persist = (
                accumulated_text[-round_text_chars:] if round_text_chars else None
            )
            await _store_message(
                session,
                conv.id,
                "assistant",
                content=this_round_text_persist,
                tool_calls=[
                    {"id": tc["id"], "name": tc["name"], "input": tc["input"]}
                    for tc in round_tool_calls
                ],
                model=stream_spec.model,
                thinking=this_round_thinking_persist,
            )
            await session.commit()

            # Tool calls present — execute in parallel, each with its OWN DB
            # session. Passing the single request-scoped `session` into
            # asyncio.gather is unsafe: AsyncSession is not concurrency-safe,
            # and two tools touching it at once raises
            # "this session is already executing". _execute_tool_parallel
            # opens a fresh session per tool (same helper the native path uses).
            tool_results_raw = await asyncio.gather(
                *[
                    _execute_tool_parallel(
                        tc,
                        user_id,
                        auth_token=auth_token,
                        conversation_id=str(conv.id),
                    )
                    for tc in round_tool_calls
                ]
            )

            tool_results_blocks: list[dict] = []
            for tc, result in zip(round_tool_calls, tool_results_raw):
                # Persist the tool result row.
                await _store_message(
                    session,
                    conv.id,
                    "tool",
                    tool_results={"tool_use_id": tc["id"], "result": result},
                )
                await session.commit()
                persisted_tool_ids.add(tc["id"])

                # SSE — let the FE render results LIVE (mirrors native path).
                # Without the rich extras here, previews/tables/charts/lottie/
                # drafts only appeared after a page reload on the Kimi path.
                _tr_event: dict = {"type": "tool_result", "tool": tc["name"], "tool_call_id": tc["id"]}
                if isinstance(result, dict):
                    if "row_count" in result:
                        _tr_event["summary"] = f"Found {result['row_count']} row{'s' if result['row_count'] != 1 else ''}"
                    elif "count" in result:
                        _tr_event["summary"] = f"Found {result['count']} results"
                    if result.get("error"):
                        _tr_event["summary"] = result["error"]
                    _tr_event.update(_tool_result_event_extras(tc["name"], result))
                yield f"data: {json.dumps(_tr_event)}\n\n"

                # If execute_python returned file_ids (from the bridge),
                # download via Anthropic file API and surface them as code
                # outputs, same as the native code_execution flow.
                if tc["name"] == "execute_python" and isinstance(result, dict) and result.get("file_ids"):
                    try:
                        images, code_files = await _download_code_exec_artifacts(result["file_ids"], user_id)
                        for img in images:
                            yield (
                                f"data: {json.dumps({'type': 'code_image', 'data': img['data'], 'mime': img['mime'], 'filename': img['filename']})}\n\n"
                            )
                        for cf in code_files:
                            yield (
                                f"data: {json.dumps({'type': 'code_file', 'filename': cf['filename'], 'mime': cf['mime'], 'drive_url': cf['drive_url'], 'drive_file_id': cf['drive_file_id'], 'size_bytes': cf['size_bytes']})}\n\n"
                            )
                    except Exception as e:
                        logger.warning("wrapper-chat: failed to surface bridge file artifacts", error=str(e))

                # Request-only cap — the full result was persisted via
                # _store_message above; only the model copy is bounded.
                tool_results_blocks.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tc["id"],
                        "content": _cap_tool_result_for_request(json.dumps(result)),
                    }
                )

            history.append({"role": "user", "content": tool_results_blocks})

        # Max rounds exhausted.
        logger.warning(
            "wrapper-chat exhausted max rounds", rounds=MAX_TOOL_ROUNDS, conversation_id=str(conv.id)
        )
        if accumulated_text:
            stored_msg = await _store_message(
                session,
                conv.id,
                "assistant",
                content=accumulated_text,
                tool_calls=accumulated_tool_calls or None,
                model=stream_spec.model,
                tokens_used=last_usage.output_tokens if last_usage else None,
                thinking=accumulated_thinking or None,
            )
            await session.commit()
            yield (
                f"data: {json.dumps({'type': 'done', 'message_id': str(stored_msg.id), 'conversation_id': str(conv.id), 'title': conv.title})}\n\n"
            )

    except asyncio.CancelledError:
        logger.info("wrapper-chat cancelled", turn_id=turn_id, conv_id=str(conv.id) if conv else None)
        if conv:
            try:
                async with sm.get_session() as cleanup:
                    await cleanup.execute(
                        update(Conversation)
                        .where(Conversation.id == conv.id)
                        .values(active_turn_id=None)
                    )
                    await cleanup.commit()
            except Exception:
                pass
        yield (
            f"data: {json.dumps({'type': 'aborted', 'conversation_id': str(conv.id) if conv else None, 'turn_id': turn_id})}\n\n"
        )
    except Exception as e:
        logger.error("wrapper-chat error", error=str(e))
        track_error(str(user_id), "chat_stream_error", user_email=user.email, detail=str(e))
        yield (
            f"data: {json.dumps({'type': 'error', 'message': 'Sorry, something went wrong. Please try again.'})}\n\n"
        )
    finally:
        await session.close()
