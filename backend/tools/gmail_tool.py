"""
Gmail Tools — list, read, search, draft, and send emails via Gmail API.
"""

import asyncio
import base64
import mimetypes
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from services.file_extraction_service import (
    MAX_FILE_SIZE,
    extract_text,
    get_extension,
    is_image,
)
from services.gideon_inbox_service import markdown_to_html
from tools.base import BaseTool


def _get_gmail_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


BODY_TRUNCATE_CHARS = 50000  # generous cap; daily updates / long threads fit easily


def _walk_parts_for_mime(part: dict, target_mime: str) -> str:
    """Recursively find the first part matching *target_mime* and return its
    decoded text body. Handles arbitrarily nested multipart containers
    (multipart/mixed > multipart/alternative > text/plain).
    """
    if part.get("mimeType") == target_mime and part.get("body", {}).get("data"):
        try:
            return base64.urlsafe_b64decode(part["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            return ""
    for sub in part.get("parts", []) or []:
        found = _walk_parts_for_mime(sub, target_mime)
        if found:
            return found
    return ""


def _walk_parts_for_attachments(part: dict, out: list[dict]) -> None:
    """Collect attachment metadata from a Gmail payload tree. Only true
    attachments (parts with a non-empty filename + an `attachmentId` body
    pointer) are surfaced — inline images / signatures are skipped."""
    filename = part.get("filename") or ""
    body = part.get("body") or {}
    attachment_id = body.get("attachmentId")
    if filename and attachment_id:
        out.append(
            {
                "filename": filename,
                "mime_type": part.get("mimeType", ""),
                "size_bytes": body.get("size", 0),
                "attachment_id": attachment_id,
            }
        )
    for sub in part.get("parts", []) or []:
        _walk_parts_for_attachments(sub, out)


def _html_to_text(html: str) -> str:
    """Minimal HTML → text fallback for emails that only ship text/html.
    Drops script/style, converts <br>/</p> to newlines, strips remaining tags,
    and unescapes entities. Good enough for Claude to parse content."""
    import html as html_lib
    import re as _re

    if not html:
        return ""
    # Drop script/style blocks entirely
    html = _re.sub(
        r"<(script|style)[^>]*>.*?</\1>", "", html, flags=_re.DOTALL | _re.IGNORECASE
    )
    # Preserve line breaks
    html = _re.sub(r"<br\s*/?>", "\n", html, flags=_re.IGNORECASE)
    html = _re.sub(r"</p\s*>", "\n\n", html, flags=_re.IGNORECASE)
    html = _re.sub(r"</div\s*>", "\n", html, flags=_re.IGNORECASE)
    html = _re.sub(r"</li\s*>", "\n", html, flags=_re.IGNORECASE)
    # Strip remaining tags
    html = _re.sub(r"<[^>]+>", "", html)
    # Collapse excessive blank lines + decode entities
    html = html_lib.unescape(html)
    html = _re.sub(r"\n{3,}", "\n\n", html)
    return html.strip()


def _parse_message(msg: dict) -> dict:
    """Extract useful fields from a Gmail message. Walks nested multipart
    containers; falls back to HTML (tag-stripped) when no text/plain exists.
    """
    headers = {
        h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])
    }

    payload = msg.get("payload", {})
    body = ""
    body_source = "plain"

    # Top-level body (simple single-part messages). Route through the HTML
    # stripper when the top-level mimeType is text/html — previously those
    # emails returned raw HTML with body_source misreported as "plain".
    top_mime = (payload.get("mimeType") or "").lower()
    if payload.get("body", {}).get("data"):
        try:
            decoded = base64.urlsafe_b64decode(payload["body"]["data"]).decode(
                "utf-8", errors="replace"
            )
        except Exception:
            decoded = ""
        if decoded:
            if top_mime == "text/html":
                body = _html_to_text(decoded)
                body_source = "html"
            else:
                body = decoded

    # Prefer recursive text/plain
    if not body:
        body = _walk_parts_for_mime(payload, "text/plain")

    # Fall back to HTML, tag-stripped
    if not body:
        html_body = _walk_parts_for_mime(payload, "text/html")
        if html_body:
            body = _html_to_text(html_body)
            body_source = "html"

    full_len = len(body)
    truncated = full_len > BODY_TRUNCATE_CHARS
    if truncated:
        body = (
            body[:BODY_TRUNCATE_CHARS]
            + f"\n\n[...truncated — showing {BODY_TRUNCATE_CHARS} of {full_len} chars]"
        )

    attachments: list[dict] = []
    _walk_parts_for_attachments(payload, attachments)

    result: dict[str, Any] = {
        "id": msg["id"],
        "thread_id": msg.get("threadId"),
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "bcc": headers.get("bcc", ""),
        "subject": headers.get("subject", ""),
        "date": headers.get("date", ""),
        "snippet": msg.get("snippet", ""),
        "body": body,
        "body_source": body_source,  # "plain" | "html" — tells model how the body was extracted
        "body_truncated": truncated,
        "labels": msg.get("labelIds", []),
        "attachments": attachments,  # metadata only — call read_email_attachment to fetch contents
    }
    return result


