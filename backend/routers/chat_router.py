"""
Chat Router — conversation and message endpoints.
POST /send streams SSE responses from Claude.
"""

from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import or_, select, delete, update, exists
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Optional
from uuid import UUID
import structlog

from db import get_db
from db.models import Conversation, Message, User
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user
from services.chat_service import stream_chat, generate_suggested_reply
from services.chat_stream_redis import (
    get_turn_meta,
    set_abort_flag,
    tail_stream,
)

logger = structlog.get_logger()

router = APIRouter()


from enum import Enum

class MentionCategory(str, Enum):
    collaborator = "collaborator"
    tool = "tool"
    gmail = "gmail"
    linear = "linear"
    notion = "notion"
    google_calendar = "google_calendar"
    slack = "slack"
    google_drive = "google_drive"
    google_docs = "google_docs"
    google_sheets = "google_sheets"
    google_slides = "google_slides"

class MentionPayload(BaseModel):
    id: str
    category: MentionCategory
    label: str
    url: Optional[str] = None

class SendMessageRequest(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    mentions: Optional[list[MentionPayload]] = None
    resume: Optional[bool] = False
    file_ids: Optional[list[str]] = None  # IDs of uploaded files to include
    auto_mode: Optional[bool] = False  # Auto-execute actions (send emails/messages without confirmation)


class ConversationResponse(BaseModel):
    id: str
    title: Optional[str] = None
    created_at: str
    updated_at: str
    is_pinned: bool = False
    # UUID of an in-flight background turn (producer still running). The
    # frontend uses this to auto-open the resume stream when a conversation
    # with an active turn is viewed.
    active_turn_id: Optional[str] = None
    # When set, this conversation was seeded from a workflow run via the
    # "Follow up in chat" button on the run detail page. The frontend uses
    # these to render a small banner ("Continuing from workflow: …") with a
    # deep link back to the originating run. Both pre-resolved so the banner
    # doesn't need a follow-up API round-trip.
    workflow_run_id: Optional[str] = None
    workflow_id: Optional[str] = None

    class Config:
        from_attributes = True


class FileAttachment(BaseModel):
    filename: str
    ext: str

class MessageResponse(BaseModel):
    id: str
    conversation_id: str
    role: str
    content: Optional[str] = None
    thinking: Optional[str] = None
    tool_calls: Optional[list] = None
    tool_results: Optional[dict] = None
    model: Optional[str] = None
    tokens_used: Optional[int] = None
    files: Optional[list[FileAttachment]] = None
    feedback: Optional[str] = None
    feedback_text: Optional[str] = None
    status: str = "done"
    created_at: str

    class Config:
        from_attributes = True


@router.post("/send")
async def send_message(
    body: SendMessageRequest,
    request: Request,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Send a message and stream Claude's response via SSE.
    Uses its own session inside stream_chat so commits happen immediately
    and other endpoints can see the data."""
    user = await get_or_create_user(session, auth_user)

    # Store auth token for background agents (trigger agent, etc.)
    from services.user_cache_service import store_user_auth_token
    await store_user_auth_token(str(user.id), request.headers.get("authorization", ""))

    return StreamingResponse(
        stream_chat(
            user_id=user.id,
            user=user,
            message=body.message,
            conversation_id=body.conversation_id,
            auth_token=request.headers.get("authorization", ""),
            mentions=[m.model_dump() for m in body.mentions] if body.mentions else None,
            resume=body.resume or False,
            file_ids=body.file_ids,
            auto_mode=body.auto_mode or False,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/resume/{turn_id}")
async def resume_stream(
    turn_id: str,
    request: Request,
    last_event_id: Optional[str] = Header(None, alias="Last-Event-ID"),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Re-attach to an in-flight (or recently-finished) chat turn via SSE.

    Ownership: the turn's Redis meta must record a user_id that matches the
    caller. If the meta has expired (beyond the terminal TTL window) we 404
    — clients fall back to re-fetching /messages, which by then holds the
    persisted final state.

    Supports `Last-Event-ID` header: if set, replay skips entries up to and
    including that id, matching the standard SSE reconnection protocol. A
    missing header replays from the start of the stream.
    """
    user = await get_or_create_user(session, auth_user)

    meta = await get_turn_meta(turn_id)
    if not meta:
        # The turn's Redis buffer is gone: TTL expired, the producer died
        # ungracefully (deploy / pod restart / OOM), or a Redis cutover orphaned
        # it. If a conversation still points at this dead turn, its
        # `active_turn_id` was never cleared (the producer's finally-block never
        # ran), so GET /conversations keeps returning it and the client loops
        # forever: resume -> 404 -> revalidate -> stale active_turn_id -> resume.
        # Clear the stale pointer (scoped to this user) so the spinner stops for
        # good and the client falls back to /messages. Best-effort.
        try:
            await session.execute(
                update(Conversation)
                .where(
                    Conversation.active_turn_id == UUID(turn_id),
                    Conversation.user_id == user.id,
                )
                .values(active_turn_id=None)
            )
            await session.commit()
        except Exception as e:
            # Best-effort cleanup — never block the 404 on it, but log so a
            # persistently-failing clear (e.g. a poisoned session) is traceable
            # rather than silently leaving the spinner stuck.
            await session.rollback()
            logger.warning(
                "failed to clear stale active_turn_id on resume 404",
                turn_id=turn_id,
                user_id=str(user.id),
                err=str(e),
            )
        raise HTTPException(status_code=404, detail="Turn not found")
    if meta.get("user_id") != str(user.id):
        raise HTTPException(status_code=404, detail="Turn not found")

    start_from = last_event_id or "0"

    async def _iter():
        async for frame in tail_stream(turn_id, last_id=start_from):
            yield frame

    return StreamingResponse(
        _iter(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/abort/{turn_id}", status_code=204)
async def abort_stream(
    turn_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Signal the background producer to wind down.

    Sets `chat_abort:{turn_id}` in Redis. The producer checks this flag
    between tool rounds, raises CancelledError, saves any partial output,
    and emits an `aborted` sentinel frame so all active tailers close.
    """
    user = await get_or_create_user(session, auth_user)

    meta = await get_turn_meta(turn_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Turn not found")
    if meta.get("user_id") != str(user.id):
        raise HTTPException(status_code=404, detail="Turn not found")

    await set_abort_flag(turn_id)
    return None


@router.get("/conversations", response_model=list[ConversationResponse])
async def list_conversations(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all conversations for the current user, most recent first.

    Empty orphan conversations (0 messages) are hidden. These accumulate from
    interrupted sends and especially file uploads (dropping a file creates a
    titled conversation immediately; if no message follows it is left empty),
    and otherwise clutter the sidebar with blank / stuck "New conversation"
    rows. An empty conversation is still shown if it is mid-stream
    (active_turn_id), workflow-linked, or pinned, so nothing in-flight or
    intentionally kept is dropped. Non-destructive: rows are filtered from the
    list, not deleted, and any attached uploaded files remain on the Files page.
    """
    user = await get_or_create_user(session, auth_user)
    has_messages = exists().where(Message.conversation_id == Conversation.id)
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .where(
            has_messages
            | Conversation.active_turn_id.isnot(None)
            | Conversation.workflow_run_id.isnot(None)
            | Conversation.workflow_id.isnot(None)
            | Conversation.is_pinned.is_(True)
        )
        .order_by(Conversation.is_pinned.desc(), Conversation.updated_at.desc())
    )
    convs = result.scalars().all()
    return [
        ConversationResponse(
            id=str(c.id),
            title=c.title,
            created_at=c.created_at.isoformat(),
            updated_at=c.updated_at.isoformat(),
            is_pinned=bool(c.is_pinned),
            active_turn_id=str(c.active_turn_id) if c.active_turn_id else None,
            workflow_run_id=str(c.workflow_run_id) if c.workflow_run_id else None,
            workflow_id=str(c.workflow_id) if c.workflow_id else None,
        )
        for c in convs
    ]


@router.post("/conversations", response_model=ConversationResponse)
async def create_conversation(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Create a new empty conversation."""
    user = await get_or_create_user(session, auth_user)
    conv = Conversation(user_id=user.id)
    session.add(conv)
    await session.flush()
    await session.refresh(conv)
    return ConversationResponse(
        id=str(conv.id),
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
    )


@router.get("/conversations/{conversation_id}/messages", response_model=list[MessageResponse])
async def get_messages(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get all messages in a conversation."""
    user = await get_or_create_user(session, auth_user)

    # Verify conversation belongs to user
    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if not conv_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Conversation not found")

    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()
    responses = []
    for m in messages:
        files = None
        if m.mentions and isinstance(m.mentions, dict) and m.mentions.get("files"):
            files = [FileAttachment(**f) for f in m.mentions["files"]]
        responses.append(MessageResponse(
            id=str(m.id),
            conversation_id=str(m.conversation_id),
            role=m.role,
            content=m.content,
            thinking=m.thinking,
            tool_calls=m.tool_calls,
            tool_results=m.tool_results,
            model=m.model,
            tokens_used=m.tokens_used,
            files=files,
            status=m.status or "done",
            created_at=m.created_at.isoformat(),
        ))
    return responses


@router.get("/conversations/{conversation_id}/suggested-reply")
async def get_suggested_reply(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Predict a short follow-up the user is likely to send next.

    Reads the latest user→assistant pair from the conversation and asks Haiku
    for a 2-10 word likely reply. Returns `{"suggestion": null}` only when
    inputs are unusable. Cheap by design — safe to call after every turn.
    """
    user = await get_or_create_user(session, auth_user)

    conv = (await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )).scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Deterministic ordering: ties on `created_at` (sub-microsecond writes during a
    # streaming turn can collide) would otherwise let the wrong message win the
    # "latest assistant" / "latest user" check below.
    rows = (await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.desc(), Message.id.desc())
        .limit(20)
    )).scalars().all()

    assistant_text: str | None = None
    user_text: str | None = None
    for m in rows:
        if assistant_text is None and m.role == "assistant" and (m.content or "").strip():
            assistant_text = m.content
            continue
        if assistant_text is not None and m.role == "user" and (m.content or "").strip():
            user_text = m.content
            break

    first_name = (user.name or "").split(" ")[0] if user.name else None
    user_id = user.id

    if not assistant_text or not user_text:
        return {"suggestion": None}

    # Release the DB session BEFORE the Anthropic call — see project CLAUDE.md
    # ("No external integration points inside an open DB session"). The LLM
    # round-trip is 1-15s and would otherwise pin a pool connection across it.
    await session.close()

    suggestion = await generate_suggested_reply(
        user_message=user_text,
        assistant_reply=assistant_text,
        user_first_name=first_name,
        user_id=user_id,
    )
    return {"suggestion": suggestion}


@router.patch("/conversations/{conversation_id}", response_model=ConversationResponse)
async def rename_conversation(
    conversation_id: str,
    body: dict,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update a conversation (rename or toggle pin)."""
    user = await get_or_create_user(session, auth_user)
    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    mutated = False
    if "title" in body:
        conv.title = body["title"]
        # Lock the title so the auto-title generator stops overwriting it.
        conv.title_locked = True
        mutated = True
    if "is_pinned" in body:
        conv.is_pinned = bool(body["is_pinned"])
        mutated = True
    if mutated:
        await session.flush()
        await session.refresh(conv)

    return ConversationResponse(
        id=str(conv.id),
        title=conv.title,
        created_at=conv.created_at.isoformat(),
        updated_at=conv.updated_at.isoformat(),
        is_pinned=bool(conv.is_pinned),
        workflow_run_id=str(conv.workflow_run_id) if conv.workflow_run_id else None,
        workflow_id=str(conv.workflow_id) if conv.workflow_id else None,
    )


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a conversation and all its messages."""
    user = await get_or_create_user(session, auth_user)

    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if not conv_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Conversation not found")

    await session.execute(
        delete(Message).where(Message.conversation_id == conversation_id)
    )
    await session.execute(
        delete(Conversation).where(Conversation.id == conversation_id)
    )

    return {"status": "deleted"}


@router.delete("/conversations/{conversation_id}/messages-from/{message_id}")
async def delete_messages_from(
    conversation_id: str,
    message_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Delete a message and all messages after it (for edit-and-resend)."""
    user = await get_or_create_user(session, auth_user)

    # Verify conversation ownership
    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if not conv_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Conversation not found")

    # Get the target message's created_at to delete it and everything after
    msg_result = await session.execute(
        select(Message.created_at).where(
            Message.id == message_id,
            Message.conversation_id == conversation_id,
            Message.role == "user",
        )
    )
    msg_row = msg_result.first()
    if not msg_row:
        raise HTTPException(status_code=404, detail="Message not found")

    # Delete target message and everything after it (composite predicate avoids timestamp collision)
    target_created_at = msg_row[0]
    from sqlalchemy import or_, and_
    await session.execute(
        delete(Message).where(
            Message.conversation_id == conversation_id,
            or_(
                Message.created_at > target_created_at,
                and_(Message.created_at == target_created_at, Message.id >= message_id),
            ),
        )
    )

    return {"status": "truncated"}


# ---------------------------------------------------------------------------
# Files Directory
# ---------------------------------------------------------------------------


class FileListItem(BaseModel):
    id: str
    filename: str
    mime_type: str
    size_bytes: int
    is_image: bool
    drive_url: Optional[str] = None
    conversation_id: Optional[str] = None
    conversation_title: Optional[str] = None
    created_at: str


@router.get("/files", response_model=list[FileListItem])
async def list_files(
    q: Optional[str] = Query(None),
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """List all files for the current user (uploaded + generated). Searchable by filename."""
    from db.models import UploadedFile

    user = await get_or_create_user(session, auth_user)

    query = (
        select(UploadedFile, Conversation.title)
        .outerjoin(Conversation, UploadedFile.conversation_id == Conversation.id)
        .where(UploadedFile.user_id == user.id)
        .order_by(UploadedFile.created_at.desc())
        .limit(100)
    )

    if q:
        query = query.where(UploadedFile.filename.ilike(f"%{q}%"))

    result = await session.execute(query)
    rows = result.all()

    return [
        FileListItem(
            id=str(f.id),
            filename=f.filename,
            mime_type=f.mime_type,
            size_bytes=f.size_bytes,
            is_image=f.is_image,
            drive_url=f.drive_url,
            conversation_id=str(f.conversation_id) if f.conversation_id else None,
            conversation_title=conv_title,
            created_at=f.created_at.isoformat(),
        )
        for f, conv_title in rows
    ]


# ---------------------------------------------------------------------------
# File Upload
# ---------------------------------------------------------------------------


class UploadResponse(BaseModel):
    id: str
    filename: str
    size_bytes: int
    is_image: bool
    mime_type: str


@router.post("/upload", response_model=UploadResponse)
async def upload_file(
    file: UploadFile = File(...),
    conversation_id: Optional[str] = Query(None),
    request: Request = None,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Upload a file for use in chat. Extracts text, uploads to Google Drive.
    If no conversation_id, creates a new conversation."""
    from services.file_extraction_service import validate_file, extract_text, is_image, file_to_base64, MAX_FILE_SIZE
    from db.models import UploadedFile, Integration
    from services.encryption_service import decrypt

    user = await get_or_create_user(session, auth_user)

    # Read file bytes (with size limit)
    file_bytes = await file.read()
    if len(file_bytes) > MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum is 10 MB.")

    # Validate
    error = validate_file(file.filename or "unknown", len(file_bytes))
    if error:
        raise HTTPException(status_code=400, detail=error)

    filename = file.filename or "unknown"
    mime_type = file.content_type or "application/octet-stream"

    # Get or create conversation
    if conversation_id:
        conv_result = await session.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user.id,
            )
        )
        conv = conv_result.scalar_one_or_none()
        if not conv:
            raise HTTPException(status_code=404, detail="Conversation not found")
    else:
        conv = Conversation(user_id=user.id, title=filename[:50])
        session.add(conv)
        await session.flush()
        await session.refresh(conv)

    # Extract text (or base64 for images)
    image_flag = is_image(filename)
    extracted = ""
    image_b64 = ""
    if image_flag:
        image_b64 = file_to_base64(file_bytes, mime_type)
    else:
        extracted = await extract_text(filename, file_bytes)

    # Upload to Google Drive (async, best-effort)
    drive_file_id = ""
    drive_url = ""
    try:
        integ_result = await session.execute(
            select(Integration).where(
                Integration.user_id == user.id,
                Integration.provider == "google",
                Integration.is_active == True,
            )
        )
        integ = integ_result.scalar_one_or_none()
        if integ:
            import asyncio
            drive_file_id, drive_url = await asyncio.to_thread(
                _upload_to_drive,
                decrypt(integ.access_token),
                filename,
                mime_type,
                file_bytes,
                conv.title or "Untitled",
            )
    except Exception as e:
        logger.warning("Drive upload failed, file still saved locally", error=str(e))

    # Store in DB
    uploaded = UploadedFile(
        user_id=user.id,
        conversation_id=conv.id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=len(file_bytes),
        extracted_text=extracted or None,
        is_image=image_flag,
        image_base64=image_b64 or None,
        drive_file_id=drive_file_id or None,
        drive_url=drive_url or None,
    )
    session.add(uploaded)
    await session.flush()
    await session.refresh(uploaded)

    logger.info("File uploaded", filename=filename, size=len(file_bytes), is_image=image_flag, conversation_id=str(conv.id))

    from services.events_service import track_file_uploaded
    track_file_uploaded(str(user.id), mime_type=mime_type, size_bytes=len(file_bytes))

    return UploadResponse(
        id=str(uploaded.id),
        filename=filename,
        size_bytes=len(file_bytes),
        is_image=image_flag,
        mime_type=mime_type,
    )


def _upload_to_drive(
    access_token: str,
    filename: str,
    mime_type: str,
    file_bytes: bytes,
    conv_title: str,
) -> tuple[str, str]:
    """Upload file to Google Drive under Gideon/Uploads/{conv_title}/. Sync function (run in thread)."""
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaInMemoryUpload

    creds = Credentials(token=access_token)
    service = build("drive", "v3", credentials=creds, cache_discovery=False)

    # Find or create Gideon folder
    gideon_folder_id = _find_or_create_folder(service, "Gideon", parent_id=None)
    uploads_folder_id = _find_or_create_folder(service, "Uploads", parent_id=gideon_folder_id)
    # Sanitize conv title for folder name
    safe_title = "".join(c for c in conv_title[:50] if c.isalnum() or c in " -_").strip() or "Untitled"
    conv_folder_id = _find_or_create_folder(service, safe_title, parent_id=uploads_folder_id)

    # Upload file
    media = MediaInMemoryUpload(file_bytes, mimetype=mime_type)
    file_metadata = {"name": filename, "parents": [conv_folder_id]}
    result = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, webViewLink",
    ).execute()

    return result.get("id", ""), result.get("webViewLink", "")


def _find_or_create_folder(service, name: str, parent_id: str | None) -> str:
    """Find a folder by name under parent, or create it."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"

    results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    # Create
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


# ---------------------------------------------------------------------------
# Message Feedback
# ---------------------------------------------------------------------------

class FeedbackRequest(BaseModel):
    feedback: str  # "up" or "down"
    feedback_text: Optional[str] = None


@router.post("/messages/{message_id}/feedback")
async def submit_feedback(
    message_id: str,
    body: FeedbackRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Submit thumbs up/down feedback on an AI message."""
    if body.feedback not in ("up", "down"):
        raise HTTPException(status_code=400, detail="feedback must be 'up' or 'down'")

    user = await get_or_create_user(session, auth_user)

    # Verify message exists and belongs to user's conversation
    result = await session.execute(
        select(Message).where(Message.id == message_id)
    )
    msg = result.scalar_one_or_none()
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")

    # Verify ownership
    conv_result = await session.execute(
        select(Conversation).where(
            Conversation.id == msg.conversation_id,
            Conversation.user_id == user.id,
        )
    )
    if not conv_result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Message not found")

    await session.execute(
        update(Message).where(Message.id == message_id).values(
            feedback=body.feedback,
            feedback_text=body.feedback_text,
        )
    )

    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Conversation Sharing
# ---------------------------------------------------------------------------

class ShareSettingsRequest(BaseModel):
    link_shared: Optional[bool] = None
    shared_with_emails: Optional[list[str]] = None


async def _check_conversation_access(
    conversation_id: str, user_email: str, user_id, session: AsyncSession
) -> tuple:
    """Check if a user has access to a conversation. Returns (conversation, access_type) or raises."""
    result = await session.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if str(conv.user_id) == str(user_id):
        return conv, "owner"
    if conv.link_shared:
        return conv, "shared_link"
    if conv.shared_with_emails and user_email.lower() in conv.shared_with_emails:
        return conv, "shared_email"

    raise HTTPException(status_code=403, detail="Access denied")


@router.patch("/conversations/{conversation_id}/share")
async def update_share_settings(
    conversation_id: str,
    body: ShareSettingsRequest,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Update sharing settings for a conversation. Owner only."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if body.link_shared is not None:
        conv.link_shared = body.link_shared
    if body.shared_with_emails is not None:
        # Normalize: lowercase, deduplicate, remove empties
        conv.shared_with_emails = list({e.lower().strip() for e in body.shared_with_emails if e.strip()})

    # Compute newly-added recipients (relative to the existing dedupe set)
    # BEFORE we commit so we can use the freshly-normalized
    # `shared_with_emails`. Append-only dedupe via
    # `share_notified_email_hashes` lets the owner shuffle the share list
    # freely without re-spamming people. The owner is excluded from
    # recipients so sharing with yourself never DMs you.
    from services.share_notification_service import (
        hash_email,
        schedule_share_notifications,
    )
    owner_email_hash = hash_email(user.email)
    already_notified_hashes = set(conv.share_notified_email_hashes or [])
    new_recipients = sorted(
        e
        for e in (conv.shared_with_emails or [])
        if hash_email(e) not in already_notified_hashes
        and hash_email(e) != owner_email_hash
    )

    # Commit before scheduling: the background task opens its own session
    # and reads the conversation; if this transaction hasn't committed
    # yet, the task sees a stale `shared_with_emails`.
    await session.commit()
    await session.refresh(conv)

    if new_recipients:
        schedule_share_notifications(conv.id, new_recipients)

    from services.events_service import track_conversation_shared
    track_conversation_shared(str(user.id))

    return {
        "link_shared": conv.link_shared,
        "shared_with_emails": conv.shared_with_emails or [],
        "share_url": f"/chat/{conv.id}",
    }


@router.get("/conversations/{conversation_id}/share")
async def get_share_settings(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get sharing settings for a conversation. Owner only."""
    user = await get_or_create_user(session, auth_user)

    result = await session.execute(
        select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user.id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    return {
        "link_shared": conv.link_shared or False,
        "shared_with_emails": conv.shared_with_emails or [],
        "share_url": f"/chat/{conv.id}",
    }


@router.get("/shared/{conversation_id}/meta")
async def get_shared_meta(
    conversation_id: str,
    session: AsyncSession = Depends(get_db),
):
    """Public metadata for OG tags. Returns title + owner name only. No auth required."""
    result = await session.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        return {"title": "Gideon - AI Work Assistant", "owner_name": None}

    # Only return metadata if link sharing is enabled (public access)
    # Email-only shares should not leak title/owner to unauthenticated requests
    if not conv.link_shared:
        return {"title": "Gideon - AI Work Assistant", "owner_name": None}

    owner_result = await session.execute(
        select(User).where(User.id == conv.user_id)
    )
    owner = owner_result.scalar_one_or_none()

    return {
        "title": conv.title or "Shared conversation",
        "owner_name": owner.name if owner else None,
    }


@router.get("/shared/{conversation_id}/access")
async def check_shared_access(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Lightweight access check for a conversation. Any logged-in user.
    Always returns 200 — the access_type field tells the frontend what to render:
      owner / shared_link / shared_email → user can view
      none → forbidden (exists, but user can't view)
      deleted → conversation no longer exists
    """
    user = await get_or_create_user(session, auth_user)
    try:
        _, access_type = await _check_conversation_access(
            conversation_id, user.email, user.id, session
        )
        return {"has_access": True, "access_type": access_type}
    except HTTPException as e:
        if e.status_code == 403:
            return {"has_access": False, "access_type": "none"}
        if e.status_code == 404:
            return {"has_access": False, "access_type": "deleted"}
        raise


@router.get("/shared/{conversation_id}")
async def get_shared_conversation(
    conversation_id: str,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Get a shared conversation with all messages. Read-only access."""
    user = await get_or_create_user(session, auth_user)
    conv, access_type = await _check_conversation_access(
        conversation_id, user.email, user.id, session
    )

    # Get owner name
    owner_result = await session.execute(
        select(User).where(User.id == conv.user_id)
    )
    owner = owner_result.scalar_one_or_none()
    owner_name = owner.name if owner else "Unknown"

    # Get all messages
    msg_result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
    )
    messages = msg_result.scalars().all()

    return {
        "conversation": {
            "id": str(conv.id),
            "title": conv.title,
            "owner_name": owner_name,
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
        },
        "messages": [
            {
                "id": str(m.id),
                "conversation_id": str(m.conversation_id),
                "role": m.role,
                "content": m.content,
                "thinking": m.thinking,
                # Shared chats show EVERYTHING the owner shared, including tool
                # inputs and outputs. The previous policy stripped tool_calls /
                # tool_results for non-owner viewers, which hid every attachment
                # the chat produced (previews, charts, data tables, draft cards,
                # code files) and left shared viewers staring at bare text. The
                # share is an explicit opt-in by the owner ("I'm bearing it all"),
                # so we honor that and pass through both fields regardless of
                # access type. PII redaction, if ever needed, should happen at
                # ingest time (in the tool wrappers), not at view time.
                "tool_calls": m.tool_calls,
                "tool_results": m.tool_results,
                "model": m.model,
                "tokens_used": m.tokens_used,
                "files": m.mentions.get("files") if isinstance(m.mentions, dict) and m.mentions.get("files") else None,
                "status": m.status or "done",
                "created_at": m.created_at.isoformat(),
            }
            for m in messages
        ],
        "access": "owner" if access_type == "owner" else "shared",
    }
