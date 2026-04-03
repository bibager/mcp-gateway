"""
GitLab MCP Server
=================
Dual-protocol server:
  - /mcp   FastMCP streamable-HTTP (for Claude CoWork / Claude Desktop)
  - /health Unauthenticated health check

Auth: Authorization: Bearer {MCP_API_KEY} on every route except public paths.

The MCP OAuth flow is synthetic: it auto-approves and issues MCP_API_KEY as the
access_token so Claude CoWork's stored token never expires.

Env vars:
  MCP_API_KEY        required; protects all /mcp endpoints
  GITLAB_TOKEN       required; GitLab personal access token
  GITLAB_PROJECT_ID  optional; default project (URL-encoded path e.g. "trackio1%2Ftrack-app")
  PORT               optional, defaults to 8003
  SERVER_URL         optional; overrides base URL detection
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
from urllib.parse import urlencode, quote

import httpx
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("gitlab_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
GITLAB_TOKEN: str = os.environ.get("GITLAB_TOKEN", "")
GITLAB_PROJECT_ID: str = os.environ.get("GITLAB_PROJECT_ID", "trackio1%2Ftrack-app")
PORT: int = int(os.getenv("PORT", "8003"))

GITLAB_BASE = "https://gitlab.com/api/v4"

# --- Helpers -----------------------------------------------------------------


def _auth_headers() -> dict[str, str]:
    return {"PRIVATE-TOKEN": GITLAB_TOKEN}


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


def _project_url(project_id: Optional[str] = None) -> str:
    pid = project_id or GITLAB_PROJECT_ID
    return f"{GITLAB_BASE}/projects/{pid}"


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "gitlab_mcp",
    instructions=(
        "Tools for monitoring and querying a GitLab repository. "
        "Default project is trackio1/track-app. "
        "Use get_recent_commits to see latest changes, get_merge_requests for open MRs, "
        "and get_pipelines to check CI/CD status."
    ),
)

# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(name="get_project_info", annotations={"readOnlyHint": True})
async def get_project_info(project_id: Optional[str] = None) -> str:
    """Get project metadata including name, description, default branch, and stats."""
    async with httpx.AsyncClient() as client:
        r = await client.get(_project_url(project_id), headers=_auth_headers())
        r.raise_for_status()
        p = r.json()
        return _json({
            "id": p["id"],
            "name": p["name"],
            "path_with_namespace": p["path_with_namespace"],
            "description": p.get("description"),
            "default_branch": p.get("default_branch"),
            "web_url": p["web_url"],
            "created_at": p.get("created_at"),
            "last_activity_at": p.get("last_activity_at"),
            "star_count": p.get("star_count"),
            "forks_count": p.get("forks_count"),
            "open_issues_count": p.get("open_issues_count"),
        })


@mcp.tool(name="get_recent_commits", annotations={"readOnlyHint": True})
async def get_recent_commits(
    branch: Optional[str] = None,
    limit: int = 20,
    project_id: Optional[str] = None,
) -> str:
    """
    Get recent commits from the repository.

    Args:
        branch: Branch name (defaults to project default branch).
        limit: Number of commits to return (max 100).
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"per_page": min(limit, 100)}
    if branch:
        params["ref_name"] = branch
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/commits",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        commits = r.json()
        return _json([{
            "id": c["short_id"],
            "title": c["title"],
            "author_name": c["author_name"],
            "created_at": c["created_at"],
            "message": c["message"].strip(),
            "web_url": c["web_url"],
        } for c in commits])


