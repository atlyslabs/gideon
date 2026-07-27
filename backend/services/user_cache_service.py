"""
User Cache Service — Redis cache for auth token validation.
Two-level cache:
  1. AUTH_TOKEN:{token_hash} → user_id  (24h TTL)
  2. USER:{user_id} → full user data    (24h TTL)
"""

import hashlib
import json
from typing import Optional

import structlog

from db.redis import get_redis
from services.auth_service import AuthUser

logger = structlog.get_logger()

TTL_SECONDS = 24 * 60 * 60  # 24 hours
TOKEN_PREFIX = "AUTH_TOKEN"
USER_PREFIX = "USER"
LOGIN_KEY = "login"


def _hash_token(authorization: str) -> str:
    return hashlib.sha256(authorization.encode()).hexdigest()[:32]


async def get_cached_user(authorization: str) -> Optional[AuthUser]:
    """Look up user from Redis cache using the auth token."""
    redis = get_redis()
    token_hash = _hash_token(authorization)

    # Level 1: token → user_id
    user_id = await redis.get(f"{TOKEN_PREFIX}:{token_hash}")
    if not user_id:
        return None

    # Level 2: user_id → full user data
    user_data = await redis.hget(f"{USER_PREFIX}:{user_id}", LOGIN_KEY)
    if not user_data:
        return None

    logger.debug("User found in cache", user_id=user_id)
    return AuthUser.from_dict(json.loads(user_data))


async def cache_user(authorization: str, user: AuthUser):
    """Store user in Redis cache at both levels."""
    redis = get_redis()
    token_hash = _hash_token(authorization)

    pipe = redis.pipeline()

    # Level 1: token → user_id
    pipe.set(f"{TOKEN_PREFIX}:{token_hash}", user.id, ex=TTL_SECONDS)

    # Level 2: user_id → full user data
    user_key = f"{USER_PREFIX}:{user.id}"
    pipe.hset(user_key, LOGIN_KEY, json.dumps(user.to_dict()))
    pipe.expire(user_key, TTL_SECONDS)

    await pipe.execute()

    logger.info("User authenticated and cached", user_id=user.id, email=user.email)


async def store_user_auth_token(user_id: str, authorization: str):
    """Store the user's auth token (encrypted) so background agents can use it for RBAC tools."""
    try:
        from services.encryption_service import encrypt
        redis = get_redis()
        await redis.set(f"USER_AUTH_TOKEN:{user_id}", encrypt(authorization), ex=TTL_SECONDS)
    except Exception:
        pass  # Best-effort, non-blocking


async def get_user_auth_token(user_id: str) -> str:
    """Retrieve the user's last-known auth token for background agent use."""
    try:
        from services.encryption_service import decrypt
        redis = get_redis()
        encrypted = await redis.get(f"USER_AUTH_TOKEN:{user_id}")
        if not encrypted:
            return ""
        return decrypt(encrypted)
    except Exception:
        return ""


async def invalidate_cache(authorization: str):
    """Remove cached user data (logout)."""
    redis = get_redis()
    token_hash = _hash_token(authorization)

    user_id = await redis.get(f"{TOKEN_PREFIX}:{token_hash}")
    if user_id:
        await redis.hdel(f"{USER_PREFIX}:{user_id}", LOGIN_KEY)

    await redis.delete(f"{TOKEN_PREFIX}:{token_hash}")
