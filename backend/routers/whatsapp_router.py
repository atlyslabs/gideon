"""WhatsApp entry point — webhook (Meta) + in-app linking endpoints.

Flow:
  - Linking (auth'd, in-app): start -> OTP to number -> verify -> `whatsapp_links` row.
  - Inbound (webhook): signature-verify -> dedup -> resolve user by phone_hash ->
    if unlinked, refuse + prompt to link; else run the trigger agent in the
    user's WhatsApp lane (`trigger_conversations` source='whatsapp') and reply.

Inert until `whatsapp_service.is_configured()` (all Meta secrets present).
"""

import asyncio
import os
import base64
import json
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import structlog
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from config import WHATSAPP_VERIFY_TOKEN
from db import get_db
from db.database import session_manager
from db.models import Conversation, TriggerConversation, User, WhatsAppLink
from middleware.auth import get_current_user
from services.auth_service import AuthUser
from services.user_service import get_or_create_user
from services.file_extraction_service import extract_text, pdf_pages_as_png
from services.auth_service import email_domain_allowed
from services import whatsapp_service as wa

router = APIRouter()
logger = structlog.get_logger()

GIDEON_APP_URL = os.environ.get("GIDEON_APP_URL", "http://localhost:3000")

# Cap extracted document text folded into the agent instruction, so a
# multi-megabyte doc can't blow the provider's context/request limit and turn
# into an expensive failed agent call. Bounds WhatsApp doc handling predictably.
_WA_MAX_DOC_CHARS = 100_000

# Settle window: after an inbound message is queued, wait this long before draining
# so rapid-fire messages (a user splitting one thought across texts) pile up and get
# answered in a single combined reply. Negligible next to a 30-90s agent run.
_LANE_SETTLE_SEC = 4.0

# Per-combined-turn budgets. The queue cap bounds message COUNT, but a burst of media
# messages could still accumulate gigabytes of base64 into one agent turn and OOM the
# worker / blow the provider request. Bound the combined turn by attachment count,
# total encoded attachment bytes, and total instruction text; content past the budget
# is dropped (and logged) rather than sent.
_WA_MAX_BATCH_ATTACHMENTS = 10
_WA_MAX_BATCH_ATTACHMENT_BYTES = 30 * 1024 * 1024  # ~30 MB base64 across the whole turn
_WA_MAX_BATCH_INSTRUCTION_CHARS = 200_000


def _approx_attachment_bytes(block: dict) -> int:
    """Rough encoded size of an attachment content block — the length of its base64
    payload (≈ the bytes it adds to the request)."""
    src = block.get("source") if isinstance(block, dict) else None
    if isinstance(src, dict):
        data = src.get("data")
        if isinstance(data, str):
            return len(data)
    return 0

# Strong refs to the per-lane lock-renewal heartbeat tasks (asyncio only holds weak
# refs; without this a heartbeat could be GC'd mid-run — see ASYNC300 in CLAUDE.md).
_lane_heartbeats: set = set()


def _on_lane_heartbeat_done(task) -> None:
    """Done-callback for the lock-renewal heartbeat: drop the strong ref and make an
    early exit observable. A normal exit only happens on confirmed lock loss (or an
    unexpected crash); either way the lane's serialization guarantee is no longer
    held, so it must be logged (the worker loop separately notices via
    `heartbeat.done()` and stops taking new batches)."""
    _lane_heartbeats.discard(task)
    if task.cancelled():
        return  # normal teardown in the worker's finally
    exc = task.exception()
    if exc is not None:
        logger.error("Lane lock heartbeat crashed", error=str(exc)[:200])
    else:
        logger.warning("Lane lock heartbeat exited early; lane lock lease may lapse")


def _is_extractor_placeholder(text: str) -> bool:
    """True when `text` is a `file_extraction_service` error/placeholder marker
    (e.g. "[PDF extraction requires …]" / "[Failed to extract …]") rather than
    real document content. Used to distinguish "couldn't parse" from legitimate
    content that merely happens to start with '[' (like a JSON array)."""
    if not text.startswith("["):
        return False
    head = text[:80]
    return "extraction requires" in head or head.startswith("[Failed to extract")


def _mask(phone: str) -> str:
    digits = wa.normalize_phone(phone)
    return f"••••••{digits[-4:]}" if len(digits) >= 4 else "••••"


