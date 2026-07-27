"""
Grafana RBAC sync + transient-error retry helper.

Two responsibilities:
- Periodic background sync of every user's Grafana role + team
  membership into Redis. The per-request path then reads from Redis and
  never has to fan out to /api/teams/{id}/members itself, so a slow Grafana
  cycle no longer times out tool calls (the original board-digest
  failure mode).
- A reusable `request_with_retry` helper for transient errors (timeouts,
  5xx). Used by the live-fallback path inside `tools/grafana_tool.py` for
  dashboard / folder permission checks too.

Plus a once-a-day token-health probe so a silently-revoked GRAFANA_TOKEN
doesn't sit broken until someone notices in a digest.
"""

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from typing import Optional

import httpx
import structlog

from config import GRAFANA_TOKEN, GRAFANA_URL
from db.redis import get_redis
from services.events_service import track_error

logger = structlog.get_logger()

# Redis key holding `{email_lowercase: {id, role, team_ids}}` for every
# user found in the Grafana org. Roles + team memberships rarely
# change, so we refresh the full map only ONCE A DAY (or on drift detection).
# 25h TTL gives the daily sync a 1h grace window before the cache evaporates.
RBAC_MAP_KEY = "grafana_rbac:user_map"
RBAC_MAP_TTL_SEC = 25 * 3600

# Day-stamp marking the last successful daily sync (UTC YYYY-MM-DD).
RBAC_LAST_SYNC_DAY_KEY = "grafana_rbac:last_sync_day"

# Hash of (userId, role, email) tuples from the last full sync. Used by the
# drift detector to decide whether anything actually changed.
RBAC_DRIFT_HASH_KEY = "grafana_rbac:drift_hash"

# Per-email fallback key written by the live-API fallback path when a user
# is missing from the main map (typically a brand-new employee). Saves the
# next caller from re-fetching.
RBAC_USER_FALLBACK_PREFIX = "grafana_rbac:user_fallback"
RBAC_USER_FALLBACK_TTL_SEC = 3600  # the daily sync should pick them up within 24h

# Per-(email, dashboard_uid) permission decision cache. Captures the result
# of the live folder-walk in `_check_dashboard_access` so back-to-back panel
# queries from the same dashboard skip the walk entirely.
PERM_DECISION_PREFIX = "grafana_perm"
PERM_DECISION_TTL_SEC = 300  # 5 min — recently-moved dashboards self-correct quickly

# Daily flag so the token healthcheck runs at most once per UTC day.
TOKEN_HEALTH_FLAG_PREFIX = "grafana_token_health_checked"

# Retry budget for transient errors. Two attempts is enough to ride through
# a momentary blip without retry-storming a struggling Grafana.
RETRY_ATTEMPTS = 2
RETRY_BACKOFF_SEC = 0.5

_RETRYABLE_STATUSES = {500, 502, 503, 504}


def _grafana_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {GRAFANA_TOKEN}", "Accept": "application/json"}


async def request_with_retry(
    method: str,
    path: str,
    *,
    timeout_sec: float = 10.0,
    json_body: Optional[dict] = None,
    extra_headers: Optional[dict[str, str]] = None,
) -> Optional[httpx.Response]:
    """HTTP wrapper that retries on transient errors only.

    Retries: connection/read timeout, network error, 5xx.
    Does NOT retry: 4xx (real failures — auth, not-found, bad-request).
    Returns the final Response (success or non-retryable failure), or None
    if all attempts raised.
    """
    headers = _grafana_headers()
    if extra_headers:
        headers.update(extra_headers)

    url = f"{GRAFANA_URL}{path}"
    last_exc: BaseException | None = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_sec) as client:
                resp = await client.request(method, url, headers=headers, json=json_body)
            if resp.status_code in _RETRYABLE_STATUSES and attempt < RETRY_ATTEMPTS:
                logger.warning(
                    "Grafana request retrying on 5xx",
                    method=method, path=path, status=resp.status_code, attempt=attempt,
                )
                await asyncio.sleep(RETRY_BACKOFF_SEC)
                continue
            return resp
        except (httpx.TimeoutException, httpx.NetworkError) as e:
            last_exc = e
            if attempt < RETRY_ATTEMPTS:
                logger.warning(
                    "Grafana request retrying on transient error",
                    method=method, path=path, attempt=attempt,
                    error_type=type(e).__name__, error=repr(e),
                )
                await asyncio.sleep(RETRY_BACKOFF_SEC)
                continue
            logger.error(
                "Grafana request failed after retries",
                method=method, path=path, attempts=attempt,
                error_type=type(e).__name__, error=repr(e),
            )
        except Exception as e:
            # Non-retryable — log type + repr so we don't lose context.
            logger.error(
                "Grafana request unexpected error",
                method=method, path=path,
                error_type=type(e).__name__, error=repr(e),
            )
            return None
    return None


# ---------------------------------------------------------------------------
# Background RBAC sync
# ---------------------------------------------------------------------------