class ListEmails(BaseTool):
    @property
    def name(self) -> str:
        return "list_emails"

    @property
    def description(self) -> str:
        return "List recent emails from Gmail inbox. Returns subject, sender, date, and snippet."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "Maximum number of emails to return (default 10, max 20)",
                    "default": 10,
                },
                "query": {
                    "type": "string",
                    "description": "Gmail search query (e.g. 'is:unread', 'from:boss@company.com')",
                },
            },
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        max_results = min(params.get("max_results", 10), 20)
        query = params.get("query") or "in:inbox"

        def _sync() -> list[dict]:
            service = _get_gmail_service(access_token)
            results = (
                service.users()
                .messages()
                .list(
                    userId="me",
                    maxResults=max_results,
                    q=query,
                )
                .execute()
            )
            out: list[dict] = []
            for msg_ref in results.get("messages", []):
                msg = (
                    service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=msg_ref["id"],
                        format="metadata",
                        metadataHeaders=["From", "To", "Subject", "Date"],
                    )
                    .execute()
                )
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                out.append(
                    {
                        "id": msg["id"],
                        "from": headers.get("from", ""),
                        "subject": headers.get("subject", ""),
                        "date": headers.get("date", ""),
                        "snippet": msg.get("snippet", ""),
                    }
                )
            return out

        messages = await asyncio.to_thread(_sync)
        return {"emails": messages, "count": len(messages)}


class ReadEmail(BaseTool):
    @property
    def name(self) -> str:
        return "read_email"

    @property
    def description(self) -> str:
        return (
            "Read the full content of a specific email by its message ID. "
            "Returns the body plus an `attachments` list with filename, mime_type, "
            "size_bytes, and attachment_id for each attached file. Attachments are "
            "NOT fetched automatically — call read_email_attachment with the "
            "attachment_id only when the user actually needs the contents."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "The Gmail message ID to read",
                },
            },
            "required": ["message_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=params["message_id"],
                    format="full",
                )
                .execute()
            )

        msg = await asyncio.to_thread(_sync)
        return _parse_message(msg)


