"""
Redis Streams transport for live chat streaming.

Producer writes Fernet-encrypted SSE frames to `chat_stream:{turn_id}`. Any
number of tailers can subscribe via `XREAD BLOCK` and resume from any point
using the Redis entry id as `Last-Event-ID`.

A wall-clock heartbeat refreshes the `:alive` key (so tailers can detect a
dead producer) and the stream key's TTL (so the stream survives as long as
the producer does, even for 10+ minute Claude loops).
"""

import asyncio
import json

import structlog

from db.redis import get_redis
from services.encryption_service import decrypt, encrypt

logger = structlog.get_logger()

# Stream key TTL while the producer is actively running. Refreshed by the
# heartbeat every ALIVE_REFRESH_SEC. Must be > ALIVE_REFRESH_SEC so a brief
# CPU starvation doesn't drop the stream.
STREAM_TTL_ACTIVE_SEC = 60
# TTL applied after the producer writes its sentinel frame — gives late
# reconnects a catch-up window before the stream is reaped.
STREAM_TTL_TERMINAL_SEC = 300
# Producer-liveness marker. Tailers treat its absence (plus no new frames)
# as "producer died".
ALIVE_TTL_SEC = 20
ALIVE_REFRESH_SEC = 10
# XREAD BLOCK timeout (ms). Also drives tailer keepalive cadence.
TAILER_READ_TIMEOUT_MS = 15_000
# Frame payloads whose `type` terminates the stream from a tailer's view.
SENTINEL_EVENT_TYPES = {"done", "error", "aborted"}


def _stream_key(turn_id: str) -> str:
    return f"chat_stream:{turn_id}"


def _alive_key(turn_id: str) -> str:
    return f"chat_stream:{turn_id}:alive"


def _abort_key(turn_id: str) -> str:
    return f"chat_abort:{turn_id}"


def _meta_key(turn_id: str) -> str:
    return f"chat_turn_meta:{turn_id}"


# Keep turn → (user_id, conversation_id) mapping alive long enough to cover
# the terminal stream TTL (so late resumes still pass auth before falling
# back to DB).
META_TTL_SEC = STREAM_TTL_TERMINAL_SEC + 60


async def set_turn_meta(turn_id: str, user_id: str, conversation_id: str) -> None:
    r = get_redis()
    try:
        await r.set(
            _meta_key(turn_id),
            json.dumps({"user_id": user_id, "conversation_id": conversation_id}),
            ex=META_TTL_SEC,
        )
    except Exception as e:
        logger.warning("chat_stream set_turn_meta failed", turn_id=turn_id, err=str(e))


async def get_turn_meta(turn_id: str) -> dict | None:
    r = get_redis()
    try:
        raw = await r.get(_meta_key(turn_id))
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def frame_sentinel_type(frame: str) -> str | None:
    """If the frame's JSON payload has `type` in SENTINEL_EVENT_TYPES, return
    that type. Otherwise None. Safe against malformed frames."""
    stripped = frame.lstrip()
    if not stripped.startswith("data:"):
        return None
    body = stripped[5:].strip()
    # SSE frames always end with a blank line — trim at the first newline.
    first_line = body.split("\n", 1)[0].strip()
    if not first_line:
        return None
    try:
        payload = json.loads(first_line)
    except (ValueError, TypeError):
        return None
    t = payload.get("type") if isinstance(payload, dict) else None
    return t if t in SENTINEL_EVENT_TYPES else None


async def init_stream(turn_id: str) -> None:
    """Create the stream with an initial entry + set the active TTL and alive
    marker. An initial entry is written so `XREAD` from `"0"` works cleanly
    for the very first tailer."""
    r = get_redis()
    try:
        await r.xadd(_stream_key(turn_id), {"data": encrypt(": stream-init\n\n")})
        await r.expire(_stream_key(turn_id), STREAM_TTL_ACTIVE_SEC)
        await r.set(_alive_key(turn_id), "1", ex=ALIVE_TTL_SEC)
    except Exception as e:
        logger.error("chat_stream init failed", turn_id=turn_id, err=str(e))
        raise


async def publish_frame(turn_id: str, frame: str) -> None:
    """XADD one SSE-formatted frame (raw string) as an encrypted entry. Never
    raises — publish failures are logged but must not kill the producer."""
    r = get_redis()
    try:
        ciphertext = encrypt(frame)
        await r.xadd(_stream_key(turn_id), {"data": ciphertext})
    except Exception as e:
        logger.warning("chat_stream publish failed", turn_id=turn_id, err=str(e))


