"""
Datarova MCP Server
===================
Wraps Datarova's internal API (https://api.datarova.com) for Amazon brand
keyword rank tracking and ASIN insights. Datarova has no public API — we
reverse-engineered the endpoints via the Chrome extension at
`tools/datarova-api-recon/`.

Auth model
----------
AWS Cognito JWT (us-west-2 user pool ``us-west-2_0CKqzWHDX``).
Three headers are sent on every authenticated request:

  Authorization: Bearer <access_token>   # Cognito access JWT, 1-hour TTL
  x-id-token: <id_token>                 # Cognito ID JWT, 1-hour TTL
  x-plan: <plan_token>                   # HS256 plan JWT, 1-hour TTL (optional)

We hold the long-lived Cognito refresh token in env (~30 day lifespan) and
mint fresh access/id tokens on demand by calling
``cognito-idp.us-west-2.amazonaws.com`` with the REFRESH_TOKEN_AUTH flow.
The token cache keys off the ``exp`` claim of the access JWT, with a 60-second
safety margin to avoid mid-flight expiry.

The ``x-plan`` header is optional — we try requests without it first; if
Datarova rejects, we fall back to the captured static token (which expires
hourly, so it's best-effort).

Env vars
--------
  MCP_API_KEY                  required; protects all /mcp endpoints
  DATAROVA_REFRESH_TOKEN       required; Cognito refresh token
  DATAROVA_COGNITO_CLIENT_ID   optional; defaults to ``5dcti49pggi19uqs3df8iae3o1``
  DATAROVA_X_PLAN              optional; static x-plan JWT fallback
  PORT                         optional, defaults to 8013
  SERVER_URL                   optional; overrides base URL detection
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
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
logger = logging.getLogger("datarova_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
DATAROVA_REFRESH_TOKEN: str = os.environ["DATAROVA_REFRESH_TOKEN"]
DATAROVA_COGNITO_CLIENT_ID: str = os.getenv(
    "DATAROVA_COGNITO_CLIENT_ID", "5dcti49pggi19uqs3df8iae3o1"
)
DATAROVA_X_PLAN: str = os.getenv("DATAROVA_X_PLAN", "").strip()
PORT: int = int(os.getenv("PORT", "8013"))

COGNITO_URL = "https://cognito-idp.us-west-2.amazonaws.com/"
DATAROVA_API = "https://api.datarova.com"

# Spoof a real Chrome to match what Datarova's frontend sends.
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


# --- Token cache -------------------------------------------------------------

_token_cache: dict[str, Any] = {"access": None, "id": None, "expires_at": 0.0}
_token_lock = asyncio.Lock()


def _decode_jwt_payload(jwt: str) -> dict[str, Any]:
    """Base64-decode the JWT payload (middle segment). No signature check."""
    payload_b64 = jwt.split(".")[1]
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))


async def _refresh_cognito_tokens() -> tuple[str, str]:
    """Call Cognito IdP InitiateAuth with REFRESH_TOKEN_AUTH and return (access, id)."""
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            COGNITO_URL,
            headers={
                "Content-Type": "application/x-amz-json-1.1",
                "X-Amz-Target": "AWSCognitoIdentityProviderService.InitiateAuth",
            },
            json={
                "AuthFlow": "REFRESH_TOKEN_AUTH",
                "ClientId": DATAROVA_COGNITO_CLIENT_ID,
                "AuthParameters": {"REFRESH_TOKEN": DATAROVA_REFRESH_TOKEN},
            },
        )
        if r.status_code != 200:
            raise RuntimeError(
                f"Cognito refresh failed ({r.status_code}): {r.text[:500]}"
            )
        result = r.json()["AuthenticationResult"]
        return result["AccessToken"], result["IdToken"]


async def _get_fresh_tokens() -> tuple[str, str]:
    """Return cached (access, id) tokens, refreshing from Cognito if near expiry."""
    now = time.time()
    if _token_cache["expires_at"] > now + 60 and _token_cache["access"]:
        return _token_cache["access"], _token_cache["id"]

    async with _token_lock:
        # Double-check under lock (another caller may have refreshed)
        now = time.time()
        if _token_cache["expires_at"] > now + 60 and _token_cache["access"]:
            return _token_cache["access"], _token_cache["id"]

        access, id_token = await _refresh_cognito_tokens()
        try:
            exp = _decode_jwt_payload(access).get("exp", now + 3600)
        except Exception:
            exp = now + 3600
        _token_cache.update({"access": access, "id": id_token, "expires_at": float(exp)})
        logger.info("Refreshed Cognito tokens, exp=%s", time.ctime(exp))
        return access, id_token


# --- Datarova API client -----------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


async def _datarova_request(
    method: str,
    path: str,
    *,
    params: Optional[dict[str, Any]] = None,
    form: Optional[dict[str, Any]] = None,
    json_body: Optional[dict[str, Any]] = None,
) -> Any:
    """Make an authenticated request to api.datarova.com, with one retry on 401.

    `path` is the path segment (e.g. ``/users/details``); we prepend the host.
    """
    access, id_token = await _get_fresh_tokens()

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {access}",
        "x-id-token": id_token,
        "x-parent-user-email": "",
        "x-parent-user-id": "",
        "User-Agent": _BROWSER_UA,
        "Origin": "https://app.datarova.com",
        "Referer": "https://app.datarova.com/",
    }
    if DATAROVA_X_PLAN:
        headers["x-plan"] = DATAROVA_X_PLAN

    kwargs: dict[str, Any] = {"headers": headers}
    if params is not None:
        kwargs["params"] = params
    if form is not None:
        kwargs["data"] = form
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif json_body is not None:
        kwargs["json"] = json_body
        headers["Content-Type"] = "application/json"

    url = f"{DATAROVA_API}{path}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.request(method, url, **kwargs)

        # If the access token was revoked mid-flight, force-refresh and retry once.
        if r.status_code == 401:
            logger.warning("401 from %s — invalidating token cache and retrying", path)
            _token_cache["expires_at"] = 0.0
            access, id_token = await _get_fresh_tokens()
            headers["Authorization"] = f"Bearer {access}"
            headers["x-id-token"] = id_token
            r = await client.request(method, url, **kwargs)

        if r.status_code >= 400:
            raise RuntimeError(
                f"Datarova API {method} {path} failed ({r.status_code}): {r.text[:1000]}"
            )

        if not r.content:
            return None
        try:
            return r.json()
        except Exception:
            return {"raw": r.text}


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "datarova_mcp",
    instructions=(
        "Tools for Amazon brand keyword rank tracking via Datarova "
        "(app.datarova.com). Covers the Rank Tracker (your tracked "
        "projects/ASINs/keywords with daily rank history) and partial "
        "Keyword Spy / ASIN Insights via the keyword-market records "
        "endpoint. Dates are YYYY-MM-DD. Default marketplace is 'US' "
        "(amazon.com)."
    ),
)


# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(
    name="get_account_summary",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_account_summary() -> str:
    """
    Get your Datarova account: plan, limits, current usage.

    Useful for: checking how many projects/keywords/ASINs you have tracked
    versus your plan limit, and confirming whether the gateway's auth is
    working.
    """
    data = await _datarova_request("GET", "/users/details")
    return _json(data)


@mcp.tool(
    name="get_asin_details",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_asin_details(asin: str, marketplace: str = "US") -> str:
    """
    Get product info (title, price, image, brand) for any Amazon ASIN.

    Useful for enriching results from the rank-tracker and market-data tools —
    pair this with ``get_keyword_market_data(..., top_asin=X)`` to know what
    product X actually is. Works on ANY ASIN, not just ones you track.

    Args:
        asin: Amazon ASIN (e.g. 'B072276HMC').
        marketplace: Marketplace code, default 'US'.
    """
    data = await _datarova_request(
        "POST", "/asin-price-detail", form={"ASIN": asin, "marketplace": marketplace}
    )
    return _json(data)


@mcp.tool(
    name="get_latest_data_date",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_latest_data_date(marketplace: str = "US") -> str:
    """
    Get the latest date for which rank data is available in a marketplace.

    Args:
        marketplace: Marketplace code, default 'US' (amazon.com). Other examples:
            'UK', 'DE', 'CA', 'IT', 'ES', 'FR', 'JP', 'AU'.
    """
    data = await _datarova_request("GET", "/latest-date", params={"marketplace": marketplace})
    return _json(data)


@mcp.tool(
    name="list_projects",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def list_projects(search: Optional[str] = None) -> str:
    """
    List your Datarova rank tracker projects.

    Args:
        search: Optional case-insensitive name filter.
    """
    data = await _datarova_request("POST", "/projects", form={"search": search or ""})
    return _json(data)


@mcp.tool(
    name="list_keywords",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def list_keywords(marketplace: str = "US") -> str:
    """
    List every keyword tracked across all your projects in a marketplace.

    Args:
        marketplace: Marketplace code, default 'US'.
    """
    data = await _datarova_request(
        "GET", "/projects/keywords/marketplace", params={"marketplace": marketplace}
    )
    return _json(data)


@mcp.tool(
    name="list_asins",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def list_asins(marketplace: str = "US") -> str:
    """
    List every ASIN tracked across all your projects in a marketplace.

    Args:
        marketplace: Marketplace code, default 'US'.
    """
    data = await _datarova_request(
        "GET", "/projects/asins/marketplace", params={"marketplace": marketplace}
    )
    return _json(data)


@mcp.tool(
    name="get_keyword_tags",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_keyword_tags(project_id: int) -> str:
    """
    Get the keyword tags configured on a project.

    Args:
        project_id: Numeric project ID (from ``list_projects``).
    """
    data = await _datarova_request(
        "GET", "/keyword-tags", params={"project_id": project_id}
    )
    return _json(data)


@mcp.tool(
    name="get_rank_history_by_asin",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_rank_history_by_asin(
    project_id: int,
    asin: str,
    range_type: str = "daily",
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """
    Get rank history for ALL tracked keywords on a single ASIN inside a project.

    This is the main "how is my product ranking" tool — returns each tracked
    keyword's organic rank time series for the ASIN over the date range.

    Args:
        project_id: Numeric project ID.
        asin: Amazon ASIN (e.g. 'B09XJDHFG6').
        range_type: One of 'daily', 'weekly', 'monthly'. Default 'daily'.
        from_date: Start date YYYY-MM-DD. Defaults to ~30 days ago server-side.
        to_date:   End date YYYY-MM-DD. Defaults to latest available.
    """
    params: dict[str, Any] = {"asin": asin, "rangeType": range_type}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    data = await _datarova_request(
        "GET", f"/projects/{project_id}/ranks/by-asin", params=params
    )
    return _json(data)


@mcp.tool(
    name="get_keyword_rank_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_keyword_rank_history(
    project_id: int,
    asin: str,
    keyword: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """
    Get organic rank time series for ONE keyword on ONE ASIN.

    Use this when you want a deep dive on a specific keyword's trajectory.

    Args:
        project_id: Numeric project ID.
        asin: Amazon ASIN.
        keyword: The keyword phrase, e.g. 'manuka honey'.
        from_date: Start date YYYY-MM-DD.
        to_date:   End date YYYY-MM-DD.
    """
    params: dict[str, Any] = {"ASIN": asin, "keyword": keyword}
    if from_date:
        params["from"] = from_date
    if to_date:
        params["to"] = to_date
    data = await _datarova_request(
        "GET", f"/projects/{project_id}/ranks/item", params=params
    )
    return _json(data)


@mcp.tool(
    name="get_keyword_market_data",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_keyword_market_data(
    keyword: str,
    from_date: str,
    to_date: str,
    asin: Optional[str] = None,
    top_asin: Optional[str] = None,
    range_type: str = "monthly",
    exact_asin_only: bool = False,
    marketplace: str = "US",
) -> str:
    """
    Get market-level data for a keyword: search volume, sales, top ranker.

    This is Datarova's Keyword Spy / ASIN Insights cross-section. If both
    ``asin`` (yours) and ``top_asin`` (current #1) are given, the response
    includes a head-to-head comparison.

    Args:
        keyword: Keyword phrase, e.g. 'manuka doctor'.
        from_date: Start date YYYY-MM-DD.
        to_date:   End date YYYY-MM-DD.
        asin: Optional — your ASIN to overlay onto market data.
        top_asin: Optional — current top-ranking ASIN to compare against.
        range_type: 'daily', 'weekly', or 'monthly'. Default 'monthly'.
        exact_asin_only: If True, restrict to exact ASIN match. Default False.
        marketplace: Marketplace code, default 'US'.
    """
    params: dict[str, Any] = {
        "keyword": keyword,
        "startDate": from_date,
        "endDate": to_date,
        "rangeType": range_type,
        "exactAsinOnly": "true" if exact_asin_only else "false",
        "marketplace": marketplace,
    }
    if asin:
        params["ASIN"] = asin
    if top_asin:
        params["topAsin"] = top_asin
    data = await _datarova_request("GET", "/records/record", params=params)
    return _json(data)


@mcp.tool(
    name="add_keyword_to_project",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False},
)
async def add_keyword_to_project(
    project_id: int,
    asin: str,
    keyword: str,
    marketplace: str = "US",
) -> str:
    """
    Add a keyword to a project's rank tracker.

    Counts against your plan's ``keywordsLimit`` (use ``get_account_summary``
    to check usage). Returns the API response verbatim — may contain an error
    if the limit is hit or the keyword already exists.

    Args:
        project_id: Numeric project ID.
        asin: Amazon ASIN to associate the keyword with.
        keyword: Keyword phrase to track.
        marketplace: Marketplace code, default 'US'.
    """
    data = await _datarova_request(
        "POST",
        "/projects/add-keyword",
        form={
            "project_id": project_id,
            "asin": asin,
            "keyword": keyword,
            "marketplace": marketplace,
        },
    )
    return _json(data)


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
        computed = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
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
        logger.info("Datarova MCP server ready on port %d", PORT)
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
