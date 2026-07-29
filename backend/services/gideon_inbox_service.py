"""
Gideon Inbox Service -- polls Gideon's own Gmail (GIDEON_INBOX_EMAIL) for new emails
that contain explicit instructions addressed to Gideon.

Runs as a background task every 60 seconds.
"""

import os
import asyncio
import base64
import re
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown as _markdown
import structlog
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from db.database import AsyncPostgresSessionGideon
from db.models import Conversation, Integration, TriggerConversation, User
from services.encryption_service import decrypt
from services.events_service import track_error

logger = structlog.get_logger()

POLL_INTERVAL_SECONDS = 60

# Gideon's own integration provider name
GIDEON_PROVIDER = "gideon_gmail"

# Pattern to detect explicit instructions addressed to Gideon
TRIGGER_PATTERN = re.compile(
    r"(?:@?gideon|hey gideon|hi gideon|\+gideon)[,:\s]+(.+)",
    re.IGNORECASE | re.DOTALL,
)


def _get_gmail_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def _extract_instruction(text: str) -> str | None:
    """Extract the instruction from email text. Returns None if no trigger found."""
    match = TRIGGER_PATTERN.search(text)
    if match:
        return match.group(1).strip()
    return None


def _parse_email_body(payload: dict) -> str:
    """Extract plain text body from Gmail message payload."""
    body = ""
    if payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                break
    return body


# The mailbox Gideon itself listens on (CC it to trigger a run).
GIDEON_EMAIL = os.environ.get("GIDEON_INBOX_EMAIL", "")


# Inline styles for table elements. Most email clients (Gmail web/mobile,
# Outlook desktop, Apple Mail) strip <head><style> blocks, so the only
# reliable way to style a table in an HTML email is per-element `style=""`
# attributes.
_EMAIL_TABLE_STYLE = (
    "border-collapse:collapse;margin:8px 0;font-size:13px;"
    "border:1px solid #d0d7de;"
)
_EMAIL_TH_STYLE = (
    "border:1px solid #d0d7de;padding:6px 10px;"
    "background:#f6f8fa;text-align:left;font-weight:600;"
)
_EMAIL_TD_STYLE = (
    "border:1px solid #d0d7de;padding:6px 10px;vertical-align:top;"
)


def _inject_style(tag: str, css: str, html_str: str) -> str:
    """Add inline `style="..."` to every opening `<tag>`.

    When the tag already carries a `style="..."` attribute (the markdown
    tables extension emits `style="text-align: center;"` for `:---:`
    alignment), we MERGE rather than skip — our base CSS goes first,
    existing styles last, so user-specified alignment overrides our
    default left-align.
    """
    pattern = re.compile(rf"<{tag}(\s[^>]*)?>", re.IGNORECASE)

    def _sub(m: re.Match) -> str:
        attrs = m.group(1) or ""
        style_match = re.search(r'style="([^"]*)"', attrs, re.IGNORECASE)
        if style_match:
            existing = style_match.group(1).strip().rstrip(";")
            combined = f"{css.rstrip(';')};{existing}" if existing else css
            new_attrs = attrs[:style_match.start(1)] + combined + attrs[style_match.end(1):]
            return f"<{tag}{new_attrs}>"
        return f"<{tag}{attrs} style=\"{css}\">"

    return pattern.sub(_sub, html_str)


_LIST_OR_TABLE_LINE = re.compile(r"^\s*(?:[-*+]\s|\d+\.\s|\|)")


def _ensure_block_separators(text: str) -> str:
    """Insert a blank line before any list/table line that immediately
    follows a non-blank, non-list/non-table line.

    Standard markdown (CommonMark) requires a blank line before a list or
    table for it to be recognized as such. The hand-rolled converter that
    preceded this one was lenient — it treated `- item` as a list item
    regardless of context. Real-world Gideon-drafted email bodies often
    omit the blank line ('Three things:\n- one\n- two'), so we
    pre-process to add it back. Idempotent on already-correct input.
    """
    lines = text.split("\n")
    out: list[str] = []
    for i, line in enumerate(lines):
        if i > 0:
            prev = lines[i - 1]
            if (
                _LIST_OR_TABLE_LINE.match(line)
                and prev.strip()  # previous line had content
                and not _LIST_OR_TABLE_LINE.match(prev)  # and it wasn't itself a list/table line
            ):
                out.append("")
        out.append(line)
    return "\n".join(out)