# ---------------------------------------------------------------------------
# Webhook (Meta)
# ---------------------------------------------------------------------------

@router.get("/webhook")
async def verify_webhook(request: Request):
    """Meta's GET verification handshake — echo `hub.challenge` when the token
    matches. Returns 403 otherwise (and always, if no verify token configured)."""
    p = request.query_params
    if (
        WHATSAPP_VERIFY_TOKEN
        and p.get("hub.mode") == "subscribe"
        and p.get("hub.verify_token") == WHATSAPP_VERIFY_TOKEN
    ):
        return Response(content=p.get("hub.challenge") or "", media_type="text/plain")
    logger.warning("WhatsApp webhook verification failed")
    return Response(status_code=403, content="verification failed")


def _extract_message(body: dict) -> Optional[dict]:
    """Pull the first inbound user message out of a webhook payload, or None for
    status/non-message events. Normalizes each supported type into a flat dict
    the handler consumes (`type` + the type-specific fields). The actual media
    download / transcription / text-conversion happens in `_prepare_agent_input`.
    """
    try:
        value = body["entry"][0]["changes"][0]["value"]
    except (KeyError, IndexError, TypeError):
        return None
    messages = value.get("messages") or []
    if not messages:
        return None  # delivery/read status callbacks, etc.
    m = messages[0]
    mtype = m.get("type")
    out = {"from": m.get("from"), "wamid": m.get("id"), "type": mtype}

    if mtype == "text":
        out["text"] = (m.get("text") or {}).get("body", "")
    elif mtype in ("image", "sticker"):
        media = m.get(mtype) or {}
        out["media_id"] = media.get("id")
        out["mime_type"] = media.get("mime_type")
        out["caption"] = media.get("caption", "")
    elif mtype == "document":
        d = m.get("document") or {}
        out["media_id"] = d.get("id")
        out["mime_type"] = d.get("mime_type")
        out["filename"] = d.get("filename") or "document"
        out["caption"] = d.get("caption", "")
    elif mtype == "audio":  # voice notes arrive as type "audio" (audio.voice=true)
        a = m.get("audio") or {}
        out["media_id"] = a.get("id")
        out["mime_type"] = a.get("mime_type")
    elif mtype == "video":
        out["caption"] = (m.get("video") or {}).get("caption", "")
    elif mtype == "location":
        out["location"] = m.get("location") or {}
    elif mtype == "contacts":
        out["contacts"] = m.get("contacts") or []
    elif mtype == "interactive":
        out["interactive"] = m.get("interactive") or {}
    elif mtype == "reaction":
        out["reaction"] = m.get("reaction") or {}
    # any other/future type falls through with just {from, wamid, type}
    return out


@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """Validate signature + dedup synchronously, ack fast, process in background
    (the agent loop is far slower than Meta's webhook timeout)."""
    raw = await request.body()
    if not wa.verify_signature(raw, request.headers.get("X-Hub-Signature-256")):
        logger.warning("WhatsApp webhook: invalid signature")
        return Response(status_code=403, content="bad signature")
    try:
        body = json.loads(raw)
    except Exception:
        return {"status": "ok"}

    msg = _extract_message(body)
    if not msg or not msg.get("from") or not msg.get("wamid"):
        return {"status": "ok"}
    if await wa.already_seen(msg["wamid"]):
        return {"status": "ok"}

    background_tasks.add_task(_handle_message, msg)
    return {"status": "ok"}


