"""WhatsApp entry point for Gideon (Meta WhatsApp Business Cloud API).

Three responsibilities:
  1. Send  — text / typing / auth-template (OTP) to a number.
  2. Verify — validate Meta's webhook signature so only Meta drives the lane.
  3. OTP    — issue + check one-time codes (Redis) for the in-app linking flow.

Identity is NOT the phone number alone: a number is only trusted after it's been
OTP-verified and bound to a Gideon user in `whatsapp_links`. This module provides
the primitives; resolution + the agent loop live in the router/linking service.

The feature is inert unless fully configured (`is_configured()` false) — every
send/verify path short-circuits, so an unprovisioned env is safe.
"""

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from typing import Any, Optional

import httpx
import structlog
from openai import AsyncOpenAI

from config import (
    OPENAI_API_KEY,
    WHATSAPP_ACCESS_TOKEN,
    WHATSAPP_APP_SECRET,
    WHATSAPP_GRAPH_VERSION,
    WHATSAPP_OTP_TEMPLATE,
    WHATSAPP_PHONE_NUMBER_ID,
)
from db.redis import get_redis

logger = structlog.get_logger()

_GRAPH_BASE = f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}"
_MESSAGES_URL = f"{_GRAPH_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"

# OTP policy
_OTP_TTL_SECONDS = 300  # code valid 5 minutes
_OTP_MAX_ATTEMPTS = 5  # wrong-guess attempts before the code is burned
_OTP_RATE_MAX = 3  # max issues per number per window
_OTP_RATE_WINDOW_SECONDS = 1800  # 30 min
_WAMID_DEDUP_TTL = 600  # webhook retry dedup window
_MEDIA_MAX_BYTES = 20 * 1024 * 1024  # cap on inbound media we download + process

# Per-lane serialization. A user typically fires several WhatsApp messages in a
# row; each is processed in its own background task. Without a lane lock, two
# tasks run the trigger agent on the SAME conversation concurrently, interleave
# writes to the shared history, and one reads the other's half-written turn
# (assistant tool_use persisted, its tool_result not yet) -> a 400 the user sees
# as an error reply next to the good one. This lock makes a burst run one at a
# time, in order.
_LANE_LOCK_TTL_SEC = 600        # lease length; renewed on a heartbeat while the worker runs
_LANE_LOCK_POLL_SEC = 1.5       # gap between acquire attempts while queued
_LANE_LOCK_MAX_WAIT_SEC = 600   # stop waiting after this and proceed (fail-open); matches the TTL
_LANE_LOCK_RENEW_SEC = 180      # heartbeat interval to renew the lease (< TTL, so it never lapses mid-run)

# Per-lane message queue. Rapid-fire messages from one number are pushed here and
# drained together into a single agent turn (see the router's settle-window flow),
# so a burst produces one combined reply instead of one reply per message.
_LANE_QUEUE_MAX = 200           # keep only the most recent N queued messages (spam guard); overflow is logged
_LANE_QUEUE_TTL_SEC = 3600      # queue key self-expires so an abandoned lane doesn't linger

# WhatsApp voice notes are ogg/opus; audio clips may be mp3/m4a/aac/amr/wav.
# Whisper infers the decoder from the filename extension, so map mime -> ext.
_AUDIO_MIME_EXT = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/mpeg": "mp3",
    "audio/mp3": "mp3", "audio/mp4": "m4a", "audio/aac": "aac",
    "audio/amr": "amr", "audio/wav": "wav", "audio/x-wav": "wav",
}

# One lazily-created AsyncOpenAI client reused across voice-note transcriptions
# so sustained audio traffic keeps connection pooling instead of spinning up
# (and leaking the async HTTP transport of) a fresh client per message.
_openai_client: Optional[AsyncOpenAI] = None


def _get_openai_client() -> AsyncOpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)
    return _openai_client


def is_configured() -> bool:
    """True only when every WhatsApp secret is present. Gates the whole feature."""
    return bool(
        WHATSAPP_PHONE_NUMBER_ID and WHATSAPP_ACCESS_TOKEN and WHATSAPP_APP_SECRET
    )


# Default region is India: a bare 10-digit mobile without a country code is
# assumed +91. This MUST run on both link input and inbound `from` so they
# produce the same canonical form (the bug: a number linked as "9971695244"
# never matched the "919999999944" WhatsApp actually sends inbound).
DEFAULT_COUNTRY_CODE = "91"