class ReadEmailAttachment(BaseTool):
    @property
    def name(self) -> str:
        return "read_email_attachment"

    @property
    def description(self) -> str:
        return (
            "Fetch and read an email attachment on demand. Returns extracted text for "
            "documents (PDF, DOCX, XLSX, PPTX, plain text/CSV/JSON), the image bytes "
            "for images (Claude reads them via vision), or the raw PDF as a document "
            "block when the PDF has no text layer. "
            "Get message_id, attachment_id, and filename from the `attachments` list "
            "returned by read_email — only call this when the user actually needs the "
            "attachment contents, not on every read."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID the attachment belongs to.",
                },
                "attachment_id": {
                    "type": "string",
                    "description": "Attachment ID from the email's `attachments` list.",
                },
                "filename": {
                    "type": "string",
                    "description": "Original filename — used to pick the right text extractor and report back to the user.",
                },
            },
            "required": ["message_id", "attachment_id", "filename"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        message_id = params["message_id"]
        attachment_id = params["attachment_id"]
        filename = params["filename"]

        def _fetch_sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .messages()
                .attachments()
                .get(
                    userId="me",
                    messageId=message_id,
                    id=attachment_id,
                )
                .execute()
            )

        try:
            attachment = await asyncio.to_thread(_fetch_sync)
        except Exception as e:
            return {"error": f"Could not fetch attachment '{filename}': {e}"}

        data = attachment.get("data", "")
        if not data:
            return {"error": f"Attachment '{filename}' returned no data."}

        try:
            file_bytes = base64.urlsafe_b64decode(data)
        except Exception as e:
            return {"error": f"Could not decode attachment '{filename}': {e}"}

        size_bytes = len(file_bytes)
        if size_bytes > MAX_FILE_SIZE:
            return {
                "error": (
                    f"Attachment '{filename}' is too large "
                    f"({size_bytes / 1024 / 1024:.1f} MB). "
                    f"Maximum is {MAX_FILE_SIZE // (1024 * 1024)} MB."
                ),
            }

        ext = get_extension(filename)
        mime_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        # Image → return for Claude vision
        if is_image(filename):
            return {
                "type": "image",
                "filename": filename,
                "mime_type": mime_type,
                "image_data": base64.b64encode(file_bytes).decode("ascii"),
                "size_bytes": size_bytes,
                "note": "This is an image attachment. Analyze it visually.",
            }

        # Try text extraction (PDF/DOCX/XLSX/PPTX/text formats).
        try:
            text = await extract_text(filename, file_bytes)
        except Exception as e:
            text = ""
            extraction_err = str(e)
        else:
            extraction_err = ""

        is_extraction_failure = (
            not text
            or text.lstrip().startswith("[Failed to extract")
            or text.lstrip().startswith("[PDF extraction requires")
        )

        if not is_extraction_failure:
            if len(text) > 500_000:
                text = (
                    text[:500_000]
                    + "\n\n[... truncated, showing first 500K characters ...]"
                )
            return {
                "type": "text",
                "filename": filename,
                "mime_type": mime_type,
                "content": text,
                "size_bytes": size_bytes,
            }

        # Scanned / image-only PDF — hand raw bytes to Claude as a document block.
        if ext == ".pdf":
            return {
                "type": "document",
                "filename": filename,
                "mime_type": "application/pdf",
                "document_data": base64.b64encode(file_bytes).decode("ascii"),
                "size_bytes": size_bytes,
                "note": "PDF with no extractable text — read directly via vision/OCR.",
            }

        return {
            "error": (
                f"Couldn't extract content from '{filename}' (ext={ext or 'none'})."
                + (f" Detail: {extraction_err}" if extraction_err else "")
            ),
        }


