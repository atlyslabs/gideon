"""
Google Drive Tools — list, search, and read files via Google Drive API.
"""

import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from tools.base import BaseTool


def _get_drive_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _parse_file(f: dict) -> dict:
    return {
        "id": f["id"],
        "name": f.get("name", ""),
        "mime_type": f.get("mimeType", ""),
        "size": f.get("size"),
        "created_time": f.get("createdTime"),
        "modified_time": f.get("modifiedTime"),
        "web_view_link": f.get("webViewLink", ""),
        "owners": [o.get("emailAddress", "") for o in f.get("owners", [])],
        "shared": f.get("shared", False),
    }


class ListDriveFiles(BaseTool):
    @property
    def name(self) -> str:
        return "list_drive_files"

    @property
    def description(self) -> str:
        return "List files in Google Drive. Can filter by folder or MIME type."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "folder_id": {
                    "type": "string",
                    "description": "Folder ID to list files from. Omit for root.",
                },
                "mime_type": {
                    "type": "string",
                    "description": "Filter by MIME type (e.g. 'application/vnd.google-apps.spreadsheet').",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum files to return (default 20, max 50).",
                    "default": 20,
                },
            },
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        q_parts = ["trashed = false"]
        if params.get("folder_id"):
            q_parts.append(f"'{params['folder_id']}' in parents")
        if params.get("mime_type"):
            q_parts.append(f"mimeType = '{params['mime_type']}'")
        query = " and ".join(q_parts)
        page_size = min(params.get("max_results", 20), 50)

        def _sync() -> dict:
            service = _get_drive_service(access_token)
            return service.files().list(
                q=query,
                pageSize=page_size,
                fields="files(id, name, mimeType, size, createdTime, modifiedTime, webViewLink, owners, shared)",
                orderBy="modifiedTime desc",
            ).execute()

        results = await asyncio.to_thread(_sync)
        files = [_parse_file(f) for f in results.get("files", [])]
        return {"files": files, "count": len(files)}