@mcp.tool(name="get_commit_diff", annotations={"readOnlyHint": True})
async def get_commit_diff(
    commit_sha: str,
    project_id: Optional[str] = None,
) -> str:
    """
    Get the diff for a specific commit.

    Args:
        commit_sha: The commit SHA or short SHA.
        project_id: Override project (URL-encoded path).
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/commits/{commit_sha}/diff",
            headers=_auth_headers(),
        )
        r.raise_for_status()
        diffs = r.json()
        return _json([{
            "old_path": d["old_path"],
            "new_path": d["new_path"],
            "new_file": d.get("new_file", False),
            "deleted_file": d.get("deleted_file", False),
            "diff": d.get("diff", "")[:2000],  # truncate large diffs
        } for d in diffs])


@mcp.tool(name="get_merge_requests", annotations={"readOnlyHint": True})
async def get_merge_requests(
    state: str = "opened",
    limit: int = 20,
    project_id: Optional[str] = None,
) -> str:
    """
    List merge requests for the project.

    Args:
        state: Filter by state: opened, closed, merged, all.
        limit: Number of MRs to return (max 100).
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"state": state, "per_page": min(limit, 100)}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/merge_requests",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        mrs = r.json()
        return _json([{
            "iid": m["iid"],
            "title": m["title"],
            "state": m["state"],
            "author": m["author"]["name"],
            "source_branch": m["source_branch"],
            "target_branch": m["target_branch"],
            "created_at": m["created_at"],
            "updated_at": m["updated_at"],
            "web_url": m["web_url"],
            "merge_status": m.get("merge_status"),
            "draft": m.get("draft", False),
        } for m in mrs])


@mcp.tool(name="get_merge_request_changes", annotations={"readOnlyHint": True})
async def get_merge_request_changes(
    mr_iid: int,
    project_id: Optional[str] = None,
) -> str:
    """
    Get the changes/diff for a specific merge request.

    Args:
        mr_iid: The merge request IID (number shown in GitLab UI).
        project_id: Override project (URL-encoded path).
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/merge_requests/{mr_iid}/changes",
            headers=_auth_headers(),
        )
        r.raise_for_status()
        data = r.json()
        changes = data.get("changes", [])
        return _json({
            "title": data["title"],
            "description": data.get("description", ""),
            "changes_count": len(changes),
            "changes": [{
                "old_path": c["old_path"],
                "new_path": c["new_path"],
                "new_file": c.get("new_file", False),
                "deleted_file": c.get("deleted_file", False),
                "diff": c.get("diff", "")[:2000],
            } for c in changes[:30]],  # limit to 30 files
        })


@mcp.tool(name="get_pipelines", annotations={"readOnlyHint": True})
async def get_pipelines(
    status: Optional[str] = None,
    limit: int = 10,
    project_id: Optional[str] = None,
) -> str:
    """
    Get recent CI/CD pipelines.

    Args:
        status: Filter by status: running, pending, success, failed, canceled, skipped.
        limit: Number of pipelines to return (max 100).
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"per_page": min(limit, 100)}
    if status:
        params["status"] = status
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/pipelines",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        pipelines = r.json()
        return _json([{
            "id": p["id"],
            "status": p["status"],
            "ref": p["ref"],
            "sha": p["sha"][:8],
            "created_at": p["created_at"],
            "updated_at": p.get("updated_at"),
            "web_url": p["web_url"],
            "source": p.get("source"),
        } for p in pipelines])


@mcp.tool(name="get_pipeline_jobs", annotations={"readOnlyHint": True})
async def get_pipeline_jobs(
    pipeline_id: int,
    project_id: Optional[str] = None,
) -> str:
    """
    Get jobs for a specific pipeline.

    Args:
        pipeline_id: The pipeline ID.
        project_id: Override project (URL-encoded path).
    """
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/pipelines/{pipeline_id}/jobs",
            headers=_auth_headers(),
        )
        r.raise_for_status()
        jobs = r.json()
        return _json([{
            "id": j["id"],
            "name": j["name"],
            "stage": j["stage"],
            "status": j["status"],
            "duration": j.get("duration"),
            "started_at": j.get("started_at"),
            "finished_at": j.get("finished_at"),
            "web_url": j["web_url"],
            "failure_reason": j.get("failure_reason"),
        } for j in jobs])


