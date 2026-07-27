"""
Google Directory Service -- resolve email addresses to display names
via Google Workspace Directory (People API).

Results cached in Redis (24h) to avoid repeated API calls.
"""

import json

import httpx
import structlog

from db.redis import get_redis

logger = structlog.get_logger()

CACHE_PREFIX = "google_directory:"
CACHE_TTL = 86400  # 24 hours


async def resolve_email_to_name(email: str, access_token: str) -> str | None:
    """Look up a display name for an email via Google Workspace Directory.
    Returns the name or None if not found. Results are cached in Redis."""
    if not email or not access_token:
        return None

    email_lower = email.strip().lower()

    # Check Redis cache first
    redis = get_redis()
    if redis:
        try:
            cached = await redis.get(f"{CACHE_PREFIX}{email_lower}")
            if cached is not None:
                return cached.decode("utf-8") if cached else None
        except Exception:
            pass

    # Query People API directory
    # Use local part (before @) as query -- full email with @ can cause poor search matches
    query_term = email_lower.split("@")[0] if "@" in email_lower else email_lower

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                "https://people.googleapis.com/v1/people:searchDirectoryPeople",
                params={
                    "query": query_term,
                    "readMask": "names,emailAddresses",
                    "sources": "DIRECTORY_SOURCE_TYPE_DOMAIN_PROFILE",
                    "pageSize": 10,
                },
                headers={"Authorization": f"Bearer {access_token}"},
            )

        if resp.status_code != 200:
            logger.warning("Directory lookup failed", status=resp.status_code, email=email_lower, body=resp.text[:300])
            return None

        data = resp.json()
        for person in data.get("people", []):
            # Match by email
            emails = [
                e.get("value", "").lower()
                for e in person.get("emailAddresses", [])
            ]
            if email_lower in emails:
                names = person.get("names", [])
                if names:
                    display_name = names[0].get("displayName", "")
                    if display_name:
                        # Cache the result
                        if redis:
                            try:
                                await redis.set(f"{CACHE_PREFIX}{email_lower}", display_name, ex=CACHE_TTL)
                            except Exception:
                                pass
                        return display_name

        # Cache negative result with short TTL (5 min) so retries happen soon
        if redis:
            try:
                await redis.set(f"{CACHE_PREFIX}{email_lower}", "", ex=300)
            except Exception:
                pass

    except Exception as e:
        logger.debug("Directory lookup error", error=str(e))

    return None


async def resolve_emails_to_names(emails: list[str], access_token: str) -> dict[str, str]:
    """Resolve multiple emails to display names. Returns {email: name} mapping."""
    result = {}
    for email in emails:
        name = await resolve_email_to_name(email.strip(), access_token)
        if name:
            result[email.strip().lower()] = name
    return result
