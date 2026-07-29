"""
Upload Tool — read content of uploaded files on demand.

Claude calls this when it needs the actual content of a file the user uploaded.
Text files return extracted text. Images return base64 for vision. Scanned /
image-only PDFs fall back to a raw `document` block fetched from Drive so
Claude can OCR them natively instead of giving up.
"""

import asyncio
import base64
from typing import Any
from uuid import UUID

import httplib2
import structlog
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp
from googleapiclient.discovery import build
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import UploadedFile
from services.oauth.token_refresh import get_valid_access_token
from tools.internal_tools import InternalTool

logger = structlog.get_logger()

# Drive download timeout (seconds). 10 MB hard upload cap means most fetches
# finish in well under 5s; this guards against a hung connection blocking the
# tool indefinitely.
_DRIVE_FETCH_TIMEOUT_SEC = 15


def _looks_like_extraction_failure(text: str) -> bool:
    """Sentinels written by file_extraction_service when extraction fails or
    yields nothing useful. Treated as 'no usable text' for fallback purposes."""
    if not text:
        return True
    head = text.lstrip()[:64]
    return head.startswith("[Failed to extract") or head.startswith("[PDF extraction requires")


def _download_drive_file_sync(access_token: str, drive_file_id: str) -> bytes:
    creds = Credentials(token=access_token)
    http = AuthorizedHttp(creds, http=httplib2.Http(timeout=_DRIVE_FETCH_TIMEOUT_SEC))
    service = build("drive", "v3", http=http, cache_discovery=False)
    return service.files().get_media(fileId=drive_file_id).execute()


class ReadUploadedFile(InternalTool):
    @property
    def name(self) -> str:
        return "read_uploaded_file"

    @property
    def description(self) -> str:
        return "Read the content of a file the user uploaded in this conversation. Returns extracted text for documents, or the image for visual files. Use the file_id from the upload context."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The ID of the uploaded file",
                },
            },
            "required": ["file_id"],
        }

    @property
    def provider(self) -> str:
        return "internal"

    async def execute_internal(self, params: dict[str, Any], user_id: UUID, session: AsyncSession, **kwargs) -> dict:
        file_id = params.get("file_id", "")
        if not file_id:
            return {"error": "file_id is required"}

        try:
            result = await session.execute(
                select(UploadedFile).where(
                    UploadedFile.id == file_id,
                    UploadedFile.user_id == user_id,
                )
            )
            uploaded = result.scalar_one_or_none()
        except Exception:
            return {"error": f"Invalid file ID: {file_id}"}

        if not uploaded:
            return {"error": "File not found or access denied"}

        if uploaded.is_image:
            # Return image for Claude vision
            if uploaded.image_base64:
                return {
                    "type": "image",
                    "filename": uploaded.filename,
                    "mime_type": uploaded.mime_type,
                    "image_data": uploaded.image_base64,
                    "note": "This is an image. Analyze it visually.",
                }
            return {"error": "Image data not available"}

        # Return extracted text if extraction produced something usable.
        text = uploaded.extracted_text or ""
        if text and not _looks_like_extraction_failure(text):
            # Cap at ~30K tokens worth of text. The old 500K-char (~125K-token)
            # cap let a single big file dominate the context window: on the Kimi
            # chat path (256K) plus the full tool-schema set + system prompt, one
            # such read tipped the whole request over the limit, so every
            # subsequent send 400'd and the chat silently wedged (user messages
            # with no reply). 120K chars keeps plenty for the model to work with
            # while leaving room for the conversation; the model can ask the user
            # to narrow the file or paste a specific section if it needs more.
            if len(text) > 120_000:
                text = text[:120_000] + "\n\n[... truncated, showing first 120K characters of a larger file ...]"
            return {
                "type": "text",
                "filename": uploaded.filename,
                "mime_type": uploaded.mime_type,
                "content": text,
                "size_bytes": uploaded.size_bytes,
            }

        # Fallback: scanned / image-only PDFs have no text layer. Re-fetch the
        # raw bytes from Drive and hand them to Claude as a document block so
        # it can OCR natively (Sonnet/Opus 4.6 read PDFs as first-class input).
        is_pdf = (uploaded.mime_type or "").lower() == "application/pdf" or (uploaded.filename or "").lower().endswith(".pdf")
        if is_pdf and uploaded.drive_file_id:
            access_token = await get_valid_access_token("google", user_id)
            if not access_token:
                return {
                    "error": (
                        f"PDF '{uploaded.filename}' had no extractable text and Google Drive "
                        "is not connected, so I can't read it as a document. Reconnect Google or "
                        "paste the content directly."
                    ),
                    "reconnect": "google",
                }
            try:
                file_bytes = await asyncio.to_thread(_download_drive_file_sync, access_token, uploaded.drive_file_id)
            except Exception as e:
                logger.error("Drive PDF fallback fetch failed", file_id=str(uploaded.id), err=str(e))
                return {
                    "error": (
                        f"PDF '{uploaded.filename}' has no text layer and the Drive fetch "
                        f"failed ({e}). Paste the content directly or describe what you need."
                    ),
                }
            return {
                "type": "document",
                "filename": uploaded.filename,
                "mime_type": "application/pdf",
                "document_data": base64.b64encode(file_bytes).decode("ascii"),
                "size_bytes": len(file_bytes),
                "note": "Scanned / image-only PDF — read it directly via vision/OCR.",
            }

        # If text was the literal extraction-failure sentinel, surface it cleanly.
        if text:
            return {
                "error": (
                    f"Couldn't extract content from '{uploaded.filename}'. "
                    f"Detail: {text[:200]}"
                ),
            }
        return {"error": f"No content extracted from {uploaded.filename}"}