@mcp.tool(name="get_issues", annotations={"readOnlyHint": True})
async def get_issues(
    state: str = "opened",
    labels: Optional[str] = None,
    limit: int = 20,
    project_id: Optional[str] = None,
) -> str:
    """
    List project issues.

    Args:
        state: Filter by state: opened, closed, all.
        labels: Comma-separated label names to filter by.
        limit: Number of issues to return (max 100).
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"state": state, "per_page": min(limit, 100)}
    if labels:
        params["labels"] = labels
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/issues",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        issues = r.json()
        return _json([{
            "iid": i["iid"],
            "title": i["title"],
            "state": i["state"],
            "author": i["author"]["name"],
            "labels": i.get("labels", []),
            "created_at": i["created_at"],
            "updated_at": i["updated_at"],
            "web_url": i["web_url"],
            "milestone": i.get("milestone", {}).get("title") if i.get("milestone") else None,
        } for i in issues])


@mcp.tool(name="get_branches", annotations={"readOnlyHint": True})
async def get_branches(
    search: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    List repository branches.

    Args:
        search: Search branches by name.
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"per_page": 50}
    if search:
        params["search"] = search
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/branches",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        branches = r.json()
        return _json([{
            "name": b["name"],
            "default": b.get("default", False),
            "merged": b.get("merged", False),
            "protected": b.get("protected", False),
            "last_commit": {
                "short_id": b["commit"]["short_id"],
                "title": b["commit"]["title"],
                "author": b["commit"]["author_name"],
                "created_at": b["commit"]["created_at"],
            },
        } for b in branches])


@mcp.tool(name="get_file_content", annotations={"readOnlyHint": True})
async def get_file_content(
    file_path: str,
    branch: Optional[str] = None,
    project_id: Optional[str] = None,
) -> str:
    """
    Read a file from the repository.

    Args:
        file_path: Path to the file in the repo (e.g. "src/app.py").
        branch: Branch to read from (defaults to project default branch).
        project_id: Override project (URL-encoded path).
    """
    encoded_path = quote(file_path, safe="")
    params: dict[str, Any] = {}
    if branch:
        params["ref"] = branch
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/files/{encoded_path}/raw",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        content = r.text
        # Truncate very large files
        if len(content) > 50000:
            content = content[:50000] + "\n\n... (truncated at 50,000 chars)"
        return content


@mcp.tool(name="get_repository_tree", annotations={"readOnlyHint": True})
async def get_repository_tree(
    path: Optional[str] = None,
    branch: Optional[str] = None,
    recursive: bool = False,
    project_id: Optional[str] = None,
) -> str:
    """
    List files and directories in the repository.

    Args:
        path: Directory path to list (defaults to root).
        branch: Branch to list from (defaults to project default branch).
        recursive: Whether to list recursively.
        project_id: Override project (URL-encoded path).
    """
    params: dict[str, Any] = {"per_page": 100}
    if path:
        params["path"] = path
    if branch:
        params["ref"] = branch
    if recursive:
        params["recursive"] = "true"
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/tree",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        items = r.json()
        return _json([{
            "name": i["name"],
            "path": i["path"],
            "type": i["type"],  # "blob" (file) or "tree" (directory)
        } for i in items])


@mcp.tool(name="compare_branches", annotations={"readOnlyHint": True})
async def compare_branches(
    source: str,
    target: str,
    project_id: Optional[str] = None,
) -> str:
    """
    Compare two branches, showing commits and diff stats.

    Args:
        source: Source branch name.
        target: Target branch name.
        project_id: Override project (URL-encoded path).
    """
    params = {"from": target, "to": source}
    async with httpx.AsyncClient() as client:
        r = await client.get(
            f"{_project_url(project_id)}/repository/compare",
            headers=_auth_headers(),
            params=params,
        )
        r.raise_for_status()
        data = r.json()
        return _json({
            "commits_count": len(data.get("commits", [])),
            "commits": [{
                "short_id": c["short_id"],
                "title": c["title"],
                "author": c["author_name"],
                "created_at": c["created_at"],
            } for c in data.get("commits", [])[:20]],
            "diffs_count": len(data.get("diffs", [])),
            "diffs": [{
                "old_path": d["old_path"],
                "new_path": d["new_path"],
                "new_file": d.get("new_file", False),
                "deleted_file": d.get("deleted_file", False),
            } for d in data.get("diffs", [])[:30]],
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


# --- Auth Middleware ----------------------------------------------------------

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
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


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


async def oauth_authorize(request: Request) -> JSONResponse:
    from starlette.responses import RedirectResponse

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

    return JSONResponse({
        "access_token": MCP_API_KEY,
        "token_type": "Bearer",
    })


# --- App Assembly ------------------------------------------------------------

mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_asgi.lifespan(app):
        logger.info("GitLab MCP server ready on port %d", PORT)
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