class SearchEmails(BaseTool):
    @property
    def name(self) -> str:
        return "search_emails"

    @property
    def description(self) -> str:
        return (
            "Search emails using Gmail search syntax. Returns matching emails with snippets. "
            "Do NOT use this to backfill missing calendar events — calendar invite emails are an "
            "indirect, lossy proxy for actual events. If the user is asking about meetings, "
            "call `list_events` again with a tighter date window or attendee/organizer filter."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail search query (e.g. 'subject:invoice after:2024/01/01')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum results (default 10, max 20)",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Reuse ListEmails logic with the query
        tool = ListEmails()
        return await tool.execute(params, access_token)


# ---------------------------------------------------------------------------
# MIME helper
# ---------------------------------------------------------------------------


def _split_address_list(raw: str) -> list[str]:
    """Split a header value like '"A" <a@x.com>, b@y.com' into trimmed entries."""
    from email.utils import getaddresses

    if not raw:
        return []
    parsed = getaddresses([raw])
    out: list[str] = []
    for name, email_addr in parsed:
        if not email_addr:
            continue
        out.append(f'"{name}" <{email_addr}>' if name else email_addr)
    return out


def _extract_email(raw: str) -> str:
    """Return just the email address from 'Name <addr@x.com>'."""
    from email.utils import parseaddr

    _, addr = parseaddr(raw or "")
    return addr or ""


def _build_raw_message(
    to: str,
    subject: str,
    body: str,
    cc: str = "",
    bcc: str = "",
    in_reply_to: str = "",
    references: str = "",
    from_name: str = "",
    from_email: str = "",
) -> str:
    # Claude often drafts in markdown. Send multipart/alternative so Gmail clients
    # render bold/lists/links as HTML instead of showing literal asterisks.
    html_body = markdown_to_html(body)
    html_content = (
        '<div style="font-family: -apple-system, BlinkMacSystemFont, sans-serif; '
        f'font-size: 14px; color: #222;">{html_body}</div>'
    )

    msg = MIMEMultipart("alternative")
    msg.attach(MIMEText(body, "plain"))
    msg.attach(MIMEText(html_content, "html"))

    if from_name and from_email:
        msg["from"] = f"{from_name} <{from_email}>"
    msg["to"] = to
    msg["subject"] = subject
    if cc:
        msg["cc"] = cc
    if bcc:
        msg["bcc"] = bcc
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = references or in_reply_to
    return base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")


# ---------------------------------------------------------------------------
# Draft tools
# ---------------------------------------------------------------------------


class DraftEmail(BaseTool):
    name = "draft_email"
    description = (
        "Create an email draft in Gmail. Use this when the user asks you to write, compose, or draft an email. "
        "The draft is NOT sent — the user must approve it first. Write in the user's communication style."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address(es), comma-separated.",
            },
            "subject": {"type": "string", "description": "Email subject line."},
            "body": {"type": "string", "description": "Email body text."},
            "cc": {"type": "string", "description": "CC recipients, comma-separated."},
            "bcc": {
                "type": "string",
                "description": "BCC recipients, comma-separated.",
            },
        },
        "required": ["to", "subject", "body"],
    }
    provider = "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        raw = _build_raw_message(
            to=params["to"],
            subject=params["subject"],
            body=params["body"],
            cc=params.get("cc", ""),
            bcc=params.get("bcc", ""),
            from_name=params.get("_from_name", ""),
            from_email=params.get("_from_email", ""),
        )

        def _sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .drafts()
                .create(
                    userId="me",
                    body={"message": {"raw": raw}},
                )
                .execute()
            )

        draft = await asyncio.to_thread(_sync)
        return {
            "draft_id": draft["id"],
            "to": params["to"],
            "subject": params["subject"],
            "body": params["body"],
            "cc": params.get("cc", ""),
            "bcc": params.get("bcc", ""),
            "message": f"Draft created. To: {params['to']}, Subject: {params['subject']}. Say 'send it' to send, or suggest changes.",
        }


