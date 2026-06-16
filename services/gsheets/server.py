"""
Google Sheets MCP Server
========================
Read/write Google Sheets via the Google Sheets API v4 + Drive API v3,
authenticated by a USER OAuth refresh token.

**Auth model (pivoted from service account):**
  We originally planned service-account auth, but Google Cloud's
  ``iam.disableServiceAccountKeyCreation`` policy blocks key generation
  on many orgs by default. Instead, this service uses the OAuth
  refresh-token pattern (same mechanism as the GA service in this
  gateway). The token grants access to whatever Sheets the
  authorizing user already has access to — no need to manually share
  individual files.

  GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are reused from the GA
  service's existing OAuth client. The Sheets-specific refresh token
  is stored separately in ``GSHEETS_REFRESH_TOKEN`` so we can rotate
  it without disturbing GA.

Env vars:
  MCP_API_KEY              required
  GOOGLE_CLIENT_ID         required (shared with GA service)
  GOOGLE_CLIENT_SECRET     required (shared with GA service)
  GSHEETS_REFRESH_TOKEN    required — refresh token issued with
                            ``spreadsheets`` + ``drive.file`` scopes
  PORT                     optional, defaults to 8017
  SERVER_URL               optional
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import secrets
import time
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlencode

import uvicorn
from fastmcp import FastMCP
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gsheets_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
GOOGLE_CLIENT_ID: str = os.environ["GOOGLE_CLIENT_ID"]
GOOGLE_CLIENT_SECRET: str = os.environ["GOOGLE_CLIENT_SECRET"]
GSHEETS_REFRESH_TOKEN: str = os.environ["GSHEETS_REFRESH_TOKEN"]
PORT: int = int(os.getenv("PORT", "8017"))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",  # read + write Sheets
    "https://www.googleapis.com/auth/drive.file",    # create + share files we create
]

# --- Google client init -----------------------------------------------------


def _build_clients():
    creds = Credentials(
        token=None,
        refresh_token=GSHEETS_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        scopes=SCOPES,
    )
    sheets = build("sheets", "v4", credentials=creds, cache_discovery=False)
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    return sheets, drive


_sheets_client, _drive_client = _build_clients()
logger.info(
    "Sheets MCP ready. Auth: user OAuth refresh token (scopes: %s)",
    [s.rsplit("/", 1)[-1] for s in SCOPES],
)


# --- Helpers -----------------------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


def _format_http_error(e: HttpError) -> str:
    try:
        body = json.loads(e.content.decode("utf-8")) if e.content else {}
        msg = (body.get("error") or {}).get("message") or str(e)
    except Exception:
        msg = str(e)
    if "PERMISSION_DENIED" in msg or e.resp.status == 403:
        return (
            f"Permission denied: {msg}. The OAuth token only sees Sheets "
            "the authorizing user already has access to. If this Sheet was "
            "created by a different account, ask that account to share it."
        )
    if e.resp.status == 404:
        return (
            f"Sheet not found: {msg}. Double-check the spreadsheet ID — "
            "it's the long ID in the URL between /d/ and /edit."
        )
    if e.resp.status == 401:
        return (
            f"Auth failed: {msg}. The GSHEETS_REFRESH_TOKEN may have been "
            "revoked or expired. Re-generate it via OAuth Playground and "
            "update the DO secret."
        )
    return msg


async def _run(fn, *args, **kwargs):
    """Wrap synchronous googleapiclient calls into the FastMCP async path."""
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except HttpError as e:
        raise RuntimeError(_format_http_error(e)) from e


# --- Formatting / batchUpdate helpers ----------------------------------------


def _hex_to_color(hex_str: Optional[str]) -> Optional[dict]:
    """Convert '#RRGGBB' or '#RRGGBBAA' to Google's {red,green,blue,alpha} (0-1 floats)."""
    if not hex_str:
        return None
    s = hex_str.lstrip("#")
    if len(s) not in (6, 8):
        raise ValueError(f"Color {hex_str!r} must be #RRGGBB or #RRGGBBAA")
    r = int(s[0:2], 16) / 255.0
    g = int(s[2:4], 16) / 255.0
    b = int(s[4:6], 16) / 255.0
    color: dict[str, float] = {"red": r, "green": g, "blue": b}
    if len(s) == 8:
        color["alpha"] = int(s[6:8], 16) / 255.0
    return color