async def heartbeat_loop(turn_id: str, stop_event: asyncio.Event) -> None:
    """Refresh the alive key + the stream's TTL every ALIVE_REFRESH_SEC until
    `stop_event` is set. Runs on a wall-clock timer so a slow tool call
    (e.g. a 2-minute Notion fetch) doesn't cause the alive key to expire."""
    r = get_redis()
    stream_key = _stream_key(turn_id)
    alive_key = _alive_key(turn_id)
    while not stop_event.is_set():
        try:
            await r.set(alive_key, "1", ex=ALIVE_TTL_SEC)
            await r.expire(stream_key, STREAM_TTL_ACTIVE_SEC)
        except Exception as e:
            logger.warning("chat_stream heartbeat failed", turn_id=turn_id, err=str(e))
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=ALIVE_REFRESH_SEC)
        except asyncio.TimeoutError:
            continue


async def finalize_stream(turn_id: str) -> None:
    """Producer is done (or crashed). Drop the alive key and shrink the stream
    key's TTL to the terminal catch-up window."""
    r = get_redis()
    try:
        await r.delete(_alive_key(turn_id))
        await r.expire(_stream_key(turn_id), STREAM_TTL_TERMINAL_SEC)
    except Exception as e:
        logger.warning("chat_stream finalize failed", turn_id=turn_id, err=str(e))


async def is_producer_alive(turn_id: str) -> bool:
    r = get_redis()
    try:
        return bool(await r.get(_alive_key(turn_id)))
    except Exception:
        return False


# Abort flag TTL: must outlive the longest possible producer round gap.
# Claude turns with deep tool loops can spend 5–10 min between abort-flag
# checks (we only poll between rounds, not inside a single `asyncio.gather`
# of tool calls). A 60s TTL could silently expire before the next check on
# those turns. Stream TTL + headroom is a safer bound — the key is cleaned
# up by the producer's finally block in the common case.
_ABORT_FLAG_TTL_SEC = 3600


async def set_abort_flag(turn_id: str) -> None:
    r = get_redis()
    try:
        await r.set(_abort_key(turn_id), "1", ex=_ABORT_FLAG_TTL_SEC)
    except Exception as e:
        logger.warning("chat_stream set_abort failed", turn_id=turn_id, err=str(e))


async def is_aborted(turn_id: str) -> bool:
    r = get_redis()
    try:
        return bool(await r.get(_abort_key(turn_id)))
    except Exception:
        return False


async def clear_abort_flag(turn_id: str) -> None:
    r = get_redis()
    try:
        await r.delete(_abort_key(turn_id))
    except Exception as e:
        logger.warning("chat_stream clear_abort failed", turn_id=turn_id, err=str(e))


async def tail_stream(turn_id: str, last_id: str = "0"):
    """Yield SSE-formatted frames (strings) from the Redis stream starting
    after `last_id`. Terminates when a sentinel frame is seen or the producer
    looks dead (alive key missing on two consecutive blocked reads).

    Each yielded frame is prefixed with an SSE `id:` line so clients can use
    it as `Last-Event-ID` on reconnect.
    """
    r = get_redis()
    stream_key = _stream_key(turn_id)
    alive_key = _alive_key(turn_id)
    cursor = last_id or "0"
    missing_alive_ticks = 0

    while True:
        try:
            resp = await r.xread({stream_key: cursor}, block=TAILER_READ_TIMEOUT_MS, count=200)
        except Exception as e:
            logger.warning("tail_stream xread failed", turn_id=turn_id, err=str(e))
            yield "data: " + json.dumps({"type": "error", "message": "Stream transport error"}) + "\n\n"
            return

        if not resp:
            # Block timed out with no new entries. Check producer liveness.
            try:
                alive = await r.get(alive_key)
            except Exception:
                alive = None
            if not alive:
                missing_alive_ticks += 1
                if missing_alive_ticks >= 2:
                    logger.warning("tail_stream producer dead", turn_id=turn_id)
                    yield "data: " + json.dumps({"type": "error", "message": "Stream ended unexpectedly"}) + "\n\n"
                    return
            else:
                # Producer is alive — reset the miss counter so we only
                # terminate on CONSECUTIVE misses, not cumulative misses
                # across healthy ticks. Intermittent Redis hiccups should
                # not accumulate toward the "dead" threshold.
                missing_alive_ticks = 0
            # Keepalive to prevent proxy idle reap.
            yield ": keepalive\n\n"
            continue

        missing_alive_ticks = 0
        for _stream, entries in resp:
            for entry_id, fields in entries:
                cursor = entry_id
                data = fields.get("data") if isinstance(fields, dict) else None
                if not data:
                    continue
                try:
                    frame = decrypt(data)
                except Exception as e:
                    logger.warning("tail_stream decrypt failed", entry_id=entry_id, err=str(e))
                    continue
                # Skip the init marker — it's internal framing.
                if frame.lstrip().startswith(": stream-init"):
                    continue
                yield f"id: {entry_id}\n{frame}"
                if frame_sentinel_type(frame):
                    return