async def _prepare_agent_input(msg: dict, from_number: str) -> Optional[dict]:
    """Turn an inbound WhatsApp message of ANY type into
    `{"instruction": str, "attachments": [<Anthropic content block>, ...]}` for
    the trigger agent, or None when there's nothing to run — either because we
    already sent a terminal reply (video, download failure, unreadable file) or
    the event is ignorable (emoji reaction).

    Coverage: text; image/sticker (Claude vision block); document (PDF as a native
    document block incl. scanned pages, other office/text formats text-extracted);
    audio/voice note (OpenAI Whisper transcript); location / contacts / interactive
    (converted to text); video + any unknown type (polite refusal)."""
    mtype = msg.get("type")
    caption = (msg.get("caption") or "").strip()

    if mtype == "text":
        text = (msg.get("text") or "").strip()
        return {"instruction": text, "attachments": []} if text else None

    if mtype in ("image", "sticker"):
        dl = await wa.download_media(msg.get("media_id"))
        if not dl:
            await wa.send_text(from_number, "I couldn't download that image. Please try sending it again.")
            return None
        data, mime = dl
        block = {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime or "image/jpeg",
                "data": base64.standard_b64encode(data).decode(),
            },
        }
        instruction = caption or "The user sent this image with no caption. Look at it and respond helpfully."
        return {"instruction": instruction, "attachments": [block]}

    if mtype == "document":
        dl = await wa.download_media(msg.get("media_id"))
        if not dl:
            await wa.send_text(from_number, "I couldn't download that file. Please try again.")
            return None
        data, mime = dl
        filename = msg.get("filename") or "document"
        is_pdf = (mime or "").startswith("application/pdf") or filename.lower().endswith(".pdf")

        # Extract text for ALL document types first — this is provider-safe (plain
        # text works on Kimi and Anthropic alike; a native `document`/PDF block
        # only works on Anthropic, and prod runs the trigger agent on Kimi).
        # `extract_text` keys off the extension, so when MIME identifies a PDF but
        # the WhatsApp filename has no `.pdf` suffix, force the PDF parser — else
        # the raw PDF bytes get decoded as plain text and sent as garbage.
        parser_name = filename
        if is_pdf and not filename.lower().endswith(".pdf"):
            parser_name = f"{filename}.pdf"
        try:
            extracted = await extract_text(parser_name, data)
        except Exception as e:
            logger.warning(
                "WhatsApp document text extraction failed",
                filename=filename, error=str(e)[:200],
            )
            extracted = ""
        body = (extracted or "").strip()
        # Accept ANY non-empty extracted text — reject only the extractor's own
        # placeholder/error markers (which look like "[PDF extraction requires …]"
        # / "[Failed to extract …]"). A blanket `startswith("[")` + length gate
        # wrongly dropped legitimate short docs and JSON arrays.
        if body and not _is_extractor_placeholder(body):
            head = caption or f"The user sent a document ('{filename}'). Here is its content:"
            return {"instruction": f"{head}\n\n{body[:_WA_MAX_DOC_CHARS]}", "attachments": []}

        # No usable text. For a scanned PDF, render its pages to images and send
        # them as vision blocks — provider-safe (image blocks translate to Kimi's
        # image_url AND are native on Anthropic), unlike a document block. The
        # render is CPU-bound, so offload it off the event loop.
        if is_pdf:
            pages = await asyncio.to_thread(pdf_pages_as_png, data)
            if pages:
                blocks = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": base64.standard_b64encode(p).decode(),
                        },
                    }
                    for p in pages
                ]
                note = caption or f"The user sent a scanned PDF ('{filename}'). Read its pages and help with it."
                if len(pages) >= 5:
                    note += " (Showing the first 5 pages.)"
                return {"instruction": note, "attachments": blocks}

        await wa.send_text(
            from_number,
            f"I got '{filename}' but couldn't read any text from it. Try sending it as text or a photo.",
        )
        return None

    if mtype == "audio":
        dl = await wa.download_media(msg.get("media_id"))
        if not dl:
            await wa.send_text(from_number, "I couldn't download that voice note. Please try again.")
            return None
        data, mime = dl
        transcript = await wa.transcribe_audio(data, mime)
        if not transcript:
            await wa.send_text(from_number, "I couldn't make out that voice note. Could you type it or try again?")
            return None
        return {"instruction": transcript, "attachments": []}

    if mtype == "video":
        await wa.send_text(
            from_number,
            "Sorry, I can't process videos yet. You can send text, an image, a document, or a voice note.",
        )
        return None

    if mtype == "location":
        loc = msg.get("location") or {}
        label = ", ".join(
            p for p in ((loc.get("name") or "").strip(), (loc.get("address") or "").strip()) if p
        ) or "no label"
        return {
            "instruction": (
                f"The user shared a location: {label} "
                f"(latitude {loc.get('latitude')}, longitude {loc.get('longitude')}). "
                "Help based on this location."
            ),
            "attachments": [],
        }

    if mtype == "contacts":
        lines = []
        for c in (msg.get("contacts") or [])[:10]:
            name = ((c.get("name") or {}).get("formatted_name") or "").strip()
            phones = ", ".join(p.get("phone", "") for p in (c.get("phones") or []) if p.get("phone"))
            lines.append(f"- {name or 'Unknown'}: {phones or 'no number'}")
        body = "\n".join(lines) or "an empty contact card"
        return {"instruction": f"The user shared these contact(s):\n{body}", "attachments": []}

    if mtype == "interactive":
        inter = msg.get("interactive") or {}
        br = inter.get("button_reply") or {}
        lr = inter.get("list_reply") or {}
        picked = (br.get("title") or lr.get("title") or br.get("id") or lr.get("id") or "").strip()
        return {"instruction": picked, "attachments": []} if picked else None

    if mtype == "reaction":
        return None  # ignore emoji reactions

    # Any other / future type — be honest instead of silently dropping it.
    await wa.send_text(
        from_number,
        "I can't handle that kind of message yet. Try text, an image, a document, or a voice note.",
    )
    return None


