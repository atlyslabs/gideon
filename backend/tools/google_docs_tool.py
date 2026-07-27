"""
Google Docs Tools — read and create documents via Google Docs API.
"""

import asyncio
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from tools.base import BaseTool


def _get_docs_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("docs", "v1", credentials=creds, cache_discovery=False)


def _extract_text(body: dict) -> str:
    """Extract plain text from a Google Docs body."""
    parts = []
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun")
            if text_run:
                parts.append(text_run.get("content", ""))
    return "".join(parts)


class ReadDocument(BaseTool):
    @property
    def name(self) -> str:
        return "read_google_doc"

    @property
    def description(self) -> str:
        return "Read the content of a Google Docs document. Returns the full text."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The Google Docs document ID (from the URL).",
                },
            },
            "required": ["document_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            service = _get_docs_service(access_token)
            return service.documents().get(documentId=params["document_id"]).execute()

        doc = await asyncio.to_thread(_sync)
        title = doc.get("title", "")
        text = _extract_text(doc.get("body", {}))

        # Truncate to avoid token overflow (~30k chars)
        if len(text) > 30000:
            text = text[:30000] + "\n\n... [truncated — document too long]"

        return {
            "document_id": doc["documentId"],
            "title": title,
            "content": text,
            "revision_id": doc.get("revisionId", ""),
        }


class CreateDocument(BaseTool):
    @property
    def name(self) -> str:
        return "create_google_doc"

    @property
    def description(self) -> str:
        return "Create a new Google Docs document with a title and optional initial text content."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title for the new document.",
                },
                "content": {
                    "type": "string",
                    "description": "Optional initial text to insert into the document.",
                },
            },
            "required": ["title"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        content = params.get("content") or ""

        def _create_sync() -> str:
            service = _get_docs_service(access_token)
            doc = service.documents().create(body={"title": params["title"]}).execute()
            inner_doc_id = doc["documentId"]
            if content:
                service.documents().batchUpdate(
                    documentId=inner_doc_id,
                    body={"requests": [{"insertText": {"location": {"index": 1}, "text": content}}]},
                ).execute()
            return inner_doc_id

        doc_id = await asyncio.to_thread(_create_sync)
        url = f"https://docs.google.com/document/d/{doc_id}/edit"

        # Move to Gideon/Docs/ folder (best-effort)
        try:
            from services.file_tracking_service import move_file_to_gideon_folder
            drive_url = await asyncio.to_thread(move_file_to_gideon_folder, access_token, doc_id, "Docs")
            if drive_url:
                url = drive_url
        except Exception:
            pass

        return {
            "document_id": doc_id,
            "title": params["title"],
            "url": url,
        }


class UpdateDocument(BaseTool):
    @property
    def name(self) -> str:
        return "update_google_doc"

    @property
    def description(self) -> str:
        return "Append text to an existing Google Docs document."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {
                    "type": "string",
                    "description": "The Google Docs document ID.",
                },
                "text": {
                    "type": "string",
                    "description": "Text to append to the end of the document.",
                },
            },
            "required": ["document_id", "text"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> str:
            service = _get_docs_service(access_token)
            doc = service.documents().get(documentId=params["document_id"]).execute()
            body = doc.get("body", {})
            content = body.get("content", [])
            end_index = content[-1].get("endIndex", 1) - 1 if content else 1
            service.documents().batchUpdate(
                documentId=params["document_id"],
                body={"requests": [{"insertText": {"location": {"index": max(end_index, 1)}, "text": "\n" + params["text"]}}]},
            ).execute()
            return doc.get("title", "")

        title = await asyncio.to_thread(_sync)
        return {
            "document_id": params["document_id"],
            "title": title,
            "status": "updated",
            "url": f"https://docs.google.com/document/d/{params['document_id']}/edit",
        }


# ---------------------------------------------------------------------------
# Targeted edits: find/replace, insert image, insert table, insert page break.
# These use Docs' batchUpdate API and are far cheaper than re-creating the
# whole document for small changes.
# ---------------------------------------------------------------------------

async def _docs_batch_update(access_token: str, doc_id: str, requests: list[dict]) -> dict:
    def _sync() -> dict:
        svc = _get_docs_service(access_token)
        return svc.documents().batchUpdate(
            documentId=doc_id, body={"requests": requests},
        ).execute()
    return await asyncio.to_thread(_sync)


class FindReplaceInDocument(BaseTool):
    @property
    def name(self) -> str:
        return "find_replace_in_document"

    @property
    def description(self) -> str:
        return (
            "Find and replace text in a Google Doc. Replaces ALL occurrences. "
            "match_case defaults to false."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "find": {"type": "string"},
                "replacement": {"type": "string"},
                "match_case": {"type": "boolean", "default": False},
            },
            "required": ["document_id", "find", "replacement"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            res = await _docs_batch_update(access_token, params["document_id"], [{
                "replaceAllText": {
                    "containsText": {
                        "text": params["find"],
                        "matchCase": bool(params.get("match_case")),
                    },
                    "replaceText": params["replacement"],
                },
            }])
        except Exception as e:
            return {"error": f"Find/replace failed: {e}"}
        replies = (res.get("replies") or [{}])[0].get("replaceAllText", {})
        return {
            "occurrences_changed": replies.get("occurrencesChanged", 0),
            "document_id": params["document_id"],
        }


class InsertImageIntoDocument(BaseTool):
    @property
    def name(self) -> str:
        return "insert_image_into_document"

    @property
    def description(self) -> str:
        return (
            "Insert an image into a Google Doc at a given index (1-indexed in "
            "the document text stream). The image must be hosted at a URL the "
            "Docs API can fetch (Drive image, public CDN). Optional width/height "
            "in points."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "image_url": {"type": "string", "description": "Public URL the Docs API will fetch."},
                "index": {"type": "integer", "description": "Insertion index (1 = start of body). Append to end if you don't know — use a large value and cap to doc length."},
                "width_pt": {"type": "number", "description": "Optional width in points."},
                "height_pt": {"type": "number", "description": "Optional height in points."},
            },
            "required": ["document_id", "image_url", "index"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        req: dict[str, Any] = {
            "insertInlineImage": {
                "uri": params["image_url"],
                "location": {"index": int(params["index"])},
            },
        }
        if params.get("width_pt") and params.get("height_pt"):
            req["insertInlineImage"]["objectSize"] = {
                "width": {"magnitude": float(params["width_pt"]), "unit": "PT"},
                "height": {"magnitude": float(params["height_pt"]), "unit": "PT"},
            }
        try:
            res = await _docs_batch_update(access_token, params["document_id"], [req])
        except Exception as e:
            return {"error": f"Insert image failed: {e}"}
        obj = (res.get("replies") or [{}])[0].get("insertInlineImage", {})
        return {
            "object_id": obj.get("objectId"),
            "image_url": params["image_url"],
            "document_id": params["document_id"],
        }


class InsertTableIntoDocument(BaseTool):
    @property
    def name(self) -> str:
        return "insert_table_into_document"

    @property
    def description(self) -> str:
        return (
            "Insert an empty table at a given index. Table starts blank — use "
            "find_replace_in_document or update_google_doc to fill cells "
            "afterward, or pre-fill with data via the optional `cells` arg "
            "(list of rows, each a list of cell strings)."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "index": {"type": "integer", "description": "Insertion index. 1 = start of body."},
                "rows": {"type": "integer", "description": "Number of rows."},
                "columns": {"type": "integer", "description": "Number of columns."},
                "cells": {
                    "type": "array",
                    "items": {"type": "array", "items": {"type": "string"}},
                    "description": "Optional 2D array to pre-fill. Length must match rows; each inner list length must match columns. Skipped if absent (table left blank).",
                },
            },
            "required": ["document_id", "index", "rows", "columns"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        rows = int(params["rows"]); cols = int(params["columns"])
        if rows < 1 or cols < 1:
            return {"error": "rows and columns must be >= 1"}
        index = int(params["index"])

        # Step 1: insert the empty table.
        try:
            await _docs_batch_update(access_token, params["document_id"], [{
                "insertTable": {
                    "rows": rows, "columns": cols,
                    "location": {"index": index},
                },
            }])
        except Exception as e:
            return {"error": f"Insert table failed: {e}"}

        if not params.get("cells"):
            return {
                "status": "inserted",
                "rows": rows, "columns": cols,
                "document_id": params["document_id"],
            }

        # Validate cells dimensions BEFORE side-effects. The previous code
        # silently sliced with [:rows] / [:cols], which made a 5x5 cells
        # input look like a successful 3x3 fill — misleading.
        cells = params["cells"]
        if not isinstance(cells, list) or any(not isinstance(r, list) for r in cells):
            return {"error": "cells must be a 2D list (list of lists)."}
        if len(cells) > rows:
            return {"error": f"cells has {len(cells)} rows but the table is {rows} rows."}
        for ri, row in enumerate(cells):
            if len(row) > cols:
                return {"error": f"cells[{ri}] has {len(row)} columns but the table is {cols} columns."}

        # Step 2: fill cells. Each cell starts with one paragraph; insertText
        # at the cell's start indices works. Cell start indices live at
        # known offsets within the table — re-read the doc to find them.
        # NOTE: this is a best-effort fill; complex pre-existing content can
        # shift offsets. For tables in fresh docs it works reliably.
        def _fill_sync():
            svc = _get_docs_service(access_token)
            doc = svc.documents().get(documentId=params["document_id"]).execute()
            # Find the FIRST table at-or-after the insertion index — that's
            # the one we just created. Pre-existing tables at higher indices
            # would otherwise overwrite this and we'd fill the wrong table.
            target_table = None
            for el in doc.get("body", {}).get("content", []):
                if el.get("table") and el.get("startIndex", 0) >= index - 1:
                    target_table = el
                    break
            if not target_table:
                return None
            requests: list[dict] = []
            for r, row in enumerate(cells):
                for c, val in enumerate(row):
                    if not val:
                        continue
                    cell = target_table["table"]["tableRows"][r]["tableCells"][c]
                    # Cell content starts at the first paragraph element index.
                    para = cell["content"][0].get("paragraph")
                    if not para or not para.get("elements"):
                        continue
                    cell_start = para["elements"][0]["startIndex"]
                    requests.append({
                        "insertText": {
                            "location": {"index": cell_start},
                            "text": str(val),
                        },
                    })
            if requests:
                # Reverse so later inserts don't shift earlier indices.
                requests.reverse()
                svc.documents().batchUpdate(
                    documentId=params["document_id"],
                    body={"requests": requests},
                ).execute()
            return len(requests)
        try:
            filled = await asyncio.to_thread(_fill_sync)
        except Exception as e:
            return {"error": f"Insert table succeeded but cell-fill failed: {e}",
                    "rows": rows, "columns": cols}

        return {
            "status": "inserted",
            "rows": rows, "columns": cols,
            "filled_cells": filled or 0,
            "document_id": params["document_id"],
        }


class InsertPageBreakIntoDocument(BaseTool):
    @property
    def name(self) -> str:
        return "insert_page_break_into_document"

    @property
    def description(self) -> str:
        return "Insert a page break at a given index in a Google Doc."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "document_id": {"type": "string"},
                "index": {"type": "integer", "description": "Insertion index (1 = start of body)."},
            },
            "required": ["document_id", "index"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        try:
            await _docs_batch_update(access_token, params["document_id"], [{
                "insertPageBreak": {"location": {"index": int(params["index"])}},
            }])
        except Exception as e:
            return {"error": f"Insert page break failed: {e}"}
        return {"status": "inserted", "document_id": params["document_id"]}
