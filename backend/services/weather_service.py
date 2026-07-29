"""
Weather Service -- polls WeatherAPI for all distinct user cities,
caches results in Redis, and serves cached weather from an endpoint.

Background loop runs every 10 min, fetching weather for all unique cities
in a single batch. Eliminates per-user frontend API calls.
"""

import asyncio
import json
import os
from typing import Optional

import structlog
from aiohttp import ClientSession, ClientTimeout
from sqlalchemy import select

from db.database import session_manager
from db.models import User
from db.redis import get_redis

logger = structlog.get_logger()

WEATHER_API_KEY = os.environ.get("WEATHER_API_KEY", "")
WEATHER_CACHE_PREFIX = "weather:"
WEATHER_CACHE_TTL = 900  # 15 min (polling is 10 min, buffer for staleness)
WEATHER_NEGATIVE_CACHE_TTL = 120  # 2 min — rate-limit retries on failed lookups
_NEGATIVE_SENTINEL = '{"missing":true}'
POLL_INTERVAL = 600  # 10 minutes


async def _write_negative_cache(city: str) -> None:
    """Write a short-lived negative cache entry to avoid hammering the API."""
    try:
        redis = get_redis()
        key = WEATHER_CACHE_PREFIX + city.lower().strip()
        await redis.set(key, _NEGATIVE_SENTINEL, ex=WEATHER_NEGATIVE_CACHE_TTL)
    except Exception:
        pass  # Best-effort


async def _fetch_weather(
    http: ClientSession, city: str
) -> Optional[dict]:
    """Fetch current weather from WeatherAPI for a single city."""
    try:
        async with http.get(
            "https://api.weatherapi.com/v1/current.json",
            params={"key": WEATHER_API_KEY, "q": city},
        ) as resp:
            if resp.status != 200:
                logger.warning("Weather API error", city=city, status=resp.status)
                await _write_negative_cache(city)
                return None
            data = await resp.json()
            return {
                "city": data.get("location", {}).get("name", city),
                "region": data.get("location", {}).get("region", ""),
                "temp_c": data.get("current", {}).get("temp_c", 0),
                "condition_code": data.get("current", {}).get("condition", {}).get("code", 1000),
                "condition_text": data.get("current", {}).get("condition", {}).get("text", ""),
                "icon": data.get("current", {}).get("condition", {}).get("icon", ""),
            }
    except Exception as e:
        logger.warning("Weather fetch failed", city=city, error=str(e))
        await _write_negative_cache(city)
        return None


async def _get_distinct_cities() -> list[str]:
    """Get all distinct non-null cities, normalized to match Redis key format."""
    async with session_manager.get_session() as session:
        result = await session.execute(
            select(User.city).where(User.city.isnot(None), User.city != "")
        )
        # Normalize to match Redis key format (lower+strip) and deduplicate
        seen: set[str] = set()
        cities: list[str] = []
        for row in result.all():
            normalized = row[0].lower().strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                cities.append(row[0].strip())  # Keep original casing for API
        return cities


async def _fetch_and_cache_city(
    city: str, http: ClientSession, redis, sem: asyncio.Semaphore
) -> bool:
    """Fetch weather for one city under semaphore, cache on success. Returns True if cached."""
    async with sem:
        data = await _fetch_weather(http, city)
        if data:
            key = WEATHER_CACHE_PREFIX + city.lower().strip()
            await redis.set(key, json.dumps(data), ex=WEATHER_CACHE_TTL)
            return True
        return False


async def _poll_all_cities() -> int:
    """Fetch weather for all distinct cities and cache in Redis. Returns count."""
    if not WEATHER_API_KEY:
        logger.warning("Weather polling skipped: WEATHER_API_KEY not set")
        return 0

    cities = await _get_distinct_cities()
    if not cities:
        return 0

    redis = get_redis()
    sem = asyncio.Semaphore(5)

    async with ClientSession(timeout=ClientTimeout(total=15)) as http:
        results = await asyncio.gather(*[
            _fetch_and_cache_city(c, http, redis, sem) for c in cities
        ])

    updated = sum(results)
    logger.info("Weather poll complete", cities=len(cities), updated=updated)
    return updated


async def get_cached_weather(city: str) -> Optional[dict]:
    """Get cached weather for a city from Redis. Returns None on miss or negative cache."""
    redis = get_redis()
    key = WEATHER_CACHE_PREFIX + city.lower().strip()
    raw = await redis.get(key)
    if not raw:
        return None
    if raw == _NEGATIVE_SENTINEL:
        return None
    data = json.loads(raw)
    if data.get("missing"):
        return None
    return data


async def _is_negative_cached(city: str) -> bool:
    """Check if a city has a negative cache entry (recent failed lookup)."""
    redis = get_redis()
    key = WEATHER_CACHE_PREFIX + city.lower().strip()
    raw = await redis.get(key)
    return raw == _NEGATIVE_SENTINEL


async def fetch_and_cache_weather(city: str) -> Optional[dict]:
    """Fetch weather for a city on demand and cache in Redis. Public API for routers."""
    if not WEATHER_API_KEY:
        return None
    # Don't re-fetch if we recently failed for this city
    if await _is_negative_cached(city):
        return None
    async with ClientSession(timeout=ClientTimeout(total=10)) as http:
        data = await _fetch_weather(http, city)
    if data:
        redis = get_redis()
        await redis.set(WEATHER_CACHE_PREFIX + city.lower().strip(), json.dumps(data), ex=WEATHER_CACHE_TTL)
    return data


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def weather_poll_loop() -> None:
    """Long-lived loop that polls weather for all user cities every 10 min."""
    logger.info("Weather poll loop started")

    # Initial delay to let the app start
    await asyncio.sleep(5)

    while True:
        try:
            await _poll_all_cities()
        except Exception as e:
            logger.error("Weather poll loop error", error=str(e))

        await asyncio.sleep(POLL_INTERVAL)