class SearchDriveFiles(BaseTool):
    @property
    def name(self) -> str:
        return "search_drive_files"

    @property
    def description(self) -> str:
        return "Search for files in Google Drive by name or content."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query — matches file names and content.",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Maximum files to return (default 10).",
                    "default": 10,
                },
            },
            "required": ["query"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        q = f"fullText contains '{params['query']}' and trashed = false"
        page_size = min(params.get("max_results", 10), 50)

        def _sync() -> dict:
            service = _get_drive_service(access_token)
            return service.files().list(
                q=q,
                pageSize=page_size,
                fields="files(id, name, mimeType, size, createdTime, modifiedTime, webViewLink, owners, shared)",
                orderBy="modifiedTime desc",
            ).execute()

        results = await asyncio.to_thread(_sync)
        files = [_parse_file(f) for f in results.get("files", [])]
        return {"files": files, "count": len(files)}


class GetDriveFile(BaseTool):
    @property
    def name(self) -> str:
        return "get_drive_file"

    @property
    def description(self) -> str:
        return "Get metadata and details of a specific Google Drive file by ID."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {
                    "type": "string",
                    "description": "The Google Drive file ID.",
                },
            },
            "required": ["file_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            service = _get_drive_service(access_token)
            return service.files().get(
                fileId=params["file_id"],
                fields="id, name, mimeType, size, createdTime, modifiedTime, webViewLink, owners, shared, description, permissions",
            ).execute()

        f = await asyncio.to_thread(_sync)
        result = _parse_file(f)
        result["description"] = f.get("description", "")
        return result


# ---------------------------------------------------------------------------
# Write surface: move / rename / trash / share
# ---------------------------------------------------------------------------

class MoveDriveFile(BaseTool):
    @property
    def name(self) -> str:
        return "move_drive_file"

    @property
    def description(self) -> str:
        return (
            "Move a file or folder to a different Drive folder. Pass the "
            "target folder's UUID as `new_parent_id`. Use 'root' for the "
            "Drive root."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "new_parent_id": {"type": "string", "description": "Target folder UUID, or 'root' for the Drive root."},
            },
            "required": ["file_id", "new_parent_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            svc = _get_drive_service(access_token)
            existing = svc.files().get(fileId=params["file_id"], fields="parents").execute()
            previous = ",".join(existing.get("parents", []))
            return svc.files().update(
                fileId=params["file_id"],
                addParents=params["new_parent_id"],
                removeParents=previous,
                fields="id, name, parents, webViewLink",
            ).execute()
        try:
            f = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Move failed: {e}"}
        return {
            "id": f.get("id"),
            "name": f.get("name"),
            "parents": f.get("parents", []),
            "url": f.get("webViewLink"),
        }


class RenameDriveFile(BaseTool):
    @property
    def name(self) -> str:
        return "rename_drive_file"

    @property
    def description(self) -> str:
        return "Rename a Drive file or folder. Same file_id, just changes the displayed name."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "new_name": {"type": "string"},
            },
            "required": ["file_id", "new_name"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            svc = _get_drive_service(access_token)
            return svc.files().update(
                fileId=params["file_id"],
                body={"name": params["new_name"]},
                fields="id, name, webViewLink",
            ).execute()
        try:
            f = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Rename failed: {e}"}
        return {"id": f.get("id"), "name": f.get("name"), "url": f.get("webViewLink")}


class TrashDriveFile(BaseTool):
    @property
    def name(self) -> str:
        return "trash_drive_file"

    @property
    def description(self) -> str:
        return (
            "Move a Drive file or folder to Trash. Recoverable for 30 days. "
            "Doesn't hard-delete — for that, the user must purge from Trash."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {"file_id": {"type": "string"}},
            "required": ["file_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            svc = _get_drive_service(access_token)
            return svc.files().update(
                fileId=params["file_id"],
                body={"trashed": True},
                fields="id, name, trashed",
            ).execute()
        try:
            f = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Trash failed: {e}"}
        return {"id": f.get("id"), "name": f.get("name"), "trashed": f.get("trashed", True)}


class ShareDriveFile(BaseTool):
    @property
    def name(self) -> str:
        return "share_drive_file"

    @property
    def description(self) -> str:
        return (
            "Grant access to a Drive file. Roles: 'reader', 'commenter', "
            "'writer'. Pass `email` to share with a specific user, or set "
            "`anyone_with_link=true` for link-shared access. Sends a "
            "notification email by default — pass send_notification=false "
            "to suppress."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "file_id": {"type": "string"},
                "role": {
                    "type": "string",
                    "enum": ["reader", "commenter", "writer"],
                    "default": "reader",
                },
                "email": {"type": "string", "description": "User/group email to grant access. Omit if using anyone_with_link."},
                "anyone_with_link": {
                    "type": "boolean",
                    "description": "If true, anyone with the link gets the role. Mutually exclusive with email.",
                },
                "send_notification": {"type": "boolean", "default": True},
                "message": {"type": "string", "description": "Optional message in the notification email."},
            },
            "required": ["file_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        role = params.get("role") or "reader"
        email = params.get("email")
        anyone = bool(params.get("anyone_with_link"))
        # The schema says these are mutually exclusive — enforce it before
        # any side effect so the caller can't silently get the wrong sharing
        # mode (anyone_with_link previously won when both were passed).
        if email and anyone:
            return {"error": "Pass either email or anyone_with_link, not both."}
        if not email and not anyone:
            return {"error": "Pass either email or anyone_with_link=true."}
        permission: dict[str, Any] = {"role": role}
        if anyone:
            permission["type"] = "anyone"
        else:
            permission["type"] = "user"
            permission["emailAddress"] = email
        # Drive's permissions.create rejects sendNotificationEmail for
        # type=anyone (and type=domain). Only forward it for user/group shares.
        send_notification = bool(params.get("send_notification", True)) and not anyone

        def _sync() -> dict:
            svc = _get_drive_service(access_token)
            kwargs: dict[str, Any] = {
                "fileId": params["file_id"],
                "body": permission,
                "fields": "id, type, role, emailAddress",
            }
            if not anyone:
                kwargs["sendNotificationEmail"] = send_notification
                if send_notification and params.get("message"):
                    kwargs["emailMessage"] = params["message"]
            return svc.permissions().create(**kwargs).execute()

        try:
            p = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Share failed: {e}"}
        return {
            "permission_id": p.get("id"),
            "type": p.get("type"),
            "role": p.get("role"),
            "email": p.get("emailAddress"),
        }