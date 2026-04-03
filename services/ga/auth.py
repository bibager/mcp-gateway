"""OAuth 2.0 endpoints for MCP server authentication.

Implements the MCP authorization spec (2025-03-26):
- /.well-known/oauth-authorization-server (metadata discovery)
- /authorize (redirect to Google OAuth)
- /token (exchange code for access token)
- /register (dynamic client registration)
"""

import base64
import hashlib
import os
import secrets
import time
from urllib.parse import urlencode

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

# In-memory stores (sufficient for single-instance personal server)
_clients = {}       # client_id -> {client_secret, redirect_uris, client_name}
_auth_codes = {}    # code -> {client_id, redirect_uri, code_challenge, email, expires_at}
_tokens = {}        # access_token -> {client_id, email, expires_at}

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"


def _server_url() -> str:
    return os.environ.get("SERVER_URL", "http://localhost:8080").rstrip("/")


def _allowed_email() -> str:
    return os.environ.get("ALLOWED_EMAIL", "")


def _google_client_id() -> str:
    return os.environ["GOOGLE_CLIENT_ID"]


def _google_client_secret() -> str:
    return os.environ["GOOGLE_CLIENT_SECRET"]


async def protected_resource_metadata(request: Request) -> JSONResponse:
    """RFC 9728 OAuth Protected Resource Metadata."""
    base = _server_url()
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
    })