def _drift_signature(org_users: list[dict]) -> str:
    """Hash of `(userId, role, email)` tuples — cheap proxy for whether any
    user role / membership changed since the last sync."""
    sig = sorted(
        (u.get("userId"), u.get("role", "Viewer"), (u.get("email") or "").lower())
        for u in org_users
    )
    return hashlib.sha256(json.dumps(sig).encode()).hexdigest()


async def sync_grafana_rbac(org_users_prefetched: Optional[list[dict]] = None) -> int:
    """Run the full Grafana RBAC fan-out and write the user map + drift
    hash + day stamp to Redis. Returns the count of users synced.

    `org_users_prefetched` lets the drift detector pass the org/users payload
    it already fetched, so we don't double-call. Pass `None` for normal use."""
    if not GRAFANA_URL or not GRAFANA_TOKEN:
        return 0

    if org_users_prefetched is not None:
        org_users = org_users_prefetched
    else:
        org_users_resp = await request_with_retry("GET", "/api/org/users", timeout_sec=10)
        if not org_users_resp or org_users_resp.status_code != 200:
            logger.warning(
                "Grafana RBAC sync: org users fetch failed",
                status=org_users_resp.status_code if org_users_resp else "no_response",
            )
            return 0
        org_users = org_users_resp.json()

    teams_resp = await request_with_retry("GET", "/api/teams/search", timeout_sec=10)
    teams: list[dict] = []
    if teams_resp and teams_resp.status_code == 200:
        teams = teams_resp.json().get("teams", [])

    # Parallel fan-out — sequential per-team lookups were the original timeout cause.
    member_responses = await asyncio.gather(
        *[
            request_with_retry("GET", f"/api/teams/{t['id']}/members", timeout_sec=10)
            for t in teams
        ],
        return_exceptions=True,
    )

    team_emails: dict[int, set[str]] = {}
    for team, resp in zip(teams, member_responses):
        if isinstance(resp, BaseException) or resp is None or resp.status_code != 200:
            continue
        team_emails[team["id"]] = {
            (m.get("email") or "").lower() for m in resp.json()
        }

    email_map: dict[str, dict] = {}
    for u in org_users:
        email = (u.get("email") or "").lower()
        if not email:
            continue
        team_ids = sorted(tid for tid, emails in team_emails.items() if email in emails)
        email_map[email] = {
            "id": u.get("userId"),
            "role": u.get("role", "Viewer"),
            "team_ids": team_ids,
        }

    if not email_map:
        return 0

    redis = get_redis()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    drift_hash = _drift_signature(org_users)
    try:
        # Write all three markers together so the drift detector + daily
        # gate observe a consistent state.
        await redis.set(RBAC_MAP_KEY, json.dumps(email_map), ex=RBAC_MAP_TTL_SEC)
        await redis.set(RBAC_DRIFT_HASH_KEY, drift_hash, ex=RBAC_MAP_TTL_SEC)
        await redis.set(RBAC_LAST_SYNC_DAY_KEY, today, ex=RBAC_MAP_TTL_SEC)
    except Exception as e:
        logger.warning("Grafana RBAC cache write failed", error_type=type(e).__name__)
        return 0

    logger.info(
        "Grafana RBAC sync complete",
        users=len(email_map), teams=len(teams),
    )
    return len(email_map)