def _col_letter_to_index(letters: str) -> int:
    """'A' -> 0, 'B' -> 1, ..., 'AA' -> 26."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


_A1_RE = re.compile(
    r"^(?:(?P<sheet>'[^']+'|[^!]+)!)?"
    r"(?P<start_col>[A-Z]+)?(?P<start_row>\d+)?"
    r"(?::(?P<end_col>[A-Z]+)?(?P<end_row>\d+)?)?$",
    re.IGNORECASE,
)


# Cache of {spreadsheet_id: {tab_name_lower: sheet_id, ...}}; invalidated on
# add/delete/duplicate sheet ops.
_sheet_id_cache: dict[str, dict[str, int]] = {}


async def _sheet_map(spreadsheet_id: str) -> dict[str, int]:
    """Map of tab-name (lowercased) -> numeric sheet ID for the given spreadsheet."""
    if spreadsheet_id in _sheet_id_cache:
        return _sheet_id_cache[spreadsheet_id]
    res = await _run(
        _sheets_client.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(title,sheetId)")
        .execute
    )
    m = {
        (s.get("properties", {}).get("title") or "").lower(): s.get("properties", {}).get("sheetId")
        for s in res.get("sheets") or []
    }
    _sheet_id_cache[spreadsheet_id] = m
    return m


def _invalidate_sheet_cache(spreadsheet_id: str) -> None:
    _sheet_id_cache.pop(spreadsheet_id, None)


async def _resolve_sheet_id(spreadsheet_id: str, name_or_id: Any) -> int:
    """Accept a tab name (string) or numeric sheet ID; return the numeric ID."""
    if isinstance(name_or_id, int):
        return name_or_id
    if isinstance(name_or_id, str) and name_or_id.isdigit():
        return int(name_or_id)
    m = await _sheet_map(spreadsheet_id)
    sid = m.get((name_or_id or "").lower())
    if sid is None:
        raise ValueError(
            f"No tab named {name_or_id!r} in spreadsheet {spreadsheet_id}. "
            f"Available tabs: {sorted(m.keys())}"
        )
    return sid


async def _a1_to_grid_range(spreadsheet_id: str, range_a1: str) -> dict:
    """Parse 'Sheet1!A1:E10' into a Google GridRange dict.

    Whole-column 'A:E' and whole-row '1:10' forms are supported.
    Missing sheet prefix uses the first tab.
    """
    m = _A1_RE.match(range_a1.strip())
    if not m:
        raise ValueError(f"Could not parse A1 range {range_a1!r}")
    sheet_name = m.group("sheet")
    if sheet_name:
        sheet_name = sheet_name.strip("'")
        sheet_id = await _resolve_sheet_id(spreadsheet_id, sheet_name)
    else:
        # Default to the first tab
        sm = await _sheet_map(spreadsheet_id)
        if not sm:
            raise ValueError(f"Spreadsheet {spreadsheet_id} has no tabs")
        sheet_id = next(iter(sm.values()))

    gr: dict[str, Any] = {"sheetId": sheet_id}
    start_col = m.group("start_col")
    start_row = m.group("start_row")
    end_col = m.group("end_col")
    end_row = m.group("end_row")
    # If the range is just a cell or just rows/cols, populate what's there.
    if start_col:
        gr["startColumnIndex"] = _col_letter_to_index(start_col)
    if start_row:
        gr["startRowIndex"] = int(start_row) - 1
    if end_col:
        gr["endColumnIndex"] = _col_letter_to_index(end_col) + 1
    elif start_col and not (end_col or end_row):
        # Single column like "A:A" or single cell — close the range
        gr["endColumnIndex"] = _col_letter_to_index(start_col) + 1
    if end_row:
        gr["endRowIndex"] = int(end_row)
    elif start_row and not (end_row or end_col):
        gr["endRowIndex"] = int(start_row)
    return gr


async def _batch_update(spreadsheet_id: str, requests: list[dict]) -> dict:
    """Run a batchUpdate call. Caller passes the raw 'requests' list."""
    return await _run(
        _sheets_client.spreadsheets()
        .batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests})
        .execute
    )


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "gsheets_mcp",
    instructions=(
        "Read and write Google Sheets. Auth is user-OAuth (refresh token) — "
        "the MCP sees any Sheet the authorizing Google account already has "
        "access to. Ranges use A1 notation, e.g. 'Sheet1!A1:E100' or "
        "'Weekly Snapshots!A:Z'. For numeric data you typically want "
        "value_input_option='USER_ENTERED' so dates/formulas are parsed "
        "natively by Sheets."
    ),
)


# --- Tools -------------------------------------------------------------------


@mcp.tool(
    name="get_spreadsheet_info",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_spreadsheet_info(spreadsheet_id: str) -> str:
    """
    Get metadata for a spreadsheet: title, locale, timezone, and all
    sheet/tab properties (id, title, row/column count, frozen rows).

    Args:
        spreadsheet_id: The Sheet's ID (the long string in the URL
            between /d/ and /edit).
    """
    res = await _run(
        _sheets_client.spreadsheets().get(spreadsheetId=spreadsheet_id).execute
    )
    return _json({
        "spreadsheet_id": res.get("spreadsheetId"),
        "title": (res.get("properties") or {}).get("title"),
        "locale": (res.get("properties") or {}).get("locale"),
        "timezone": (res.get("properties") or {}).get("timeZone"),
        "url": res.get("spreadsheetUrl"),
        "sheets": [
            {
                "sheet_id": s.get("properties", {}).get("sheetId"),
                "title": s.get("properties", {}).get("title"),
                "index": s.get("properties", {}).get("index"),
                "row_count": s.get("properties", {}).get("gridProperties", {}).get("rowCount"),
                "column_count": s.get("properties", {}).get("gridProperties", {}).get("columnCount"),
                "frozen_rows": s.get("properties", {}).get("gridProperties", {}).get("frozenRowCount"),
                "frozen_columns": s.get("properties", {}).get("gridProperties", {}).get("frozenColumnCount"),
            }
            for s in res.get("sheets") or []
        ],
    })


@mcp.tool(
    name="list_sheets",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def list_sheets(spreadsheet_id: str) -> str:
    """
    Just the sheet/tab names of a spreadsheet — lighter than
    get_spreadsheet_info when you only need to know which tabs exist.

    Args:
        spreadsheet_id: The Sheet's ID.
    """
    res = await _run(
        _sheets_client.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets.properties(title,sheetId,index)")
        .execute
    )
    titles = [
        s.get("properties", {}).get("title")
        for s in res.get("sheets") or []
    ]
    return _json({"spreadsheet_id": spreadsheet_id, "tab_names": titles})


@mcp.tool(
    name="get_values",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_values(
    spreadsheet_id: str,
    range_a1: str,
    value_render_option: str = "FORMATTED_VALUE",
) -> str:
    """
    Read a range of cells. Returns a 2D array of values.

    Args:
        spreadsheet_id: The Sheet's ID.
        range_a1: A1-notation range, e.g. 'Sheet1!A1:E100' or
            'Weekly Snapshots!A:Z' (whole columns).
        value_render_option: 'FORMATTED_VALUE' (default — what's displayed),
            'UNFORMATTED_VALUE' (raw numbers/dates), or 'FORMULA' (cells'
            formulas, useful for inspecting templates).
    """
    if value_render_option not in {"FORMATTED_VALUE", "UNFORMATTED_VALUE", "FORMULA"}:
        raise ValueError("value_render_option must be FORMATTED_VALUE, UNFORMATTED_VALUE, or FORMULA")
    res = await _run(
        _sheets_client.spreadsheets()
        .values()
        .get(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueRenderOption=value_render_option,
        )
        .execute
    )
    values = res.get("values") or []
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "range": res.get("range"),
        "row_count": len(values),
        "values": values,
    })


@mcp.tool(
    name="batch_get_values",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def batch_get_values(
    spreadsheet_id: str,
    ranges: list[str],
    value_render_option: str = "FORMATTED_VALUE",
) -> str:
    """
    Read multiple ranges in a single API call. Cheaper than calling
    get_values N times for N ranges.

    Args:
        spreadsheet_id: The Sheet's ID.
        ranges: List of A1-notation ranges.
        value_render_option: See get_values.
    """
    if not ranges:
        raise ValueError("ranges must be a non-empty list")
    res = await _run(
        _sheets_client.spreadsheets()
        .values()
        .batchGet(
            spreadsheetId=spreadsheet_id,
            ranges=ranges,
            valueRenderOption=value_render_option,
        )
        .execute
    )
    out = []
    for vr in res.get("valueRanges") or []:
        out.append({
            "range": vr.get("range"),
            "row_count": len(vr.get("values") or []),
            "values": vr.get("values") or [],
        })
    return _json({"spreadsheet_id": spreadsheet_id, "result_sets": out})


@mcp.tool(
    name="update_values",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def update_values(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
) -> str:
    """
    Overwrite the values in a range. Existing data in the target range
    is replaced.

    Args:
        spreadsheet_id: The Sheet's ID.
        range_a1: A1-notation range — the TOP-LEFT cell defines where
            writing starts; the supplied ``values`` 2D array determines
            how far it extends. Best practice: pass exact extents like
            'Sheet1!A2:D10' to make intent clear.
        values: 2D array of cell values (rows of columns). Numbers,
            strings, booleans, formulas (start with '=' when
            value_input_option='USER_ENTERED').
        value_input_option: 'USER_ENTERED' (default — Sheets parses
            strings as if a user typed them, so '5/15/2026' becomes a
            date, '=A1+1' becomes a formula) OR 'RAW' (every value
            inserted as-is, no parsing — use for arbitrary user input).
    """
    if value_input_option not in {"USER_ENTERED", "RAW"}:
        raise ValueError("value_input_option must be USER_ENTERED or RAW")
    res = await _run(
        _sheets_client.spreadsheets()
        .values()
        .update(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input_option,
            body={"values": values},
        )
        .execute
    )
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "updated_range": res.get("updatedRange"),
        "updated_rows": res.get("updatedRows"),
        "updated_columns": res.get("updatedColumns"),
        "updated_cells": res.get("updatedCells"),
    })


@mcp.tool(
    name="append_values",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def append_values(
    spreadsheet_id: str,
    range_a1: str,
    values: list[list[Any]],
    value_input_option: str = "USER_ENTERED",
    insert_data_option: str = "INSERT_ROWS",
) -> str:
    """
    Append rows to a table. Google looks for the first empty row after
    existing data in the given range and inserts there — ideal for
    "log this week's snapshot" workflows.

    Args:
        spreadsheet_id: The Sheet's ID.
        range_a1: A1-notation range — typically the whole tab like
            'Weekly Snapshots!A:Z'. Sheets finds the table and appends
            after it.
        values: 2D array of new rows to append.
        value_input_option: 'USER_ENTERED' (default) or 'RAW'. See
            update_values.
        insert_data_option: 'INSERT_ROWS' (default — pushes existing
            rows down) or 'OVERWRITE' (overwrites existing rows below
            the table).
    """
    if value_input_option not in {"USER_ENTERED", "RAW"}:
        raise ValueError("value_input_option must be USER_ENTERED or RAW")
    if insert_data_option not in {"INSERT_ROWS", "OVERWRITE"}:
        raise ValueError("insert_data_option must be INSERT_ROWS or OVERWRITE")
    res = await _run(
        _sheets_client.spreadsheets()
        .values()
        .append(
            spreadsheetId=spreadsheet_id,
            range=range_a1,
            valueInputOption=value_input_option,
            insertDataOption=insert_data_option,
            body={"values": values},
        )
        .execute
    )
    updates = res.get("updates") or {}
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "table_range": res.get("tableRange"),
        "updated_range": updates.get("updatedRange"),
        "updated_rows": updates.get("updatedRows"),
        "updated_columns": updates.get("updatedColumns"),
        "updated_cells": updates.get("updatedCells"),
    })


@mcp.tool(
    name="clear_values",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def clear_values(spreadsheet_id: str, range_a1: str) -> str:
    """
    Clear the values in a range (cell formatting is preserved). Use for
    resetting a tab before a fresh weekly snapshot, etc.

    Args:
        spreadsheet_id: The Sheet's ID.
        range_a1: A1-notation range to clear.
    """
    res = await _run(
        _sheets_client.spreadsheets()
        .values()
        .clear(spreadsheetId=spreadsheet_id, range=range_a1, body={})
        .execute
    )
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "cleared_range": res.get("clearedRange"),
    })


@mcp.tool(
    name="create_spreadsheet",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def create_spreadsheet(
    title: str,
    share_with_emails: Optional[list[str]] = None,
    initial_tabs: Optional[list[str]] = None,
) -> str:
    """
    Create a new Google Sheet owned by the service account. Optionally
    share it with one or more email addresses.

    Args:
        title: Spreadsheet title.
        share_with_emails: Optional list of email addresses to grant
            Editor access to. Useful so YOU can also open the new Sheet
            in your browser.
        initial_tabs: Optional list of tab names to create (the default
            single 'Sheet1' is replaced if given).
    """
    body: dict[str, Any] = {"properties": {"title": title}}
    if initial_tabs:
        body["sheets"] = [{"properties": {"title": t}} for t in initial_tabs]

    created = await _run(
        _sheets_client.spreadsheets().create(body=body).execute
    )
    sid = created.get("spreadsheetId")

    shared_with: list[str] = []
    if share_with_emails:
        for email in share_with_emails:
            try:
                await _run(
                    _drive_client.permissions()
                    .create(
                        fileId=sid,
                        body={"type": "user", "role": "writer", "emailAddress": email},
                        sendNotificationEmail=False,
                    )
                    .execute
                )
                shared_with.append(email)
            except Exception as e:
                logger.warning("Failed to share %s with %s: %s", sid, email, e)

    return _json({
        "spreadsheet_id": sid,
        "title": title,
        "url": created.get("spreadsheetUrl"),
        "sheets": [
            (s.get("properties") or {}).get("title")
            for s in created.get("sheets") or []
        ],
        "shared_with": shared_with,
        "note": (
            "The new Sheet is owned by the OAuth-authorizing Google account "
            "(typically the same account you used to mint the refresh token). "
            "Anyone in share_with_emails is granted Editor access."
        ),
    })


# --- Tools: Formatting & layout (batchUpdate-backed) ------------------------


_HALIGN = {"LEFT", "CENTER", "RIGHT"}
_VALIGN = {"TOP", "MIDDLE", "BOTTOM"}
_WRAP = {"OVERFLOW_CELL", "LEGACY_WRAP", "CLIP", "WRAP"}
_NUMFMT_TYPES = {"NUMBER", "PERCENT", "CURRENCY", "DATE", "TIME", "DATE_TIME",
                 "SCIENTIFIC", "TEXT"}


@mcp.tool(
    name="format_cells",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def format_cells(
    spreadsheet_id: str,
    range_a1: str,
    bold: Optional[bool] = None,
    italic: Optional[bool] = None,
    underline: Optional[bool] = None,
    strikethrough: Optional[bool] = None,
    font_size: Optional[int] = None,
    font_family: Optional[str] = None,
    text_color: Optional[str] = None,
    background_color: Optional[str] = None,
    horizontal_alignment: Optional[str] = None,
    vertical_alignment: Optional[str] = None,
    wrap_strategy: Optional[str] = None,
    number_format_type: Optional[str] = None,
    number_format_pattern: Optional[str] = None,
) -> str:
    """
    Apply formatting to a range. All parameters are optional — only the
    ones you set are changed, others are untouched.

    Args:
        spreadsheet_id: Sheet ID.
        range_a1: A1 range, e.g. 'Sheet1!A1:E1' for a header row.
        bold/italic/underline/strikethrough: text styles.
        font_size: integer point size (e.g. 11, 14).
        font_family: e.g. 'Arial', 'Inter', 'Roboto', 'Calibri'.
        text_color / background_color: '#RRGGBB' hex.
        horizontal_alignment: 'LEFT' | 'CENTER' | 'RIGHT'.
        vertical_alignment: 'TOP' | 'MIDDLE' | 'BOTTOM'.
        wrap_strategy: 'OVERFLOW_CELL' (default Sheets behavior), 'WRAP',
            'CLIP'.
        number_format_type: 'NUMBER' | 'PERCENT' | 'CURRENCY' | 'DATE' |
            'TIME' | 'DATE_TIME' | 'SCIENTIFIC' | 'TEXT'.
        number_format_pattern: Custom pattern like '$#,##0.00', '0.0%',
            'mmm dd, yyyy'. Use with number_format_type.
    """
    if horizontal_alignment and horizontal_alignment not in _HALIGN:
        raise ValueError(f"horizontal_alignment must be one of {sorted(_HALIGN)}")
    if vertical_alignment and vertical_alignment not in _VALIGN:
        raise ValueError(f"vertical_alignment must be one of {sorted(_VALIGN)}")
    if wrap_strategy and wrap_strategy not in _WRAP:
        raise ValueError(f"wrap_strategy must be one of {sorted(_WRAP)}")
    if number_format_type and number_format_type not in _NUMFMT_TYPES:
        raise ValueError(f"number_format_type must be one of {sorted(_NUMFMT_TYPES)}")

    cell_format: dict[str, Any] = {}
    text_format: dict[str, Any] = {}
    fields: list[str] = []

    if bold is not None:
        text_format["bold"] = bold; fields.append("userEnteredFormat.textFormat.bold")
    if italic is not None:
        text_format["italic"] = italic; fields.append("userEnteredFormat.textFormat.italic")
    if underline is not None:
        text_format["underline"] = underline; fields.append("userEnteredFormat.textFormat.underline")
    if strikethrough is not None:
        text_format["strikethrough"] = strikethrough; fields.append("userEnteredFormat.textFormat.strikethrough")
    if font_size is not None:
        text_format["fontSize"] = int(font_size); fields.append("userEnteredFormat.textFormat.fontSize")
    if font_family is not None:
        text_format["fontFamily"] = font_family; fields.append("userEnteredFormat.textFormat.fontFamily")
    if text_color is not None:
        text_format["foregroundColor"] = _hex_to_color(text_color)
        fields.append("userEnteredFormat.textFormat.foregroundColor")
    if text_format:
        cell_format["textFormat"] = text_format

    if background_color is not None:
        cell_format["backgroundColor"] = _hex_to_color(background_color)
        fields.append("userEnteredFormat.backgroundColor")
    if horizontal_alignment:
        cell_format["horizontalAlignment"] = horizontal_alignment
        fields.append("userEnteredFormat.horizontalAlignment")
    if vertical_alignment:
        cell_format["verticalAlignment"] = vertical_alignment
        fields.append("userEnteredFormat.verticalAlignment")
    if wrap_strategy:
        cell_format["wrapStrategy"] = wrap_strategy
        fields.append("userEnteredFormat.wrapStrategy")
    if number_format_type or number_format_pattern:
        nf: dict[str, Any] = {}
        if number_format_type: nf["type"] = number_format_type
        if number_format_pattern: nf["pattern"] = number_format_pattern
        cell_format["numberFormat"] = nf
        fields.append("userEnteredFormat.numberFormat")

    if not fields:
        raise ValueError("No formatting parameters provided — nothing to apply.")

    grid = await _a1_to_grid_range(spreadsheet_id, range_a1)
    requests = [{
        "repeatCell": {
            "range": grid,
            "cell": {"userEnteredFormat": cell_format},
            "fields": ",".join(fields),
        }
    }]
    res = await _batch_update(spreadsheet_id, requests)
    return _json({"spreadsheet_id": spreadsheet_id, "range": range_a1, "applied": fields, "replies": res.get("replies", [])})


_BORDER_STYLES = {"DOTTED", "DASHED", "SOLID", "SOLID_MEDIUM", "SOLID_THICK", "DOUBLE", "NONE"}


@mcp.tool(
    name="set_borders",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def set_borders(
    spreadsheet_id: str,
    range_a1: str,
    top: bool = False,
    bottom: bool = False,
    left: bool = False,
    right: bool = False,
    inner_horizontal: bool = False,
    inner_vertical: bool = False,
    all_outer: bool = False,
    all: bool = False,
    style: str = "SOLID",
    color: str = "#000000",
) -> str:
    """
    Apply borders to a range. Use the boolean flags to specify which
    sides to draw, or use shortcuts:

      - ``all_outer=True``: top + bottom + left + right
      - ``all=True``: every side including inner gridlines

    Args:
        spreadsheet_id: Sheet ID.
        range_a1: A1 range.
        top/bottom/left/right/inner_horizontal/inner_vertical: per-side toggles.
        all_outer/all: shortcuts.
        style: 'DOTTED' | 'DASHED' | 'SOLID' | 'SOLID_MEDIUM' |
            'SOLID_THICK' | 'DOUBLE' | 'NONE'.
        color: '#RRGGBB' hex.
    """
    if style not in _BORDER_STYLES:
        raise ValueError(f"style must be one of {sorted(_BORDER_STYLES)}")
    if all:
        top = bottom = left = right = inner_horizontal = inner_vertical = True
    elif all_outer:
        top = bottom = left = right = True
    if not any([top, bottom, left, right, inner_horizontal, inner_vertical]):
        raise ValueError("No sides specified — pass a side flag or all/all_outer.")

    border = {"style": style, "color": _hex_to_color(color) or {"red": 0, "green": 0, "blue": 0}}
    update: dict[str, Any] = {"range": await _a1_to_grid_range(spreadsheet_id, range_a1)}
    for side, flag in [("top", top), ("bottom", bottom), ("left", left), ("right", right),
                       ("innerHorizontal", inner_horizontal), ("innerVertical", inner_vertical)]:
        if flag:
            update[side] = border

    res = await _batch_update(spreadsheet_id, [{"updateBorders": update}])
    return _json({"spreadsheet_id": spreadsheet_id, "range": range_a1, "replies": res.get("replies", [])})


@mcp.tool(
    name="merge_cells",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def merge_cells(
    spreadsheet_id: str,
    range_a1: str,
    merge_type: str = "MERGE_ALL",
) -> str:
    """
    Merge cells in a range. The top-left cell's value is preserved.

    Args:
        spreadsheet_id: Sheet ID.
        range_a1: A1 range.
        merge_type: 'MERGE_ALL' (default — one big cell), 'MERGE_COLUMNS'
            (merge each column separately), 'MERGE_ROWS' (merge each row
            separately).
    """
    valid = {"MERGE_ALL", "MERGE_COLUMNS", "MERGE_ROWS"}
    if merge_type not in valid:
        raise ValueError(f"merge_type must be one of {sorted(valid)}")
    grid = await _a1_to_grid_range(spreadsheet_id, range_a1)
    res = await _batch_update(spreadsheet_id, [{"mergeCells": {"range": grid, "mergeType": merge_type}}])
    return _json({"spreadsheet_id": spreadsheet_id, "range": range_a1, "merge_type": merge_type, "replies": res.get("replies", [])})


@mcp.tool(
    name="freeze_rows_columns",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def freeze_rows_columns(
    spreadsheet_id: str,
    sheet_name_or_id: Any,
    frozen_rows: Optional[int] = None,
    frozen_columns: Optional[int] = None,
) -> str:
    """
    Freeze the first N rows and/or columns of a tab so they stay visible
    when scrolling.

    Args:
        spreadsheet_id: Sheet ID.
        sheet_name_or_id: Tab name (e.g. 'TestTab') or numeric sheet ID.
        frozen_rows: e.g. 1 to freeze the header row. Pass 0 to unfreeze.
        frozen_columns: e.g. 1 to freeze the leftmost column. Pass 0 to unfreeze.
    """
    sheet_id = await _resolve_sheet_id(spreadsheet_id, sheet_name_or_id)
    props: dict[str, Any] = {"sheetId": sheet_id, "gridProperties": {}}
    fields: list[str] = []
    if frozen_rows is not None:
        props["gridProperties"]["frozenRowCount"] = int(frozen_rows)
        fields.append("gridProperties.frozenRowCount")
    if frozen_columns is not None:
        props["gridProperties"]["frozenColumnCount"] = int(frozen_columns)
        fields.append("gridProperties.frozenColumnCount")
    if not fields:
        raise ValueError("Pass at least one of frozen_rows or frozen_columns.")

    res = await _batch_update(spreadsheet_id, [{
        "updateSheetProperties": {"properties": props, "fields": ",".join(fields)}
    }])
    return _json({"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "applied": fields, "replies": res.get("replies", [])})


@mcp.tool(
    name="set_column_width",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def set_column_width(
    spreadsheet_id: str,
    sheet_name_or_id: Any,
    start_column: int,
    end_column: int,
    width_pixels: int,
) -> str:
    """
    Set the pixel width of one or more columns.

    Args:
        spreadsheet_id: Sheet ID.
        sheet_name_or_id: Tab name or numeric ID.
        start_column: 1-indexed column number (A=1, B=2, ...).
        end_column: 1-indexed column number (inclusive).
        width_pixels: Column width in pixels (Sheets default is ~100).
    """
    sheet_id = await _resolve_sheet_id(spreadsheet_id, sheet_name_or_id)
    res = await _batch_update(spreadsheet_id, [{
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": int(start_column) - 1,
                "endIndex": int(end_column),
            },
            "properties": {"pixelSize": int(width_pixels)},
            "fields": "pixelSize",
        }
    }])
    return _json({"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "columns": f"{start_column}-{end_column}", "width_pixels": width_pixels, "replies": res.get("replies", [])})


@mcp.tool(
    name="auto_resize_columns",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def auto_resize_columns(
    spreadsheet_id: str,
    sheet_name_or_id: Any,
    start_column: int,
    end_column: int,
) -> str:
    """
    Auto-fit one or more columns to their content.

    Args:
        spreadsheet_id: Sheet ID.
        sheet_name_or_id: Tab name or numeric ID.
        start_column: 1-indexed column number.
        end_column: 1-indexed column number (inclusive).
    """
    sheet_id = await _resolve_sheet_id(spreadsheet_id, sheet_name_or_id)
    res = await _batch_update(spreadsheet_id, [{
        "autoResizeDimensions": {
            "dimensions": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": int(start_column) - 1,
                "endIndex": int(end_column),
            }
        }
    }])
    return _json({"spreadsheet_id": spreadsheet_id, "sheet_id": sheet_id, "columns": f"{start_column}-{end_column}", "replies": res.get("replies", [])})


# --- Tools: Sheet (tab) management -------------------------------------------


@mcp.tool(
    name="add_sheet",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def add_sheet(
    spreadsheet_id: str,
    title: str,
    rows: Optional[int] = None,
    columns: Optional[int] = None,
    index: Optional[int] = None,
) -> str:
    """
    Add a new tab to an existing spreadsheet.

    Args:
        spreadsheet_id: Sheet ID.
        title: New tab name (must be unique within the spreadsheet).
        rows: Optional initial row count (default Sheets: 1000).
        columns: Optional initial column count (default Sheets: 26).
        index: Optional 0-indexed position; default appends to the end.
    """
    props: dict[str, Any] = {"title": title}
    grid: dict[str, Any] = {}
    if rows is not None: grid["rowCount"] = int(rows)
    if columns is not None: grid["columnCount"] = int(columns)
    if grid: props["gridProperties"] = grid
    if index is not None: props["index"] = int(index)
    res = await _batch_update(spreadsheet_id, [{"addSheet": {"properties": props}}])
    _invalidate_sheet_cache(spreadsheet_id)
    new_sheet = (res.get("replies") or [{}])[0].get("addSheet", {}).get("properties", {})
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "sheet_id": new_sheet.get("sheetId"),
        "title": new_sheet.get("title"),
        "index": new_sheet.get("index"),
    })


@mcp.tool(
    name="delete_sheet",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True},
)
async def delete_sheet(spreadsheet_id: str, sheet_name_or_id: Any) -> str:
    """
    Delete a tab from a spreadsheet. Cannot delete the only remaining tab.

    Args:
        spreadsheet_id: Sheet ID.
        sheet_name_or_id: Tab name or numeric sheet ID.
    """
    sheet_id = await _resolve_sheet_id(spreadsheet_id, sheet_name_or_id)
    res = await _batch_update(spreadsheet_id, [{"deleteSheet": {"sheetId": sheet_id}}])
    _invalidate_sheet_cache(spreadsheet_id)
    return _json({"spreadsheet_id": spreadsheet_id, "deleted_sheet_id": sheet_id, "replies": res.get("replies", [])})


@mcp.tool(
    name="duplicate_sheet",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def duplicate_sheet(
    spreadsheet_id: str,
    source_sheet_name_or_id: Any,
    new_title: str,
    insert_at_index: Optional[int] = None,
) -> str:
    """
    Duplicate an existing tab (including all data and formatting) under
    a new name.

    Args:
        spreadsheet_id: Sheet ID.
        source_sheet_name_or_id: Tab to copy.
        new_title: Name for the new copy (must be unique).
        insert_at_index: Optional 0-indexed position; default appends.
    """
    src_id = await _resolve_sheet_id(spreadsheet_id, source_sheet_name_or_id)
    body: dict[str, Any] = {
        "sourceSheetId": src_id,
        "newSheetName": new_title,
    }
    if insert_at_index is not None:
        body["insertSheetIndex"] = int(insert_at_index)
    res = await _batch_update(spreadsheet_id, [{"duplicateSheet": body}])
    _invalidate_sheet_cache(spreadsheet_id)
    new_sheet = (res.get("replies") or [{}])[0].get("duplicateSheet", {}).get("properties", {})
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "source_sheet_id": src_id,
        "new_sheet_id": new_sheet.get("sheetId"),
        "new_title": new_sheet.get("title"),
    })


# --- Tools: Charts -----------------------------------------------------------


_CHART_TYPES = {"LINE", "BAR", "COLUMN", "AREA", "SCATTER", "COMBO", "STEPPED_AREA", "PIE"}


@mcp.tool(
    name="add_chart",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def add_chart(
    spreadsheet_id: str,
    chart_type: str,
    data_range_a1: str,
    title: Optional[str] = None,
    anchor_sheet_name_or_id: Optional[Any] = None,
    anchor_row: int = 1,
    anchor_column: int = 1,
    width_pixels: int = 600,
    height_pixels: int = 371,
    legend_position: str = "BOTTOM_LEGEND",
    headers_in_first_row: bool = True,
    headers_in_first_column: bool = False,
) -> str:
    """
    Add a chart to a sheet. Covers the common cases — for highly
    customized charts use the batch_update escape hatch.

    Args:
        spreadsheet_id: Sheet ID.
        chart_type: 'LINE' | 'BAR' | 'COLUMN' | 'AREA' | 'SCATTER' |
            'COMBO' | 'STEPPED_AREA' | 'PIE'.
        data_range_a1: A1 range of the source data including headers,
            e.g. 'Data!A1:C20'.
        title: Optional chart title.
        anchor_sheet_name_or_id: Tab to place the chart on. Default: the
            same tab as data_range_a1.
        anchor_row / anchor_column: 1-indexed cell to anchor the chart's
            top-left corner.
        width_pixels / height_pixels: Chart dimensions.
        legend_position: 'BOTTOM_LEGEND' (default), 'TOP_LEGEND',
            'LEFT_LEGEND', 'RIGHT_LEGEND', 'NO_LEGEND', 'LABELED_LEGEND'.
        headers_in_first_row: First row is column headers (default True).
        headers_in_first_column: First column is row labels (default
            True effectively — Sheets uses it as the domain axis).
    """
    chart_type = chart_type.upper()
    if chart_type not in _CHART_TYPES:
        raise ValueError(f"chart_type must be one of {sorted(_CHART_TYPES)}")

    data_grid = await _a1_to_grid_range(spreadsheet_id, data_range_a1)
    anchor_sheet_id = (
        await _resolve_sheet_id(spreadsheet_id, anchor_sheet_name_or_id)
        if anchor_sheet_name_or_id is not None
        else data_grid["sheetId"]
    )

    spec: dict[str, Any] = {}
    if title:
        spec["title"] = title

    if chart_type == "PIE":
        # Pie chart wants domain (labels) + one series (values).
        # Convention: first column = labels, second column = values.
        sheet_id = data_grid["sheetId"]
        s_row = data_grid.get("startRowIndex", 0)
        e_row = data_grid.get("endRowIndex")
        s_col = data_grid.get("startColumnIndex", 0)
        e_col = data_grid.get("endColumnIndex", s_col + 2)
        spec["pieChart"] = {
            "legendPosition": legend_position,
            "threeDimensional": False,
            "domain": {"sourceRange": {"sources": [{
                "sheetId": sheet_id,
                "startRowIndex": s_row + (1 if headers_in_first_row else 0),
                "endRowIndex": e_row,
                "startColumnIndex": s_col,
                "endColumnIndex": s_col + 1,
            }]}},
            "series": {"sourceRange": {"sources": [{
                "sheetId": sheet_id,
                "startRowIndex": s_row + (1 if headers_in_first_row else 0),
                "endRowIndex": e_row,
                "startColumnIndex": s_col + 1,
                "endColumnIndex": s_col + 2,
            }]}},
        }
    else:
        # basicChart: domain = first column, series = remaining columns.
        sheet_id = data_grid["sheetId"]
        s_row = data_grid.get("startRowIndex", 0)
        e_row = data_grid.get("endRowIndex")
        s_col = data_grid.get("startColumnIndex", 0)
        e_col = data_grid.get("endColumnIndex", s_col + 2)
        series = []
        for c in range(s_col + 1, e_col):
            series.append({
                "series": {"sourceRange": {"sources": [{
                    "sheetId": sheet_id,
                    "startRowIndex": s_row,
                    "endRowIndex": e_row,
                    "startColumnIndex": c,
                    "endColumnIndex": c + 1,
                }]}},
                "targetAxis": "LEFT_AXIS",
            })
        spec["basicChart"] = {
            "chartType": chart_type,
            "legendPosition": legend_position,
            "headerCount": 1 if headers_in_first_row else 0,
            "domains": [{
                "domain": {"sourceRange": {"sources": [{
                    "sheetId": sheet_id,
                    "startRowIndex": s_row,
                    "endRowIndex": e_row,
                    "startColumnIndex": s_col,
                    "endColumnIndex": s_col + 1,
                }]}}
            }],
            "series": series,
        }

    request = {
        "addChart": {
            "chart": {
                "spec": spec,
                "position": {
                    "overlayPosition": {
                        "anchorCell": {
                            "sheetId": anchor_sheet_id,
                            "rowIndex": int(anchor_row) - 1,
                            "columnIndex": int(anchor_column) - 1,
                        },
                        "widthPixels": int(width_pixels),
                        "heightPixels": int(height_pixels),
                    }
                },
            }
        }
    }
    res = await _batch_update(spreadsheet_id, [request])
    new_chart = (res.get("replies") or [{}])[0].get("addChart", {}).get("chart", {})
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "chart_id": new_chart.get("chartId"),
        "chart_type": chart_type,
        "title": title,
        "anchor": {"sheet_id": anchor_sheet_id, "row": anchor_row, "column": anchor_column},
        "data_range": data_range_a1,
    })


# --- Tools: Escape hatch -----------------------------------------------------


@mcp.tool(
    name="batch_update",
    annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False},
)
async def batch_update(spreadsheet_id: str, requests: list[dict]) -> str:
    """
    Raw spreadsheets.batchUpdate escape hatch — runs any combination of
    the ~50 request types the Sheets API supports (conditional formatting,
    pivot tables, named ranges, protected ranges, banding, find/replace,
    sort/filter, slicers, etc.).

    Pass the exact 'requests' list per Google's spec:
    https://developers.google.com/sheets/api/reference/rest/v4/spreadsheets/request

    Use the dedicated wrappers (format_cells, add_chart, etc.) when they
    cover what you need — they handle GridRange conversion and field
    masking for you. Drop down to this tool only for features the
    wrappers don't expose.

    Args:
        spreadsheet_id: Sheet ID.
        requests: List of request dicts, each keyed by request type
            (e.g. {'addConditionalFormatRule': {...}}).
    """
    if not requests:
        raise ValueError("requests must be a non-empty list")
    res = await _batch_update(spreadsheet_id, requests)
    return _json({
        "spreadsheet_id": spreadsheet_id,
        "request_count": len(requests),
        "replies": res.get("replies", []),
    })


# --- OAuth 2.0 with PKCE (synthetic — issues MCP_API_KEY as access_token) ---

_oauth_codes: dict[str, dict[str, Any]] = {}
_oauth_clients: dict[str, dict[str, Any]] = {}
OAUTH_CODE_TTL = 300


def _cleanup_expired_codes() -> None:
    now = time.time()
    expired = [c for c, m in _oauth_codes.items() if m["expires_at"] < now]
    for c in expired:
        del _oauth_codes[c]


_OAUTH_PUBLIC_PATHS = {
    "/health",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/token",
    "/register",
}


class APIKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path.rstrip("/")
        if path in _OAUTH_PUBLIC_PATHS:
            return await call_next(request)
        auth = request.headers.get("Authorization", "")
        if not (auth.startswith("Bearer ") and auth[7:] == MCP_API_KEY):
            logger.warning("Unauthorized request to %s", request.url.path)
            return JSONResponse({"error": "Unauthorized"}, status_code=401)
        return await call_next(request)


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _get_base_url(request: Request) -> str:
    base = os.environ.get("SERVER_URL", "").rstrip("/")
    if base:
        return base
    base = str(request.base_url).rstrip("/")
    proto = request.headers.get("x-forwarded-proto", "")
    if proto == "https" and base.startswith("http://"):
        base = "https://" + base[7:]
    elif not proto and "ondigitalocean.app" in base:
        base = base.replace("http://", "https://")
    return base


async def oauth_metadata(request: Request) -> JSONResponse:
    base = _get_base_url(request)
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "token_endpoint_auth_methods_supported": ["none"],
        "code_challenge_methods_supported": ["S256"],
    })


async def oauth_protected_resource(request: Request) -> JSONResponse:
    base = _get_base_url(request)
    return JSONResponse({"resource": base, "authorization_servers": [base]})


async def oauth_register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    client_id = secrets.token_hex(16)
    client_info = {
        "client_id": client_id,
        "client_name": body.get("client_name", "unknown"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": "none",
    }
    _oauth_clients[client_id] = client_info
    return JSONResponse(client_info, status_code=201)


async def oauth_authorize(request: Request) -> RedirectResponse | JSONResponse:
    params = dict(request.query_params)
    redirect_uri = params.get("redirect_uri", "")
    state = params.get("state", "")
    code_challenge = params.get("code_challenge", "")
    code_challenge_method = params.get("code_challenge_method", "S256")
    if not redirect_uri:
        return JSONResponse({"error": "missing redirect_uri"}, status_code=400)
    _cleanup_expired_codes()
    code = secrets.token_urlsafe(32)
    _oauth_codes[code] = {
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
        "expires_at": time.time() + OAUTH_CODE_TTL,
    }
    qs = urlencode({"code": code, "state": state} if state else {"code": code})
    return RedirectResponse(f"{redirect_uri}?{qs}", status_code=302)


async def oauth_token(request: Request) -> JSONResponse:
    try:
        body = dict(await request.form())
    except Exception:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)
    if body.get("grant_type", "") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    _cleanup_expired_codes()
    code_meta = _oauth_codes.pop(body.get("code", ""), None)
    if not code_meta:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Code expired or invalid"},
            status_code=400,
        )
    if code_meta["code_challenge"]:
        digest = hashlib.sha256(body.get("code_verifier", "").encode("ascii")).digest()
        computed = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed != code_meta["code_challenge"]:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )
    return JSONResponse({"access_token": MCP_API_KEY, "token_type": "Bearer"})


# --- App Assembly ------------------------------------------------------------

mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_asgi.lifespan(app):
        logger.info("Google Sheets MCP server ready on port %d", PORT)
        yield


app = Starlette(
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", endpoint=oauth_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource, methods=["GET"]),
        Route("/authorize", endpoint=oauth_authorize, methods=["GET"]),
        Route("/token", endpoint=oauth_token, methods=["POST"]),
        Route("/register", endpoint=oauth_register, methods=["POST"]),
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)

app.add_middleware(APIKeyMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