_WA_SOURCE_CONTEXT = (
    "You are replying inside a WhatsApp chat. Format EVERY reply as plain "
    "WhatsApp text only: WhatsApp markup (*bold*, _italic_, ~strike~, • bullets), "
    "short and mobile-friendly, no long tables. "
    "NEVER output React/JSX components, wireframes, HTML, code-preview blocks, or "
    "image/chart/file artifacts — WhatsApp renders none of those, so always convert "
    "your result into a concise text summary instead. "
    "WhatsApp is for quick chat. If the user asks for something WhatsApp can't show "
    "(a chart, graph, dashboard, spreadsheet, deck, interactive view, or a downloadable "
    "file), still help as much as you can in text, then gently suggest they open "
    "the Gideon web app for the full visual or interactive version. Do not refuse — just "
    "give the text answer and add the nudge."
)


async def _handle_message(msg: dict) -> None:
    """Background entry point (runs after the webhook ack). Queue this message,
    let a short settle window pass so a rapid-fire burst piles up, then — under the
    per-lane lock — drain the whole queue and process it as ONE combined turn.

    Why this shape:
    - Coalescing: a user who splits one thought across several quick texts gets a
      single combined reply, not one reply per message.
    - Serialization: the lane lock guarantees only one agent run per conversation
      at a time, so concurrent runs never race on the shared history (the bug that
      produced a good reply and an error reply side by side). The lock is taken
      OUTSIDE any DB session so waiting never holds a DB connection idle.
    - Redis-outage safe: if enqueue fails, process just this message directly."""
    from_number = msg["from"]
    ph = wa.phone_hash(from_number)

    enqueued = await wa.enqueue_lane_message(ph, msg)
    if enqueued:
        await asyncio.sleep(_LANE_SETTLE_SEC)  # let a burst accumulate before we run

    lane_token = await wa.acquire_lane_lock(ph)
    # Renew the lease on a heartbeat so a long run / sustained drain loop can't let
    # the lock TTL lapse and admit a second concurrent worker onto this lane. Only
    # when we actually hold a lock (Redis-down acquire returns None → no lease to
    # keep alive, and processing proceeds unlocked as a fail-open).
    heartbeat = None
    if lane_token is not None:
        heartbeat = asyncio.create_task(wa.lane_lock_heartbeat(ph, lane_token))
        _lane_heartbeats.add(heartbeat)
        heartbeat.add_done_callback(_on_lane_heartbeat_done)
    try:
        while True:
            # If the heartbeat stopped (confirmed lock loss / crash), we can no longer
            # guarantee exclusive access to the lane — stop taking new batches rather
            # than risk the concurrent-run race the lock exists to prevent.
            if heartbeat is not None and heartbeat.done():
                logger.warning("Lane lock heartbeat stopped; halting drain loop to avoid a concurrent run")
                break
            if enqueued:
                batch = await wa.drain_lane_messages(ph)
                if batch is None:
                    # Redis drain error: the batch is still queued (DEL didn't run),
                    # but don't strand the message we're holding — process it now and
                    # stop (its siblings each have their own task that will fall back
                    # the same way). A subsequent successful drain after Redis
                    # recovers could re-see this message; a duplicate reply is the
                    # acceptable cost of never dropping one.
                    batch = [msg]
                    enqueued = False
            else:
                batch = [msg]
            if not batch:
                break
            await _process_batch(from_number, ph, batch)
            if not enqueued:
                break  # fallback path: single message, nothing to re-drain
    finally:
        if heartbeat is not None:
            heartbeat.cancel()
            # Let the cancellation settle before releasing, so a renew can't fire
            # between our release and the task actually stopping.
            try:
                await asyncio.wait_for(heartbeat, timeout=2.0)
            except (asyncio.CancelledError, Exception):
                pass
        await wa.release_lane_lock(ph, lane_token)