async def sync_grafana_rbac_if_due() -> int:
    """Daily-gated wrapper. Skips the expensive fan-out if we've already
    synced today. Called every scheduler cycle but actually does work
    once per UTC day."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    redis = get_redis()
    try:
        last = await redis.get(RBAC_LAST_SYNC_DAY_KEY)
    except Exception:
        last = None
    if last == today:
        return 0
    return await sync_grafana_rbac()


async def detect_and_handle_drift() -> bool:
    """Cheap O(1) check — fetch only `/api/org/users`, hash the result, and
    if it differs from the last stored hash, trigger an immediate full sync.
    Catches mid-day role changes within 10 min instead of waiting for the
    next daily sync. Returns True if drift was detected and resync ran."""
    if not GRAFANA_URL or not GRAFANA_TOKEN:
        return False
    resp = await request_with_retry("GET", "/api/org/users", timeout_sec=10)
    if resp is None or resp.status_code != 200:
        return False
    org_users = resp.json()
    new_hash = _drift_signature(org_users)

    redis = get_redis()
    try:
        last_hash = await redis.get(RBAC_DRIFT_HASH_KEY)
    except Exception:
        last_hash = None

    if last_hash == new_hash:
        return False  # no drift; daily cadence handles it

    logger.info("Grafana RBAC drift detected, resyncing")
    # Pass the already-fetched org_users to avoid double-fetching
    count = await sync_grafana_rbac(org_users_prefetched=org_users)
    return count > 0


async def get_cached_user_info(email: str) -> Optional[dict]:
    """Read `{id, role, team_ids}` for a single email. Lookup order:
    1. Main daily-synced map (`RBAC_MAP_KEY`)
    2. Per-user fallback key written by the live-API path on cache miss
    Returns None if both miss — callers fall back to live `_get_grafana_user_info`."""
    if not email:
        return None
    email_lc = email.lower()
    redis = get_redis()

    try:
        raw = await redis.get(RBAC_MAP_KEY)
        if raw:
            try:
                m = json.loads(raw)
                info = m.get(email_lc)
                if info:
                    return {
                        "id": info.get("id"),
                        "role": info.get("role", "Viewer"),
                        "team_ids": set(info.get("team_ids", [])),
                    }
            except (ValueError, TypeError):
                pass
    except Exception:
        pass

    # Fallback key (written by live fetch when user wasn't in the daily map)
    try:
        raw = await redis.get(f"{RBAC_USER_FALLBACK_PREFIX}:{email_lc}")
    except Exception:
        return None
    if not raw:
        return None
    try:
        info = json.loads(raw)
        return {
            "id": info.get("id"),
            "role": info.get("role", "Viewer"),
            "team_ids": set(info.get("team_ids", [])),
        }
    except (ValueError, TypeError):
        return None


async def write_user_fallback(email: str, info: dict) -> None:
    """Persist a single user's RBAC info after a live-API fetch so the next
    caller (and concurrent callers) hit Redis instead of fanning out again.
    Daily sync will eventually fold them into the main map."""
    if not email or not info:
        return
    redis = get_redis()
    payload = {
        "id": info.get("id"),
        "role": info.get("role", "Viewer"),
        "team_ids": sorted(list(info.get("team_ids") or [])),
    }
    try:
        await redis.set(
            f"{RBAC_USER_FALLBACK_PREFIX}:{email.lower()}",
            json.dumps(payload),
            ex=RBAC_USER_FALLBACK_TTL_SEC,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Per-(email, dashboard) permission decision cache
# ---------------------------------------------------------------------------

async def get_cached_perm_decision(email: str, dashboard_uid: str) -> Optional[bool]:
    """Read a previously-computed allow/deny decision. Returns True/False on
    hit, None on miss (caller should run the live folder-walk)."""
    if not email or not dashboard_uid:
        return None
    redis = get_redis()
    try:
        val = await redis.get(f"{PERM_DECISION_PREFIX}:{email.lower()}:{dashboard_uid}")
    except Exception:
        return None
    if val == "allow":
        return True
    if val == "deny":
        return False
    return None


async def set_cached_perm_decision(email: str, dashboard_uid: str, allow: bool) -> None:
    """Cache an allow/deny decision for 5 minutes so back-to-back panel
    queries from the same dashboard share the perm-walk work."""
    if not email or not dashboard_uid:
        return
    redis = get_redis()
    try:
        await redis.set(
            f"{PERM_DECISION_PREFIX}:{email.lower()}:{dashboard_uid}",
            "allow" if allow else "deny",
            ex=PERM_DECISION_TTL_SEC,
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Daily token healthcheck
# ---------------------------------------------------------------------------

async def check_grafana_token_health() -> None:
    """Once per UTC day, hit /api/org with the service-account token. On 401
    we fire `track_error` and log loudly so a silently-revoked token doesn't
    sit broken until someone notices in a digest. Idempotent within a day
    via a Redis flag."""
    if not GRAFANA_URL or not GRAFANA_TOKEN:
        return
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    flag_key = f"{TOKEN_HEALTH_FLAG_PREFIX}:{today}"
    redis = get_redis()
    try:
        already = await redis.get(flag_key)
        if already:
            return
        # Claim the flag first — even if the probe fails we don't want a
        # tight loop hammering Grafana from every scheduler cycle today.
        await redis.set(flag_key, "1", ex=86400 + 3600)
    except Exception:
        # If Redis is degraded, skip rather than risk re-running every cycle.
        return

    resp = await request_with_retry("GET", "/api/org", timeout_sec=10)
    if resp is None:
        logger.error("Grafana token healthcheck: no response")
        track_error(
            user_id="system",
            error_type="grafana_token_health_no_response",
            provider="grafana",
            detail="No response from /api/org during daily healthcheck",
        )
        return
    if resp.status_code == 401:
        logger.error(
            "Grafana token healthcheck: 401 — token expired or revoked",
            status=resp.status_code,
        )
        track_error(
            user_id="system",
            error_type="grafana_token_expired",
            provider="grafana",
            detail="GRAFANA_TOKEN returned 401 from /api/org — rotate the secret in gideon-secrets",
        )
        return
    if resp.status_code != 200:
        logger.warning(
            "Grafana token healthcheck: unexpected status",
            status=resp.status_code,
        )
        track_error(
            user_id="system",
            error_type="grafana_token_health_unexpected",
            provider="grafana",
            detail=f"/api/org returned {resp.status_code}",
        )
        return
    logger.info("Grafana token healthcheck: ok")
