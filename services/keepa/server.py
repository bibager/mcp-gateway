"""
Keepa MCP Server
================
Wraps the Keepa API (https://keepa.com) for Amazon historical price, sales
rank (BSR), buy-box, rating, and competitive-discovery data. Built on the
`keepa` PyPI package so we never hand-roll the CSV-history index map or
KeepaTime conversion.

This is the historical data layer for the Manuka Doctor advertising
optimization workflow — Keepa supplies time-series and competitor snapshots;
review text, A+ content, and your own promo calendar live in other services
(Oxylabs, SP-API).

Normalization (non-negotiable — agents should NEVER see Keepa encodings):
  - timestamps → ISO 8601 UTC strings
  - prices    → decimal dollars (Keepa returns integer cents)
  - -1 values → null (Keepa's "no data / out of stock" sentinel)

Caching:
  Simple SQLite kv store at ``KEEPA_CACHE_PATH`` (default ``/tmp/keepa-cache.db``).
  Saves tokens on repeated history pulls within a deploy. **Ephemeral on DO
  App Platform** — resets across deploys. For durable history baseline,
  consume the responses and persist to your own store.

Env vars:
  MCP_API_KEY              required; protects all /mcp endpoints
  KEEPA_API_KEY            required; Keepa private API key
  KEEPA_DEFAULT_DOMAIN     optional; defaults to 'US' (Keepa domainId 1)
  KEEPA_CACHE_PATH         optional; default '/tmp/keepa-cache.db'
  KEEPA_CACHE_TTL_SECONDS  optional; default 86400 (24h)
  PORT                     optional, defaults to 8014
  SERVER_URL               optional; overrides base URL detection
"""

from __future__ import annotations

import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import time
from base64 import urlsafe_b64encode
from contextlib import asynccontextmanager
from typing import Any, Optional
from urllib.parse import urlencode

