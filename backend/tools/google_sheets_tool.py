"""
Google Sheets Tools — read, create, and update spreadsheets via Google Sheets API.
"""

import asyncio
import re
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from tools.base import BaseTool

_RANGE_RE = re.compile(
    r"^(?:(?P<sheet>.+)!)?(?P<start_col>[A-Za-z]+)(?P<start_row>\d*)(?::(?P<end_col>[A-Za-z]+)(?P<end_row>\d*))?$"
)


def _col_letter_to_index(letter: str) -> int:
    """A=1, B=2, ..., Z=26, AA=27, ..."""
    n = 0
    for ch in letter.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n


def _col_index_to_letter(index: int) -> str:
    """1=A, 26=Z, 27=AA, ..."""
    out = ""
    while index > 0:
        index, rem = divmod(index - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _widen_range_if_needed(range_str: str, values: list[list[Any]]) -> str:
    """If the caller-supplied bounded range is narrower than the data, expand the
    end column so Sheets doesn't reject the write with 'tried writing to column X'.

    Leaves the range unchanged if it has no end column (open-ended) or if the
    values already fit. Only widens — never narrows.
    """
    if not values:
        return range_str
    m = _RANGE_RE.match(range_str)
    if not m:
        return range_str
    end_col = m.group("end_col")
    if not end_col:
        return range_str  # open-ended range (e.g. "A1"), Sheets will grow to fit
    data_width = max((len(row) for row in values), default=0)
    sheet = m.group("sheet")
    start_col = m.group("start_col")
    start_row = m.group("start_row") or ""
    end_row = m.group("end_row") or ""
    start_idx = _col_letter_to_index(start_col)
    end_idx = _col_letter_to_index(end_col)
    declared_width = end_idx - start_idx + 1
    if data_width <= declared_width:
        return range_str
    new_end_col = _col_index_to_letter(start_idx + data_width - 1)
    new_range = f"{start_col}{start_row}:{new_end_col}{end_row}"
    if sheet:
        new_range = f"{sheet}!{new_range}"
    return new_range


def _get_sheets_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def _get_drive_service(access_token: str):
    creds = Credentials(token=access_token)
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class ReadSpreadsheet(BaseTool):
    @property
    def name(self) -> str:
        return "read_spreadsheet"

    @property
    def description(self) -> str:
        return "Read data from a Google Sheets spreadsheet. Returns cell values from a specified range."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID (from the URL).",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range, e.g. 'Sheet1!A1:D10'. Defaults to 'Sheet1' (all data).",
                    "default": "Sheet1",
                },
            },
            "required": ["spreadsheet_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        range_name = params.get("range", "Sheet1")

        def _sync() -> dict:
            service = _get_sheets_service(access_token)
            return service.spreadsheets().values().get(
                spreadsheetId=params["spreadsheet_id"],
                range=range_name,
            ).execute()

        result = await asyncio.to_thread(_sync)
        values = result.get("values", [])
        return {
            "range": result.get("range", range_name),
            "rows": len(values),
            "data": values[:200],  # Cap at 200 rows to avoid token overflow
        }


class GetSpreadsheetInfo(BaseTool):
    @property
    def name(self) -> str:
        return "get_spreadsheet_info"

    @property
    def description(self) -> str:
        return "Get metadata about a Google Sheets spreadsheet — title, sheets/tabs, and properties."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
            },
            "required": ["spreadsheet_id"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        def _sync() -> dict:
            service = _get_sheets_service(access_token)
            return service.spreadsheets().get(
                spreadsheetId=params["spreadsheet_id"],
                fields="spreadsheetId,properties.title,sheets.properties",
            ).execute()

        spreadsheet = await asyncio.to_thread(_sync)

        sheets = []
        for s in spreadsheet.get("sheets", []):
            props = s.get("properties", {})
            sheets.append({
                "title": props.get("title", ""),
                "sheet_id": props.get("sheetId"),
                "row_count": props.get("gridProperties", {}).get("rowCount"),
                "column_count": props.get("gridProperties", {}).get("columnCount"),
            })

        return {
            "spreadsheet_id": spreadsheet.get("spreadsheetId"),
            "title": spreadsheet.get("properties", {}).get("title", ""),
            "sheets": sheets,
        }


class UpdateSpreadsheet(BaseTool):
    @property
    def name(self) -> str:
        return "update_spreadsheet"

    @property
    def description(self) -> str:
        return "Write data to a Google Sheets spreadsheet. Overwrites the specified range with the provided values."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {
                    "type": "string",
                    "description": "The spreadsheet ID.",
                },
                "range": {
                    "type": "string",
                    "description": "A1 notation range to write to, e.g. 'Sheet1!A1:C3'.",
                },
                "values": {
                    "type": "array",
                    "description": "2D array of values to write (rows of cells).",
                    "items": {"type": "array", "items": {}},
                },
            },
            "required": ["spreadsheet_id", "range", "values"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        # Auto-widen the range if the data has more columns than declared, so a
        # range/data mismatch doesn't hard-fail the write.
        effective_range = _widen_range_if_needed(params["range"], params["values"])
        body = {"values": params["values"]}

        def _sync() -> dict:
            service = _get_sheets_service(access_token)
            return service.spreadsheets().values().update(
                spreadsheetId=params["spreadsheet_id"],
                range=effective_range,
                valueInputOption="USER_ENTERED",
                body=body,
            ).execute()

        result = await asyncio.to_thread(_sync)

        return {
            "updated_range": result.get("updatedRange", ""),
            "updated_rows": result.get("updatedRows", 0),
            "updated_columns": result.get("updatedColumns", 0),
            "updated_cells": result.get("updatedCells", 0),
        }


class CreateSpreadsheet(BaseTool):
    @property
    def name(self) -> str:
        return "create_spreadsheet"

    @property
    def description(self) -> str:
        return "Create a new Google Sheets spreadsheet with a title and optional initial data."

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "description": "Title for the new spreadsheet.",
                },
                "sheet_names": {
                    "type": "array",
                    "description": "Optional list of sheet/tab names to create.",
                    "items": {"type": "string"},
                },
            },
            "required": ["title"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        service = _get_sheets_service(access_token)

        sheets = []
        for name in params.get("sheet_names", ["Sheet1"]):
            sheets.append({"properties": {"title": name}})

        body = {
            "properties": {"title": params["title"]},
            "sheets": sheets,
        }
        spreadsheet = service.spreadsheets().create(body=body).execute()
        file_id = spreadsheet["spreadsheetId"]
        url = spreadsheet.get("spreadsheetUrl", "")

        # Move to Gideon/Sheets/ folder (best-effort)
        try:
            from services.file_tracking_service import move_file_to_gideon_folder
            drive_url = await asyncio.to_thread(move_file_to_gideon_folder, access_token, file_id, "Sheets")
            if drive_url:
                url = drive_url
        except Exception:
            pass

        return {
            "spreadsheet_id": file_id,
            "title": spreadsheet["properties"]["title"],
            "url": url,
            "sheets": [s["properties"]["title"] for s in spreadsheet.get("sheets", [])],
        }


# ---------------------------------------------------------------------------
# Append / clear / find-replace / sort / copy / chart
# These cover the bulk of "modify the sheet" flows that update_spreadsheet
# couldn't express directly (it only writes a fixed range verbatim).
# ---------------------------------------------------------------------------

def _resolve_sheet_id_sync(svc, spreadsheet_id: str, sheet_name: str | None) -> int:
    """Return the numeric sheetId for a tab name (or 0 = first tab if None)."""
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id).execute()
    sheets = meta.get("sheets", [])
    if not sheets:
        raise ValueError("Spreadsheet has no sheets")
    if sheet_name is None:
        return sheets[0]["properties"]["sheetId"]
    for s in sheets:
        if s["properties"]["title"] == sheet_name:
            return s["properties"]["sheetId"]
    raise ValueError(f"Sheet '{sheet_name}' not found")