class UpdateDraft(BaseTool):
    name = "update_draft"
    description = "Update an existing email draft. Use when the user wants to change the draft before sending."
    input_schema = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft ID to update."},
            "to": {"type": "string", "description": "Updated recipient(s)."},
            "subject": {"type": "string", "description": "Updated subject."},
            "body": {"type": "string", "description": "Updated body text."},
            "cc": {"type": "string", "description": "Updated CC."},
            "bcc": {"type": "string", "description": "Updated BCC."},
        },
        "required": ["draft_id", "to", "subject", "body"],
    }
    provider = "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Preserve thread context: a reply draft carries `threadId` on its message
        # plus `In-Reply-To` / `References` headers on the MIME body. Rebuilding
        # the raw without these strands the updated draft in a new thread.
        def _sync() -> dict:
            service = _get_gmail_service(access_token)
            existing = (
                service.users()
                .drafts()
                .get(
                    userId="me",
                    id=params["draft_id"],
                    format="metadata",
                )
                .execute()
            )
            existing_msg = existing.get("message", {}) or {}
            thread_id_inner = existing_msg.get("threadId", "")
            existing_headers = {
                h["name"].lower(): h["value"]
                for h in (existing_msg.get("payload", {}) or {}).get("headers", [])
            }
            in_reply_to = existing_headers.get("in-reply-to", "")
            references = existing_headers.get("references", "") or in_reply_to

            raw_inner = _build_raw_message(
                to=params["to"],
                subject=params["subject"],
                body=params["body"],
                cc=params.get("cc", ""),
                bcc=params.get("bcc", ""),
                in_reply_to=in_reply_to,
                references=references,
                from_name=params.get("_from_name", ""),
                from_email=params.get("_from_email", ""),
            )
            update_body: dict[str, Any] = {"message": {"raw": raw_inner}}
            if thread_id_inner:
                update_body["message"]["threadId"] = thread_id_inner
            updated = (
                service.users()
                .drafts()
                .update(
                    userId="me",
                    id=params["draft_id"],
                    body=update_body,
                )
                .execute()
            )
            return {"draft": updated, "thread_id": thread_id_inner}

        synced = await asyncio.to_thread(_sync)
        draft = synced["draft"]
        thread_id = synced["thread_id"]
        return {
            "draft_id": draft["id"],
            "to": params["to"],
            "subject": params["subject"],
            "body": params["body"],
            "cc": params.get("cc", ""),
            "bcc": params.get("bcc", ""),
            "thread_id": thread_id,
            "message": "Draft updated.",
        }


class SendDraft(BaseTool):
    name = "send_draft"
    description = (
        "Send a previously created email draft. Only use this when the user explicitly says to send "
        "(e.g., 'send it', 'looks good, send', 'go ahead')."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "draft_id": {"type": "string", "description": "The draft ID to send."},
        },
        "required": ["draft_id"],
    }
    provider = "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .drafts()
                .send(
                    userId="me",
                    body={"id": params["draft_id"]},
                )
                .execute()
            )

        result = await asyncio.to_thread(_sync)
        return {"message_id": result.get("id"), "message": "Email sent successfully."}


class ListDrafts(BaseTool):
    name = "list_drafts"
    description = "List existing email drafts in Gmail."
    input_schema = {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max drafts to return. Default 10.",
                "default": 10,
            },
        },
    }
    provider = "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        limit = min(params.get("limit", 10), 20)

        def _sync() -> list[dict]:
            service = _get_gmail_service(access_token)
            results = (
                service.users().drafts().list(userId="me", maxResults=limit).execute()
            )
            out: list[dict] = []
            for d in results.get("drafts", []):
                draft = (
                    service.users()
                    .drafts()
                    .get(userId="me", id=d["id"], format="metadata")
                    .execute()
                )
                msg = draft.get("message", {})
                headers = {
                    h["name"].lower(): h["value"]
                    for h in msg.get("payload", {}).get("headers", [])
                }
                out.append(
                    {
                        "draft_id": d["id"],
                        "to": headers.get("to", ""),
                        "subject": headers.get("subject", ""),
                        "snippet": msg.get("snippet", ""),
                    }
                )
            return out

        drafts = await asyncio.to_thread(_sync)
        return {"drafts": drafts, "count": len(drafts)}


