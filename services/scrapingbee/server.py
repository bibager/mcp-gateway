"""
ScrapingBee MCP Server
======================
Live competitor-content layer for the Manuka Doctor optimization workflow:
review text/sentiment, competitor listing & A+ content, organic SERP rank,
current competitor price/buy-box snapshot.

Built on ScrapingBee's Amazon API templates (Product, Search) plus the
general HTML API with AI extraction for reviews (no dedicated reviews
endpoint exists — confirmed via probe).

Localization rule (confirmed by API): if the chosen Amazon domain matches
the country you're targeting (e.g. ``domain=com`` for US), pass ``zip_code``
NOT ``country``. ``country`` is for routing the request from a non-matching
country. We default to ``domain=com`` + a configurable US ZIP so weekly
competitor prices stay comparable.

Credit costs (per ScrapingBee docs + confirmed):
  - /amazon/product, /amazon/search   light_request=true:  5 credits
                                       light_request=false: 15 credits
  - /api/v1 (general HTML)            no JS: 1, JS: 5, premium+JS: 25
                                       + AI extraction: +5 credits
  - /usage                            free

Env vars:
  MCP_API_KEY                  required
  SCRAPINGBEE_API_KEY          required
  SCRAPINGBEE_DEFAULT_ZIP      optional; default '30301' (Atlanta — consistent US)
  SCRAPINGBEE_DEFAULT_COUNTRY  optional; default '' (paired with zip; only set if domain != local)
  SCRAPINGBEE_CREDIT_BUDGET    optional; soft per-process cap (default unlimited)
  SCRAPINGBEE_CACHE_PATH       optional; default '/tmp/scrapingbee-cache.db'
  SCRAPINGBEE_CACHE_TTL_SECONDS  optional; default 604800 (7d)
  PORT                         optional, defaults to 8015
  SERVER_URL                   optional
"""

from __future__ import annotations

import datetime as dt
import hashlib
import html as html_mod
import json
import logging
import os
import re
import secrets
import sqlite3
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
logger = logging.getLogger("scrapingbee_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
SB_API_KEY: str = os.environ["SCRAPINGBEE_API_KEY"]
SB_DEFAULT_ZIP: str = os.getenv("SCRAPINGBEE_DEFAULT_ZIP", "30301")
SB_DEFAULT_COUNTRY: str = os.getenv("SCRAPINGBEE_DEFAULT_COUNTRY", "").lower()
SB_CREDIT_BUDGET: Optional[int] = (
    int(os.environ["SCRAPINGBEE_CREDIT_BUDGET"])
    if os.getenv("SCRAPINGBEE_CREDIT_BUDGET")
    else None
)
SB_CACHE_PATH: str = os.getenv("SCRAPINGBEE_CACHE_PATH", "/tmp/scrapingbee-cache.db")
SB_CACHE_TTL: int = int(os.getenv("SCRAPINGBEE_CACHE_TTL_SECONDS", "604800"))
PORT: int = int(os.getenv("PORT", "8015"))

SB_BASE = "https://app.scrapingbee.com/api/v1"

# Amazon domain code map (country code -> Amazon TLD path)
_AMAZON_DOMAINS = {
    "us": "com", "uk": "co.uk", "de": "de", "fr": "fr", "jp": "co.jp",
    "ca": "ca", "it": "it", "es": "es", "in": "in", "mx": "com.mx",
    "br": "com.br", "au": "com.au",
}


def _amazon_domain(country: str) -> str:
    code = (country or "us").lower()
    if code not in _AMAZON_DOMAINS:
        raise ValueError(
            f"Unknown country {code!r}. Valid: {sorted(_AMAZON_DOMAINS.keys())}"
        )
    return _AMAZON_DOMAINS[code]


# --- Credit accounting -------------------------------------------------------

_credits_spent_this_run: int = 0


def _check_budget(estimated_cost: int) -> None:
    if SB_CREDIT_BUDGET is None:
        return
    if _credits_spent_this_run + estimated_cost > SB_CREDIT_BUDGET:
        raise RuntimeError(
            f"ScrapingBee credit budget exceeded. "
            f"This call would cost ~{estimated_cost} credits; "
            f"this process has already spent {_credits_spent_this_run} of "
            f"{SB_CREDIT_BUDGET}. Raise SCRAPINGBEE_CREDIT_BUDGET or wait for "
            f"the process to restart. Account-level remaining credits are "
            f"reported by get_account_usage."
        )


def _record_spend(cost: int) -> None:
    global _credits_spent_this_run
    _credits_spent_this_run += cost


# --- Normalization helpers ---------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    no_tags = _HTML_TAG_RE.sub(" ", s)
    return html_mod.unescape(no_tags).strip()


def _to_decimal_price(p: Any) -> Optional[float]:
    if p is None or p == "" or p == "-1":
        return None
    try:
        return round(float(p), 2)
    except (TypeError, ValueError):
        return None


def _parse_iso_date_loose(s: Any) -> Optional[str]:
    """Try to coax an arbitrary date string into ISO YYYY-MM-DD.

    ScrapingBee's product-endpoint review timestamps come back as
    ``"Reviewed in the United States April 27, 2026"`` (no "on" preposition).
    The full-page reviews format is ``"... on May 15, 2025"``. Handle both
    plus already-ISO strings.
    """
    if not s:
        return None
    s = str(s).strip()
    # Already ISO?
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Anywhere in the string: "Month DD, YYYY" — covers both Amazon dialects.
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", s)
    if m:
        try:
            return dt.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s  # give back whatever we got rather than discard


# --- Cache (SQLite kv, pure JSON-safe — unlike Keepa's numpy headache) -------


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(SB_CACHE_PATH, isolation_level=None, timeout=10)
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
            (key, int(time.time()), int(ttl or SB_CACHE_TTL), json.dumps(value, default=str)),
        )
        conn.close()
    except Exception as e:
        logger.warning("cache_set failed: %s", e)


