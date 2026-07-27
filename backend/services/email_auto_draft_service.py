"""
Email Auto-Draft Service -- generates AI draft replies for incoming emails.

Called from data_scheduler_loop every 10 minutes. For each user with Google
connected, checks recent inbox emails and generates reply drafts using the
user's learned communication style.
"""

import asyncio
import base64
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import parseaddr
from uuid import UUID

import structlog
from services.auth_service import email_domain_allowed
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from services.llm import get_spec, llm
from db.database import AsyncPostgresSessionGideon
from db.models import ActivityLog, EmailAutoDraft, Integration, Skill, User
from services.integration_health import clear_integration_errors
from services.oauth.token_refresh import get_valid_access_token
from services.usage_service import log_usage

logger = structlog.get_logger()

MAX_DRAFTS_PER_CYCLE = 5
FALLBACK_LOOKBACK_HOURS = 3

# Sender patterns to skip (automated/newsletter emails)
_SKIP_SENDERS = {
    "noreply",
    "no-reply",
    "mailer-daemon",
    "notifications",
    "donotreply",
    "do-not-reply",
}


def _fetch_full_gmail_message(access_token: str, msg_id: str) -> dict:
    """Fetch a full Gmail message by ID (sync, for use with asyncio.to_thread)."""
    service = build(
        "gmail",
        "v1",
        credentials=Credentials(token=access_token),
        cache_discovery=False,
    )
    return (
        service.users().messages().get(userId="me", id=msg_id, format="full").execute()
    )


def create_gmail_draft(
    access_token: str,
    thread_id: str,
    email_id: str,
    draft_body: str,
    user_name: str,
) -> str | None:
    """Create a Gmail draft reply in the original thread. Returns gmail_draft_id or None."""
    service = build(
        "gmail",
        "v1",
        credentials=Credentials(token=access_token),
        cache_discovery=False,
    )

    # Get original message headers for reply
    original = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=email_id,
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID"],
        )
        .execute()
    )
    headers = {
        h["name"].lower(): h["value"]
        for h in original.get("payload", {}).get("headers", [])
    }

    reply_to = headers.get("from", "")
    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = headers.get("message-id", "")

    profile = service.users().getProfile(userId="me").execute()
    user_email = profile.get("emailAddress", "")

    # Build CC list
    original_to = headers.get("to", "")
    original_cc = headers.get("cc", "")
    all_recipients = [
        r.strip() for r in f"{original_to},{original_cc}".split(",") if r.strip()
    ]
    sender_addr = parseaddr(reply_to)[1].lower()
    cc_list = []
    for r in all_recipients:
        addr = parseaddr(r)[1].lower()
        if addr and addr != user_email.lower() and addr != sender_addr:
            cc_list.append(r)

    msg = MIMEText(draft_body, "plain")
    if user_name:
        msg["from"] = f"{user_name} <{user_email}>"
    msg["to"] = reply_to
    if cc_list:
        msg["cc"] = ", ".join(cc_list)
    msg["subject"] = subject
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    result = (
        service.users()
        .drafts()
        .create(
            userId="me",
            body={"message": {"raw": raw, "threadId": thread_id or ""}},
        )
        .execute()
    )
    return result.get("id")