async def oauth_metadata(request: Request) -> JSONResponse:
    """RFC 8414 OAuth Authorization Server Metadata."""
    base = _server_url()
    return JSONResponse({
        "issuer": base,
        "authorization_endpoint": f"{base}/authorize",
        "token_endpoint": f"{base}/token",
        "registration_endpoint": f"{base}/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    })


async def register(request: Request) -> JSONResponse:
    """Dynamic Client Registration (RFC 7591)."""
    body = await request.json()
    client_id = secrets.token_urlsafe(32)
    client_secret = secrets.token_urlsafe(48)
    _clients[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": body.get("redirect_uris", []),
        "client_name": body.get("client_name", "Unknown"),
    }
    return JSONResponse({
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Unknown"),
        "redirect_uris": body.get("redirect_uris", []),
    }, status_code=201)


async def authorize(request: Request) -> Response:
    """Authorization endpoint — redirects to Google OAuth."""
    client_id = request.query_params.get("client_id", "")
    redirect_uri = request.query_params.get("redirect_uri", "")
    state = request.query_params.get("state", "")
    code_challenge = request.query_params.get("code_challenge", "")
    code_challenge_method = request.query_params.get("code_challenge_method", "")

    if client_id not in _clients:
        return JSONResponse({"error": "invalid_client"}, status_code=400)

    # Store the pending auth request keyed by internal state
    internal_state = secrets.token_urlsafe(32)
    _auth_codes[f"pending:{internal_state}"] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method,
    }

    # Redirect to Google OAuth
    params = {
        "client_id": _google_client_id(),
        "redirect_uri": f"{_server_url()}/callback",
        "response_type": "code",
        "scope": "openid email",
        "state": internal_state,
        "access_type": "online",
    }
    url = f"{GOOGLE_AUTH_URL}?{urlencode(params)}"
    return RedirectResponse(url)


async def google_callback(request: Request) -> Response:
    """Handle Google OAuth callback, then redirect back to CoWork."""
    google_code = request.query_params.get("code", "")
    internal_state = request.query_params.get("state", "")

    pending_key = f"pending:{internal_state}"
    if pending_key not in _auth_codes:
        return JSONResponse({"error": "invalid_state"}, status_code=400)

    pending = _auth_codes.pop(pending_key)

    # Exchange Google auth code for tokens
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(GOOGLE_TOKEN_URL, data={
            "code": google_code,
            "client_id": _google_client_id(),
            "client_secret": _google_client_secret(),
            "redirect_uri": f"{_server_url()}/callback",
            "grant_type": "authorization_code",
        })
        if token_resp.status_code != 200:
            return JSONResponse({"error": "google_token_exchange_failed"}, status_code=502)
        google_tokens = token_resp.json()

        # Get user email
        userinfo_resp = await client.get(GOOGLE_USERINFO_URL, headers={
            "Authorization": f"Bearer {google_tokens['access_token']}"
        })
        if userinfo_resp.status_code != 200:
            return JSONResponse({"error": "failed_to_get_userinfo"}, status_code=502)
        userinfo = userinfo_resp.json()

    email = userinfo.get("email", "")
    allowed = _allowed_email()
    if allowed and email.lower() != allowed.lower():
        return JSONResponse(
            {"error": "access_denied", "description": "Email not authorized"},
            status_code=403,
        )

    # Generate our auth code for CoWork
    auth_code = secrets.token_urlsafe(48)
    _auth_codes[auth_code] = {
        "client_id": pending["client_id"],
        "redirect_uri": pending["redirect_uri"],
        "code_challenge": pending["code_challenge"],
        "email": email,
        "expires_at": time.time() + 600,
    }

    # Redirect back to CoWork's callback
    params = {"code": auth_code, "state": pending["state"]}
    return RedirectResponse(f"{pending['redirect_uri']}?{urlencode(params)}")


async def token(request: Request) -> JSONResponse:
    """Token endpoint — exchange auth code for access token."""
    body = await request.form()
    grant_type = body.get("grant_type", "")

    if grant_type == "authorization_code":
        code = body.get("code", "")
        code_verifier = body.get("code_verifier", "")
        client_id = body.get("client_id", "")

        if code not in _auth_codes:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        auth = _auth_codes.pop(code)

        if auth["expires_at"] < time.time():
            return JSONResponse({"error": "invalid_grant", "error_description": "code expired"}, status_code=400)

        if auth["client_id"] != client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)

        # Verify PKCE
        if auth.get("code_challenge"):
            expected = hashlib.sha256(code_verifier.encode()).digest()
            expected_b64 = base64.urlsafe_b64encode(expected).rstrip(b"=").decode()
            if expected_b64 != auth["code_challenge"]:
                return JSONResponse(
                    {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                    status_code=400,
                )

        # Issue MCP_API_KEY as the access token — never expires.
        # Google API auth is handled separately via ADC credentials.
        mcp_api_key = os.environ.get("MCP_API_KEY", "")
        access_token = mcp_api_key if mcp_api_key else secrets.token_urlsafe(48)
        refresh_token = secrets.token_urlsafe(48)

        _tokens[access_token] = {
            "client_id": client_id,
            "email": auth["email"],
            "expires_at": time.time() + 86400 * 365 * 10,  # 10 years
            "refresh_token": refresh_token,
        }
        _tokens[f"refresh:{refresh_token}"] = {
            "client_id": client_id,
            "email": auth["email"],
        }

        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer",
        })

    elif grant_type == "refresh_token":
        refresh_token = body.get("refresh_token", "")
        refresh_key = f"refresh:{refresh_token}"
        if refresh_key not in _tokens:
            return JSONResponse({"error": "invalid_grant"}, status_code=400)

        refresh_data = _tokens[refresh_key]
        mcp_api_key = os.environ.get("MCP_API_KEY", "")
        access_token = mcp_api_key if mcp_api_key else secrets.token_urlsafe(48)

        _tokens[access_token] = {
            "client_id": refresh_data["client_id"],
            "email": refresh_data["email"],
            "expires_at": time.time() + 86400 * 365 * 10,
            "refresh_token": refresh_token,
        }

        return JSONResponse({
            "access_token": access_token,
            "token_type": "bearer",
        })

    return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)


def validate_token(authorization: str) -> dict | None:
    """Validate a Bearer token. Returns token data or None."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token_str = authorization[7:]
    # Always accept MCP_API_KEY — survives restarts without re-auth
    mcp_api_key = os.environ.get("MCP_API_KEY", "")
    if mcp_api_key and token_str == mcp_api_key:
        return {"client_id": "mcp", "email": "mcp_api_key"}
    token_data = _tokens.get(token_str)
    if not token_data:
        return None
    if token_data.get("expires_at", 0) < time.time():
        del _tokens[token_str]
        return None
    return token_data


# Starlette routes for the auth endpoints
auth_routes = [
    Route("/.well-known/oauth-protected-resource", protected_resource_metadata, methods=["GET"]),
    Route("/.well-known/oauth-authorization-server", oauth_metadata, methods=["GET"]),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize, methods=["GET"]),
    Route("/callback", google_callback, methods=["GET"]),
    Route("/token", token, methods=["POST"]),
]