async def _process_batch(from_number: str, ph: str, batch: list[dict]) -> None:
    """Resolve the linked user, normalize every message in the batch (any type)
    into agent input, run the agent ONCE over the combined instruction, and reply.
    Errors here send one apology and return, so a bad batch never wedges the worker
    loop or skips the lock release.

    Order matters: resolve the link FIRST (before any media download/transcription)
    so an unknown number never triggers costly work and always gets the link prompt,
    then acknowledge (read + typing) BEFORE the slow normalization so the user sees
    activity instead of silence."""
    try:
        # 1. Resolve the linked user first (short session), before any slow work.
        async with session_manager.get_session() as session:
            link = (
                await session.execute(
                    select(WhatsAppLink).where(
                        WhatsAppLink.phone_hash == ph, WhatsAppLink.is_active == True
                    )
                )
            ).scalars().first()
            if not link:
                await wa.send_text(
                    from_number,
                    "Hi! This number isn't linked to a Gideon account yet. "
                    f"Link it at {GIDEON_APP_URL}/integrations to start chatting.",
                )
                return
            user_id = link.user_id  # capture the value before the session closes

        # 2. Acknowledge fast: mark read + typing before the (possibly slow) media
        #    downloads / transcription / PDF rendering below.
        await wa.send_typing(batch[-1]["wamid"])

        # 3. Normalize each message. `_prepare_agent_input` may itself send a terminal
        #    reply (video / failed download) and return None for that message; the
        #    rest of the batch still runs. Runs outside any DB session. Bounded by the
        #    per-turn budgets so a media burst can't accumulate unbounded memory /
        #    request size — content past the budget is dropped and logged.
        instr_parts: list[str] = []
        attachments: list[dict] = []
        instr_chars = 0
        attach_bytes = 0
        over_budget = False
        for m in batch:
            prepared = await _prepare_agent_input(m, from_number)
            if not prepared:
                continue
            instr = prepared["instruction"]
            if instr:
                if instr_chars + len(instr) <= _WA_MAX_BATCH_INSTRUCTION_CHARS:
                    instr_parts.append(instr)
                    instr_chars += len(instr)
                else:
                    over_budget = True
            for att in prepared["attachments"]:
                size = _approx_attachment_bytes(att)
                if (
                    len(attachments) < _WA_MAX_BATCH_ATTACHMENTS
                    and attach_bytes + size <= _WA_MAX_BATCH_ATTACHMENT_BYTES
                ):
                    attachments.append(att)
                    attach_bytes += size
                else:
                    over_budget = True

        if over_budget:
            logger.warning(
                "WhatsApp combined turn exceeded budget; dropped overflow content",
                attachments=len(attachments),
                attach_bytes=attach_bytes,
                instr_chars=instr_chars,
            )

        if not instr_parts and not attachments:
            return  # nothing runnable (e.g. a lone video — already replied) / ignorable

        instruction = "\n\n".join(instr_parts) if instr_parts else "(see attached)"

        # 4. Run the agent (fresh session) and reply.
        async with session_manager.get_session() as session:
            tc = await _get_or_create_lane(session, user_id, ph, instruction)
            if not tc:
                await wa.send_text(from_number, "Something went wrong starting our chat. Please try again.")
                return

            from services.user_cache_service import get_user_auth_token
            from services.trigger_agent_service import run_trigger_agent

            auth_token = await get_user_auth_token(str(user_id))
            response_text = await run_trigger_agent(
                trigger_conv_id=str(tc.id),
                user_id=str(user_id),
                instruction=instruction,
                attachments=attachments or None,
                source_context=_WA_SOURCE_CONTEXT,
                source_info={"source": "WhatsApp", "auth_token": auth_token},
                session=session,
            )
            if response_text:
                await wa.send_text(from_number, wa.markdown_to_whatsapp(response_text))
    except Exception as e:
        logger.error("WhatsApp message handling failed", error=str(e)[:200])
        try:
            await wa.send_text(from_number, "Sorry, I hit an error handling that. Please try again.")
        except Exception:
            pass