def update_gmail_draft(
    access_token: str,
    gmail_draft_id: str,
    draft_body: str,
    thread_id: str,
    email_id: str,
    user_name: str,
) -> bool:
    """Update an existing Gmail draft. Returns True on success, False if draft was deleted (404)."""
    from googleapiclient.errors import HttpError

    service = build(
        "gmail",
        "v1",
        credentials=Credentials(token=access_token),
        cache_discovery=False,
    )

    original = (
        service.users()
        .messages()
        .get(
            userId="me",
            id=email_id,
            format="metadata",
            metadataHeaders=["From", "To", "Cc", "Subject", "Message-ID"],
        )
        .execute()
    )
    headers = {
        h["name"].lower(): h["value"]
        for h in original.get("payload", {}).get("headers", [])
    }

    reply_to = headers.get("from", "")
    subject = headers.get("subject", "")
    if not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"
    message_id = headers.get("message-id", "")

    profile = service.users().getProfile(userId="me").execute()
    user_email = profile.get("emailAddress", "")

    original_to = headers.get("to", "")
    original_cc = headers.get("cc", "")
    all_recipients = [
        r.strip() for r in f"{original_to},{original_cc}".split(",") if r.strip()
    ]
    sender_addr = parseaddr(reply_to)[1].lower()
    cc_list = []
    for r in all_recipients:
        addr = parseaddr(r)[1].lower()
        if addr and addr != user_email.lower() and addr != sender_addr:
            cc_list.append(r)

    msg = MIMEText(draft_body, "plain")
    if user_name:
        msg["from"] = f"{user_name} <{user_email}>"
    msg["to"] = reply_to
    if cc_list:
        msg["cc"] = ", ".join(cc_list)
    msg["subject"] = subject
    if message_id:
        msg["In-Reply-To"] = message_id
        msg["References"] = message_id

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    try:
        service.users().drafts().update(
            userId="me",
            id=gmail_draft_id,
            body={"message": {"raw": raw, "threadId": thread_id or ""}},
        ).execute()
        return True
    except HttpError as e:
        if e.resp.status == 404:
            return False  # draft already deleted
        raise


def delete_gmail_draft(access_token: str, gmail_draft_id: str) -> None:
    """Delete a Gmail draft. Silently ignores 404 (already deleted by user)."""
    from googleapiclient.errors import HttpError

    service = build(
        "gmail",
        "v1",
        credentials=Credentials(token=access_token),
        cache_discovery=False,
    )
    try:
        service.users().drafts().delete(userId="me", id=gmail_draft_id).execute()
    except HttpError as e:
        if e.resp.status == 404:
            return  # draft already deleted
        raise


def _is_skip_sender(email: str) -> bool:
    """Return True if the sender is automated/newsletter."""
    local = (
        email.split("@")[0].lower().replace(".", "").replace("-", "").replace("_", "")
    )
    return any(skip in local for skip in _SKIP_SENDERS)


def _parse_email_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    body = ""
    if payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode(
            "utf-8", errors="replace"
        )
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get(
                "data"
            ):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode(
                    "utf-8", errors="replace"
                )
                break
    return body[:3000]  # Truncate


# Google tokens are resolved via the shared lock-coordinated helper
# `services.oauth.token_refresh.get_valid_access_token("google", user_id)`.
# It manages its own short-lived DB sessions + a Redis lock, records the
# integration error and flips `is_active=False` on a confirmed-dead refresh
# token (invalid_grant), which breaks the retry-every-cycle spiral this
# service used to cause with an inline `GoogleOAuthProvider().refresh_...`.
# Do NOT call it while this service holds an open DB transaction.


async def _get_email_tone(session: AsyncSession, user_id: UUID) -> str:
    """Load the user's Email Tone skill memory for style matching."""
    try:
        result = await session.execute(
            select(Skill.memory_content).where(
                Skill.user_id == user_id,
                Skill.name == "Email Tone",
                Skill.status == "completed",
                Skill.memory_content.isnot(None),
            )
        )
        row = result.scalar_one_or_none()
        return row or ""
    except Exception:
        return ""


async def _get_sender_avatar(sender_email: str, session: AsyncSession) -> str | None:
    """Look up sender avatar from Slack member cache if internal."""
    if not email_domain_allowed(sender_email):
        return None
    try:
        from services.workspace_member_service import get_member_avatar

        return await get_member_avatar(sender_email, session)
    except Exception:
        return None


def _user_is_addressed(user_email: str, to_list: list[str], cc_header: str) -> bool:
    """Return True if user_email appears in To or CC (exact email match)."""
    from email.utils import getaddresses

    user_lower = user_email.lower()
    # Combine all header strings; getaddresses handles multi-address headers
    all_headers = list(to_list)
    if cc_header:
        all_headers.append(cc_header)
    for _, addr in getaddresses(all_headers):
        if addr.lower() == user_lower:
            return True
    return False


