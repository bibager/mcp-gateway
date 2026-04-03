"""
Todoist MCP Server
==================
Dual-protocol server:
  - /mcp   FastMCP streamable-HTTP (for Claude CoWork / Claude Desktop)
  - /health Unauthenticated health check

Auth: Authorization: Bearer {MCP_API_KEY} on every route except public paths.

The MCP OAuth flow is synthetic: it auto-approves and issues MCP_API_KEY as the
access_token. This means Claude CoWork's stored token never expires — no more
daily 401s.

Todoist API auth supports two modes:
  1. OAuth (preferred): Visit /todoist/auth?key={MCP_API_KEY} in a browser to
     authorize via your Todoist developer app. The token is stored in memory
     and displayed so you can persist it as TODOIST_API_TOKEN in env vars.
  2. Static token fallback: Set TODOIST_API_TOKEN directly.

Env vars:
  MCP_API_KEY            required; protects all /mcp endpoints
  TODOIST_API_TOKEN      optional fallback token; used until OAuth flow completes
  TODOIST_CLIENT_ID      required for Todoist OAuth flow
  TODOIST_CLIENT_SECRET  required for Todoist OAuth flow
  PORT                   optional, defaults to 8001
  SERVER_URL             optional; overrides base URL detection (e.g. https://todoist.bibager.com)
"""

from __future__ import annotations

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

import httpx
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("todoist_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
TODOIST_API_TOKEN: str = os.getenv("TODOIST_API_TOKEN", "")  # fallback; may be empty if using OAuth
TODOIST_CLIENT_ID: str = os.getenv("TODOIST_CLIENT_ID", "")
TODOIST_CLIENT_SECRET: str = os.getenv("TODOIST_CLIENT_SECRET", "")
PORT: int = int(os.getenv("PORT", "8001"))

TODOIST_BASE = "https://api.todoist.com/api/v1"

# Active Todoist token — starts from env var, replaced after OAuth flow
_todoist_access_token: str = TODOIST_API_TOKEN

# Pending Todoist OAuth states: state -> expires_at timestamp
_todoist_oauth_states: dict[str, float] = {}

# --- OAuth 2.0 with PKCE (synthetic — issues MCP_API_KEY as access_token) ---

_oauth_codes: dict[str, dict[str, Any]] = {}
_oauth_clients: dict[str, dict[str, Any]] = {}
OAUTH_CODE_TTL = 300


def _cleanup_expired_codes() -> None:
    now = time.time()
    expired = [c for c, m in _oauth_codes.items() if m["expires_at"] < now]
    for c in expired:
        del _oauth_codes[c]


# --- Helpers -----------------------------------------------------------------

def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_todoist_access_token}",
        "Content-Type": "application/json",
    }


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "todoist_mcp",
    instructions=(
        "Tools for managing Todoist tasks, projects, sections, and labels. "
        "Call get_projects first to discover project IDs when filtering tasks. "
        "Priority scale: 1=normal, 2=medium, 3=high, 4=urgent."
    ),
)

# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(name="get_projects", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_projects() -> str:
    """Return all Todoist projects with their IDs, names, and metadata."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/projects", headers=_auth_headers())
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="get_tasks", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_tasks(
    project_id: Optional[str] = None,
    section_id: Optional[str] = None,
    label: Optional[str] = None,
    filter: Optional[str] = None,
    ids: Optional[list[str]] = None,
) -> str:
    """
    Return tasks with optional filters.

    Args:
        project_id: Filter by project ID (get IDs from get_projects).
        section_id: Filter by section ID.
        label: Filter by label name.
        filter: Todoist filter query e.g. "today", "overdue", "p1", "no date".
        ids: List of specific task IDs to retrieve.
    """
    params: dict[str, Any] = {}
    if project_id:
        params["project_id"] = project_id
    if section_id:
        params["section_id"] = section_id
    if label:
        params["label"] = label
    if filter:
        params["filter"] = filter
    if ids:
        params["ids"] = ",".join(ids)
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/tasks", headers=_auth_headers(), params=params)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="get_task", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_task(task_id: str) -> str:
    """
    Return a single task by ID.

    Args:
        task_id: The task ID.
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/tasks/{task_id}", headers=_auth_headers())
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="create_task", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_task(
    content: str,
    project_id: Optional[str] = None,
    section_id: Optional[str] = None,
    parent_id: Optional[str] = None,
    description: Optional[str] = None,
    labels: Optional[list[str]] = None,
    priority: Optional[int] = None,
    due_string: Optional[str] = None,
    due_date: Optional[str] = None,
    due_datetime: Optional[str] = None,
) -> str:
    """
    Create a new task.

    Args:
        content: Task title (required).
        project_id: Project to add to (defaults to Inbox).
        section_id: Section within the project.
        parent_id: Parent task ID to create a subtask.
        description: Longer description / notes.
        labels: Label names to apply e.g. ["work", "urgent"].
        priority: 1=normal, 2=medium, 3=high, 4=urgent.
        due_string: Natural language due date e.g. "tomorrow", "every Monday".
        due_date: Due date in YYYY-MM-DD format.
        due_datetime: Due datetime in RFC3339 format.
    """
    body: dict[str, Any] = {"content": content}
    for k, v in {
        "project_id": project_id,
        "section_id": section_id,
        "parent_id": parent_id,
        "description": description,
        "labels": labels,
        "priority": priority,
        "due_string": due_string,
        "due_date": due_date,
        "due_datetime": due_datetime,
    }.items():
        if v is not None:
            body[k] = v
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/tasks", headers=_auth_headers(), json=body)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="update_task", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def update_task(
    task_id: str,
    content: Optional[str] = None,
    description: Optional[str] = None,
    labels: Optional[list[str]] = None,
    priority: Optional[int] = None,
    due_string: Optional[str] = None,
    due_date: Optional[str] = None,
    due_datetime: Optional[str] = None,
) -> str:
    """
    Update an existing task.

    Args:
        task_id: The task ID to update (required).
        content: New title/content.
        description: New description/notes.
        labels: New label list — replaces existing labels.
        priority: 1=normal, 2=medium, 3=high, 4=urgent.
        due_string: Natural language due date e.g. "next Friday".
        due_date: Due date in YYYY-MM-DD.
        due_datetime: Due datetime in RFC3339.
    """
    body: dict[str, Any] = {}
    for k, v in {
        "content": content,
        "description": description,
        "labels": labels,
        "priority": priority,
        "due_string": due_string,
        "due_date": due_date,
        "due_datetime": due_datetime,
    }.items():
        if v is not None:
            body[k] = v
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/tasks/{task_id}", headers=_auth_headers(), json=body)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="move_task", annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True})
async def move_task(
    task_id: str,
    project_id: Optional[str] = None,
    section_id: Optional[str] = None,
    parent_id: Optional[str] = None,
) -> str:
    """
    Move a task to a different project, section, or parent.

    Args:
        task_id: The task ID to move.
        project_id: Target project ID.
        section_id: Target section ID.
        parent_id: New parent task ID (converts task to a subtask).
    """
    body: dict[str, Any] = {}
    if project_id:
        body["project_id"] = project_id
    if section_id:
        body["section_id"] = section_id
    if parent_id:
        body["parent_id"] = parent_id
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/tasks/{task_id}", headers=_auth_headers(), json=body)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="complete_task", annotations={"readOnlyHint": False, "destructiveHint": False})
async def complete_task(task_id: str) -> str:
    """
    Mark a task as complete (close it).

    Args:
        task_id: The task ID to complete.
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/tasks/{task_id}/close", headers=_auth_headers())
        r.raise_for_status()
        return _json({"status": "completed", "task_id": task_id})


@mcp.tool(name="reopen_task", annotations={"readOnlyHint": False, "destructiveHint": False})
async def reopen_task(task_id: str) -> str:
    """
    Reopen a previously completed task.

    Args:
        task_id: The task ID to reopen.
    """
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/tasks/{task_id}/reopen", headers=_auth_headers())
        r.raise_for_status()
        return _json({"status": "reopened", "task_id": task_id})


@mcp.tool(name="delete_task", annotations={"readOnlyHint": False, "destructiveHint": True})
async def delete_task(task_id: str) -> str:
    """
    Permanently delete a task. This cannot be undone.

    Args:
        task_id: The task ID to delete.
    """
    async with httpx.AsyncClient() as client:
        r = await client.delete(f"{TODOIST_BASE}/tasks/{task_id}", headers=_auth_headers())
        r.raise_for_status()
        return _json({"status": "deleted", "task_id": task_id})


@mcp.tool(name="get_sections", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_sections(project_id: Optional[str] = None) -> str:
    """
    Return sections, optionally filtered by project.

    Args:
        project_id: Filter sections to this project ID.
    """
    params: dict[str, Any] = {}
    if project_id:
        params["project_id"] = project_id
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/sections", headers=_auth_headers(), params=params)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="create_section", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_section(name: str, project_id: str, order: Optional[int] = None) -> str:
    """
    Create a new section in a project.

    Args:
        name: Section name.
        project_id: Project to create the section in.
        order: Display order position.
    """
    body: dict[str, Any] = {"name": name, "project_id": project_id}
    if order is not None:
        body["order"] = order
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/sections", headers=_auth_headers(), json=body)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="get_labels", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_labels() -> str:
    """Return all personal labels."""
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/labels", headers=_auth_headers())
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="get_comments", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_comments(
    task_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Return comments for a task or project.

    Args:
        task_id: Get comments for this task ID.
        project_id: Get comments for this project ID.
    """
    params: dict[str, Any] = {}
    if task_id:
        params["task_id"] = task_id
    if project_id:
        params["project_id"] = project_id
    async with httpx.AsyncClient() as client:
        r = await client.get(f"{TODOIST_BASE}/comments", headers=_auth_headers(), params=params)
        r.raise_for_status()
        return _json(r.json())