async def _get_or_create_lane(
    session: AsyncSession, user_id: UUID, phone_hash: str, first_text: str
) -> Optional[TriggerConversation]:
    """Find or create the user's WhatsApp lane (a `trigger_conversations` row,
    source='whatsapp', keyed on the phone hash). Mirrors the Slack/Gmail pattern."""
    tc = (
        await session.execute(
            select(TriggerConversation).where(
                TriggerConversation.source == "whatsapp",
                TriggerConversation.source_thread_id == phone_hash,
                TriggerConversation.user_id == user_id,
            )
        )
    ).scalars().first()
    if tc:
        return tc
    try:
        async with session.begin_nested():
            conv = Conversation(user_id=user_id, title=f"WhatsApp: {first_text[:80]}")
            session.add(conv)
            await session.flush()
            await session.refresh(conv)
            tc = TriggerConversation(
                user_id=user_id,
                conversation_id=conv.id,
                source="whatsapp",
                source_thread_id=phone_hash,
            )
            session.add(tc)
        await session.commit()
        return tc
    except IntegrityError:
        await session.rollback()
        return (
            await session.execute(
                select(TriggerConversation).where(
                    TriggerConversation.source == "whatsapp",
                    TriggerConversation.source_thread_id == phone_hash,
                    TriggerConversation.user_id == user_id,
                )
            )
        ).scalars().first()


# ---------------------------------------------------------------------------
# In-app linking (authenticated)
# ---------------------------------------------------------------------------

class LinkStartBody(BaseModel):
    phone: str


class LinkVerifyBody(BaseModel):
    phone: str
    code: str


@router.post("/link/start")
async def link_start(
    body: LinkStartBody,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Send an OTP to the number the logged-in user wants to link."""
    user = await get_or_create_user(session, auth_user)
    if not email_domain_allowed(user.email or ""):
        raise HTTPException(status_code=403, detail="WhatsApp linking is not available for this account.")
    if not wa.is_configured():
        raise HTTPException(status_code=503, detail="WhatsApp is not configured yet.")
    if len(wa.normalize_phone(body.phone)) < 8:
        raise HTTPException(status_code=400, detail="Enter a valid phone number with country code.")

    res = await wa.issue_otp(body.phone)
    if not res.get("sent"):
        if res.get("reason") == "rate_limited":
            raise HTTPException(status_code=429, detail="Too many attempts. Try again in a bit.")
        raise HTTPException(status_code=502, detail="Could not send the code. Check the number and try again.")
    return {"sent": True}


@router.post("/link/verify")
async def link_verify(
    body: LinkVerifyBody,
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    """Verify the OTP and bind the number to this user."""
    user = await get_or_create_user(session, auth_user)
    if not await wa.check_otp(body.phone, body.code):
        raise HTTPException(status_code=400, detail="That code is invalid or expired.")

    ph = wa.phone_hash(body.phone)
    existing = (
        await session.execute(select(WhatsAppLink).where(WhatsAppLink.phone_hash == ph))
    ).scalars().first()
    # OTP proves current ownership — reassign an inactive/own row, but never steal
    # a number actively linked to a different account.
    if existing and existing.is_active and existing.user_id != user.id:
        raise HTTPException(status_code=409, detail="This number is already linked to another account.")

    now = datetime.now(timezone.utc)
    if existing:
        existing.user_id = user.id
        existing.phone = wa.normalize_phone(body.phone)
        existing.is_active = True
        existing.verified_at = now
        existing.updated_at = now
    else:
        session.add(
            WhatsAppLink(
                user_id=user.id,
                phone_hash=ph,
                phone=wa.normalize_phone(body.phone),
            )
        )
    await session.commit()
    return {"linked": True, "phone_masked": _mask(body.phone)}


@router.get("/link")
async def link_status(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user = await get_or_create_user(session, auth_user)
    link = (
        await session.execute(
            select(WhatsAppLink).where(
                WhatsAppLink.user_id == user.id, WhatsAppLink.is_active == True
            )
        )
    ).scalars().first()
    if not link:
        return {"linked": False, "configured": wa.is_configured()}
    return {"linked": True, "configured": wa.is_configured(), "phone_masked": _mask(link.phone)}


@router.delete("/link")
async def link_delete(
    auth_user: AuthUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_db),
):
    user = await get_or_create_user(session, auth_user)
    await session.execute(
        update(WhatsAppLink).where(WhatsAppLink.user_id == user.id).values(is_active=False)
    )
    await session.commit()
    return {"linked": False}
