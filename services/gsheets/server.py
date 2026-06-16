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