def normalize_phone(raw: str) -> str:
    """Canonicalize to digits-only E.164 (no '+', no spaces). Meta's webhook
    `from` and the send `to` are both full-international digit strings, so we
    prepend the default country code to a bare 10-digit Indian mobile to match.
    Idempotent on already-prefixed numbers (they aren't 10 digits)."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10 and digits[0] in "6789":
        digits = DEFAULT_COUNTRY_CODE + digits
    return digits


def phone_hash(raw: str) -> str:
    """Deterministic lookup key for `whatsapp_links.phone_hash`. Sha256 of the
    normalized number — matchable (unlike Fernet) and PII-safe at rest."""
    return hashlib.sha256(normalize_phone(raw).encode()).hexdigest()


def verify_signature(payload: bytes, header: Optional[str]) -> bool:
    """Validate Meta's `X-Hub-Signature-256: sha256=<hmac>` over the raw body
    using the app secret. Constant-time compare. Returns False if unconfigured
    or the header is missing/malformed — callers must reject on False."""
    if not WHATSAPP_APP_SECRET or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(
        WHATSAPP_APP_SECRET.encode(), payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


async def _post(payload: dict[str, Any]) -> dict[str, Any]:
    """POST to the WhatsApp messages endpoint. Never raises — returns a
    {success, ...} envelope so callers/the lane don't break on a send failure."""
    if not is_configured():
        return {"success": False, "error": "whatsapp_not_configured"}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                _MESSAGES_URL,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code == 200:
            return {"success": True, "data": resp.json()}
        logger.warning(
            "WhatsApp send failed", status=resp.status_code, body=resp.text[:300]
        )
        return {"success": False, "error": resp.text[:300], "status": resp.status_code}
    except Exception as e:
        logger.error("WhatsApp send exception", error=str(e)[:160])
        return {"success": False, "error": str(e)[:160]}


async def send_text(to: str, message: str) -> dict[str, Any]:
    """Send a plain text message (only valid inside the 24h customer-service
    window the user opened by messaging us — fine for replies in the lane)."""
    return await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to),
            "type": "text",
            "text": {"body": message},
        }
    )


async def send_typing(message_id: str) -> None:
    """Mark a message read + show the typing indicator. Best-effort."""
    if not is_configured() or not message_id:
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                _MESSAGES_URL,
                headers={
                    "Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}",
                    "Content-Type": "application/json",
                },
                json={
                    "messaging_product": "whatsapp",
                    "status": "read",
                    "message_id": message_id,
                    "typing_indicator": {"type": "text"},
                },
            )
    except Exception as e:
        logger.warning("WhatsApp typing indicator failed", error=str(e)[:120])


# --- Inbound media ----------------------------------------------------------


async def download_media(media_id: str) -> Optional[tuple[bytes, str]]:
    """Fetch an inbound media object's bytes + mime via Meta's two-step media API:
    `GET /{media-id}` returns a short-lived CDN url + mime + size, then `GET` that
    url (bearer required) returns the bytes. One helper serves every media type
    (image / sticker / document / audio). Returns `(bytes, mime)` or None on any
    failure or when over `_MEDIA_MAX_BYTES`. Never raises."""
    if not is_configured() or not media_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            meta = await client.get(
                f"{_GRAPH_BASE}/{media_id}",
                headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"},
            )
            if meta.status_code != 200:
                logger.warning(
                    "WhatsApp media meta fetch failed",
                    status=meta.status_code, body=meta.text[:200],
                )
                return None
            info = meta.json()
            url = info.get("url")
            mime = (info.get("mime_type") or "").split(";")[0].strip()
            size = int(info.get("file_size") or 0)
            if not url:
                return None
            if size and size > _MEDIA_MAX_BYTES:
                logger.warning("WhatsApp media over size cap", size=size)
                return None
            # STREAM the CDN download and abort once we cross the cap, instead of
            # buffering the whole response first — `file_size` above is a cheap
            # pre-check but can be absent or wrong, so enforce the ceiling during
            # download too. The CDN url also requires the bearer token.
            chunks: list[bytes] = []
            total = 0
            async with client.stream(
                "GET", url, headers={"Authorization": f"Bearer {WHATSAPP_ACCESS_TOKEN}"}
            ) as blob:
                if blob.status_code != 200:
                    logger.warning("WhatsApp media download failed", status=blob.status_code)
                    return None
                async for chunk in blob.aiter_bytes():
                    total += len(chunk)
                    if total > _MEDIA_MAX_BYTES:
                        logger.warning(
                            "WhatsApp media exceeded size cap mid-download",
                            cap=_MEDIA_MAX_BYTES,
                        )
                        return None
                    chunks.append(chunk)
            return b"".join(chunks), mime
    except Exception as e:
        logger.error("WhatsApp media download exception", error=str(e)[:160])
        return None