async def _should_draft_reply(
    user_id: UUID,
    user_name: str,
    user_email: str,
    sender_name: str,
    sender_email: str,
    subject: str,
    body: str,
    to_header: str,
    cc_header: str,
) -> bool:
    """Haiku triage: is this an email the user likely needs to reply to?

    Returns False for informational emails, status updates addressed to others,
    mass updates, FYI threads, etc.
    """
    prompt = f"""You are deciding whether {user_name} ({user_email}) needs to reply to this email.

From: {sender_name} <{sender_email}>
To: {to_header}
Cc: {cc_header}
Subject: {subject}

{body[:1500]}

Reply ONLY "YES" or "NO".

Reply YES if:
- The email asks {user_name} a direct question or requests action from them
- {user_name} is in To (not just CC) and a response is expected
- It's a 1:1 or small-group conversation where not replying would be rude

Reply NO if:
- It's a status update, report, or FYI (even if {user_name} is in To/CC)
- The action items are for someone else (e.g. "Alex please check", "team please review")
- It's a mass/broadcast email (large CC list, company-wide)
- It's a notification from a tool or service
- {user_name} is only in CC (unless directly asked something)
- It's a thread where {user_name} was looped in for visibility"""

    try:
        from dataclasses import replace as _replace

        # max_tokens must leave room for the answer AFTER any hidden reasoning.
        # Reasoning models (Kimi k2.5/k2.6, OpenAI o-series) emit
        # reasoning_content that counts against max_tokens — an 8-token cap
        # (which was fine for Anthropic Haiku's bare "YES"/"NO") gets entirely
        # consumed by reasoning on those models, leaving EMPTY visible text.
        # That silently made every email triage as "NO" → drafts stopped
        # completely on Kimi. 512 gives the reasoning room to finish and still
        # emit the verdict; cost is negligible for a triage call.
        triage_spec = _replace(get_spec("suggestions"), max_tokens=512)
        response = await llm.complete(
            triage_spec,
            messages=[{"role": "user", "content": prompt}],
            timeout=90.0,  # was 60s — triage now reasons (Kimi) before YES/NO
        )
        usage = getattr(response.raw, "usage", None) or response.usage
        if usage:
            log_usage(user_id, triage_spec.model, "email_draft_triage", usage)

        # Robust parse: a reasoning model may wrap the verdict in prose, and
        # the visible text could still be empty if reasoning hit the cap. Look
        # for an explicit NO/YES; if the model produced nothing usable, DEFAULT
        # TO DRAFTING (True) — same safe failure mode as the except branch.
        # Better to draft something the user can ignore than to silently never
        # draft (the exact failure we just hit on Kimi).
        answer = (response.text or "").strip().upper()
        if not answer:
            logger.debug("Email draft triage returned empty text, defaulting to draft",
                         model=triage_spec.model)
            return True
        # Decide on the first explicit YES/NO token seen.
        if answer.startswith("NO") or (" NO" in answer and " YES" not in answer):
            return False
        if "YES" in answer:
            return True
        # Ambiguous → draft (conservative toward producing a draft).
        return True
    except Exception as e:
        logger.debug("Email draft triage failed, defaulting to draft", error=str(e))
        return True