class ReplyToEmail(BaseTool):
    name = "reply_to_email"
    description = (
        "Create a reply to an existing email. Reads the original email for context, then creates a reply draft. "
        "Defaults to reply-all whenever the original thread has anyone on To: or Cc: besides the user and the sender — "
        "those recipients are preserved on the new draft. Pass reply_all=false to force sender-only. "
        "Explicit cc/bcc parameters merge with reply-all recipients. "
        "The user must approve before sending. Write the reply in the user's communication style."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The message ID to reply to.",
            },
            "body": {"type": "string", "description": "The reply body text."},
            "reply_all": {
                "type": "boolean",
                "description": "Override the reply-all default. Omit for auto (reply-all when the original has anyone on To:/Cc: besides the user and sender, else sender-only). Explicit true forces reply-all; explicit false forces sender-only.",
            },
            "cc": {
                "type": "string",
                "description": "Extra CC recipients, comma-separated.",
            },
            "bcc": {
                "type": "string",
                "description": "BCC recipients, comma-separated.",
            },
        },
        "required": ["message_id", "body"],
    }
    provider = "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _fetch_original_sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=params["message_id"],
                    format="metadata",
                    metadataHeaders=[
                        "From",
                        "To",
                        "Cc",
                        "Subject",
                        "Message-ID",
                        "Reply-To",
                    ],
                )
                .execute()
            )

        original = await asyncio.to_thread(_fetch_original_sync)
        headers = {
            h["name"].lower(): h["value"]
            for h in original.get("payload", {}).get("headers", [])
        }

        reply_to = headers.get("reply-to") or headers.get("from", "")
        subject = headers.get("subject", "")
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        message_id = headers.get("message-id", "")
        thread_id = original.get("threadId", "")

        my_email = (params.get("_from_email") or "").strip().lower()
        reply_to_addr = _extract_email(reply_to).lower()
        original_to = _split_address_list(headers.get("to", ""))
        original_cc = _split_address_list(headers.get("cc", ""))

        # Count distinct "other" recipients on the original (exclude user + sender dupes)
        other_recipients: list[str] = []
        for addr in original_to + original_cc:
            normalized = _extract_email(addr).lower()
            if not normalized or normalized == my_email or normalized == reply_to_addr:
                continue
            if addr not in other_recipients:
                other_recipients.append(addr)

        # Auto reply-all when the original has at least one recipient besides the user and sender;
        # explicit reply_all param overrides.
        reply_all_param = params.get("reply_all")
        reply_all = (
            bool(other_recipients) if reply_all_param is None else bool(reply_all_param)
        )

        cc_list: list[str] = list(other_recipients) if reply_all else []
        extra_cc = (params.get("cc") or "").strip()
        if extra_cc:
            for addr in _split_address_list(extra_cc):
                if addr not in cc_list:
                    cc_list.append(addr)
        cc_header = ", ".join(cc_list)
        bcc_header = (params.get("bcc") or "").strip()

        raw = _build_raw_message(
            to=reply_to,
            subject=subject,
            body=params["body"],
            cc=cc_header,
            bcc=bcc_header,
            in_reply_to=message_id,
            references=message_id,
            from_name=params.get("_from_name", ""),
            from_email=params.get("_from_email", ""),
        )

        def _create_draft_sync() -> dict:
            service = _get_gmail_service(access_token)
            return (
                service.users()
                .drafts()
                .create(
                    userId="me",
                    body={"message": {"raw": raw, "threadId": thread_id}},
                )
                .execute()
            )

        draft = await asyncio.to_thread(_create_draft_sync)
        mode_label = " (reply-all)" if reply_all and cc_list else ""
        return {
            "draft_id": draft["id"],
            "to": reply_to,
            "subject": subject,
            "body": params["body"],
            "cc": cc_header,
            "bcc": bcc_header,
            "thread_id": thread_id,
            "message": f"Reply draft created to {reply_to}{mode_label}. Say 'send it' to send, or suggest changes.",
        }


# ---------------------------------------------------------------------------
# Inbox triage: archive / trash / mark-read / forward / labels
# ---------------------------------------------------------------------------