class AppendRowsToSpreadsheet(BaseTool):
    @property
    def name(self) -> str:
        return "append_rows_to_spreadsheet"

    @property
    def description(self) -> str:
        return (
            "Append one or more rows to the end of a sheet (auto-detects the "
            "first empty row). Use this instead of update_spreadsheet when "
            "you're adding to a log-style sheet. Pass `values` as a list of "
            "rows; each row is a list of cell values."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string", "description": "Sheet/tab name. Defaults to the first sheet."},
                "values": {
                    "type": "array",
                    "items": {"type": "array", "items": {}},
                    "description": "List of rows to append. Each row is a list of cell values (string/number/bool).",
                },
                "value_input_option": {
                    "type": "string",
                    "enum": ["RAW", "USER_ENTERED"],
                    "description": "USER_ENTERED parses values like the Sheets UI does (formulas, dates). RAW stores literally. Default USER_ENTERED.",
                },
            },
            "required": ["spreadsheet_id", "values"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]
        sheet = params.get("sheet")
        values = params["values"]
        if not isinstance(values, list) or not values:
            return {"error": "values must be a non-empty list of rows"}
        # Escape single quotes in sheet names (A1 notation uses '' to mean a
        # literal '). Otherwise tab names like `Team's Tasks` produce
        # `'Team's Tasks'!A1` which is malformed and Sheets rejects it.
        if sheet:
            escaped = sheet.replace("'", "''")
            rng = f"'{escaped}'!A1"
        else:
            rng = "A1"
        vio = params.get("value_input_option") or "USER_ENTERED"

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            return svc.spreadsheets().values().append(
                spreadsheetId=sid, range=rng,
                valueInputOption=vio,
                insertDataOption="INSERT_ROWS",
                body={"values": values},
            ).execute()

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Append failed: {e}"}
        upd = res.get("updates", {})
        return {
            "appended_rows": len(values),
            "updated_range": upd.get("updatedRange"),
            "updated_cells": upd.get("updatedCells", 0),
        }


