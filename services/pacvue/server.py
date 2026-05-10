"""
Pacvue MCP Proxy
================
Transparent HTTP-level streaming proxy for the upstream Pacvue MCP server
(https://mcp.pacvue.com/mcp). The MCP session lives directly between Claude
CoWork and Pacvue — we just rewrite the Authorization header.

Flow:
  CoWork  --Bearer MCP_API_KEY-->  pacvue.bibager.com/mcp
  gateway --pv_<token>          --> mcp.pacvue.com/mcp

NOTE: Pacvue's auth header is a RAW token (no `Bearer ` prefix) — different
from TrackIQ which uses `Bearer tiq_...`. Spec from Pacvue-MCP README:
    Authorization: pv_<your-api-token>

The MCP OAuth flow at the gateway is synthetic: it auto-approves and issues
MCP_API_KEY as the access_token so Claude CoWork's stored token never expires.

Env vars:
  MCP_API_KEY      required; protects the gateway side
  PACVUE_API_KEY   required; upstream Pacvue token (pv_...)
  PACVUE_UPSTREAM  optional; defaults to https://mcp.pacvue.com/mcp
  PORT             optional, defaults to 8008
  SERVER_URL       optional; overrides base URL detection
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
from base64 import urlsafe_b64encode
from typing import Any
from urllib.parse import urlencode

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, StreamingResponse
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pacvue_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
PACVUE_API_KEY: str = os.environ["PACVUE_API_KEY"]
PACVUE_UPSTREAM: str = os.getenv("PACVUE_UPSTREAM", "https://mcp.pacvue.com/mcp")
PORT: int = int(os.getenv("PORT", "8008"))

# Hop-by-hop headers (RFC 7230 §6.1) — never forward these.
_HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}
# Headers we strip from the inbound request (we set fresh values or skip them).
_REQ_STRIP = _HOP_BY_HOP | {
    "host", "authorization", "content-length",
    "x-forwarded-for", "x-forwarded-proto", "x-forwarded-host",
    "via", "cdn-loop", "do-connecting-ip", "x-cloud-trace-context",
}
# Headers we strip from the upstream response before relaying to CoWork.
_RESP_STRIP = _HOP_BY_HOP | {"content-length"}


# --- /mcp passthrough --------------------------------------------------------


async def proxy_mcp(request: Request) -> StreamingResponse:
    """Stream-forward any /mcp request to Pacvue with our raw pv_ token."""
    upstream_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in _REQ_STRIP
    }
    # Pacvue spec: raw token, NO 'Bearer ' prefix.
    upstream_headers["Authorization"] = PACVUE_API_KEY

    body = await request.body()

    # No total timeout — MCP streamable HTTP can hold the connection open.
    client = httpx.AsyncClient(timeout=httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0))
    upstream_req = client.build_request(
        request.method,
        PACVUE_UPSTREAM,
        headers=upstream_headers,
        params=dict(request.query_params),
        content=body,
    )
    try:
        upstream_resp = await client.send(upstream_req, stream=True)
    except Exception as exc:
        await client.aclose()
        logger.error("Upstream connect failed: %s", exc)
        return JSONResponse({"error": "upstream_unavailable", "detail": str(exc)}, status_code=502)

    resp_headers = {
        k: v for k, v in upstream_resp.headers.items()
        if k.lower() not in _RESP_STRIP
    }

    async def body_iter():
        try:
            async for chunk in upstream_resp.aiter_raw():
                yield chunk
        finally:
            await upstream_resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=upstream_resp.status_code,
        headers=resp_headers,
        media_type=upstream_resp.headers.get("content-type"),
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


# --- Auth Middleware ---------------------------------------------------------

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


app = Starlette(
    routes=[
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", endpoint=oauth_metadata, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", endpoint=oauth_protected_resource, methods=["GET"]),
        Route("/authorize", endpoint=oauth_authorize, methods=["GET"]),
        Route("/token", endpoint=oauth_token, methods=["POST"]),
        Route("/register", endpoint=oauth_register, methods=["POST"]),
        # /mcp catch-all proxy — accepts POST (RPC), GET (SSE long-poll), DELETE (session close)
        Route("/mcp", endpoint=proxy_mcp, methods=["GET", "POST", "DELETE", "OPTIONS"]),
    ],
)

app.add_middleware(APIKeyMiddleware)

if __name__ == "__main__":
    logger.info("Pacvue MCP HTTP proxy starting on port %d (upstream: %s)", PORT, PACVUE_UPSTREAM)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
