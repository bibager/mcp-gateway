"""
Weather MCP Server
==================
Dual-protocol server:
  - /mcp   FastMCP streamable-HTTP (for Claude CoWork / Claude Desktop)
  - /health Unauthenticated health check

Auth: Authorization: Bearer {MCP_API_KEY} on every route except public paths.

The MCP OAuth flow is synthetic: it auto-approves and issues MCP_API_KEY as the
access_token so Claude CoWork's stored token never expires.

Data source: Open-Meteo (https://open-meteo.com) — free, no API key required.
  - Geocoding: geocoding-api.open-meteo.com
  - Forecast:  api.open-meteo.com

Env vars:
  MCP_API_KEY  required; protects all /mcp endpoints
  PORT         optional, defaults to 8004
  SERVER_URL   optional; overrides base URL detection
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
logger = logging.getLogger("weather_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
PORT: int = int(os.getenv("PORT", "8004"))

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# --- Helpers -----------------------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


async def _geocode(client: httpx.AsyncClient, location: str) -> dict[str, Any]:
    """Resolve a location string to coordinates + timezone via Open-Meteo geocoder.

    Open-Meteo's geocoder takes a single name (not "City, State"), so we split
    on commas and use the trailing parts as a state/country disambiguator.
    """
    parts = [p.strip() for p in location.split(",") if p.strip()]
    name = parts[0] if parts else location
    qualifiers = [p.lower() for p in parts[1:]]

    r = await client.get(GEOCODE_URL, params={"name": name, "count": 10, "language": "en"})
    r.raise_for_status()
    results = r.json().get("results") or []
    if not results:
        raise ValueError(f"No location match for {location!r}")

    if qualifiers:
        for hit in results:
            haystack = " ".join(
                str(hit.get(k, "")).lower()
                for k in ("admin1", "admin2", "country", "country_code")
            )
            if all(q in haystack for q in qualifiers):
                chosen = hit
                break
        else:
            chosen = results[0]
    else:
        chosen = results[0]

    return {
        "name": chosen.get("name"),
        "admin1": chosen.get("admin1"),
        "country": chosen.get("country"),
        "latitude": chosen["latitude"],
        "longitude": chosen["longitude"],
        "timezone": chosen.get("timezone", "auto"),
    }


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "weather_mcp",
    instructions=(
        "Tools for looking up weather forecasts and daily summaries by location. "
        "Powered by Open-Meteo (free, no API key). "
        "Pass locations as 'City' or 'City, State' (e.g. 'Norman, Oklahoma'). "
        "Default temperature unit is Fahrenheit; pass units='celsius' to switch."
    ),
)

# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(
    name="get_daily_summary",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_daily_summary(
    location: str,
    date: Optional[str] = None,
    units: str = "fahrenheit",
) -> str:
    """
    Get a one-day weather summary for a location.

    Returns sunrise, sunset, daily high/low, hourly temperatures, and current
    conditions — everything you need for a morning briefing.

    Args:
        location: City name or "City, State" / "City, Country" (e.g. "Norman, Oklahoma").
        date: Target date in YYYY-MM-DD (in the location's local timezone). Defaults to today.
        units: Temperature unit — "fahrenheit" (default) or "celsius".
    """
    if units not in ("fahrenheit", "celsius"):
        raise ValueError("units must be 'fahrenheit' or 'celsius'")

    async with httpx.AsyncClient(timeout=15.0) as client:
        loc = await _geocode(client, location)

        params: dict[str, Any] = {
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum,weather_code",
            "hourly": "temperature_2m,precipitation_probability,weather_code",
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "timezone": "auto",
            "temperature_unit": units,
            "wind_speed_unit": "mph" if units == "fahrenheit" else "kmh",
            "precipitation_unit": "inch" if units == "fahrenheit" else "mm",
            "forecast_days": 1,
        }
        if date:
            params["start_date"] = date
            params["end_date"] = date
            params.pop("forecast_days", None)

        r = await client.get(FORECAST_URL, params=params)
        r.raise_for_status()
        data = r.json()

    daily = data.get("daily", {})
    hourly = data.get("hourly", {})
    current = data.get("current", {})

    times = hourly.get("time", [])
    temps = hourly.get("temperature_2m", [])
    precip = hourly.get("precipitation_probability", [])
    hourly_rows = [
        {
            "time": t,
            "temperature": temps[i] if i < len(temps) else None,
            "precip_probability_pct": precip[i] if i < len(precip) else None,
        }
        for i, t in enumerate(times)
    ]

    return _json({
        "location": {
            "query": location,
            "resolved": f"{loc['name']}, {loc.get('admin1') or ''}, {loc.get('country') or ''}".strip(", "),
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "timezone": data.get("timezone"),
        },
        "units": {
            "temperature": data.get("daily_units", {}).get("temperature_2m_max"),
            "precipitation": data.get("daily_units", {}).get("precipitation_sum"),
            "wind_speed": data.get("current_units", {}).get("wind_speed_10m"),
        },
        "date": (daily.get("time") or [None])[0],
        "sunrise": (daily.get("sunrise") or [None])[0],
        "sunset": (daily.get("sunset") or [None])[0],
        "high": (daily.get("temperature_2m_max") or [None])[0],
        "low": (daily.get("temperature_2m_min") or [None])[0],
        "precipitation_total": (daily.get("precipitation_sum") or [None])[0],
        "current": {
            "time": current.get("time"),
            "temperature": current.get("temperature_2m"),
            "wind_speed": current.get("wind_speed_10m"),
        },
        "hourly": hourly_rows,
    })


@mcp.tool(
    name="get_forecast",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_forecast(
    location: str,
    days: int = 7,
    units: str = "fahrenheit",
) -> str:
    """
    Get a multi-day forecast for a location.

    Args:
        location: City name or "City, State" (e.g. "Norman, Oklahoma").
        days: Number of forecast days (1-16). Default 7.
        units: Temperature unit — "fahrenheit" (default) or "celsius".
    """
    if units not in ("fahrenheit", "celsius"):
        raise ValueError("units must be 'fahrenheit' or 'celsius'")
    days = max(1, min(int(days), 16))

    async with httpx.AsyncClient(timeout=15.0) as client:
        loc = await _geocode(client, location)
        r = await client.get(FORECAST_URL, params={
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "daily": "sunrise,sunset,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max,weather_code",
            "timezone": "auto",
            "temperature_unit": units,
            "precipitation_unit": "inch" if units == "fahrenheit" else "mm",
            "forecast_days": days,
        })
        r.raise_for_status()
        data = r.json()

    daily = data.get("daily", {})
    times = daily.get("time", [])
    rows = [
        {
            "date": times[i],
            "sunrise": (daily.get("sunrise") or [None] * len(times))[i],
            "sunset": (daily.get("sunset") or [None] * len(times))[i],
            "high": (daily.get("temperature_2m_max") or [None] * len(times))[i],
            "low": (daily.get("temperature_2m_min") or [None] * len(times))[i],
            "precipitation_total": (daily.get("precipitation_sum") or [None] * len(times))[i],
            "precipitation_probability_max_pct": (daily.get("precipitation_probability_max") or [None] * len(times))[i],
            "weather_code": (daily.get("weather_code") or [None] * len(times))[i],
        }
        for i in range(len(times))
    ]

    return _json({
        "location": {
            "query": location,
            "resolved": f"{loc['name']}, {loc.get('admin1') or ''}, {loc.get('country') or ''}".strip(", "),
            "latitude": loc["latitude"],
            "longitude": loc["longitude"],
            "timezone": data.get("timezone"),
        },
        "units": {
            "temperature": data.get("daily_units", {}).get("temperature_2m_max"),
            "precipitation": data.get("daily_units", {}).get("precipitation_sum"),
        },
        "days": rows,
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
        logger.info("Weather MCP server ready on port %d", PORT)
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