class ClearSpreadsheetRange(BaseTool):
    @property
    def name(self) -> str:
        return "clear_spreadsheet_range"

    @property
    def description(self) -> str:
        return (
            "Clear all values in a range (formatting is preserved). Use "
            "A1-style notation: 'Sheet1!A2:D' clears column A-D from row 2 to "
            "the bottom of the data. Pass just 'Sheet1' to clear the whole tab."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "range": {"type": "string", "description": "A1 range, e.g. 'Sheet1!A2:D' or 'Sheet1'."},
            },
            "required": ["spreadsheet_id", "range"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            return svc.spreadsheets().values().clear(
                spreadsheetId=sid, range=params["range"], body={},
            ).execute()

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Clear failed: {e}"}
        return {"cleared_range": res.get("clearedRange", params["range"])}


class FindReplaceSpreadsheet(BaseTool):
    @property
    def name(self) -> str:
        return "find_replace_spreadsheet"

    @property
    def description(self) -> str:
        return (
            "Find and replace text across a sheet, or all sheets in a workbook. "
            "Pass `sheet` to scope to one tab; omit it to replace across the "
            "entire spreadsheet. `match_case` and `match_entire_cell` default false."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "find": {"type": "string"},
                "replacement": {"type": "string"},
                "sheet": {"type": "string", "description": "Optional sheet/tab name to scope the replace."},
                "match_case": {"type": "boolean", "default": False},
                "match_entire_cell": {"type": "boolean", "default": False},
            },
            "required": ["spreadsheet_id", "find", "replacement"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]
        find_replace: dict[str, Any] = {
            "find": params["find"],
            "replacement": params["replacement"],
            "matchCase": bool(params.get("match_case")),
            "matchEntireCell": bool(params.get("match_entire_cell")),
        }

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            if params.get("sheet"):
                find_replace["sheetId"] = _resolve_sheet_id_sync(svc, sid, params["sheet"])
            else:
                find_replace["allSheets"] = True
            return svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"findReplace": find_replace}]},
            ).execute()

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Find/replace failed: {e}"}
        replies = (res.get("replies") or [{}])[0].get("findReplace", {})
        return {
            "occurrences_changed": replies.get("occurrencesChanged", 0),
            "rows_changed": replies.get("rowsChanged", 0),
            "sheets_changed": replies.get("sheetsChanged", 0),
            "values_changed": replies.get("valuesChanged", 0),
            "formulas_changed": replies.get("formulasChanged", 0),
        }