@mcp.tool(name="add_comment", annotations={"readOnlyHint": False, "destructiveHint": False})
async def add_comment(
    content: str,
    task_id: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Add a comment to a task or project.

    Args:
        content: Comment text (markdown supported).
        task_id: Task to comment on.
        project_id: Project to comment on.
    """
    body: dict[str, Any] = {"content": content}
    if task_id:
        body["task_id"] = task_id
    if project_id:
        body["project_id"] = project_id
    async with httpx.AsyncClient() as client:
        r = await client.post(f"{TODOIST_BASE}/comments", headers=_auth_headers(), json=body)
        r.raise_for_status()
        return _json(r.json())


# --- Todoist OAuth Routes ----------------------------------------------------


async def todoist_auth_start(request: Request) -> RedirectResponse | JSONResponse:
    """
    Start the Todoist OAuth 2.0 authorization flow.
    Visit /todoist/auth?key={MCP_API_KEY} in a browser to authorize.
    Protected by key= query param so only the server owner can trigger it.
    """
    if request.query_params.get("key", "") != MCP_API_KEY:
        return JSONResponse({"error": "Forbidden — include ?key={MCP_API_KEY}"}, status_code=403)
    if not TODOIST_CLIENT_ID or not TODOIST_CLIENT_SECRET:
        return JSONResponse(
            {"error": "TODOIST_CLIENT_ID and TODOIST_CLIENT_SECRET env vars are required"},
            status_code=500,
        )

    state = secrets.token_urlsafe(32)
    _todoist_oauth_states[state] = time.time() + 600  # 10-minute window

    base = _get_base_url(request)
    redirect_uri = f"{base}/todoist/callback"
    params = urlencode({
        "client_id": TODOIST_CLIENT_ID,
        "scope": "data:read_write,data:delete",
        "state": state,
        "redirect_uri": redirect_uri,
    })
    return RedirectResponse(f"https://todoist.com/oauth/authorize?{params}", status_code=302)


async def todoist_auth_callback(request: Request) -> JSONResponse:
    """
    Todoist OAuth callback — exchanges auth code for an access token.
    The token is stored in memory and shown so you can persist it in env vars.
    """
    global _todoist_access_token

    params = dict(request.query_params)
    error = params.get("error", "")
    if error:
        return JSONResponse({"error": error}, status_code=400)

    code = params.get("code", "")
    state = params.get("state", "")
    if not code or not state:
        return JSONResponse({"error": "missing code or state"}, status_code=400)

    expires_at = _todoist_oauth_states.pop(state, None)
    if not expires_at or time.time() > expires_at:
        return JSONResponse({"error": "invalid or expired state — restart the flow"}, status_code=400)

    base = _get_base_url(request)
    redirect_uri = f"{base}/todoist/callback"

    async with httpx.AsyncClient() as client:
        r = await client.post(
            "https://todoist.com/oauth/access_token",
            data={
                "client_id": TODOIST_CLIENT_ID,
                "client_secret": TODOIST_CLIENT_SECRET,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        r.raise_for_status()
        data = r.json()

    token = data.get("access_token", "")
    if not token:
        return JSONResponse({"error": "no access_token in response", "detail": data}, status_code=500)

    _todoist_access_token = token
    logger.info("Todoist access token updated via OAuth flow")

    return JSONResponse({
        "status": "authorized",
        "message": (
            "Todoist OAuth successful! The server is now using this token. "
            "To persist it across restarts, copy the access_token below and set "
            "TODOIST_API_TOKEN to this value in your DigitalOcean environment variables."
        ),
        "access_token": token,
    })


# --- Auth Middleware ----------------------------------------------------------

_OAUTH_PUBLIC_PATHS = {
    "/health",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    "/authorize",
    "/token",
    "/register",
    "/todoist/auth",
    "/todoist/callback",
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


# --- Utility Routes ----------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --- OAuth 2.0 Endpoints (synthetic PKCE flow) -------------------------------


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
    """RFC 8414 OAuth Authorization Server Metadata."""
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
    """RFC 9728 OAuth Protected Resource Metadata."""
    base = _get_base_url(request)
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


async def oauth_register(request: Request) -> JSONResponse:
    """RFC 7591 Dynamic Client Registration."""
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
    """OAuth 2.0 Authorization Endpoint — auto-approves, redirects with code."""
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
    """OAuth 2.0 Token Endpoint — exchanges auth code for MCP_API_KEY."""
    try:
        body = dict(await request.form())
    except Exception:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid_request"}, status_code=400)

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    _cleanup_expired_codes()
    code_meta = _oauth_codes.pop(code, None)
    if not code_meta:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Code expired or invalid"},
            status_code=400,
        )

    if code_meta["code_challenge"]:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed != code_meta["code_challenge"]:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

    # Issue MCP_API_KEY as the access token — it never expires
    return JSONResponse({
        "access_token": MCP_API_KEY,
        "token_type": "Bearer",
    })


# --- App Assembly ------------------------------------------------------------

mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_asgi.lifespan(app):
        logger.info("Todoist MCP server ready on port %d", PORT)
        yield


app = Starlette(
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", endpoint=oauth_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource, methods=["GET"]),
        Route("/authorize", endpoint=oauth_authorize, methods=["GET"]),
        Route("/token", endpoint=oauth_token, methods=["POST"]),
        Route("/register", endpoint=oauth_register, methods=["POST"]),
        Route("/todoist/auth", endpoint=todoist_auth_start, methods=["GET"]),
        Route("/todoist/callback", endpoint=todoist_auth_callback, methods=["GET"]),
        Mount("/", app=mcp_asgi),
    ],
    lifespan=lifespan,
)

app.add_middleware(APIKeyMiddleware)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
