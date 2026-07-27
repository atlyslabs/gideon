"""
File Tracking Service -- tracks all files (uploaded + generated) in the Gideon Drive folder.

Provides helpers to:
1. Move a newly created Google file (Sheet/Doc) into Gideon/{type}/ folder
2. Save a reference in the uploaded_files table
"""

import structlog
from uuid import UUID

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import UploadedFile

logger = structlog.get_logger()


def _get_drive_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _find_or_create_folder(service, name: str, parent_id: str | None) -> str:
    """Find a folder by name under parent, or create it."""
    query = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        query += f" and '{parent_id}' in parents"
    results = service.files().list(q=query, spaces="drive", fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def move_file_to_gideon_folder(
    access_token: str,
    file_id: str,
    subfolder: str = "Generated",
) -> str:
    """Move a Google Drive file into Gideon/{subfolder}/ folder.
    Returns the webViewLink. Sync function -- call via asyncio.to_thread."""
    service = _get_drive_service(access_token)

    gideon_folder = _find_or_create_folder(service, "Gideon", None)
    target_folder = _find_or_create_folder(service, subfolder, gideon_folder)

    # Get current parents to remove
    file_info = service.files().get(fileId=file_id, fields="parents, webViewLink").execute()
    current_parents = ",".join(file_info.get("parents", []))
    web_link = file_info.get("webViewLink", "")

    # Move: add new parent, remove old
    service.files().update(
        fileId=file_id,
        addParents=target_folder,
        removeParents=current_parents,
        fields="id",
    ).execute()

    return web_link


async def track_generated_file(
    session: AsyncSession,
    user_id: UUID,
    conversation_id: UUID | str | None,
    filename: str,
    mime_type: str,
    drive_file_id: str,
    drive_url: str,
) -> UploadedFile:
    """Save a generated file reference in the uploaded_files table."""
    uploaded = UploadedFile(
        user_id=user_id,
        conversation_id=conversation_id,
        filename=filename,
        mime_type=mime_type,
        size_bytes=0,  # Unknown for generated files
        extracted_text=None,
        is_image=False,
        drive_file_id=drive_file_id,
        drive_url=drive_url,
    )
    session.add(uploaded)
    await session.flush()
    return uploaded