def _escape_html_keep_blockquotes(text: str) -> str:
    """Neutralize HTML-tag and entity injection while preserving markdown
    blockquote syntax (`> quote`).

    Why this exists: python-markdown 3.x passes raw HTML through unchanged.
    The hand-rolled predecessor here called `html.escape()` on every line,
    which neutralized inline-HTML injection from untrusted content (quoted
    customer emails, ticket comments, web-search snippets) flowing into
    outbound email bodies. Dropping that safeguard reintroduced the
    injection path; this function restores it.

    Why we escape `<` and `&` but NOT `>`:
      - `<` neutralizes every HTML-tag injection (`<script>`,
        `<img onerror>`, `<a href="javascript:...">`, `<svg onload>` —
        all require a `<` to open a tag).
      - `&` blocks entity-encoded re-injection (`&#x3C;script&#x3E;` would
        otherwise decode to `<script>` in the recipient's mail client).
      - `>` is left alone so markdown blockquote syntax (`> quote`) still
        parses. A `>` without a preceding unescaped `<` cannot form a tag.

    Side effect: literal `&amp;` / `&copy;` etc. in the input render as
    literal text (`&amp;`, `&copy;`) rather than decoded entities. That's
    the right trade for an email body — users get the © glyph by typing
    `©` (Unicode), not by typing HTML entities.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;")


def markdown_to_html(text: str) -> str:
    """Convert markdown to HTML for email rendering.

    Uses the `markdown` library with extensions:
      - `tables`     — GFM-style pipe tables (`| a | b |\n|---|---|`)
      - `fenced_code` — ```code``` blocks
      - `sane_lists` — less aggressive list autodetection

    Input is pre-escaped to neutralize inline-HTML injection from
    untrusted content (see `_escape_html_keep_blockquotes`), then
    normalized to insert blank lines before lists/tables that lack
    them (see `_ensure_block_separators`).

    NOTE: `nl2br` is intentionally OFF. With it on, a single newline
    between a paragraph and a following bullet line gets converted to a
    `<br>` BEFORE `sane_lists` can detect the list. Without it, single
    newlines inside a paragraph collapse to spaces (standard markdown
    behavior, which is what email bodies want anyway — text wraps
    naturally and blank lines separate paragraphs).

    Table elements get inline `style=""` attributes so they render in
    Gmail / Outlook / Apple Mail — those clients strip embedded
    `<style>` blocks, so per-element styling is the only reliable path.
    """
    if not text:
        return ""

    escaped = _escape_html_keep_blockquotes(text)
    normalized = _ensure_block_separators(escaped)

    html = _markdown.markdown(
        normalized,
        extensions=["tables", "fenced_code", "sane_lists"],
        output_format="html5",
    )

    html = _inject_style("table", _EMAIL_TABLE_STYLE, html)
    html = _inject_style("th", _EMAIL_TH_STYLE, html)
    html = _inject_style("td", _EMAIL_TD_STYLE, html)

    return html


def _build_raw_reply(
    to: str,
    subject: str,
    body: str,
    in_reply_to: str = "",
    references: str = "",
    cc: str = "",
) -> str:
    """Build a raw email reply with HTML formatting and reply-all support."""
    html_body = markdown_to_html(body)
    html_content = (
        '<div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; '
        f'font-size: 14px; color: #222;">{html_body}</div>'
    )

    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    msg["from"] = f"Gideon AI <{GIDEON_EMAIL}>"
    msg["to"] = to
    if cc:
        msg["cc"] = cc
    msg["subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


def _collect_thread_recipients(
    triggering_sender: str,
    thread_messages: list[dict],
) -> tuple[str, str]:
    """Collect all unique participants from the entire email thread for reply-all.

    Scans From, To, and CC of every message in the thread to find all participants.
    Excludes Gideon's own address. The triggering sender goes in To, everyone else in CC.

    Returns (to, cc).
    """

    def extract_email(addr: str) -> str:
        if "<" in addr and ">" in addr:
            return addr.split("<")[1].split(">")[0].strip().lower()
        return addr.strip().lower()

    # Map email -> display address (preserve "Name <email>" format)
    seen: dict[str, str] = {}
    sender_email = extract_email(triggering_sender)

    for m in thread_messages:
        hdrs = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        for field in ["from", "to", "cc"]:
            value = hdrs.get(field, "")
            if not value:
                continue
            for addr in value.split(","):
                addr = addr.strip()
                if not addr:
                    continue
                email = extract_email(addr)
                if email == GIDEON_EMAIL:
                    continue
                if email not in seen:
                    seen[email] = addr

    # To = triggering sender, CC = everyone else
    cc_list = [addr for email, addr in seen.items() if email != sender_email]
    return triggering_sender, ", ".join(cc_list)


async def _get_gideon_token(session) -> str | None:
    """Get Gideon's own Gmail access token. No user_id filter needed -- gideon_gmail is a
    single-instance system provider (only one row exists, for the Gideon mailbox account)."""
    result = await session.execute(
        select(Integration).where(
            Integration.provider == GIDEON_PROVIDER,
            Integration.is_active == True,
        )
    )
    integ = result.scalar_one_or_none()
    if not integ:
        return None

    token = decrypt(integ.access_token)

    # Refresh if expired
    if integ.token_expiry and datetime.now(timezone.utc) >= integ.token_expiry:
        if integ.refresh_token:
            try:
                from services.oauth.google_oauth import GoogleOAuthProvider
                from services.encryption_service import encrypt
                new_tokens = await GoogleOAuthProvider().refresh_access_token(decrypt(integ.refresh_token))
                integ.access_token = encrypt(new_tokens.access_token)
                if new_tokens.refresh_token:
                    integ.refresh_token = encrypt(new_tokens.refresh_token)
                integ.token_expiry = new_tokens.token_expiry
                await session.flush()
                token = new_tokens.access_token
                logger.info("Gideon Gmail token refreshed")
            except Exception as e:
                logger.error("Gideon Gmail token refresh failed", error=str(e))
                track_error("gideon_system", "token_refresh_failed", provider="google", tool_name="gideon_inbox", detail=str(e)[:500])
                return None

    return token


async def _process_email(session, gmail_service, message_id: str, access_token: str):
    """Process a single email: check for trigger, resolve user, run agent."""
    try:
        msg = await asyncio.to_thread(
            lambda: gmail_service.users().messages().get(
                userId="me", id=message_id, format="full"
            ).execute()
        )

        headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
        sender = headers.get("from", "")
        subject = headers.get("subject", "")
        thread_id = msg.get("threadId", "")
        msg_message_id = headers.get("message-id", "")
        body = _parse_email_body(msg.get("payload", {}))

        # Check for explicit instruction
        instruction = _extract_instruction(body)
        if not instruction:
            logger.debug("No Gideon instruction in email, skipping", message_id=message_id)
            return

        logger.info("Gideon instruction found in email", message_id=message_id, instruction_length=len(instruction))

        # Extract sender email address (strip display name)
        sender_email = sender
        if "<" in sender and ">" in sender:
            sender_email = sender.split("<")[1].split(">")[0]

        # Resolve sender to Gideon user
        user_result = await session.execute(
            select(User).where(User.email == sender_email)
        )
        user = user_result.scalar_one_or_none()
        if not user:
            logger.warning("Email sender not a Gideon user", message_id=message_id)
            return

        # Look up or create trigger conversation
        tc_result = await session.execute(
            select(TriggerConversation).where(
                TriggerConversation.source == "gmail",
                TriggerConversation.source_thread_id == thread_id,
                TriggerConversation.user_id == user.id,
            )
        )
        tc = tc_result.scalar_one_or_none()

        if tc:
            conversation_id = str(tc.conversation_id)
        else:
            try:
                async with session.begin_nested():
                    conv = Conversation(
                        user_id=user.id,
                        title=f"Email: {subject[:80]}",
                    )
                    session.add(conv)
                    await session.flush()
                    await session.refresh(conv)
                    conversation_id = str(conv.id)

                    tc = TriggerConversation(
                        user_id=user.id,
                        conversation_id=conv.id,
                        source="gmail",
                        source_thread_id=thread_id,
                        source_email=sender_email,
                    )
                    session.add(tc)
                await session.commit()
            except IntegrityError:
                await session.rollback()
                tc_result = await session.execute(
                    select(TriggerConversation).where(
                        TriggerConversation.source == "gmail",
                        TriggerConversation.source_thread_id == thread_id,
                        TriggerConversation.user_id == user.id,
                    )
                )
                tc = tc_result.scalar_one_or_none()
                if not tc:
                    logger.error("Gmail trigger conversation creation failed", message_id=message_id)
                    return
                conversation_id = str(tc.conversation_id)

        # Fetch thread context (other emails in the thread) + all participants for reply-all.
        # Use format="full" so we can include the actual body text and To/Cc headers — the
        # agent needs to know who else was on the thread to draft a contextual reply.
        # Budgets prevent a pathological 100-message thread from blowing up the context window.
        thread_context = ""
        thread_messages: list[dict] = []
        PER_MESSAGE_BODY_CAP = 5000
        TOTAL_CONTEXT_CAP = 50000
        try:
            thread_data = await asyncio.to_thread(
                lambda: gmail_service.users().threads().get(
                    userId="me", id=thread_id, format="full",
                ).execute()
            )
            thread_messages = thread_data.get("messages", [])
            # Walk newest -> oldest so we keep the most recent context when truncating.
            entries: list[str] = []
            used = 0
            for m in reversed(thread_messages):
                m_headers = {hdr["name"].lower(): hdr["value"] for hdr in m.get("payload", {}).get("headers", [])}
                body_text = _parse_email_body(m.get("payload", {})).strip()
                if len(body_text) > PER_MESSAGE_BODY_CAP:
                    body_text = body_text[:PER_MESSAGE_BODY_CAP] + "\n[...truncated...]"
                marker = " (current message — contains the @gideon instruction)" if m["id"] == message_id else ""
                entry = (
                    f"From: {m_headers.get('from', '')}{marker}\n"
                    f"To: {m_headers.get('to', '')}\n"
                    f"Cc: {m_headers.get('cc', '')}\n"
                    f"Date: {m_headers.get('date', '')}\n"
                    f"Subject: {m_headers.get('subject', '')}\n"
                    f"\n{body_text}\n---"
                )
                if used + len(entry) > TOTAL_CONTEXT_CAP:
                    entries.append("[...older messages omitted due to size...]")
                    break
                entries.append(entry)
                used += len(entry)
            # Restore chronological order for readability.
            thread_context = "\n\n".join(reversed(entries))
        except Exception as e:
            logger.warning("Failed to fetch Gmail thread context", error=str(e))

        from services.events_service import track_trigger_received
        track_trigger_received(str(user.id), source="gmail")

        # Retrieve user's last-known auth token for permission-gated tools
        from services.user_cache_service import get_user_auth_token
        cached_auth_token = await get_user_auth_token(str(user.id))

        # Run trigger agent
        from services.trigger_agent_service import run_trigger_agent
        response_text = await run_trigger_agent(
            trigger_conv_id=str(tc.id),
            user_id=str(user.id),
            instruction=instruction,
            source_context=thread_context,
            source_info={
                "source": "Gmail",
                "thread_id": thread_id,
                "message_id": message_id,
                "auth_token": cached_auth_token,
            },
            session=session,
        )

        # Auto-send reply-all in the email thread
        if response_text:
            reply_to, reply_cc = _collect_thread_recipients(sender, thread_messages)
            raw = _build_raw_reply(
                to=reply_to,
                subject=subject,
                body=response_text,
                in_reply_to=msg_message_id,
                references=msg_message_id,
                cc=reply_cc,
            )
            await asyncio.to_thread(
                lambda: gmail_service.users().messages().send(
                    userId="me",
                    body={"raw": raw, "threadId": thread_id},
                ).execute()
            )
            logger.info("Gideon replied to email", thread_id=thread_id, message_id=message_id)

    except Exception as e:
        logger.error("Failed to process Gideon email", error=str(e), message_id=message_id)
        track_error("gideon_system", "inbox_email_processing_failed", provider="google", tool_name="gideon_inbox", detail=str(e)[:500])


async def gideon_inbox_poll_loop():
    """Background loop: poll Gideon's Gmail inbox for new emails with instructions."""
    from config import IS_PRODUCTION
    if not IS_PRODUCTION:
        logger.info("Gideon inbox poller disabled (not production)")
        return

    logger.info("Gideon inbox poller started")

    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

            sm = AsyncPostgresSessionGideon()
            async with sm.get_session() as session:
                access_token = await _get_gideon_token(session)
                if not access_token:
                    logger.debug("No Gideon Gmail integration configured, skipping inbox poll")
                    continue

                gmail_service = _get_gmail_service(access_token)

                # Fetch unread emails
                results = await asyncio.to_thread(
                    lambda: gmail_service.users().messages().list(
                        userId="me", q="is:unread", maxResults=10
                    ).execute()
                )

                messages = results.get("messages", [])
                if not messages:
                    continue

                logger.info("Gideon inbox: found unread emails", count=len(messages))

                for msg_ref in messages:
                    await _process_email(session, gmail_service, msg_ref["id"], access_token)

                    # Mark as read
                    try:
                        await asyncio.to_thread(
                            lambda mid=msg_ref["id"]: gmail_service.users().messages().modify(
                                userId="me", id=mid,
                                body={"removeLabelIds": ["UNREAD"]},
                            ).execute()
                        )
                    except Exception as e:
                        logger.warning("Failed to mark email as read", error=str(e))

        except asyncio.CancelledError:
            logger.info("Gideon inbox poller stopped")
            return
        except Exception as e:
            logger.error("Gideon inbox poll error", error=str(e))
            track_error("gideon_system", "inbox_poll_error", provider="google", tool_name="gideon_inbox", detail=str(e)[:500])
            await asyncio.sleep(10)  # Brief pause on error before retrying
