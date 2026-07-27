"""
Slack auto-join service.

When a user connects Slack, Gideon needs to be a member of every public channel
for @Gideon mentions to deliver an `app_mention` event. This module enumerates
all public channels in the workspace and has the bot join them.
"""

import asyncio

import structlog
from slack_sdk.errors import SlackApiError
from services.slack_client import get_slack_client

logger = structlog.get_logger()

# Slack conversations.join is Tier 3 (~50 req/min). One request every 1.3s keeps
# us comfortably under the budget even with other bot traffic in-flight.
JOIN_INTERVAL_SECONDS = 1.3
LIST_PAGE_SIZE = 1000
MAX_CHANNELS_SAFETY_CAP = 5000  # sanity upper bound


async def auto_join_public_channels(bot_token: str) -> dict:
    """Join the bot to every public, non-archived channel it isn't already in.

    Returns a summary dict: {listed, already_member, joined, failed, errors[]}.
    Never raises — logs failures and continues so one bad channel can't abort the sweep.
    """
    client = get_slack_client(token=bot_token)
    summary = {"listed": 0, "already_member": 0, "joined": 0, "failed": 0, "errors": []}

    cursor: str | None = None
    channels_to_join: list[dict] = []

    # Phase 1: enumerate
    while True:
        try:
            resp = await client.conversations_list(
                types="public_channel",
                exclude_archived=True,
                limit=LIST_PAGE_SIZE,
                cursor=cursor or "",
            )
        except SlackApiError as e:
            logger.warning("Slack auto-join: conversations.list failed", error=str(e))
            summary["errors"].append(f"list: {e.response.get('error', str(e))}")
            return summary
        except Exception as e:
            logger.warning("Slack auto-join: conversations.list exception", error=str(e))
            summary["errors"].append(f"list: {str(e)}")
            return summary

        page = resp.get("channels", [])
        summary["listed"] += len(page)

        for ch in page:
            if ch.get("is_member"):
                summary["already_member"] += 1
                continue
            channels_to_join.append({"id": ch["id"], "name": ch.get("name", "")})

        cursor = (resp.get("response_metadata") or {}).get("next_cursor", "")
        if not cursor:
            break
        if summary["listed"] >= MAX_CHANNELS_SAFETY_CAP:
            logger.warning("Slack auto-join: hit safety cap, stopping enumeration", cap=MAX_CHANNELS_SAFETY_CAP)
            break

    logger.info(
        "Slack auto-join: starting joins",
        total_listed=summary["listed"],
        already_member=summary["already_member"],
        to_join=len(channels_to_join),
    )

    # Phase 2: join, throttled
    for ch in channels_to_join:
        try:
            await client.conversations_join(channel=ch["id"])
            summary["joined"] += 1
        except SlackApiError as e:
            err_code = e.response.get("error", str(e))
            # Common benign errors
            if err_code in ("already_in_channel", "method_not_supported_for_channel_type"):
                summary["already_member"] += 1
            elif err_code == "ratelimited":
                # Respect Slack's Retry-After and retry the same channel once
                retry_after = 5
                try:
                    retry_after = int(e.response.headers.get("Retry-After", 5))
                except (ValueError, TypeError, AttributeError):
                    pass
                logger.warning("Slack auto-join: rate limited, sleeping", retry_after=retry_after, channel=ch["name"])
                await asyncio.sleep(retry_after)
                try:
                    await client.conversations_join(channel=ch["id"])
                    summary["joined"] += 1
                except SlackApiError as retry_err:
                    retry_code = retry_err.response.get("error", str(retry_err))
                    if retry_code in ("already_in_channel", "method_not_supported_for_channel_type"):
                        summary["already_member"] += 1
                    else:
                        summary["failed"] += 1
                        summary["errors"].append(f"{ch['name']}: {retry_code} (after retry)")
                except Exception as retry_err:
                    summary["failed"] += 1
                    summary["errors"].append(f"{ch['name']}: {str(retry_err)} (after retry)")
            else:
                summary["failed"] += 1
                summary["errors"].append(f"{ch['name']}: {err_code}")
        except Exception as e:
            summary["failed"] += 1
            summary["errors"].append(f"{ch['name']}: {str(e)}")
        await asyncio.sleep(JOIN_INTERVAL_SECONDS)

    logger.info(
        "Slack auto-join: done",
        joined=summary["joined"],
        already_member=summary["already_member"],
        failed=summary["failed"],
    )
    return summary