async def transcribe_audio(audio_bytes: bytes, mime_type: str) -> Optional[str]:
    """Transcribe a WhatsApp voice note / audio clip to text via OpenAI Whisper
    (the same OpenAI account already used for memory/embeddings). WhatsApp voice
    notes are ogg/opus, which Whisper accepts. Returns the transcript, or None on
    failure / empty audio. Best-effort — never raises into the caller."""
    if not OPENAI_API_KEY or not audio_bytes:
        return None
    try:
        ext = _AUDIO_MIME_EXT.get((mime_type or "").split(";")[0].strip(), "ogg")
        client = _get_openai_client()
        resp = await client.audio.transcriptions.create(
            model="whisper-1",
            file=(f"audio.{ext}", audio_bytes, mime_type or "audio/ogg"),
        )
        text = (resp.text or "").strip()
        return text or None
    except Exception as e:
        logger.error("WhatsApp audio transcription failed", error=str(e)[:160])
        return None


async def _send_otp_template(to: str, code: str) -> dict[str, Any]:
    """Deliver the OTP via the approved authentication template. A cold number
    (hasn't messaged us in 24h) can only be reached by a template, hence this."""
    return await _post(
        {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": normalize_phone(to),
            "type": "template",
            "template": {
                "name": WHATSAPP_OTP_TEMPLATE,
                "language": {"code": "en"},
                "components": [
                    {"type": "body", "parameters": [{"type": "text", "text": code}]},
                    # Auth templates use a one-tap copy-code button bound to the code.
                    {
                        "type": "button",
                        "sub_type": "url",
                        "index": "0",
                        "parameters": [{"type": "text", "text": code}],
                    },
                ],
            },
        }
    )


# --- Webhook dedup -----------------------------------------------------------


async def already_seen(wamid: str) -> bool:
    """True if this inbound message id was already processed (Meta retries
    webhooks). Atomic SETNX with TTL — same pattern as the Slack event dedup."""
    if not wamid:
        return False
    try:
        redis = get_redis()
        was_set = await redis.set(f"wa_msg:{wamid}", "1", nx=True, ex=_WAMID_DEDUP_TTL)
        return not was_set
    except Exception:
        return False  # fail-open: better to risk a dup than drop a message


# --- Per-lane serialization --------------------------------------------------


async def acquire_lane_lock(ph: str) -> Optional[str]:
    """Acquire the per-lane processing lock, WAITING (not dropping) while another
    message for the same number is being handled, so a burst is serialized in
    order. Returns a unique token to release with, or None if Redis is
    unavailable / the wait budget was exhausted — in which case the caller
    proceeds anyway (fail-open: dropping the user's message would be worse, and
    the orphan-repair sanitizer covers the small residual race)."""
    key = f"wa_lane_lock:{ph}"
    token = secrets.token_hex(16)
    attempts = max(1, int(_LANE_LOCK_MAX_WAIT_SEC / _LANE_LOCK_POLL_SEC))
    try:
        redis = get_redis()
        for _ in range(attempts):
            if await redis.set(key, token, nx=True, ex=_LANE_LOCK_TTL_SEC):
                return token
            await asyncio.sleep(_LANE_LOCK_POLL_SEC)
    except Exception as e:
        logger.warning("WhatsApp lane lock unavailable; proceeding unlocked", error=str(e)[:200])
        return None
    logger.warning("WhatsApp lane lock wait budget exhausted; proceeding unlocked")
    return None


async def release_lane_lock(ph: str, token: Optional[str]) -> None:
    """Release the per-lane lock iff we still own it (Lua compare-and-delete, so a
    TTL-expired-then-reacquired lock isn't released out from under another task)."""
    if not token:
        return
    lua = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('del', KEYS[1]) else return 0 end"
    )
    try:
        await get_redis().eval(lua, 1, f"wa_lane_lock:{ph}", token)
    except Exception:
        pass  # lock will expire via TTL


