"""
Technical Analysis MCP Server
=============================
Computes pivot points, session VWAP, anchored VWAP, and volume profile
locally from Alpaca's bar/trade data — same numbers TradingView's
Premium indicators produce, no licensing required.

Data source: Alpaca Markets via `alpaca-py`. Reuses the same
ALPACA_API_KEY / ALPACA_SECRET_KEY env vars as the alpaca proxy
service (DO env vars are app-scoped, not program-scoped).

Tools:
  - get_pivot_points      Standard / Camarilla / Woodie pivots from prior session H/L/C
  - get_session_vwap      VWAP for a given session (regular or extended)
  - get_anchored_vwap     Cumulative VWAP from a specific anchor bar to now
  - get_volume_profile    POC, VAH, VAL + price-volume histogram for a session

Env vars:
  MCP_API_KEY        required; protects all /mcp endpoints
  ALPACA_API_KEY     required; live or paper
  ALPACA_SECRET_KEY  required
  PORT               optional, defaults to 8011
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
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import uvicorn
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("ta_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
ALPACA_API_KEY: str = os.environ["ALPACA_API_KEY"]
ALPACA_SECRET_KEY: str = os.environ["ALPACA_SECRET_KEY"]
PORT: int = int(os.getenv("PORT", "8011"))

ET = ZoneInfo("America/New_York")
REGULAR_OPEN = dtime(9, 30)   # 9:30 ET
REGULAR_CLOSE = dtime(16, 0)  # 16:00 ET
EXTENDED_OPEN = dtime(4, 0)   # 04:00 ET (pre-market)
EXTENDED_CLOSE = dtime(20, 0)  # 20:00 ET (after-hours)

# --- Helpers -----------------------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


_alpaca_client: Optional[StockHistoricalDataClient] = None


def _client() -> StockHistoricalDataClient:
    global _alpaca_client
    if _alpaca_client is None:
        _alpaca_client = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
    return _alpaca_client


def _resolve_session_date(session_date: Optional[str]) -> date:
    """Default to most recent weekday (US market) if not specified."""
    if session_date:
        return date.fromisoformat(session_date)
    # Most recent weekday in ET
    now_et = datetime.now(ET)
    d = now_et.date()
    # If currently before market open, use yesterday
    if now_et.time() < REGULAR_OPEN:
        d = d - timedelta(days=1)
    while d.weekday() >= 5:  # Saturday=5, Sunday=6
        d = d - timedelta(days=1)
    return d


def _session_window(session_date: date, session: str) -> tuple[datetime, datetime]:
    """Return (start_utc, end_utc) for the given session date in the requested session type."""
    if session == "regular":
        start_et = datetime.combine(session_date, REGULAR_OPEN, tzinfo=ET)
        end_et = datetime.combine(session_date, REGULAR_CLOSE, tzinfo=ET)
    elif session == "extended":
        start_et = datetime.combine(session_date, EXTENDED_OPEN, tzinfo=ET)
        end_et = datetime.combine(session_date, EXTENDED_CLOSE, tzinfo=ET)
    else:
        raise ValueError("session must be 'regular' or 'extended'")
    return start_et.astimezone(timezone.utc), end_et.astimezone(timezone.utc)


def _prev_trading_day(d: date) -> date:
    """Walk back one calendar day, then back further over weekends.

    NOTE: doesn't account for US market holidays — caller is on the hook
    to pass an explicit session_date if precision matters around holidays.
    """
    d = d - timedelta(days=1)
    while d.weekday() >= 5:
        d = d - timedelta(days=1)
    return d


def _fetch_bars(
    symbol: str,
    timeframe: TimeFrame,
    start: datetime,
    end: datetime,
    feed: str = "iex",
) -> list[dict[str, Any]]:
    """Fetch bars and return a list of dicts (sorted ascending by time)."""
    req = StockBarsRequest(
        symbol_or_symbols=symbol,
        timeframe=timeframe,
        start=start,
        end=end,
        feed=feed,  # "iex" works on free tier; user's account may have "sip"
    )
    bars = _client().get_stock_bars(req)
    raw = bars.data.get(symbol, [])
    out: list[dict[str, Any]] = []
    for bar in raw:
        out.append({
            "t": bar.timestamp,
            "o": float(bar.open),
            "h": float(bar.high),
            "l": float(bar.low),
            "c": float(bar.close),
            "v": float(bar.volume),
            "n": int(getattr(bar, "trade_count", 0) or 0),
            "vw": float(getattr(bar, "vwap", 0.0) or 0.0),
        })
    out.sort(key=lambda b: b["t"])
    return out


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "ta_mcp",
    instructions=(
        "Technical analysis tools computed locally from Alpaca's market data. "
        "Pivot Points (Standard/Camarilla/Woodie), Session VWAP, Anchored VWAP, "
        "and Volume Profile (POC, VAH, VAL) for any US-equity symbol. "
        "Same math as TradingView's Premium indicators — no scraping, no UI dependency. "
        "All times in ISO 8601; session_date in YYYY-MM-DD; defaults to most recent "
        "trading day (US weekday) if omitted."
    ),
)

# --- Tool: Pivot Points ------------------------------------------------------


@mcp.tool(
    name="get_pivot_points",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_pivot_points(
    symbol: str,
    type: str = "standard",
    session_date: Optional[str] = None,
) -> str:
    """
    Compute pivot points from the prior trading session's H/L/C.

    Returns the central pivot (P) plus support (S1-S4) and resistance (R1-R4)
    levels. Uses regular-session daily bars from Alpaca.

    Args:
        symbol: Ticker (e.g. "AAPL", "OKLO").
        type: One of "standard" (default), "camarilla", "woodie".
        session_date: Date pivots ARE FOR (today). Pivots are computed from
                     PRIOR session's bars. YYYY-MM-DD. Defaults to most recent weekday.
    """
    if type not in ("standard", "camarilla", "woodie"):
        raise ValueError("type must be 'standard', 'camarilla', or 'woodie'")

    target_date = _resolve_session_date(session_date)
    prior = _prev_trading_day(target_date)

    # Fetch the prior day's regular-session bar via daily timeframe + slice.
    # Daily bars in Alpaca aggregate the regular session.
    start_utc = datetime.combine(prior, dtime(0, 0), tzinfo=ET).astimezone(timezone.utc)
    end_utc = datetime.combine(target_date, dtime(0, 0), tzinfo=ET).astimezone(timezone.utc)

    bars = _fetch_bars(symbol, TimeFrame.Day, start_utc, end_utc)
    if not bars:
        return _json({"error": f"No bars for {symbol} on {prior.isoformat()}"})

    bar = bars[-1]
    H, L, C = bar["h"], bar["l"], bar["c"]
    rng = H - L

    if type == "standard":
        P = (H + L + C) / 3
        levels = {
            "P": P,
            "R1": 2 * P - L,
            "S1": 2 * P - H,
            "R2": P + rng,
            "S2": P - rng,
            "R3": H + 2 * (P - L),
            "S3": L - 2 * (H - P),
        }
    elif type == "camarilla":
        levels = {
            "R1": C + rng * 1.1 / 12,
            "R2": C + rng * 1.1 / 6,
            "R3": C + rng * 1.1 / 4,
            "R4": C + rng * 1.1 / 2,
            "S1": C - rng * 1.1 / 12,
            "S2": C - rng * 1.1 / 6,
            "S3": C - rng * 1.1 / 4,
            "S4": C - rng * 1.1 / 2,
        }
    else:  # woodie
        P = (H + L + 2 * C) / 4
        levels = {
            "P": P,
            "R1": 2 * P - L,
            "S1": 2 * P - H,
            "R2": P + rng,
            "S2": P - rng,
        }

    levels = {k: round(v, 4) for k, v in levels.items()}

    return _json({
        "symbol": symbol,
        "type": type,
        "for_date": target_date.isoformat(),
        "computed_from": {
            "prior_session_date": prior.isoformat(),
            "high": H,
            "low": L,
            "close": C,
        },
        "levels": levels,
    })


# --- Tool: Session VWAP ------------------------------------------------------


@mcp.tool(
    name="get_session_vwap",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_session_vwap(
    symbol: str,
    session: str = "regular",
    session_date: Optional[str] = None,
    timeframe: str = "1Min",
) -> str:
    """
    Compute VWAP for an entire trading session.

    Args:
        symbol: Ticker (e.g. "AAPL", "OKLO").
        session: "regular" (9:30-16:00 ET) or "extended" (4:00-20:00 ET).
        session_date: YYYY-MM-DD. Defaults to most recent weekday.
        timeframe: Bar size — "1Min" (most accurate) or "5Min".
    """
    target_date = _resolve_session_date(session_date)
    start_utc, end_utc = _session_window(target_date, session)

    tf = TimeFrame(1, TimeFrameUnit.Minute) if timeframe == "1Min" else TimeFrame(5, TimeFrameUnit.Minute)
    bars = _fetch_bars(symbol, tf, start_utc, end_utc)
    if not bars:
        return _json({"error": f"No bars for {symbol} on {target_date.isoformat()} ({session} session)"})

    cumulative_pv = 0.0
    cumulative_v = 0.0
    high_bar = bars[0]
    low_bar = bars[0]
    for bar in bars:
        typical = (bar["h"] + bar["l"] + bar["c"]) / 3
        cumulative_pv += typical * bar["v"]
        cumulative_v += bar["v"]
        if bar["h"] > high_bar["h"]:
            high_bar = bar
        if bar["l"] < low_bar["l"]:
            low_bar = bar

    vwap = cumulative_pv / cumulative_v if cumulative_v > 0 else 0.0

    return _json({
        "symbol": symbol,
        "session": session,
        "session_date": target_date.isoformat(),
        "timeframe": timeframe,
        "bar_count": len(bars),
        "session_open": bars[0]["o"],
        "session_close": bars[-1]["c"],
        "session_high": high_bar["h"],
        "session_low": low_bar["l"],
        "total_volume": cumulative_v,
        "vwap": round(vwap, 4),
    })


# --- Tool: Anchored VWAP -----------------------------------------------------


@mcp.tool(
    name="get_anchored_vwap",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_anchored_vwap(
    symbol: str,
    anchor_date: str,
    anchor_time: str = "09:30",
    end_date: Optional[str] = None,
    timeframe: str = "1Min",
) -> str:
    """
    Compute cumulative VWAP from an anchor point forward.

    Use this for "VWAP from the earnings gap-down" or "VWAP from the IPO open"
    style analysis. The anchor is interpreted in US Eastern time.

    Args:
        symbol: Ticker.
        anchor_date: YYYY-MM-DD — the date to anchor VWAP from.
        anchor_time: HH:MM in ET (default "09:30" for regular open).
        end_date: YYYY-MM-DD — the end of the lookback window. Defaults to today.
        timeframe: "1Min", "5Min", "15Min", "1Hour", or "1Day".
    """
    anchor_d = date.fromisoformat(anchor_date)
    end_d = date.fromisoformat(end_date) if end_date else date.today()
    h, m = anchor_time.split(":")
    anchor_dt_et = datetime.combine(anchor_d, dtime(int(h), int(m)), tzinfo=ET)
    end_dt_et = datetime.combine(end_d, dtime(20, 0), tzinfo=ET)
    start_utc = anchor_dt_et.astimezone(timezone.utc)
    end_utc = end_dt_et.astimezone(timezone.utc)

    tf_map = {
        "1Min": TimeFrame(1, TimeFrameUnit.Minute),
        "5Min": TimeFrame(5, TimeFrameUnit.Minute),
        "15Min": TimeFrame(15, TimeFrameUnit.Minute),
        "1Hour": TimeFrame(1, TimeFrameUnit.Hour),
        "1Day": TimeFrame.Day,
    }
    if timeframe not in tf_map:
        raise ValueError(f"timeframe must be one of {list(tf_map.keys())}")

    bars = _fetch_bars(symbol, tf_map[timeframe], start_utc, end_utc)
    if not bars:
        return _json({"error": f"No bars for {symbol} from {anchor_date} {anchor_time} ET"})

    cumulative_pv = 0.0
    cumulative_v = 0.0
    snapshots = []  # capture VWAP at end of each session day for trend visibility
    last_date = None
    for bar in bars:
        typical = (bar["h"] + bar["l"] + bar["c"]) / 3
        cumulative_pv += typical * bar["v"]
        cumulative_v += bar["v"]
        bar_dt_et = bar["t"].astimezone(ET) if hasattr(bar["t"], "astimezone") else bar["t"]
        bar_d = bar_dt_et.date() if hasattr(bar_dt_et, "date") else None
        if bar_d != last_date and last_date is not None:
            snapshots.append({
                "date": last_date.isoformat(),
                "vwap": round(cumulative_pv / cumulative_v, 4) if cumulative_v > 0 else 0.0,
            })
        last_date = bar_d

    vwap = cumulative_pv / cumulative_v if cumulative_v > 0 else 0.0
    final_price = bars[-1]["c"]

    return _json({
        "symbol": symbol,
        "anchor": f"{anchor_date} {anchor_time} ET",
        "end_date": end_d.isoformat(),
        "timeframe": timeframe,
        "bar_count": len(bars),
        "anchored_vwap": round(vwap, 4),
        "final_price": final_price,
        "price_vs_avwap_pct": round((final_price - vwap) / vwap * 100, 2) if vwap else None,
        "total_volume": cumulative_v,
        "daily_snapshots": snapshots[-30:],  # cap to last 30 days for response size
    })


# --- Tool: Volume Profile ----------------------------------------------------


@mcp.tool(
    name="get_volume_profile",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_volume_profile(
    symbol: str,
    session_date: Optional[str] = None,
    session: str = "regular",
    value_area_pct: float = 70.0,
    num_buckets: int = 50,
) -> str:
    """
    Compute volume profile for a session: POC, Value Area High (VAH),
    Value Area Low (VAL), and the full price-volume histogram.

    Volume is distributed across buckets uniformly over each minute bar's
    [low, high] range — a standard approximation when tick data isn't used.
    POC = bucket with highest traded volume. VAH/VAL bracket the
    `value_area_pct` (default 70%) of total volume centered on POC.

    Args:
        symbol: Ticker.
        session_date: YYYY-MM-DD. Defaults to most recent weekday.
        session: "regular" (9:30-16:00 ET) or "extended" (4:00-20:00 ET).
        value_area_pct: Default 70 — the volume bracket around POC. 0-100.
        num_buckets: Histogram resolution. Default 50.
    """
    if not (0 < value_area_pct <= 100):
        raise ValueError("value_area_pct must be in (0, 100]")
    if num_buckets < 5 or num_buckets > 500:
        raise ValueError("num_buckets must be 5-500")

    target_date = _resolve_session_date(session_date)
    start_utc, end_utc = _session_window(target_date, session)
    bars = _fetch_bars(symbol, TimeFrame(1, TimeFrameUnit.Minute), start_utc, end_utc)
    if not bars:
        return _json({"error": f"No minute bars for {symbol} on {target_date.isoformat()} ({session})"})

    profile_low = min(b["l"] for b in bars)
    profile_high = max(b["h"] for b in bars)
    if profile_high <= profile_low:
        return _json({"error": "Degenerate price range (no movement in session)"})

    bucket_size = (profile_high - profile_low) / num_buckets
    profile = [0.0] * num_buckets

    for bar in bars:
        bl, bh, bv = bar["l"], bar["h"], bar["v"]
        if bv <= 0:
            continue
        # Bucket indices (clamped)
        lo_idx = max(0, min(num_buckets - 1, int((bl - profile_low) / bucket_size)))
        hi_idx = max(0, min(num_buckets - 1, int((bh - profile_low) / bucket_size)))
        if hi_idx == lo_idx:
            profile[lo_idx] += bv
            continue
        bar_range = bh - bl
        if bar_range <= 0:
            profile[lo_idx] += bv
            continue
        for idx in range(lo_idx, hi_idx + 1):
            bucket_lo = profile_low + idx * bucket_size
            bucket_hi = bucket_lo + bucket_size
            overlap = min(bucket_hi, bh) - max(bucket_lo, bl)
            if overlap > 0:
                profile[idx] += bv * (overlap / bar_range)

    total_volume = sum(profile)
    if total_volume == 0:
        return _json({"error": "No volume in session"})

    target_volume = total_volume * (value_area_pct / 100)
    poc_idx = max(range(num_buckets), key=lambda i: profile[i])

    # Expand outward from POC to capture value area
    accumulated = profile[poc_idx]
    upper_idx = lower_idx = poc_idx
    while accumulated < target_volume:
        above = profile[upper_idx + 1] if upper_idx + 1 < num_buckets else -1
        below = profile[lower_idx - 1] if lower_idx - 1 >= 0 else -1
        if above < 0 and below < 0:
            break
        if above >= below:
            upper_idx += 1
            accumulated += profile[upper_idx]
        else:
            lower_idx -= 1
            accumulated += profile[lower_idx]

    poc_price = profile_low + (poc_idx + 0.5) * bucket_size
    vah_price = profile_low + (upper_idx + 1) * bucket_size  # top edge
    val_price = profile_low + lower_idx * bucket_size  # bottom edge

    histogram = [
        {
            "price_low": round(profile_low + i * bucket_size, 4),
            "price_high": round(profile_low + (i + 1) * bucket_size, 4),
            "volume": round(profile[i], 2),
            "pct_of_total": round(profile[i] / total_volume * 100, 2) if total_volume else 0,
        }
        for i in range(num_buckets)
    ]

    return _json({
        "symbol": symbol,
        "session_date": target_date.isoformat(),
        "session": session,
        "bar_count": len(bars),
        "price_range": {"low": profile_low, "high": profile_high},
        "bucket_size": round(bucket_size, 4),
        "num_buckets": num_buckets,
        "total_volume": round(total_volume, 2),
        "value_area_pct": value_area_pct,
        "poc": round(poc_price, 4),
        "vah": round(vah_price, 4),
        "val": round(val_price, 4),
        "value_area_volume": round(accumulated, 2),
        "value_area_volume_pct": round(accumulated / total_volume * 100, 2) if total_volume else 0,
        "histogram": histogram,
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

mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_asgi.lifespan(app):
        logger.info("TA MCP server ready on port %d", PORT)
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
