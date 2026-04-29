"""
TrackIQ MCP Proxy
=================
Wraps the upstream TrackIQ MCP server (https://app.trackiq.com/mcp) with the
gateway's standard auth model so Claude CoWork can use it as a connector.

Flow:
  CoWork  --Bearer MCP_API_KEY-->  trackiq.bibager.com/mcp
  gateway --Bearer TRACKIQ_API_KEY--> app.trackiq.com/mcp

The MCP OAuth flow is synthetic: it auto-approves and issues MCP_API_KEY as the
access_token so Claude CoWork's stored token never expires.

Tools are not reimplemented — FastMCP.as_proxy forwards every call upstream
with the TrackIQ Bearer token injected. The 16 TrackIQ tools (Amazon ads,
DSP, sales, inventory, search query performance) are exposed as-is.

Env vars:
  MCP_API_KEY       required; protects the gateway side
  TRACKIQ_API_KEY   required; upstream TrackIQ Bearer token (tiq_live_...)
  TRACKIQ_UPSTREAM  optional; defaults to https://app.trackiq.com/mcp
  PORT              optional, defaults to 8005
  SERVER_URL        optional; overrides base URL detection
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("trackiq_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
TRACKIQ_API_KEY: str = os.environ["TRACKIQ_API_KEY"]
TRACKIQ_UPSTREAM: str = os.getenv("TRACKIQ_UPSTREAM", "https://app.trackiq.com/mcp")
PORT: int = int(os.getenv("PORT", "8005"))

# --- FastMCP proxy -----------------------------------------------------------

# Build a Client pointed at upstream TrackIQ with the API key as a Bearer header.
# FastMCP.as_proxy creates a server that forwards every list/call to that client.
_upstream_transport = StreamableHttpTransport(
    url=TRACKIQ_UPSTREAM,
    headers={"Authorization": f"Bearer {TRACKIQ_API_KEY}"},
)
_upstream = Client(transport=_upstream_transport)

mcp = FastMCP.as_proxy(
    _upstream,
    name="trackiq_proxy",
    instructions=(
        "Amazon ads & marketplace analytics for the Manuka Doctor brand via TrackIQ. "
        "Call list_marketplaces first to enumerate connected marketplaces and grab "
        "their account_id values, then pass those into the other tools."
    ),
)

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
        logger.info("TrackIQ MCP proxy ready on port %d (upstream: %s)", PORT, TRACKIQ_UPSTREAM)
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