class SortSpreadsheetRange(BaseTool):
    @property
    def name(self) -> str:
        return "sort_spreadsheet_range"

    @property
    def description(self) -> str:
        return (
            "Sort a range by one or more columns. Pass `sheet`, `start_row` "
            "(1-indexed; row 1 is usually a header), `end_row` (inclusive; "
            "0/None = last row), and a `sort_specs` list. Each spec is "
            "{column_index, ascending} where column_index is 0-based "
            "relative to the START COLUMN."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string", "description": "Sheet/tab name."},
                "start_row": {"type": "integer", "description": "First row to sort (1-indexed). Use 2 to keep the header row pinned."},
                "end_row": {"type": "integer", "description": "Last row inclusive (1-indexed). 0 or null = sort to the bottom."},
                "start_column": {"type": "string", "description": "First column letter (e.g. 'A'). Default 'A'."},
                "end_column": {"type": "string", "description": "Last column letter (e.g. 'Z'). Default = entire sheet width."},
                "sort_specs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "column_index": {"type": "integer", "description": "0-based, relative to start_column."},
                            "ascending": {"type": "boolean", "default": True},
                        },
                        "required": ["column_index"],
                    },
                    "description": "Ordered list of sort keys. Earlier specs take precedence.",
                },
            },
            "required": ["spreadsheet_id", "sheet", "sort_specs"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]
        start_row = int(params.get("start_row") or 2) - 1  # API is 0-indexed
        end_row = int(params.get("end_row") or 0)
        start_col = _col_letter_to_index(params.get("start_column") or "A") - 1
        end_col_letter = params.get("end_column")
        end_col_idx = _col_letter_to_index(end_col_letter) if end_col_letter else None

        sort_specs = [
            {
                "dimensionIndex": start_col + int(s.get("column_index", 0)),
                "sortOrder": "ASCENDING" if s.get("ascending", True) else "DESCENDING",
            }
            for s in params["sort_specs"]
        ]

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            sheet_id = _resolve_sheet_id_sync(svc, sid, params["sheet"])
            rng = {
                "sheetId": sheet_id,
                "startRowIndex": start_row,
                "startColumnIndex": start_col,
            }
            if end_row > 0:
                rng["endRowIndex"] = end_row
            if end_col_idx is not None:
                rng["endColumnIndex"] = end_col_idx
            return svc.spreadsheets().batchUpdate(
                spreadsheetId=sid,
                body={"requests": [{"sortRange": {"range": rng, "sortSpecs": sort_specs}}]},
            ).execute()

        try:
            await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Sort failed: {e}"}
        return {"status": "sorted", "spreadsheet_id": sid, "sheet": params["sheet"]}


class CopySpreadsheetSheet(BaseTool):
    @property
    def name(self) -> str:
        return "copy_spreadsheet_sheet"

    @property
    def description(self) -> str:
        return (
            "Copy a tab into the same workbook (duplicate) or into another "
            "workbook. Pass `destination_spreadsheet_id` to move it cross-file; "
            "omit it to duplicate inside the same file."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "sheet": {"type": "string", "description": "Tab name to copy."},
                "destination_spreadsheet_id": {
                    "type": "string",
                    "description": "If omitted, the copy goes into the same workbook with a '(Copy)' suffix.",
                },
                "new_title": {
                    "type": "string",
                    "description": "Optional rename for the duplicated tab (within-workbook copies only).",
                },
            },
            "required": ["spreadsheet_id", "sheet"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]
        dest = params.get("destination_spreadsheet_id") or sid

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            sheet_id = _resolve_sheet_id_sync(svc, sid, params["sheet"])
            res = svc.spreadsheets().sheets().copyTo(
                spreadsheetId=sid, sheetId=sheet_id,
                body={"destinationSpreadsheetId": dest},
            ).execute()
            new_sheet_id = res.get("sheetId")
            new_title = params.get("new_title")
            if new_title and new_sheet_id is not None and dest == sid:
                svc.spreadsheets().batchUpdate(
                    spreadsheetId=dest,
                    body={"requests": [{
                        "updateSheetProperties": {
                            "properties": {"sheetId": new_sheet_id, "title": new_title},
                            "fields": "title",
                        },
                    }]},
                ).execute()
            return res

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Copy failed: {e}"}
        return {
            "new_sheet_id": res.get("sheetId"),
            "title": params.get("new_title") or res.get("title"),
            "destination_spreadsheet_id": dest,
        }