async def renew_lane_lock(ph: str, token: Optional[str]) -> Optional[bool]:
    """Extend the lease iff we still own it (Lua compare-and-expire). Called on a
    heartbeat by the lock holder so a long agent run — or a sustained drain loop
    processing many batches back to back — can't let the TTL lapse and allow a
    second worker onto the same lane (the interleaved-write race this lock
    prevents).

    Tri-state so the caller can tell a recoverable blip from a real loss:
    - True  — renewed; we still own the lock.
    - False — CONFIRMED loss: the key is gone or holds another token. Stop.
    - None  — transient Redis error; ownership unknown. The caller should keep
              trying (a single blip must not surrender the lease — the TTL has
              ample headroom over the renew interval)."""
    if not token:
        return False
    lua = (
        "if redis.call('get', KEYS[1]) == ARGV[1] "
        "then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
    )
    try:
        res = await get_redis().eval(lua, 1, f"wa_lane_lock:{ph}", token, _LANE_LOCK_TTL_SEC)
        return bool(res)
    except Exception as e:
        logger.warning("WhatsApp lane lock renew failed (transient)", error=str(e)[:200])
        return None


async def lane_lock_heartbeat(ph: str, token: Optional[str]) -> None:
    """Renew the lane lock on an interval for as long as the holder runs, so a slow
    agent run (up to 50 rounds) or a sustained drain loop can't let the TTL lapse
    and admit a second concurrent worker onto the lane. The caller runs this as a
    background task and cancels it in its `finally`; if the pod dies, the task dies
    with it and the lease expires normally (crash auto-recovery preserved).

    Stops ONLY on a confirmed ownership loss. A transient Redis error is retried on
    the next tick rather than abandoning the lease — the renew interval is well
    under the TTL, so a blip or two is absorbed without the lock lapsing."""
    if not token:
        return
    try:
        while True:
            await asyncio.sleep(_LANE_LOCK_RENEW_SEC)
            if await renew_lane_lock(ph, token) is False:
                logger.warning("WhatsApp lane lock no longer owned; heartbeat stopping")
                return  # confirmed loss — stop renewing
    except asyncio.CancelledError:
        return


async def enqueue_lane_message(ph: str, msg: dict) -> bool:
    """Append a normalized inbound message to the lane's pending queue (ordered,
    capped, self-expiring). Returns True on success, False if Redis is
    unavailable — the caller then falls back to processing that one message
    directly so a Redis outage never drops a user's message."""
    key = f"wa_lane_queue:{ph}"
    try:
        redis = get_redis()
        pipe = redis.pipeline()  # transactional (MULTI/EXEC) by default
        pipe.rpush(key, json.dumps(msg))
        pipe.ltrim(key, -_LANE_QUEUE_MAX, -1)
        pipe.expire(key, _LANE_QUEUE_TTL_SEC)
        results = await pipe.execute()
        # RPUSH returns the pre-trim length. If it exceeds the cap, LTRIM just
        # dropped the oldest (len - cap) already-acked messages. That's an extreme
        # burst (200+ queued for one lane); we keep the most recent for context but
        # LOG the loss so a vanished instruction is diagnosable, not silent.
        length_after_push = results[0] if results else 0
        if isinstance(length_after_push, int) and length_after_push > _LANE_QUEUE_MAX:
            logger.warning(
                "WhatsApp lane queue overflow; dropped oldest messages",
                dropped=length_after_push - _LANE_QUEUE_MAX,
                cap=_LANE_QUEUE_MAX,
            )
        return True
    except Exception as e:
        logger.warning("WhatsApp lane enqueue failed; will process message directly", error=str(e)[:200])
        return False