class ArchiveEmail(BaseTool):
    @property
    def name(self) -> str:
        return "archive_email"

    @property
    def description(self) -> str:
        return "Archive an email — removes the INBOX label so it no longer appears in the inbox. The email is not deleted and remains searchable."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
            },
            "required": ["message_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)

        def _run() -> None:
            service.users().messages().modify(
                userId="me",
                id=params["message_id"],
                body={"removeLabelIds": ["INBOX"]},
            ).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            return {"error": f"Archive failed: {e}"}
        return {"status": "archived", "message_id": params["message_id"]}


class DeleteDraft(BaseTool):
    @property
    def name(self) -> str:
        return "delete_draft"

    @property
    def description(self) -> str:
        return (
            "Permanently delete an email draft. Use this for drafts (IDs typically start with 'r-'). "
            "Do NOT use trash_email on draft IDs — that API only works on sent/received messages."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "draft_id": {
                    "type": "string",
                    "description": "Gmail draft ID (e.g., r-1234...).",
                },
            },
            "required": ["draft_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)

        def _run() -> None:
            service.users().drafts().delete(
                userId="me",
                id=params["draft_id"],
            ).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            return {"error": f"Delete draft failed: {e}"}
        return {"status": "deleted", "draft_id": params["draft_id"]}


class TrashEmail(BaseTool):
    @property
    def name(self) -> str:
        return "trash_email"

    @property
    def description(self) -> str:
        return (
            "Move a sent or received email to Trash. Recoverable from Trash within 30 days; "
            "after that it's permanently deleted by Gmail. For draft emails, use delete_draft instead — "
            "this tool does not accept draft IDs."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
            },
            "required": ["message_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)

        def _run() -> None:
            service.users().messages().trash(
                userId="me",
                id=params["message_id"],
            ).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            return {"error": f"Trash failed: {e}"}
        return {"status": "trashed", "message_id": params["message_id"]}


class MarkAsRead(BaseTool):
    @property
    def name(self) -> str:
        return "mark_email_as_read"

    @property
    def description(self) -> str:
        return "Mark an email as read (removes UNREAD label) or unread (re-adds it)."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
                "read": {
                    "type": "boolean",
                    "description": "true to mark as read, false to mark as unread. Default true.",
                    "default": True,
                },
            },
            "required": ["message_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)
        read = params.get("read", True)
        body = {"removeLabelIds": ["UNREAD"]} if read else {"addLabelIds": ["UNREAD"]}

        def _run() -> None:
            service.users().messages().modify(
                userId="me",
                id=params["message_id"],
                body=body,
            ).execute()

        try:
            await asyncio.to_thread(_run)
        except Exception as e:
            return {"error": f"Mark read failed: {e}"}
        return {
            "status": "read" if read else "unread",
            "message_id": params["message_id"],
        }


class ForwardEmail(BaseTool):
    @property
    def name(self) -> str:
        return "forward_email"

    @property
    def description(self) -> str:
        return (
            "Forward an email to one or more recipients. Creates a DRAFT forward "
            "(not sent immediately). The user can review and say 'send it' before it goes out. "
            "The original subject is prefixed with 'Fwd:' and the original body is quoted."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Gmail message ID of the email to forward.",
                },
                "to": {
                    "type": "string",
                    "description": "Comma-separated recipient email(s).",
                },
                "cc": {
                    "type": "string",
                    "description": "Comma-separated cc email(s) (optional).",
                },
                "note": {
                    "type": "string",
                    "description": "Optional note to add ABOVE the quoted original (e.g. 'FYI — see below'). Blank by default.",
                },
            },
            "required": ["message_id", "to"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)

        def _get_original() -> dict:
            return (
                service.users()
                .messages()
                .get(
                    userId="me",
                    id=params["message_id"],
                    format="full",
                )
                .execute()
            )

        try:
            original = await asyncio.to_thread(_get_original)
        except Exception as e:
            return {"error": f"Could not load original: {e}"}

        parsed = _parse_message(original)
        subject = parsed["subject"]
        if not subject.lower().startswith("fwd:"):
            subject = f"Fwd: {subject}"

        note = params.get("note") or ""
        quoted = (
            f"{note}\n\n"
            f"---------- Forwarded message ----------\n"
            f"From: {parsed['from']}\n"
            f"Date: {parsed['date']}\n"
            f"Subject: {parsed['subject']}\n"
            f"To: {parsed['to']}\n\n"
            f"{parsed['body']}"
        )

        raw = _build_raw_message(
            to=params["to"],
            subject=subject,
            body=quoted,
            cc=params.get("cc", ""),
            from_name=params.get("_from_name", ""),
            from_email=params.get("_from_email", ""),
        )

        def _create_draft() -> dict:
            return (
                service.users()
                .drafts()
                .create(
                    userId="me",
                    body={"message": {"raw": raw, "threadId": parsed["thread_id"]}},
                )
                .execute()
            )

        try:
            draft = await asyncio.to_thread(_create_draft)
        except Exception as e:
            return {"error": f"Forward draft failed: {e}"}

        return {
            "draft_id": draft["id"],
            "to": params["to"],
            "subject": subject,
            "thread_id": parsed["thread_id"],
            "message": f"Forward draft created to {params['to']}. Say 'send it' to send.",
        }


class CreateGmailLabel(BaseTool):
    @property
    def name(self) -> str:
        return "create_gmail_label"

    @property
    def description(self) -> str:
        return "Create a new Gmail label. Labels are Gmail's equivalent of folders/tags. If the label already exists, returns the existing one."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Label name (use '/' for nesting, e.g. 'Clients/Acme').",
                },
            },
            "required": ["name"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)
        name = params["name"]

        def _list_labels() -> dict:
            return service.users().labels().list(userId="me").execute()

        try:
            existing = await asyncio.to_thread(_list_labels)
        except Exception as e:
            return {"error": f"List labels failed: {e}"}

        for label in existing.get("labels", []):
            if label.get("name", "").lower() == name.lower():
                return {
                    "status": "already_exists",
                    "label_id": label["id"],
                    "name": label["name"],
                }

        def _create_label() -> dict:
            return (
                service.users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": name,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
                .execute()
            )

        try:
            label = await asyncio.to_thread(_create_label)
        except Exception as e:
            return {"error": f"Create label failed: {e}"}

        return {"status": "created", "label_id": label["id"], "name": label["name"]}