import httpx  # noqa: F401 — kept for future direct calls if needed
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("keepa_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
KEEPA_API_KEY: str = os.environ["KEEPA_API_KEY"]
KEEPA_DEFAULT_DOMAIN: str = os.getenv("KEEPA_DEFAULT_DOMAIN", "US").upper()
KEEPA_CACHE_PATH: str = os.getenv("KEEPA_CACHE_PATH", "/tmp/keepa-cache.db")
KEEPA_CACHE_TTL_SECONDS: int = int(os.getenv("KEEPA_CACHE_TTL_SECONDS", "86400"))
PORT: int = int(os.getenv("PORT", "8014"))

# Keepa domain code -> domainId
_DOMAIN_IDS = {
    "RESERVED": 0, "US": 1, "UK": 2, "DE": 3, "FR": 4, "JP": 5, "CA": 6,
    "CN": 7, "IT": 8, "ES": 9, "IN": 10, "MX": 11, "BR": 12, "AU": 13,
}


def _domain_id(code: Optional[str]) -> int:
    code = (code or KEEPA_DEFAULT_DOMAIN).upper()
    if code not in _DOMAIN_IDS:
        raise ValueError(
            f"Unknown domain {code!r}. Valid: {sorted(_DOMAIN_IDS.keys())}"
        )
    return _DOMAIN_IDS[code]


# --- Normalization helpers ---------------------------------------------------


def _cents_to_dollars(v: Any) -> Optional[float]:
    """Keepa returns integer cents; -1 means no data / out of stock."""
    if v is None:
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    if iv == -1:
        return None
    return round(iv / 100.0, 2)


def _raw_int(v: Any) -> Optional[int]:
    """For series where -1 means missing (BSR, review counts) but no cents conversion."""
    if v is None:
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return None if iv == -1 else iv


def _rating(v: Any) -> Optional[float]:
    """Keepa stores rating as 10x its actual value (e.g. 47 → 4.7)."""
    if v is None:
        return None
    try:
        iv = int(v)
    except (TypeError, ValueError):
        return None
    return None if iv == -1 else round(iv / 10.0, 1)


def _to_iso(t: Any) -> Optional[str]:
    """Normalize any time-ish value to ISO 8601 UTC string.

    Handles: datetime.datetime, numpy.datetime64, pandas Timestamp, ISO-formatted
    str (already-converted, e.g. round-tripped through cache), and Keepa-minutes ints.
    """
    if t is None:
        return None
    if isinstance(t, dt.datetime):
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    # str input (ISO already, or numpy str form like "2019-01-16 22:56:00")
    if isinstance(t, str):
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                d = dt.datetime.strptime(t, fmt).replace(tzinfo=dt.timezone.utc)
                return d.strftime("%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
        return None
    # numpy datetime64 / pandas Timestamp
    if hasattr(t, "astype"):
        try:
            secs = t.astype("datetime64[s]").astype(int)
            return dt.datetime.fromtimestamp(int(secs), tz=dt.timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            pass
    if hasattr(t, "isoformat"):
        try:
            return _to_iso(t.to_pydatetime() if hasattr(t, "to_pydatetime") else t.isoformat())
        except Exception:
            pass
    # keepa minutes int fallback
    try:
        return dt.datetime.fromtimestamp(
            (int(t) + 21564000) * 60, tz=dt.timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
    except Exception:
        return None


def _parsed_price(v: Any) -> Optional[float]:
    """Value already-parsed by keepa.parse_csv — dollars float with NaN for missing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    if f < 0:  # also guard against -1 leakage
        return None
    return round(f, 2)


def _parsed_count(v: Any) -> Optional[int]:
    """Value already-parsed by keepa.parse_csv — int with -1 or NaN for missing.

    Used for BSR (SALES) and review counts.
    """
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    iv = int(f)
    return None if iv == -1 else iv


def _parsed_rating(v: Any) -> Optional[float]:
    """Rating already-parsed by keepa — 4.7 directly (NOT 47), NaN for missing."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f or f < 0:
        return None
    return round(f, 1)


def _parse_date_param(s: Optional[str]) -> Optional[dt.datetime]:
    """Parse YYYY-MM-DD to a UTC datetime (start-of-day)."""
    if not s:
        return None
    return dt.datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=dt.timezone.utc)


def _filter_series(
    times: list,
    values: list,
    value_fn,
    start: Optional[dt.datetime],
    end: Optional[dt.datetime],
) -> list[dict[str, Any]]:
    """Zip + normalize a (times, values) pair into [{timestamp, value}, ...]."""
    out: list[dict[str, Any]] = []
    for t, v in zip(times, values):
        iso = _to_iso(t)
        if iso is None:
            continue
        if start or end:
            try:
                dts = dt.datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=dt.timezone.utc
                )
            except ValueError:
                continue
            if start and dts < start:
                continue
            if end and dts > end:
                continue
        out.append({"timestamp": iso, "value": value_fn(v)})
    return out


# --- Cache (simple SQLite kv store) ------------------------------------------


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(KEEPA_CACHE_PATH, isolation_level=None, timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS kv ("
        " key TEXT PRIMARY KEY,"
        " fetched_at INTEGER NOT NULL,"
        " ttl_seconds INTEGER NOT NULL,"
        " data TEXT NOT NULL"
        ")"
    )
    return conn


def _cache_key(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def cache_get(key: str) -> Any:
    try:
        conn = _cache_conn()
        row = conn.execute(
            "SELECT fetched_at, ttl_seconds, data FROM kv WHERE key=?", (key,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        fetched_at, ttl, data = row
        if time.time() - fetched_at > ttl:
            return None
        return json.loads(data)
    except Exception as e:
        logger.warning("cache_get failed: %s", e)
        return None


def cache_set(key: str, value: Any, ttl: Optional[int] = None) -> None:
    try:
        conn = _cache_conn()
        conn.execute(
            "INSERT OR REPLACE INTO kv (key, fetched_at, ttl_seconds, data) VALUES (?,?,?,?)",
            (key, int(time.time()), int(ttl or KEEPA_CACHE_TTL_SECONDS), json.dumps(value)),
        )
        conn.close()
    except Exception as e:
        logger.warning("cache_set failed: %s", e)


# --- Lazy Keepa client -------------------------------------------------------

_keepa_client = None
_keepa_lock = asyncio.Lock()


async def _get_client():
    """Lazily construct the keepa.Keepa client on first use.

    The constructor pings Keepa to fetch token state, which can fail if the
    key/subscription isn't activated. We defer until a tool actually needs it.
    """
    global _keepa_client
    if _keepa_client is not None:
        return _keepa_client
    async with _keepa_lock:
        if _keepa_client is not None:
            return _keepa_client
        import keepa  # local import — slow module load
        _keepa_client = await asyncio.to_thread(
            keepa.Keepa, KEEPA_API_KEY, timeout=30
        )
        logger.info(
            "Keepa client ready. tokens_left=%s, refill_rate=%s/min",
            getattr(_keepa_client, "tokens_left", "?"),
            getattr(_keepa_client, "status", {}).get("refillRate", "?")
            if isinstance(getattr(_keepa_client, "status", None), dict)
            else "?",
        )
        return _keepa_client


def _format_keepa_error(err: Exception) -> str:
    """Turn Keepa lib exceptions into actionable text the agent can read."""
    msg = str(err) or err.__class__.__name__
    low = msg.lower()
    if "token" in low and ("not enough" in low or "wait" in low or "deplet" in low):
        return (
            "Keepa token bucket is empty. Use ``get_token_status`` to see "
            "current balance and refill rate; retry after enough tokens accrue."
        )
    if "401" in msg or "invalid" in low and "key" in low:
        return (
            "Keepa API rejected the key. Check KEEPA_API_KEY is set and the "
            "subscription is active (not 'Payment Pending')."
        )
    if "429" in msg:
        return "Keepa rate-limited the request (HTTP 429). Back off and retry."
    return msg


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "keepa_mcp",
    instructions=(
        "Tools for Amazon historical data from Keepa: prices, sales rank "
        "(BSR), buy box, rating/review trajectory, plus competitive "
        "discovery (product finder, deals, bestsellers, seller lookup). "
        "Default marketplace is 'US' (Keepa domainId 1); pass domain='UK', "
        "'DE', etc. for others. Dates are YYYY-MM-DD. Each call costs "
        "Keepa tokens — use ``get_token_status`` to check your balance."
    ),
)


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


# --- Core: snapshot + history ------------------------------------------------


@mcp.tool(
    name="get_product_snapshot",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_product_snapshot(asins: list[str], domain: str = "US") -> str:
    """
    Get current state for up to 100 ASINs in a single token-light call.

    Returns per ASIN: title, brand, parent/child ASIN map, current Amazon
    price, Buy Box price + sellerId + isFBA, current BSR + category node,
    rating, review count, active deal/coupon, monthly sold estimate, and
    in-stock flag.

    Args:
        asins: List of ASINs (max 100 per call — batched internally if longer).
        domain: Marketplace code, default 'US'. Other: 'UK', 'DE', 'FR', 'JP',
            'CA', 'IT', 'ES', 'IN', 'MX', 'BR', 'AU', 'CN'.
    """
    if not asins:
        raise ValueError("asins must be a non-empty list")
    did = _domain_id(domain)
    client = await _get_client()

    # Batch in chunks of 100 (Keepa product endpoint max)
    chunks = [asins[i : i + 100] for i in range(0, len(asins), 100)]
    results: list[dict[str, Any]] = []
    try:
        for chunk in chunks:
            products = await asyncio.to_thread(
                client.query,
                chunk,
                domain=domain,
                stats=30,        # 30-day stats for monthlySold + summary
                history=False,
                offers=20,       # surface buy-box seller/FBA
                buybox=True,
                wait=False,
            )
            for p in products or []:
                results.append(_snapshot_one(p))
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e

    return _json({
        "domain": domain,
        "domain_id": did,
        "count": len(results),
        "products": results,
    })


def _snapshot_one(p: dict) -> dict:
    """Reduce a Keepa product dict to a snapshot object."""
    stats = p.get("stats") or {}
    current = stats.get("current") or [None] * 32
    cat_tree = p.get("categoryTree") or []
    bb_seller = p.get("buyBoxSellerIdHistory") or []
    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "manufacturer": p.get("manufacturer"),
        "parentAsin": p.get("parentAsin"),
        "variationCSV": p.get("variationCSV"),
        "current": {
            "amazon_price": _cents_to_dollars(current[0]),
            "new_price": _cents_to_dollars(current[1]),
            "used_price": _cents_to_dollars(current[2]),
            "buy_box_price": _cents_to_dollars(current[18]),
            "sales_rank": _raw_int(current[3]),
            "rating": _rating(current[16]),
            "review_count": _raw_int(current[17]),
            "list_price": _cents_to_dollars(current[4]),
        },
        "category_node": cat_tree[-1] if cat_tree else None,
        "monthly_sold": p.get("monthlySold"),
        "buy_box_seller_id": bb_seller[-1] if bb_seller else None,
        "in_stock": p.get("availabilityAmazon") in (0, None) and current[0] not in (-1, None),
        "coupon": p.get("coupon"),
        "lightning_deal": p.get("lightningDealInfo"),
    }


@mcp.tool(
    name="get_price_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_price_history(
    asin: str,
    price_type: str = "amazon",
    start: Optional[str] = None,
    end: Optional[str] = None,
    domain: str = "US",
) -> str:
    """
    Get clean price time series for one ASIN. Out-of-stock intervals are null.

    Args:
        asin: Amazon ASIN.
        price_type: One of 'amazon', 'new', 'used', 'buybox', 'fbm', 'fba',
            'list_price'. Default 'amazon'.
        start: Optional YYYY-MM-DD lower bound (UTC).
        end: Optional YYYY-MM-DD upper bound (UTC).
        domain: Marketplace code, default 'US'.
    """
    series_map = {
        "amazon": "AMAZON",
        "new": "NEW",
        "used": "USED",
        "buybox": "BUY_BOX_SHIPPING",
        "fbm": "NEW_FBM_SHIPPING",
        "fba": "NEW_FBA",
        "list_price": "LISTPRICE",
    }
    key = price_type.lower()
    if key not in series_map:
        raise ValueError(f"price_type must be one of {sorted(series_map)}")
    series = series_map[key]

    product = await _fetch_product_history(asin, domain, want_offers=(key == "buybox"))
    data = product.get("data") or {}
    times_arr = data.get(f"{series}_time")
    values_arr = data.get(series)
    times = list(times_arr) if times_arr is not None else []
    values = list(values_arr) if values_arr is not None else []
    if len(times) == 0 or len(values) == 0:
        return _json({
            "asin": asin,
            "domain": domain,
            "price_type": price_type,
            "points": [],
            "note": f"No data for series {series}. Series may not be populated for this ASIN.",
        })
    pts = _filter_series(
        times, values, _parsed_price,
        _parse_date_param(start), _parse_date_param(end),
    )
    return _json({
        "asin": asin,
        "domain": domain,
        "price_type": price_type,
        "series": series,
        "count": len(pts),
        "points": pts,
    })


@mcp.tool(
    name="get_sales_rank_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_sales_rank_history(
    asin: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    domain: str = "US",
) -> str:
    """
    Get BSR (sales rank) time series for an ASIN — the core
    "is this competitor gaining momentum?" signal.

    Returns the category node alongside the series, since BSR is per-category.

    Args:
        asin: Amazon ASIN.
        start: Optional YYYY-MM-DD lower bound.
        end: Optional YYYY-MM-DD upper bound.
        domain: Marketplace code, default 'US'.
    """
    product = await _fetch_product_history(asin, domain)
    data = product.get("data") or {}
    times_arr = data.get("SALES_time")
    values_arr = data.get("SALES")
    times = list(times_arr) if times_arr is not None else []
    values = list(values_arr) if values_arr is not None else []
    cat_tree = product.get("categoryTree") or []
    pts = _filter_series(
        times, values, _parsed_count,
        _parse_date_param(start), _parse_date_param(end),
    )
    return _json({
        "asin": asin,
        "domain": domain,
        "category_node": cat_tree[-1] if cat_tree else None,
        "category_tree": cat_tree,
        "count": len(pts),
        "points": pts,
    })


@mcp.tool(
    name="get_buybox_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_buybox_history(
    asin: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    domain: str = "US",
) -> str:
    """
    Get Buy Box price + seller (when available) over time.

    Requires the offers data (more expensive in tokens). Returns parallel
    price and sellerId arrays, plus a current isFBA flag.

    Args:
        asin: Amazon ASIN.
        start: Optional YYYY-MM-DD lower bound.
        end: Optional YYYY-MM-DD upper bound.
        domain: Marketplace code, default 'US'.
    """
    product = await _fetch_product_history(asin, domain, want_offers=True)
    data = product.get("data") or {}
    times_arr = data.get("BUY_BOX_SHIPPING_time")
    prices_arr = data.get("BUY_BOX_SHIPPING")
    times = list(times_arr) if times_arr is not None else []
    prices = list(prices_arr) if prices_arr is not None else []
    seller_hist = product.get("buyBoxSellerIdHistory") or []

    price_pts = _filter_series(
        times, prices, _parsed_price,
        _parse_date_param(start), _parse_date_param(end),
    )

    # buyBoxSellerIdHistory is interleaved [keepaTime, sellerId, keepaTime, sellerId, ...]
    seller_pts: list[dict[str, Any]] = []
    for i in range(0, len(seller_hist) - 1, 2):
        iso = _to_iso(seller_hist[i])
        sid = seller_hist[i + 1] if seller_hist[i + 1] not in (-1, None, "") else None
        if iso:
            seller_pts.append({"timestamp": iso, "seller_id": sid})

    return _json({
        "asin": asin,
        "domain": domain,
        "price_count": len(price_pts),
        "price_points": price_pts,
        "seller_count": len(seller_pts),
        "seller_points": seller_pts,
        "current_is_fba": product.get("isFBA"),
    })


@mcp.tool(
    name="get_rating_history",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_rating_history(
    asin: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    domain: str = "US",
) -> str:
    """
    Get rating (out of 5) and review-count time series.

    Rating updates less frequently than price — gaps in the series usually
    mean "no change observed", not a real drop. Treat with care.

    Args:
        asin: Amazon ASIN.
        start: Optional YYYY-MM-DD lower bound.
        end: Optional YYYY-MM-DD upper bound.
        domain: Marketplace code, default 'US'.
    """
    product = await _fetch_product_history(asin, domain, want_rating=True)
    data = product.get("data") or {}

    rt_arr = data.get("RATING_time")
    rv_arr = data.get("RATING")
    ct_arr = data.get("COUNT_REVIEWS_time")
    cv_arr = data.get("COUNT_REVIEWS")
    rating_times = list(rt_arr) if rt_arr is not None else []
    rating_vals = list(rv_arr) if rv_arr is not None else []
    count_times = list(ct_arr) if ct_arr is not None else []
    count_vals = list(cv_arr) if cv_arr is not None else []

    rating_pts = _filter_series(
        rating_times, rating_vals, _parsed_rating,
        _parse_date_param(start), _parse_date_param(end),
    )
    review_pts = _filter_series(
        count_times, count_vals, _parsed_count,
        _parse_date_param(start), _parse_date_param(end),
    )

    return _json({
        "asin": asin,
        "domain": domain,
        "note": "Rating updates less frequently than price — gaps may indicate no change rather than missing data.",
        "rating_points": rating_pts,
        "review_count_points": review_pts,
    })


@mcp.tool(
    name="get_product_stats",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_product_stats(
    asins: list[str],
    days: int = 90,
    domain: str = "US",
) -> str:
    """
    Cheap weekly snapshot of many ASINs — Keepa-summarized avg/min/max/current.

    Use this in place of full history pulls when you only need an interval
    summary across a wide ASIN set.

    Args:
        asins: List of ASINs (max 100 per call, batched if larger).
        days: Look-back window in days (commonly 30, 90, 180, 365). Default 90.
        domain: Marketplace code, default 'US'.
    """
    if not asins:
        raise ValueError("asins must be a non-empty list")
    client = await _get_client()
    chunks = [asins[i : i + 100] for i in range(0, len(asins), 100)]
    rows: list[dict[str, Any]] = []
    try:
        for chunk in chunks:
            products = await asyncio.to_thread(
                client.query, chunk, domain=domain, stats=days, history=False, wait=False
            )
            for p in products or []:
                rows.append(_stats_row(p, days))
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e

    return _json({
        "domain": domain,
        "interval_days": days,
        "count": len(rows),
        "products": rows,
    })


def _stats_row(p: dict, days: int) -> dict:
    """Project a Keepa stats blob onto a compact summary row.

    Keepa's ``stats`` shape:
      stats.current[i] = current value at CSV index i
      stats.avg[i]     = interval avg at CSV index i
      stats.min[i]     = [keepaTime, value] of interval minimum at index i
      stats.max[i]     = [keepaTime, value] of interval maximum at index i
    Indices we care about: 0=AMAZON price, 3=SALES (BSR), 18=BUY_BOX_SHIPPING.
    """
    s = p.get("stats") or {}

    def at(arr, i):
        if not arr or i >= len(arr):
            return None
        return arr[i]

    def min_max_value(key, i):
        entry = at(s.get(key), i)
        if not entry or not isinstance(entry, (list, tuple)) or len(entry) < 2:
            return None
        return entry[1]

    return {
        "asin": p.get("asin"),
        "title": p.get("title"),
        "amazon_price": {
            "current": _cents_to_dollars(at(s.get("current"), 0)),
            "avg": _cents_to_dollars(at(s.get("avg"), 0)),
            "min": _cents_to_dollars(min_max_value("min", 0)),
            "max": _cents_to_dollars(min_max_value("max", 0)),
        },
        "sales_rank": {
            "current": _raw_int(at(s.get("current"), 3)),
            "avg": _raw_int(at(s.get("avg"), 3)),
            "min": _raw_int(min_max_value("min", 3)),
            "max": _raw_int(min_max_value("max", 3)),
        },
        "buy_box_price": {
            "current": _cents_to_dollars(at(s.get("current"), 18)),
            "avg": _cents_to_dollars(at(s.get("avg"), 18)),
            "min": _cents_to_dollars(min_max_value("min", 18)),
            "max": _cents_to_dollars(min_max_value("max", 18)),
        },
        "monthly_sold": p.get("monthlySold"),
    }


# --- Discovery: competitive set + promos -------------------------------------


@mcp.tool(
    name="find_products",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def find_products(
    filters: dict[str, Any],
    domain: str = "US",
    per_page: int = 50,
) -> str:
    """
    Use Keepa Product Finder to discover ASINs matching filters.

    The ``filters`` dict is passed through to Keepa's Product Finder API.
    Common keys: ``categories_include`` (list of category nodes),
    ``brand``, ``current_SALES`` (BSR range tuple), ``current_AMAZON``
    (price cents range tuple), ``current_COUNT_REVIEWS`` etc. See
    keepa.com/#!discuss/t/product-finder for the full schema.

    Args:
        filters: Keepa Product Finder filter dict.
        domain: Marketplace code, default 'US'.
        per_page: Max ASINs to return (default 50).
    """
    client = await _get_client()
    try:
        asins = await asyncio.to_thread(
            client.product_finder, filters, domain=domain, n_products=per_page, wait=False
        )
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e
    return _json({"domain": domain, "count": len(asins or []), "asins": asins or []})


@mcp.tool(
    name="get_bestsellers",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_bestsellers(category_node: int, domain: str = "US") -> str:
    """
    Get the top-selling ASINs for a category node — useful for refreshing
    your competitive set.

    Args:
        category_node: Numeric Amazon category node ID (e.g. 6973671011).
        domain: Marketplace code, default 'US'.
    """
    client = await _get_client()
    try:
        asins = await asyncio.to_thread(
            client.best_sellers_query, category_node, domain=domain, wait=False
        )
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e
    return _json({
        "domain": domain,
        "category_node": category_node,
        "count": len(asins or []),
        "asins": asins or [],
    })


@mcp.tool(
    name="get_deals",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_deals(filters: dict[str, Any], domain: str = "US") -> str:
    """
    Get current price-drops / Lightning Deals matching filter criteria —
    competitor promo monitoring.

    ``filters`` follows Keepa's Deals API schema. Common keys:
    ``categories``, ``priceTypes`` (list of CSV indices), ``deltaPercentRange``,
    ``minRating``, ``isLightningDeal``. See keepa.com docs.

    Args:
        filters: Keepa Deals filter dict.
        domain: Marketplace code, default 'US'.
    """
    client = await _get_client()
    # Ensure domain is in the filter (Keepa requires it)
    deal_parms = dict(filters)
    deal_parms.setdefault("domainId", _domain_id(domain))
    try:
        result = await asyncio.to_thread(client.deals, deal_parms, wait=False)
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e
    return _json(result)


@mcp.tool(
    name="get_seller",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_seller(seller_id: str, domain: str = "US") -> str:
    """
    Get seller information and (where available) their tracked ASIN list.

    Useful for monitoring a specific competitor seller. The seller_id is
    Amazon's internal seller identifier (e.g. 'A2L77EE7U53NWQ' for Amazon.com).

    Args:
        seller_id: Amazon seller ID.
        domain: Marketplace code, default 'US'.
    """
    client = await _get_client()
    try:
        result = await asyncio.to_thread(
            client.seller_query, seller_id, domain=domain, wait=False
        )
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e
    return _json(result)


@mcp.tool(
    name="search_products",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def search_products(term: str, domain: str = "US", per_page: int = 40) -> str:
    """
    Map a keyword/phrase to matching ASINs.

    Uses Keepa's /search REST endpoint directly. The keepa Python lib's
    ``search_for_categories`` returns category nodes, which is a different
    discovery path — use ``get_bestsellers(category_node)`` if you want
    to drill into a category.

    Args:
        term: Search keyword or phrase.
        domain: Marketplace code, default 'US'.
        per_page: Max ASINs to return, default 40.
    """
    did = _domain_id(domain)
    try:
        async with httpx.AsyncClient(timeout=30.0) as http:
            r = await http.get(
                "https://api.keepa.com/search",
                params={
                    "key": KEEPA_API_KEY,
                    "domain": did,
                    "type": "product",
                    "term": term,
                },
            )
        if r.status_code != 200:
            raise RuntimeError(
                f"Keepa /search returned HTTP {r.status_code}: {r.text[:300]}"
            )
        body = r.json()
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e

    products = body.get("products") or []
    asins = [p.get("asin") for p in products if p.get("asin")][:per_page]
    return _json({
        "term": term,
        "domain": domain,
        "count": len(asins),
        "asins": asins,
        "tokens_left": body.get("tokensLeft"),
    })


# --- Operations --------------------------------------------------------------


def _status_dict(status_obj: Any) -> dict[str, Any]:
    """Normalize keepa's Status (older versions: dict; newer: dataclass-like object)."""
    if status_obj is None:
        return {}
    if isinstance(status_obj, dict):
        return status_obj
    if hasattr(status_obj, "__dict__"):
        return {k: v for k, v in vars(status_obj).items() if not k.startswith("_")}
    if hasattr(status_obj, "_asdict"):
        return dict(status_obj._asdict())
    return {}


@mcp.tool(
    name="get_token_status",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_token_status() -> str:
    """
    Get your current Keepa token state — balance, refill rate, time to refill.

    Use this to pace heavy batch jobs. If the subscription is "Payment Pending"
    or inactive, this surfaces a zero balance and explains why.
    """
    try:
        client = await _get_client()
    except Exception as e:
        return _json({
            "ok": False,
            "error": _format_keepa_error(e),
            "tokens_left": 0,
        })
    status = _status_dict(getattr(client, "status", None))
    refill_in = status.get("refillIn")
    return _json({
        "ok": True,
        "tokens_left": getattr(client, "tokens_left", None),
        "refill_rate_per_minute": status.get("refillRate"),
        "refill_in_seconds": (refill_in / 1000) if isinstance(refill_in, (int, float)) else None,
        "tokens_consumed_total": status.get("tokensConsumed"),
        "token_flow_reduction": status.get("tokenFlowReduction"),
        "subscription": {
            "expires_at_epoch_ms": status.get("subscriptionExpires"),
            "max_tokens": status.get("maxTokens"),
        },
        "raw_status": status,
    })


# --- Internal: cached product history fetch ----------------------------------


async def _fetch_product_history(
    asin: str,
    domain: str,
    *,
    want_offers: bool = False,
    want_rating: bool = True,
) -> dict:
    """Fetch a single Keepa product with history parsed (no caching).

    The earlier cache layer was removed: it had to JSONify numpy arrays for
    storage, which corrupted the parsed-time/parsed-value shape that
    ``keepa.parse_csv`` produces and broke every downstream history tool.
    For v1 we re-fetch on each call — Keepa's per-product history cost is
    low enough that this is acceptable; revisit with a typed parquet cache
    if a workload demands it.
    """
    client = await _get_client()
    kwargs: dict[str, Any] = {
        "domain": domain,
        "history": True,
        "rating": want_rating,
        "wait": False,
    }
    if want_offers:
        kwargs["offers"] = 20
        kwargs["buybox"] = True
    try:
        products = await asyncio.to_thread(client.query, asin, **kwargs)
    except Exception as e:
        raise RuntimeError(_format_keepa_error(e)) from e
    if not products:
        raise RuntimeError(f"No product returned for ASIN {asin!r}")
    return products[0]


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
        logger.info("Keepa MCP server ready on port %d", PORT)
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
