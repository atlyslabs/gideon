import redis.asyncio as aioredis
import structlog

from config import REDIS_HOST, REDIS_PORT, REDIS_PASSWORD, REDIS_DB

logger = structlog.get_logger()

_redis_client: aioredis.Redis | None = None


async def connect_redis() -> aioredis.Redis:
    global _redis_client
    _redis_client = aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD or None,
        db=REDIS_DB,
        decode_responses=True,
    )
    logger.info("Redis connected", host=REDIS_HOST, port=REDIS_PORT)
    return _redis_client


async def disconnect_redis():
    global _redis_client
    if _redis_client:
        await _redis_client.close()
        _redis_client = None
        logger.info("Redis disconnected")


def get_redis() -> aioredis.Redis:
    if _redis_client is None:
        raise RuntimeError("Redis client not initialized. Call connect_redis() first.")
    return _redis_client