class AddLabelToEmail(BaseTool):
    @property
    def name(self) -> str:
        return "add_label_to_email"

    @property
    def description(self) -> str:
        return (
            "Add an existing Gmail label to a message (or remove it). Pass the label NAME — "
            "the tool resolves it to an ID. If the label doesn't exist, returns an error "
            "suggesting you call create_gmail_label first."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Gmail message ID."},
                "label_name": {
                    "type": "string",
                    "description": "Exact label name (case-insensitive match).",
                },
                "remove": {
                    "type": "boolean",
                    "description": "true to remove the label instead of adding it. Default false.",
                    "default": False,
                },
            },
            "required": ["message_id", "label_name"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_gmail_service(access_token)
        name = params["label_name"]
        remove = params.get("remove", False)

        def _list_labels() -> dict:
            return service.users().labels().list(userId="me").execute()

        try:
            labels = await asyncio.to_thread(_list_labels)
        except Exception as e:
            return {"error": f"List labels failed: {e}"}

        label_id = None
        for label in labels.get("labels", []):
            if label.get("name", "").lower() == name.lower():
                label_id = label["id"]
                break
        if not label_id:
            return {
                "error": f"Label '{name}' not found. Call create_gmail_label first."
            }

        body = {"removeLabelIds": [label_id]} if remove else {"addLabelIds": [label_id]}

        def _modify() -> None:
            service.users().messages().modify(
                userId="me",
                id=params["message_id"],
                body=body,
            ).execute()

        try:
            await asyncio.to_thread(_modify)
        except Exception as e:
            return {"error": f"Modify labels failed: {e}"}

        return {
            "status": "label_removed" if remove else "label_added",
            "message_id": params["message_id"],
            "label_name": name,
            "label_id": label_id,
        }
