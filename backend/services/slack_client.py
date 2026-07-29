"""Shared Slack ``AsyncWebClient`` factory — single source of truth.

slack_sdk does NOT retry HTTP 429s by default: it raises ``SlackApiError``
immediately. Every Slack client in Gideon MUST be built here so that:

- rate-limit responses honor Slack's ``Retry-After`` (``AsyncRateLimitErrorRetryHandler``)
- transient socket drops are absorbed (``AsyncConnectionErrorRetryHandler``)

Without this, a single rate-limited ``conversations.history`` / ``conversations.list``
surfaces as a hard tool/service failure — a scheduled workflow that reads a busy
channel will intermittently die with "rate-limited (429)". ``max_retry_count=3``
keeps wall-clock bounded while smoothing over the common burst-then-recover case.

Do NOT construct ``AsyncWebClient(...)`` directly anywhere else — import
``get_slack_client`` instead.
"""

from slack_sdk.http_retry.builtin_async_handlers import (
    AsyncConnectionErrorRetryHandler,
    AsyncRateLimitErrorRetryHandler,
)
from slack_sdk.web.async_client import AsyncWebClient

# Handler instances are safe to share across clients and requests — slack_sdk
# threads a per-request RetryState through them, so they hold no per-call state.
_RETRY_HANDLERS = [
    AsyncRateLimitErrorRetryHandler(max_retry_count=3),
    AsyncConnectionErrorRetryHandler(max_retry_count=3),
]


def get_slack_client(token: str) -> AsyncWebClient:
    """Build an ``AsyncWebClient`` with 429 + connection retry handling."""
    return AsyncWebClient(token=token, retry_handlers=list(_RETRY_HANDLERS))