# --- ScrapingBee HTTP client -------------------------------------------------


async def _sb_get(path: str, params: dict[str, Any], *, timeout: float = 90.0) -> tuple[int, Any]:
    """Call a ScrapingBee endpoint. Returns (status_code, json_or_text)."""
    params = dict(params)
    params["api_key"] = SB_API_KEY
    url = f"{SB_BASE}{path}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.get(url, params=params)
    ct = r.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    return r.status_code, r.text


def _build_localization(country: Optional[str], zip_code: Optional[str]) -> dict[str, str]:
    """Resolve country/zip params per ScrapingBee's rule.

    If the target country matches the Amazon domain (e.g. country='us' on
    domain='com'), passing ``country`` is rejected. Pass ``zip_code`` instead.
    """
    out: dict[str, str] = {}
    c = (country or "").lower()
    z = zip_code or ""
    domain = _amazon_domain(c or "us")
    out["domain"] = domain
    if z:
        out["zip_code"] = z
    elif c and domain != _AMAZON_DOMAINS.get(c, ""):
        # Routing the request from a non-matching country
        out["country"] = c
    else:
        # Same country as domain — use default zip
        if SB_DEFAULT_ZIP and (c in ("", "us")):
            out["zip_code"] = SB_DEFAULT_ZIP
        if SB_DEFAULT_COUNTRY and not out.get("zip_code"):
            out["country"] = SB_DEFAULT_COUNTRY
    return out


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "scrapingbee_mcp",
    instructions=(
        "Tools for live Amazon competitor content: product snapshots, "
        "review text, organic SERP rank, and buy-box state. Use Keepa for "
        "history. Default marketplace is US (amazon.com); pass country=UK, "
        "DE, etc. for other markets. Each call consumes ScrapingBee "
        "credits — call get_account_usage to see your balance."
    ),
)


# --- Tools: Core -------------------------------------------------------------


