"""Auth middleware — the FastAPI dependency every protected route depends on.

`get_current_user` is the single seam between Gideon and whatever identity
system you run. It resolves the caller through `services.auth_service`, whose
backend is selected by `AUTH_MODE` (see that module). Routes never care which
backend is active, so swapping identity providers touches only these two files.
"""

from fastapi import HTTPException, Request
import structlog

from services.auth_service import AUTH_MODE, AuthUser, single_user, verify_headers
from services.user_cache_service import get_cached_user, cache_user

logger = structlog.get_logger()


async def get_current_user(request: Request) -> AuthUser:
    """Extract and validate the caller's credentials.

    Flow:
    1. In single_user mode, short-circuit to the local identity (no header
       needed, nothing to cache).
    2. Otherwise require an Authorization header.
    3. Check the Redis cache (token hash -> user).
    4. On a miss, verify with the configured provider and cache the result.

    Raises:
        HTTPException 401: missing or invalid credentials
        HTTPException 500: the auth provider could not be reached
    """
    # No credentials exist in single-user mode — every request is the owner.
    if AUTH_MODE == "single_user":
        return single_user()

    authorization = request.headers.get("authorization")
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")

    user = await get_cached_user(authorization)
    if user:
        return user

    logger.debug("Auth cache miss, verifying with provider")
    try:
        user = await verify_headers(authorization)
    except Exception:
        raise HTTPException(status_code=500, detail="Authentication verification failed")

    if not user:
        raise HTTPException(status_code=401, detail="Invalid authentication token")

    await cache_user(authorization, user)
    return user