async def generate_auto_drafts(
    user_id: UUID, session: AsyncSession, since: datetime | None = None
) -> int:
    """Generate auto-draft replies for a user's recent inbox emails. Returns count of new drafts.

    Uses activity_log for candidate identification (no Gmail list API call),
    then fetches full message body from Gmail only for the few that need drafts.
    """
    # Resolve the Google token FIRST, before any read opens a transaction on
    # `session` — get_valid_access_token manages its own short-lived sessions
    # and Redis lock and must not run inside an open transaction. Returns None
    # for a missing integration or a confirmed-dead refresh token (which it
    # marks needs-reconnect internally) → user is skipped this cycle.
    access_token = await get_valid_access_token("google", user_id)
    if not access_token:
        return 0

    # Token is valid -- clear any prior errors
    integration_result = await session.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == "google",
            Integration.is_active == True,
        )
    )
    integration = integration_result.scalar_one_or_none()
    if not integration:
        return 0
    if integration.error_count and integration.error_count > 0:
        await clear_integration_errors(session, integration.id)

    # Load user info
    user_result = await session.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        return 0

    # Compute lookback from since or fallback
    if since is None:
        since = datetime.now(timezone.utc) - timedelta(hours=FALLBACK_LOOKBACK_HOURS)

    # Read candidates from activity_log (already ingested, no Gmail API call)
    candidates_result = await session.execute(
        select(ActivityLog)
        .where(
            ActivityLog.user_id == user_id,
            ActivityLog.source == "gmail",
            ActivityLog.event_type == "email_received",
            ActivityLog.created_at >= since,
        )
        .order_by(ActivityLog.created_at.desc())
        .limit(10)
    )
    candidates = candidates_result.scalars().all()

    if not candidates:
        return 0

    # Deduplicate by thread: keep only the latest message per thread.
    # Candidates are ordered by created_at DESC, so first seen per thread wins.
    seen_threads: set[str] = set()
    deduped_candidates: list[ActivityLog] = []
    for act in candidates:
        tid = (act.extra_data or {}).get("thread_id", "")
        if tid and tid in seen_threads:
            continue
        if tid:
            seen_threads.add(tid)
        deduped_candidates.append(act)
    candidates = deduped_candidates

    # Mark stale "generating" drafts as failed (crashed/interrupted runs)
    stale_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
    await session.execute(
        update(EmailAutoDraft)
        .where(
            EmailAutoDraft.user_id == user_id,
            EmailAutoDraft.status == "generating",
            EmailAutoDraft.created_at < stale_cutoff,
        )
        .values(status="failed", updated_at=datetime.now(timezone.utc))
    )

    # Check which emails/threads already have drafts
    email_ids = [act.reference_id for act in candidates if act.reference_id]
    thread_ids = [
        (act.extra_data or {}).get("thread_id", "")
        for act in candidates
        if (act.extra_data or {}).get("thread_id")
    ]
    existing_ids: set[str] = set()
    existing_threads: set[str] = set()

    _active_statuses = ("ready", "sent", "skipped", "generating")

    if email_ids:
        existing_result = await session.execute(
            select(EmailAutoDraft.email_id, EmailAutoDraft.thread_id).where(
                EmailAutoDraft.user_id == user_id,
                EmailAutoDraft.email_id.in_(email_ids),
                EmailAutoDraft.status.in_(_active_statuses),
            )
        )
        for row in existing_result.all():
            existing_ids.add(row[0])
            if row[1]:
                existing_threads.add(row[1])

    if thread_ids:
        thread_result = await session.execute(
            select(EmailAutoDraft.thread_id).where(
                EmailAutoDraft.user_id == user_id,
                EmailAutoDraft.thread_id.in_(thread_ids),
                EmailAutoDraft.status.in_(_active_statuses),
            )
        )
        for row in thread_result.all():
            if row[0]:
                existing_threads.add(row[0])

    # Load email tone for style matching
    email_tone = await _get_email_tone(session, user_id)

    # All reads (and the stale-draft sweep) are done. Commit the transaction
    # BEFORE the per-candidate loop starts its external I/O (Gmail fetch +
    # triage/draft LLM calls, up to ~90-120s each) — otherwise this session
    # pins a pool connection idle-in-transaction across the whole loop. Same
    # pattern as meeting_prep_service's pre-fan-out commit; see the project
    # CLAUDE.md rule #3. Writes inside the loop open fresh short transactions
    # and commit immediately, as before. (expire_on_commit=False, so the
    # already-loaded `user` / `candidates` ORM rows stay usable.)
    await session.commit()

    # Filter candidates and fetch full body only for those that need drafts
    new_count = 0

    for act in candidates:
        if new_count >= MAX_DRAFTS_PER_CYCLE:
            break

        msg_id = act.reference_id
        if not msg_id or msg_id in existing_ids:
            continue

        meta = act.extra_data or {}
        sender = meta.get("from", "")
        subject = act.title or ""
        thread_id = meta.get("thread_id", "")

        # Skip if this thread already has a draft (from a different message in the same thread)
        if thread_id and thread_id in existing_threads:
            continue

        # Extract sender email
        sender_email = sender
        if "<" in sender and ">" in sender:
            sender_email = sender.split("<")[1].split(">")[0]

        # Skip automated senders
        if _is_skip_sender(sender_email):
            continue

        # Fetch full message body from Gmail (only for this one email)
        try:
            full_msg = await asyncio.to_thread(
                _fetch_full_gmail_message, access_token, msg_id
            )
        except Exception as e:
            logger.debug(
                "Email draft: failed to fetch full message",
                email_id=msg_id,
                error=str(e),
            )
            continue

        full_headers = {
            h["name"].lower(): h["value"]
            for h in full_msg.get("payload", {}).get("headers", [])
        }

        # Skip mailing-list / group / automated broadcast emails
        # List-* headers are the RFC-standard signal for bulk mail (Google Groups, newsletters, etc.)
        if (
            full_headers.get("list-unsubscribe")
            or full_headers.get("list-id")
            or full_headers.get("list-post")
            or full_headers.get("mailing-list")
            or full_headers.get("precedence", "").lower() in ("bulk", "list", "junk")
            or full_headers.get("auto-submitted", "").lower() not in ("", "no")
        ):
            continue

        # Skip if user is not directly addressed (To or CC)
        to_header = full_headers.get("to", "")
        cc_header = full_headers.get("cc", "")
        if not _user_is_addressed(user.email, [to_header], cc_header):
            continue

        body = _parse_email_body(full_msg.get("payload", {}))
        if not body.strip():
            continue

        date_str = full_headers.get("date", "")

        # Extract sender name
        sender_name = sender_email.split("@")[0]
        if "<" in sender:
            sender_name = sender.split("<")[0].strip().strip('"')

        # Haiku triage: skip emails the user is unlikely to reply to
        should_reply = await _should_draft_reply(
            user_id,
            user.name or "",
            user.email,
            sender_name,
            sender_email,
            subject,
            body,
            to_header,
            cc_header,
        )
        if not should_reply:
            logger.debug(
                "Email draft: triage skipped (low reply likelihood)",
                email_id=msg_id,
                subject=subject[:80],
            )
            # Persist skip so we don't re-triage on the next cycle
            try:
                async with session.begin_nested():
                    session.add(
                        EmailAutoDraft(
                            user_id=user_id,
                            email_id=msg_id,
                            thread_id=thread_id,
                            sender_name=sender_name,
                            sender_email=sender_email,
                            subject=subject,
                            status="skipped",
                        )
                    )
                await session.commit()
            except IntegrityError:
                pass  # already exists
            if thread_id:
                existing_threads.add(thread_id)
            continue

        # Parse date
        email_date = None
        try:
            from email.utils import parsedate_to_datetime

            email_date = parsedate_to_datetime(date_str)
        except Exception:
            email_date = datetime.now(timezone.utc)

        # Get sender avatar
        sender_avatar = await _get_sender_avatar(sender_email, session)

        # Create draft record (savepoint for concurrent insert dedup)
        try:
            async with session.begin_nested():
                draft = EmailAutoDraft(
                    user_id=user_id,
                    email_id=msg_id,
                    thread_id=thread_id,
                    sender_name=sender_name,
                    sender_email=sender_email,
                    sender_avatar_url=sender_avatar,
                    subject=subject,
                    email_body=body[:1500],
                    email_date=email_date,
                    status="generating",
                )
                session.add(draft)
        except IntegrityError:
            # Concurrent insert hit unique index — skip
            logger.debug("Email draft already exists, skipping", email_id=msg_id)
            continue
        await session.refresh(draft)
        draft_id = draft.id

        # Commit placeholder row and release DB connection before calling Claude
        await session.commit()

        # Generate reply with Claude (no DB session held)
        try:
            style_context = ""
            if email_tone:
                style_context = f"\n\n## User's Email Writing Style\n{email_tone}\n\nMatch this style exactly."

            system_prompt = f"""You are drafting an email reply for {user.name} ({user.email}).
Generate a concise, helpful reply to the email below. Write in the user's communication style.
Do NOT include the subject line. Just write the reply body.
Do NOT use em dashes.{style_context}"""

            user_prompt = f"""From: {sender_name} <{sender_email}>
Subject: {subject}
Date: {date_str}

{body[:2000]}"""

            from dataclasses import replace as _replace

            # 16384: a reply draft is short, but Kimi reasoning models spend the
            # budget on reasoning_content first — a 1024 cap left the draft empty.
            draft_spec = _replace(get_spec("email_draft"), max_tokens=16384)
            response = await llm.complete(
                draft_spec,
                messages=[{"role": "user", "content": user_prompt}],
                system=system_prompt,
                timeout=120.0,
            )

            draft_text = response.text

            # Create Gmail draft in the original thread (non-blocking, failure is OK)
            gmail_draft_id = None
            try:
                gmail_draft_id = await asyncio.to_thread(
                    create_gmail_draft,
                    access_token,
                    thread_id,
                    msg_id,
                    draft_text.strip(),
                    user.name or "",
                )
            except Exception as gmail_err:
                logger.debug(
                    "Failed to create Gmail draft",
                    email_id=msg_id,
                    error=str(gmail_err),
                )

            # Update draft with generated text + gmail_draft_id
            update_values = {
                "draft_body": draft_text.strip(),
                "status": "ready",
                "updated_at": datetime.now(timezone.utc),
            }
            if gmail_draft_id:
                update_values["gmail_draft_id"] = gmail_draft_id
            await session.execute(
                update(EmailAutoDraft)
                .where(EmailAutoDraft.id == draft_id)
                .values(**update_values)
            )
            await session.commit()

            usage = getattr(response.raw, "usage", None) or response.usage
            if usage:
                log_usage(user_id, draft_spec.model, "email_draft", usage)

            new_count += 1
            if thread_id:
                existing_threads.add(thread_id)
            logger.info("Email draft generated", user_id=str(user_id), email_id=msg_id)

        except Exception as e:
            logger.error(
                "Email draft generation failed",
                error_type=type(e).__name__,
                email_id=msg_id,
            )
            await session.execute(
                update(EmailAutoDraft)
                .where(EmailAutoDraft.id == draft_id)
                .values(status="failed", updated_at=datetime.now(timezone.utc))
            )
            await session.commit()

    return new_count