async def drain_lane_messages(ph: str) -> Optional[list[dict]]:
    """Atomically pop EVERY queued message for the lane (LRANGE + DEL in one
    transaction, so nothing is lost between read and clear) and return them in
    arrival order.

    Returns `[]` for a genuinely empty queue, but `None` when Redis is
    UNAVAILABLE — the caller must distinguish these: on an empty queue there is
    nothing to do, but on a Redis error the DEL likely didn't run so the batch is
    still queued, and treating that as "empty" would strand the acked messages.
    The caller falls back to processing the in-hand message directly on `None`."""
    key = f"wa_lane_queue:{ph}"
    try:
        redis = get_redis()
        pipe = redis.pipeline()  # transactional: read-then-clear is atomic
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        raw, _ = await pipe.execute()
    except Exception as e:
        logger.warning("WhatsApp lane drain failed", error=str(e)[:200])
        return None
    out: list[dict] = []
    for item in raw or []:
        try:
            out.append(json.loads(item))
        except Exception as e:
            # Consistent with the rest of this module: never drop silently.
            logger.warning("WhatsApp lane queue item unreadable; skipping", error=str(e)[:200])
            continue
    return out


# --- OTP (in-app linking) ----------------------------------------------------


async def issue_otp(raw_phone: str) -> dict[str, Any]:
    """Generate, store (hashed), and send a 6-digit OTP to `raw_phone`.

    Rate-limited per number. Returns {sent: bool, reason?}. The code itself is
    never returned or logged — only its sha256 is stored, TTL'd in Redis.
    """
    ph = phone_hash(raw_phone)
    redis = get_redis()
    rate_key = f"wa_otp_rate:{ph}"
    try:
        count = await redis.incr(rate_key)
        if count == 1:
            await redis.expire(rate_key, _OTP_RATE_WINDOW_SECONDS)
        if count > _OTP_RATE_MAX:
            return {"sent": False, "reason": "rate_limited"}
    except Exception as e:
        logger.warning("OTP rate-limit check failed", error=str(e)[:120])

    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    try:
        await redis.set(
            f"wa_otp:{ph}",
            json.dumps({"hash": code_hash, "attempts": 0}),
            ex=_OTP_TTL_SECONDS,
        )
    except Exception as e:
        logger.error("OTP store failed", error=str(e)[:120])
        return {"sent": False, "reason": "store_failed"}

    res = await _send_otp_template(raw_phone, code)
    if not res.get("success"):
        return {"sent": False, "reason": "send_failed"}
    return {"sent": True}


_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
_MD_BOLD_ITALIC = re.compile(r"\*\*\*(.+?)\*\*\*")
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$", re.MULTILINE)
_MD_BULLET = re.compile(r"^(\s*)[-*]\s+", re.MULTILINE)


def markdown_to_whatsapp(text: str) -> str:
    """Convert GFM-ish model output to WhatsApp formatting.

    WhatsApp marks: *bold*, _italic_, ~strike~, ```mono```. It does NOT render
    Markdown links or headings, so links collapse to "text (url)" and headings
    become bold lines. Bold-italic is handled before bold (regex order matters,
    same gotcha as the Slack converter)."""
    if not text:
        return text
    # [text](url) -> "text (url)", or just the url when text == url.
    text = _MD_LINK.sub(
        lambda m: (
            m.group(2) if m.group(1) == m.group(2) else f"{m.group(1)} ({m.group(2)})"
        ),
        text,
    )
    # ***x*** -> *_x_* (bold-italic) BEFORE **x**, else the inner ** is consumed.
    text = _MD_BOLD_ITALIC.sub(r"*_\1_*", text)
    text = _MD_BOLD.sub(r"*\1*", text)  # **bold** -> *bold*
    text = _MD_HEADING.sub(r"*\1*", text)  # ## Heading -> *Heading*
    text = _MD_BULLET.sub(r"\1• ", text)  # - item -> • item
    return text


async def check_otp(raw_phone: str, code: str) -> bool:
    """Verify a submitted OTP. Burns the code on success or attempt-exhaustion."""
    ph = phone_hash(raw_phone)
    redis = get_redis()
    key = f"wa_otp:{ph}"
    try:
        raw = await redis.get(key)
        if not raw:
            return False
        state = json.loads(raw)
        if state.get("attempts", 0) >= _OTP_MAX_ATTEMPTS:
            await redis.delete(key)
            return False
        ok = hmac.compare_digest(
            state.get("hash", ""), hashlib.sha256((code or "").encode()).hexdigest()
        )
        if ok:
            await redis.delete(key)
            return True
        state["attempts"] = state.get("attempts", 0) + 1
        await redis.set(key, json.dumps(state), ex=_OTP_TTL_SECONDS)
        return False
    except Exception as e:
        logger.error("OTP check failed", error=str(e)[:120])
        return False