@mcp.tool(
    name="get_account_usage",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_account_usage() -> str:
    """
    Get ScrapingBee account credit usage and concurrency.

    Free to call — does not consume credits. Use this before heavy batch
    jobs to know how much headroom you have.
    """
    status, body = await _sb_get("/usage", {})
    if status != 200:
        raise RuntimeError(f"ScrapingBee /usage HTTP {status}: {str(body)[:300]}")
    return _json({
        "ok": True,
        "max_api_credit": body.get("max_api_credit"),
        "used_api_credit": body.get("used_api_credit"),
        "remaining_credit": (
            (body.get("max_api_credit") or 0) - (body.get("used_api_credit") or 0)
            if body.get("max_api_credit") is not None else None
        ),
        "max_concurrency": body.get("max_concurrency"),
        "current_concurrency": body.get("current_concurrency"),
        "renewal_subscription_date": body.get("renewal_subscription_date"),
        "this_process_credits_spent": _credits_spent_this_run,
        "this_process_credit_budget": SB_CREDIT_BUDGET,
    })


@mcp.tool(
    name="get_product",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_product(
    asin: str,
    country: str = "us",
    zip_code: Optional[str] = None,
    include_aplus: bool = False,
) -> str:
    """
    Listing snapshot for an ASIN: title, bullets, price, rating, review
    count, images, category, buy-box, delivery, coupon/deal, BSR ladder.

    Args:
        asin: 10-character Amazon ASIN.
        country: Marketplace code, default 'us'. Other: 'uk', 'de', 'fr',
            'jp', 'ca', 'it', 'es', 'in', 'mx', 'br', 'au'.
        zip_code: Optional ZIP for US localization (defaults to
            SCRAPINGBEE_DEFAULT_ZIP).
        include_aplus: If True, runs an extra ai_extract_rules pass to
            pull A+ content modules — costs an extra +5 credits.

    Credit cost: 5 (base) + 5 (if include_aplus).
    """
    cost = 5 + (5 if include_aplus else 0)
    _check_budget(cost)

    loc = _build_localization(country, zip_code)
    cache_key = _cache_key("product", asin, loc.get("domain"), loc.get("zip_code"), loc.get("country"), int(include_aplus))
    cached = cache_get(cache_key)
    if cached is not None:
        cached["_from_cache"] = True
        return _json(cached)

    params: dict[str, Any] = {"query": asin, **loc}
    status, body = await _sb_get("/amazon/product", params)
    if status != 200:
        raise RuntimeError(
            f"ScrapingBee /amazon/product HTTP {status} for ASIN {asin}: {str(body)[:300]}"
        )
    _record_spend(5)
    out = _normalize_product(body)

    if include_aplus:
        aplus = await _ai_extract_aplus(asin, country, zip_code)
        out["aplus"] = aplus
        _record_spend(5)

    out["credits_consumed_this_call"] = cost
    out["credits_consumed_this_process"] = _credits_spent_this_run
    cache_set(cache_key, out)
    return _json(out)


def _as_dict(x: Any) -> dict:
    """Defensively coerce a possibly-list-or-dict subtree to a dict for .get() calls."""
    if isinstance(x, dict):
        return x
    if isinstance(x, list) and x and isinstance(x[0], dict):
        return x[0]  # first entry — most relevant
    return {}


def _normalize_product(p: dict) -> dict:
    """Project the rich ScrapingBee product response into the spec's snapshot shape.

    ScrapingBee's response shape is inconsistent between fields: ``buybox``
    may come back as a dict OR a list of offers; ``delivery`` may come back
    as a dict OR a list of delivery options. We coerce both, and stash the
    raw subtree for callers that need the full structure.
    """
    buybox_raw = p.get("buybox")
    delivery_raw = p.get("delivery")
    buybox = _as_dict(buybox_raw)
    delivery = _as_dict(delivery_raw)
    sales_rank = p.get("sales_rank") or []
    if not isinstance(sales_rank, list):
        sales_rank = []
    bsr_primary = sales_rank[0] if sales_rank else None

    return {
        "asin": p.get("asin"),
        "url": p.get("url"),
        "title": p.get("title"),
        "brand": p.get("brand"),
        "description": p.get("description"),
        "bullet_points": p.get("bullet_points"),
        "category": p.get("category"),
        "price": _to_decimal_price(p.get("price")),
        "currency": p.get("currency"),
        "rating": p.get("rating"),
        "reviews_count": p.get("reviews_count"),
        "answered_questions_count": p.get("answered_questions_count"),
        "images": p.get("images"),
        "buybox": {
            "price": _to_decimal_price(buybox.get("price")),
            "currency": buybox.get("currency"),
            "seller": buybox.get("seller") or buybox.get("seller_name"),
            "is_fba": buybox.get("is_fba"),
            "stock": buybox.get("stock") or buybox.get("availability"),
        },
        "buybox_raw": buybox_raw,
        "coupon": p.get("coupon"),
        "coupon_discount_percentage": p.get("coupon_discount_percentage"),
        "deal_type": p.get("deal_type"),
        "delivery": {
            "delivery_date": delivery.get("delivery_date"),
            "fastest_delivery": delivery.get("fastest_delivery"),
            "from": delivery.get("from"),
        },
        "delivery_raw": delivery_raw,
        "bsr_primary": bsr_primary,
        "sales_rank_ladder": sales_rank,
    }


async def _ai_extract_aplus(asin: str, country: str, zip_code: Optional[str]) -> dict:
    """Use the general HTML API + ai_extract_rules to pull A+ content modules."""
    domain = _amazon_domain(country)
    amazon_url = f"https://www.amazon.{domain}/dp/{asin}"
    rules = {
        "aplus_modules": {
            "type": "list",
            "description": "All distinct A+ content modules on this product page. Skip standard product details bullets.",
            "output": {
                "heading": "module heading text",
                "body": "module body text with HTML stripped",
                "has_image": "boolean, true if module includes an image",
            },
        },
    }
    params: dict[str, Any] = {
        "url": amazon_url,
        "render_js": "true",
        "ai_extract_rules": json.dumps(rules),
    }
    z = zip_code or SB_DEFAULT_ZIP
    if z:
        params["country_code"] = "us"  # general API uses country_code not country
    status, body = await _sb_get("/", params, timeout=120.0)
    if status != 200:
        return {"error": f"AI extraction HTTP {status}: {str(body)[:300]}"}
    return body if isinstance(body, dict) else {"raw": str(body)[:2000]}


@mcp.tool(
    name="get_offers",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_offers(
    asin: str,
    country: str = "us",
    zip_code: Optional[str] = None,
) -> str:
    """
    Current offer/buy-box snapshot for an ASIN: buy-box price, seller, FBA
    flag, other-offer count, stock state.

    Reuses /amazon/product and projects the buybox subtree (saves a round
    trip vs the spec's separate endpoint idea — ScrapingBee derives
    buy-box info from the product page).

    Credit cost: 5.
    """
    _check_budget(5)
    loc = _build_localization(country, zip_code)
    params: dict[str, Any] = {"query": asin, **loc}
    status, body = await _sb_get("/amazon/product", params)
    if status != 200:
        raise RuntimeError(
            f"ScrapingBee /amazon/product HTTP {status}: {str(body)[:300]}"
        )
    _record_spend(5)
    buybox_raw = body.get("buybox")
    buybox = _as_dict(buybox_raw)
    return _json({
        "asin": asin,
        "domain": loc.get("domain"),
        "zip_code": loc.get("zip_code"),
        "buybox": {
            "price": _to_decimal_price(buybox.get("price")),
            "currency": buybox.get("currency"),
            "seller": buybox.get("seller") or buybox.get("seller_name"),
            "is_fba": buybox.get("is_fba"),
            "stock": buybox.get("stock") or buybox.get("availability"),
        },
        "other_offers": body.get("other_offers"),
        "raw_buybox": buybox_raw,
        "credits_consumed_this_call": 5,
        "credits_consumed_this_process": _credits_spent_this_run,
    })


@mcp.tool(
    name="search_keyword",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def search_keyword(
    keyword: str,
    country: str = "us",
    zip_code: Optional[str] = None,
    sort_by: str = "featured",
    max_pages: int = 1,
) -> str:
    """
    Live Amazon Search results for a keyword. Returns ranked results
    separated into organic vs sponsored — never conflate them.

    Args:
        keyword: Search phrase.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP for US localization.
        sort_by: One of 'featured', 'most_recent', 'price_low_to_high',
            'price_high_to_low', 'average_review', 'bestsellers'.
        max_pages: Pages to fetch (default 1, cap 5).

    Credit cost: 5 per page (so max_pages=3 costs 15).
    """
    valid_sort = {
        "featured", "most_recent", "price_low_to_high",
        "price_high_to_low", "average_review", "bestsellers",
    }
    if sort_by not in valid_sort:
        raise ValueError(f"sort_by must be one of {sorted(valid_sort)}")
    max_pages = max(1, min(int(max_pages), 5))
    cost = 5 * max_pages
    _check_budget(cost)

    loc = _build_localization(country, zip_code)
    params: dict[str, Any] = {
        "query": keyword,
        "sort_by": sort_by,
        "pages": max_pages,
        **loc,
    }
    status, body = await _sb_get("/amazon/search", params, timeout=120.0)
    if status != 200:
        raise RuntimeError(
            f"ScrapingBee /amazon/search HTTP {status}: {str(body)[:300]}"
        )
    _record_spend(cost)

    products = body.get("products") or []
    organic: list[dict] = []
    sponsored: list[dict] = []
    organic_rank = 0
    for p in products:
        is_sponsored = bool(p.get("is_sponsored") or p.get("sponsored") or p.get("ad"))
        row = {
            "asin": p.get("asin"),
            "title": p.get("title"),
            "price": _to_decimal_price(p.get("price")),
            "currency": p.get("currency"),
            "rating": p.get("rating"),
            "reviews_count": p.get("reviews_count"),
            "url": p.get("url"),
            "image": (p.get("images") or [None])[0] if isinstance(p.get("images"), list) else p.get("image"),
        }
        if is_sponsored:
            sponsored.append(row)
        else:
            organic_rank += 1
            row["organic_rank"] = organic_rank
            organic.append(row)

    return _json({
        "keyword": keyword,
        "domain": loc.get("domain"),
        "zip_code": loc.get("zip_code"),
        "sort_by": sort_by,
        "pages_fetched": max_pages,
        "organic_count": len(organic),
        "sponsored_count": len(sponsored),
        "organic": organic,
        "sponsored": sponsored,
        "refinements": body.get("refinements"),
        "credits_consumed_this_call": cost,
        "credits_consumed_this_process": _credits_spent_this_run,
    })


@mcp.tool(
    name="get_bestsellers",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_bestsellers(
    keyword_or_category: str,
    country: str = "us",
    zip_code: Optional[str] = None,
    max_pages: int = 1,
) -> str:
    """
    Top ASINs via /amazon/search with sort_by='bestsellers'. Useful for
    competitive-set discovery.

    Args:
        keyword_or_category: Search phrase to bestseller-sort, e.g.
            "manuka honey" or "cough drops".
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP for US localization.
        max_pages: Pages to fetch (default 1, cap 5).

    Credit cost: 5 per page.
    """
    return await search_keyword(
        keyword=keyword_or_category,
        country=country,
        zip_code=zip_code,
        sort_by="bestsellers",
        max_pages=max_pages,
    )


@mcp.tool(
    name="get_reviews",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_reviews(
    asin: str,
    country: str = "us",
    zip_code: Optional[str] = None,
) -> str:
    """
    Pull review text + metadata + 5-star distribution for an ASIN.

    Returns the ~8 "top reviews" that Amazon surfaces inline on the
    product page (typically highest-helpful-vote across all stars).
    Plenty for sentiment / theme analysis on a single ASIN; for broader
    coverage, call across your competitive set and aggregate.

    Why only 8 instead of the spec's 100+:
      Amazon's full /product-reviews/{ASIN} page requires login from any
      ScrapingBee proxy IP (confirmed: HTTP 500 with "Redirected to
      login" even on premium_proxy AND stealth_proxy). The reliable,
      cheap path is to surface the reviews already returned inline by
      /amazon/product. If you need deep review pulls, that's a future
      Oxylabs / authenticated-session integration.

    Args:
        asin: 10-character Amazon ASIN.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP for US localization (defaults to
            SCRAPINGBEE_DEFAULT_ZIP).

    Credit cost: 5. (Same call as get_product — agents can call this
    OR get_product depending on what they need.)
    """
    _check_budget(5)
    loc = _build_localization(country, zip_code)
    params: dict[str, Any] = {"query": asin, **loc}
    status, body = await _sb_get("/amazon/product", params, timeout=90.0)
    if status != 200:
        raise RuntimeError(
            f"ScrapingBee /amazon/product HTTP {status} for ASIN {asin}: {str(body)[:300]}"
        )
    _record_spend(5)

    raw_reviews = body.get("reviews") or []
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for r in raw_reviews:
        rid = r.get("id")
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        body_text = _strip_html(r.get("content"))
        normalized.append({
            "rating": r.get("rating"),
            "title": _strip_html(r.get("title")),
            "body": body_text,
            "date": _parse_iso_date_loose(r.get("timestamp")),
            "verified_purchase": r.get("is_verified"),
            "helpful_votes": r.get("helpful_count") or 0,
            "variant": r.get("product_attributes"),
            "reviewer_name": r.get("author"),
            "review_id": rid,
        })

    return _json({
        "asin": asin,
        "domain": loc.get("domain"),
        "reviews_count_total": body.get("reviews_count"),
        "rating_overall": body.get("rating"),
        "rating_stars_distribution": body.get("rating_stars_distribution"),
        "reviews_returned": len(normalized),
        "reviews": normalized,
        "note": (
            "Only the ~8 top reviews Amazon surfaces inline are returned; "
            "the full /product-reviews/ page requires login on Amazon and "
            "isn't accessible via ScrapingBee proxies. For deeper review "
            "pulls across many ASINs, use this tool repeatedly."
        ),
        "credits_consumed_this_call": 5,
        "credits_consumed_this_process": _credits_spent_this_run,
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
        logger.info("ScrapingBee MCP server ready on port %d", PORT)
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