class CreateSpreadsheetChart(BaseTool):
    @property
    def name(self) -> str:
        return "create_spreadsheet_chart"

    @property
    def description(self) -> str:
        return (
            "Add a chart to a sheet, sourced from an existing data range. "
            "Supports common chart types (LINE, BAR, COLUMN, PIE, SCATTER, "
            "AREA). The first row of the range is used as series names; "
            "the first column is used as domain (x-axis) labels."
        )

    @property
    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "spreadsheet_id": {"type": "string"},
                "data_sheet": {"type": "string", "description": "Sheet/tab containing the source data."},
                "data_range": {
                    "type": "string",
                    "description": "A1 range relative to data_sheet, e.g. 'A1:D20'. First row = series headers, first col = x-axis values.",
                },
                "chart_type": {
                    "type": "string",
                    "enum": ["LINE", "BAR", "COLUMN", "PIE", "SCATTER", "AREA"],
                    "default": "COLUMN",
                },
                "title": {"type": "string", "description": "Chart title."},
                "anchor_sheet": {
                    "type": "string",
                    "description": "Sheet where the chart should be placed. Defaults to data_sheet.",
                },
                "anchor_cell": {
                    "type": "string",
                    "description": "Top-left anchor cell on the anchor sheet (default 'F2').",
                },
            },
            "required": ["spreadsheet_id", "data_sheet", "data_range"],
        }

    @property
    def provider(self) -> str:
        return "google"

    async def execute(self, params: dict[str, Any], access_token: str) -> dict:
        sid = params["spreadsheet_id"]
        chart_type = params.get("chart_type") or "COLUMN"
        title = params.get("title") or ""

        # Parse data_range like "A1:D20" (sheet prefix optional — we always use data_sheet)
        m = _RANGE_RE.match(params["data_range"])
        if not m:
            return {"error": f"Invalid range '{params['data_range']}'"}
        sc = _col_letter_to_index(m.group("start_col")) - 1
        sr = (int(m.group("start_row")) - 1) if m.group("start_row") else 0
        ec = _col_letter_to_index(m.group("end_col")) if m.group("end_col") else sc + 1
        er = (int(m.group("end_row"))) if m.group("end_row") else sr + 1

        anchor_cell = params.get("anchor_cell") or "F2"
        ac_m = _RANGE_RE.match(anchor_cell)
        if not ac_m:
            return {"error": f"Invalid anchor_cell '{anchor_cell}'"}
        ac = _col_letter_to_index(ac_m.group("start_col")) - 1
        ar = (int(ac_m.group("start_row")) - 1) if ac_m.group("start_row") else 1

        def _sync() -> dict:
            svc = _get_sheets_service(access_token)
            data_sheet_id = _resolve_sheet_id_sync(svc, sid, params["data_sheet"])
            anchor_sheet_id = (
                _resolve_sheet_id_sync(svc, sid, params["anchor_sheet"])
                if params.get("anchor_sheet") else data_sheet_id
            )
            # Per Google's BasicChartSpec, both `domains[].domain` and
            # `series[].series` must wrap their range as ChartData with a
            # `sourceRange.sources` array. Bare `sources` is rejected with
            # "Unknown name 'sources' at ... .domain".
            domain_chart_data = {
                "sourceRange": {
                    "sources": [{
                        "sheetId": data_sheet_id,
                        "startRowIndex": sr, "endRowIndex": er,
                        "startColumnIndex": sc, "endColumnIndex": sc + 1,
                    }],
                },
            }
            # A BAR chart draws horizontal bars, so its VALUE axis is the
            # bottom one — series must target BOTTOM_AXIS or Sheets 400s with
            # "Bar charts series may only target the BOTTOM_AXIS". For
            # COLUMN/LINE/AREA the value axis is LEFT_AXIS (the default).
            series_axis = "BOTTOM_AXIS" if chart_type == "BAR" else "LEFT_AXIS"
            series = []
            for col in range(sc + 1, ec):
                series.append({
                    "series": {"sourceRange": {"sources": [{
                        "sheetId": data_sheet_id,
                        "startRowIndex": sr, "endRowIndex": er,
                        "startColumnIndex": col, "endColumnIndex": col + 1,
                    }]}},
                    "targetAxis": series_axis,
                })
            req = {
                "addChart": {
                    "chart": {
                        "spec": {
                            "title": title,
                            "basicChart": {
                                "chartType": chart_type,
                                "legendPosition": "RIGHT_LEGEND",
                                "axis": [
                                    {"position": "BOTTOM_AXIS"},
                                    {"position": "LEFT_AXIS"},
                                ],
                                "domains": [{"domain": domain_chart_data}],
                                "series": series,
                                "headerCount": 1,
                            } if chart_type != "PIE" else None,
                            "pieChart": {
                                "legendPosition": "RIGHT_LEGEND",
                                "domain": domain_chart_data,
                                "series": series[0]["series"] if series else None,
                            } if chart_type == "PIE" else None,
                        },
                        "position": {
                            "overlayPosition": {
                                "anchorCell": {
                                    "sheetId": anchor_sheet_id,
                                    "rowIndex": ar,
                                    "columnIndex": ac,
                                },
                                "widthPixels": 600,
                                "heightPixels": 371,
                            },
                        },
                    },
                },
            }
            # Strip None subkeys (basicChart vs pieChart) so the API doesn't reject.
            spec = req["addChart"]["chart"]["spec"]
            for k in list(spec.keys()):
                if spec[k] is None:
                    del spec[k]
            return svc.spreadsheets().batchUpdate(
                spreadsheetId=sid, body={"requests": [req]},
            ).execute()

        try:
            res = await asyncio.to_thread(_sync)
        except Exception as e:
            return {"error": f"Create chart failed: {e}"}
        chart = (res.get("replies") or [{}])[0].get("addChart", {}).get("chart", {})
        return {
            "chart_id": chart.get("chartId"),
            "title": title,
            "type": chart_type,
        }