DRAFT_CONCURRENCY = 10


async def auto_generate_email_drafts(
    pre_stamp_cursors: dict[UUID, datetime | None] | None = None,
) -> None:
    """Called from data scheduler. Generates auto-drafts for all eligible users.
    Users are processed in parallel, bounded by DRAFT_CONCURRENCY.

    pre_stamp_cursors: if provided, maps user_id -> last_scheduler_run BEFORE stamping.
    This avoids reading the already-advanced timestamp from DB.
    """
    sm = AsyncPostgresSessionGideon()

    user_ids: list[UUID] = []
    async with sm.get_session() as session:
        result = await session.execute(
            select(User.id).where(
                User.id.in_(
                    select(Integration.user_id)
                    .where(
                        Integration.provider == "google",
                        Integration.is_active == True,
                    )
                    .distinct()
                ),
                User.onboarding_completed == True,
                User.feature_email_drafts == True,
            )
        )
        user_ids = [row[0] for row in result.all()]

    if not user_ids:
        return

    # Build (uid, last_run) using pre-stamp cursors when available
    user_data = [
        (uid, pre_stamp_cursors.get(uid) if pre_stamp_cursors else None)
        for uid in user_ids
    ]

    logger.info(
        "Email auto-drafts: checking for eligible users", user_count=len(user_data)
    )

    semaphore = asyncio.Semaphore(DRAFT_CONCURRENCY)
    tasks = [
        _process_user_drafts(uid, last_run, semaphore, sm)
        for uid, last_run in user_data
    ]
    await asyncio.gather(*tasks, return_exceptions=True)


async def _process_user_drafts(
    uid: UUID, last_run: datetime | None, semaphore: asyncio.Semaphore, sm
) -> None:
    """Process email auto-drafts for a single user under semaphore."""
    async with semaphore:
        # Calculate since from last_scheduler_run (with 2 min buffer) or fallback
        if last_run:
            since = last_run - timedelta(minutes=2)
        else:
            since = datetime.now(timezone.utc) - timedelta(
                hours=FALLBACK_LOOKBACK_HOURS
            )

        try:
            async with sm.get_session() as session:
                count = await generate_auto_drafts(uid, session, since=since)
                if count > 0:
                    logger.info(
                        "Email auto-drafts generated", user_id=str(uid), count=count
                    )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(
                "Email auto-draft failed for user", user_id=str(uid), error=str(e)
            )
