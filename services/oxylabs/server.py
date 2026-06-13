"""
Oxylabs MCP Server
==================
The Oxylabs arm of the A/B test against the ScrapingBee MCP — same job,
same data layer (live competitor content + SERP), drop-in swappable
output shapes so the two can be compared on identical inputs.

This MCP exposes the same tool surface as ScrapingBee (get_product,
get_reviews, search_keyword, get_offers, get_bestsellers,
get_account_usage) plus the Oxylabs-only bonus tool ``get_seller``.

Stack: Python + FastMCP, calling Oxylabs' synchronous Realtime endpoint
``https://realtime.oxylabs.io/v1/queries`` with HTTP Basic auth.
``parse: true`` is always sent — Oxylabs' structured-JSON parsing is
the main reason to prefer them over hand-rolling AI extraction.

Account tier note (verified via probe on trackiq_5OWSJ):
  Available sources:    amazon_product, amazon_search, amazon_pricing,
                        amazon_sellers, amazon_bestsellers.
  NOT available:        amazon_reviews, amazon_questions.

The reviews tier-gate forces a pivot — ``get_reviews`` uses
``amazon_product``'s inline reviews (same ~8 top reviews + 5-star
distribution that ScrapingBee returns). This is also a notable
SPLIT-TEST RESULT: neither provider delivers the spec's ``100+``
review target on these credentials.

Env vars:
  MCP_API_KEY                  required
  OXYLABS_USERNAME             required
  OXYLABS_PASSWORD             required
  OXYLABS_DEFAULT_DOMAIN       optional; default 'com' (US)
  OXYLABS_DEFAULT_GEO          optional; default '30301' (Atlanta — keep
                                 weekly competitor prices comparable)
  OXYLABS_RESULT_BUDGET        optional; soft per-process result cap
  OXYLABS_CACHE_PATH           optional; default '/tmp/oxylabs-cache.db'
  OXYLABS_CACHE_TTL_SECONDS    optional; default 604800 (7d)
  PORT                         optional, defaults to 8016
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
logger = logging.getLogger("oxylabs_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
OXY_USER: str = os.environ["OXYLABS_USERNAME"]
OXY_PASS: str = os.environ["OXYLABS_PASSWORD"]
OXY_DEFAULT_DOMAIN: str = os.getenv("OXYLABS_DEFAULT_DOMAIN", "com").lower()
OXY_DEFAULT_GEO: str = os.getenv("OXYLABS_DEFAULT_GEO", "30301")
OXY_RESULT_BUDGET: Optional[int] = (
    int(os.environ["OXYLABS_RESULT_BUDGET"])
    if os.getenv("OXYLABS_RESULT_BUDGET")
    else None
)
OXY_CACHE_PATH: str = os.getenv("OXYLABS_CACHE_PATH", "/tmp/oxylabs-cache.db")
OXY_CACHE_TTL: int = int(os.getenv("OXYLABS_CACHE_TTL_SECONDS", "604800"))
PORT: int = int(os.getenv("PORT", "8016"))

OXY_REALTIME_URL = "https://realtime.oxylabs.io/v1/queries"

# Country-code -> Amazon domain TLD (matches ScrapingBee's mapping)
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


# --- Results accounting (Oxylabs bills per successful result) ----------------

_results_spent_this_run: int = 0


def _check_budget(estimated_results: int) -> None:
    if OXY_RESULT_BUDGET is None:
        return
    if _results_spent_this_run + estimated_results > OXY_RESULT_BUDGET:
        raise RuntimeError(
            f"Oxylabs result budget exceeded. This call would consume "
            f"~{estimated_results} results; this process has already "
            f"spent {_results_spent_this_run} of {OXY_RESULT_BUDGET}. "
            f"Raise OXYLABS_RESULT_BUDGET or wait for the process to restart."
        )


def _record_spend(results: int) -> None:
    global _results_spent_this_run
    _results_spent_this_run += results


# --- Normalization helpers (same shapes as ScrapingBee for parity) -----------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: Optional[str]) -> Optional[str]:
    if not s:
        return s
    no_tags = _HTML_TAG_RE.sub(" ", s)
    return html_mod.unescape(no_tags).strip()


def _to_decimal_price(p: Any) -> Optional[float]:
    if p is None or p == "" or p == "-1" or p == 0:
        return None
    try:
        f = float(p)
        return None if f <= 0 else round(f, 2)
    except (TypeError, ValueError):
        return None


def _parse_iso_date_loose(s: Any) -> Optional[str]:
    """Coax arbitrary date strings to ISO YYYY-MM-DD. Same handler as
    ScrapingBee MCP — Oxylabs returns the same Amazon-style timestamps."""
    if not s:
        return None
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    m = re.search(r"([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})", s)
    if m:
        try:
            return dt.datetime.strptime(
                f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y"
            ).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return s


def _as_dict(x: Any) -> dict:
    """Defensive list/dict coercion — Oxylabs returns buybox/delivery as
    lists (same as ScrapingBee). Take the first dict entry."""
    if isinstance(x, dict):
        return x
    if isinstance(x, list) and x and isinstance(x[0], dict):
        return x[0]
    return {}


# --- Cache (SQLite kv) -------------------------------------------------------


def _cache_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(OXY_CACHE_PATH, isolation_level=None, timeout=10)
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
    return hashlib.sha1("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()


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
            (key, int(time.time()), int(ttl or OXY_CACHE_TTL), json.dumps(value, default=str)),
        )
        conn.close()
    except Exception as e:
        logger.warning("cache_set failed: %s", e)


# --- Oxylabs HTTP client -----------------------------------------------------


async def _oxy_post(payload: dict[str, Any], *, timeout: float = 120.0) -> tuple[int, Any]:
    """POST to /v1/queries with Basic auth. Returns (status_code, json_or_text)."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(
            OXY_REALTIME_URL,
            auth=(OXY_USER, OXY_PASS),
            json=payload,
        )
    ct = r.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, r.text
    return r.status_code, r.text


def _build_payload(
    source: str,
    *,
    query: Optional[str] = None,
    url: Optional[str] = None,
    country: Optional[str] = None,
    zip_code: Optional[str] = None,
    pages: Optional[int] = None,
    start_page: Optional[int] = None,
    context: Optional[list] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {"source": source, "parse": True}
    if query is not None:
        payload["query"] = query
    if url is not None:
        payload["url"] = url
    payload["domain"] = _amazon_domain(country or OXY_DEFAULT_DOMAIN)
    # geo_location applies for matching-country runs; Oxylabs accepts ZIP for US
    geo = zip_code or OXY_DEFAULT_GEO
    if geo:
        payload["geo_location"] = geo
    if pages is not None:
        payload["pages"] = pages
    if start_page is not None:
        payload["start_page"] = start_page
    if context:
        payload["context"] = context
    return payload


def _first_content(body: Any) -> dict:
    """Pull the structured content out of Oxylabs' {results: [{content: {...}}]} envelope."""
    if not isinstance(body, dict):
        raise RuntimeError(f"Unexpected Oxylabs response shape: {type(body).__name__}")
    if "message" in body and "results" not in body:
        raise RuntimeError(f"Oxylabs error: {body['message']}")
    results = body.get("results") or []
    if not results:
        raise RuntimeError("Oxylabs returned an empty results list")
    return results[0].get("content") or {}


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "oxylabs_mcp",
    instructions=(
        "Tools for live Amazon competitor content via Oxylabs Web Scraper API. "
        "Drop-in swappable with the ScrapingBee MCP — same tool names, "
        "same input/output shapes, so the two can be A/B compared on "
        "identical inputs. Default marketplace is US (amazon.com); pass "
        "country='uk', 'de', etc. for others. Each call consumes Oxylabs "
        "result-credits; call get_account_usage to see per-process spend."
    ),
)


# --- Tools (parity set 1-6, then bonus 7) ------------------------------------


@mcp.tool(
    name="get_account_usage",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_account_usage() -> str:
    """
    Per-process result-consumption counter.

    Oxylabs does not expose a public usage / remaining-balance endpoint
    (we probed; nothing under /v1/usage, /v1/stats, /v1/account). Account
    totals must be checked at https://dashboard.oxylabs.io. This tool
    reports only what THIS process has spent since boot, plus the budget
    cap if set.
    """
    return _json({
        "ok": True,
        "this_process_results_spent": _results_spent_this_run,
        "this_process_result_budget": OXY_RESULT_BUDGET,
        "note": (
            "Oxylabs has no public usage endpoint — check "
            "https://dashboard.oxylabs.io for the account-level balance. "
            "This counter only tracks the current service process."
        ),
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
    Listing snapshot for an ASIN — same output shape as the ScrapingBee MCP
    so the two are drop-in swappable.

    Returns title, bullets, price, rating, review count, images, category,
    buy-box (price/seller/FBA/stock), delivery, coupon, BSR ladder, plus
    Oxylabs-only bonus fields ``buy_it_with``, ``frequently_bought_together``,
    ``price_per_unit``.

    Args:
        asin: 10-character Amazon ASIN.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP (defaults to OXYLABS_DEFAULT_GEO).
        include_aplus: Reserved for parity with ScrapingBee. Oxylabs' native
            parse already returns A+ content inline (description /
            bullet_points) for most ASINs — flag is accepted but no-op.
    """
    _check_budget(1)
    cache_key = _cache_key("product", asin, country, zip_code or OXY_DEFAULT_GEO)
    cached = cache_get(cache_key)
    if cached is not None:
        cached["_from_cache"] = True
        return _json(cached)

    payload = _build_payload(
        "amazon_product", query=asin, country=country, zip_code=zip_code
    )
    status, body = await _oxy_post(payload)
    if status != 200:
        raise RuntimeError(
            f"Oxylabs amazon_product HTTP {status} for ASIN {asin}: {str(body)[:300]}"
        )
    _record_spend(1)
    content = _first_content(body)
    out = _normalize_product(content)
    out["credits_consumed_this_call"] = 1
    out["credits_consumed_this_process"] = _results_spent_this_run
    cache_set(cache_key, out)
    return _json(out)


def _normalize_product(c: dict) -> dict:
    """Map Oxylabs amazon_product content -> same shape as ScrapingBee's."""
    buybox_raw = c.get("buybox")
    delivery_raw = c.get("delivery")
    buybox = _as_dict(buybox_raw)
    delivery = _as_dict(delivery_raw)
    sales_rank = c.get("sales_rank") or []
    if not isinstance(sales_rank, list):
        sales_rank = []
    bsr_primary = sales_rank[0] if sales_rank else None

    return {
        "asin": c.get("asin"),
        "url": c.get("url"),
        "title": c.get("title") or c.get("product_name"),
        "brand": c.get("brand"),
        "description": c.get("description"),
        "bullet_points": c.get("bullet_points"),
        "category": c.get("category"),
        "price": _to_decimal_price(c.get("price")),
        "currency": c.get("currency"),
        "rating": c.get("rating"),
        "reviews_count": c.get("reviews_count"),
        "answered_questions_count": c.get("answered_questions_count"),
        "images": c.get("images"),
        "buybox": {
            "price": _to_decimal_price(buybox.get("price")),
            "currency": buybox.get("currency"),
            "seller": buybox.get("seller") or buybox.get("seller_name") or buybox.get("name"),
            "is_fba": buybox.get("is_fba") or buybox.get("is_amazon_fulfilled"),
            "stock": buybox.get("stock") or buybox.get("availability"),
        },
        "buybox_raw": buybox_raw,
        "coupon": c.get("coupon"),
        "coupon_discount_percentage": c.get("discount_percentage"),
        "deal_type": c.get("deal_type"),
        "delivery": {
            "delivery_date": delivery.get("delivery_date") or delivery.get("date"),
            "fastest_delivery": delivery.get("fastest_delivery"),
            "from": delivery.get("from") or delivery.get("origin"),
        },
        "delivery_raw": delivery_raw,
        "bsr_primary": bsr_primary,
        "sales_rank_ladder": sales_rank,
        # Oxylabs-only bonus fields (kept under a label so parity comparison stays clean)
        "_oxylabs_bonus": {
            "buy_it_with": c.get("buy_it_with"),
            "frequently_bought_together": c.get("frequently_bought_together"),
            "price_per_unit": c.get("price_per_unit"),
            "price_sns": c.get("price_sns"),
            "rating_stars_distribution": c.get("rating_stars_distribution"),
            "featured_merchant": c.get("featured_merchant"),
        },
    }


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
    Buy-box snapshot via Oxylabs' dedicated amazon_pricing source.

    Unlike the ScrapingBee MCP (which projects buybox from the product
    response), Oxylabs has a separate offer-listing endpoint with
    structured per-offer detail. This returns the buybox + every other
    offer (seller, price, condition, FBA flag).

    Args:
        asin: 10-character Amazon ASIN.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP.
    """
    _check_budget(1)
    payload = _build_payload(
        "amazon_pricing", query=asin, country=country, zip_code=zip_code
    )
    status, body = await _oxy_post(payload)
    if status != 200:
        raise RuntimeError(
            f"Oxylabs amazon_pricing HTTP {status}: {str(body)[:300]}"
        )
    _record_spend(1)
    content = _first_content(body)
    # The pricing source returns a list of offers under various keys depending
    # on the page state — preserve raw and project the cheapest/featured.
    offers = content.get("pricing") or content.get("offers") or []
    return _json({
        "asin": asin,
        "domain": payload.get("domain"),
        "geo_location": payload.get("geo_location"),
        "offers_count": len(offers) if isinstance(offers, list) else None,
        "offers": offers,
        "raw_content_keys": sorted(content.keys()),
        "credits_consumed_this_call": 1,
        "credits_consumed_this_process": _results_spent_this_run,
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
    Live Amazon search results — organic / sponsored / amazons_choices
    cleanly separated. Same output shape as ScrapingBee for parity.

    Oxylabs returns the result groups already pre-segmented (no manual
    sponsored-detection like ScrapingBee).

    Args:
        keyword: Search phrase.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP.
        sort_by: One of 'featured', 'most_recent', 'price_low_to_high',
            'price_high_to_low', 'average_review', 'bestsellers'.
        max_pages: Pages to fetch (default 1, cap 5). Each page is one result.
    """
    valid_sort = {
        "featured", "most_recent", "price_low_to_high",
        "price_high_to_low", "average_review", "bestsellers",
    }
    if sort_by not in valid_sort:
        raise ValueError(f"sort_by must be one of {sorted(valid_sort)}")
    max_pages = max(1, min(int(max_pages), 5))
    _check_budget(max_pages)

    context = []
    if sort_by and sort_by != "featured":
        context.append({"key": "sort_by", "value": sort_by})

    payload = _build_payload(
        "amazon_search",
        query=keyword,
        country=country,
        zip_code=zip_code,
        pages=max_pages,
        context=context or None,
    )
    status, body = await _oxy_post(payload, timeout=180.0)
    if status != 200:
        raise RuntimeError(f"Oxylabs amazon_search HTTP {status}: {str(body)[:300]}")
    _record_spend(max_pages)
    content = _first_content(body)
    groups = content.get("results") or {}
    if not isinstance(groups, dict):
        groups = {}

    def _project_row(p: dict, idx: int) -> dict:
        return {
            "asin": p.get("asin"),
            "title": p.get("title"),
            "price": _to_decimal_price(p.get("price")),
            "currency": p.get("currency"),
            "rating": p.get("rating"),
            "reviews_count": p.get("reviews_count"),
            "url": p.get("url"),
            "image": (p.get("images") or [None])[0] if isinstance(p.get("images"), list) else p.get("image"),
            "organic_rank": idx,
            "is_amazons_choice": p.get("is_amazons_choice"),
            "best_seller": p.get("best_seller"),
            "is_prime": p.get("is_prime"),
        }

    organic_list = groups.get("organic") or []
    paid_list = groups.get("paid") or []
    choices_list = groups.get("amazons_choices") or []

    organic = [_project_row(p, i + 1) for i, p in enumerate(organic_list)]
    sponsored = [_project_row(p, i + 1) for i, p in enumerate(paid_list)]
    amazons_choices = [_project_row(p, i + 1) for i, p in enumerate(choices_list)]

    return _json({
        "keyword": keyword,
        "domain": payload.get("domain"),
        "zip_code": payload.get("geo_location"),
        "sort_by": sort_by,
        "pages_fetched": max_pages,
        "organic_count": len(organic),
        "sponsored_count": len(sponsored),
        "organic": organic,
        "sponsored": sponsored,
        "amazons_choices": amazons_choices,
        "total_results_count": content.get("total_results_count"),
        "last_visible_page": content.get("last_visible_page"),
        "refinements": content.get("refinements"),
        "credits_consumed_this_call": max_pages,
        "credits_consumed_this_process": _results_spent_this_run,
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
    Top ASINs via Oxylabs' dedicated amazon_bestsellers source.

    Better than ScrapingBee's wrap-around-search approach — this hits
    Amazon's actual bestsellers listing. Accepts a category node ID
    (e.g. '3761311' for Cough Drops) OR a keyword (e.g. 'cough drops').

    Args:
        keyword_or_category: Amazon category-node ID or search phrase.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP.
        max_pages: Pages (default 1, cap 5).
    """
    max_pages = max(1, min(int(max_pages), 5))
    _check_budget(max_pages)
    payload = _build_payload(
        "amazon_bestsellers",
        query=keyword_or_category,
        country=country,
        zip_code=zip_code,
        pages=max_pages,
    )
    status, body = await _oxy_post(payload, timeout=120.0)
    if status != 200:
        raise RuntimeError(
            f"Oxylabs amazon_bestsellers HTTP {status}: {str(body)[:300]}"
        )
    _record_spend(max_pages)
    content = _first_content(body)
    return _json({
        "query": keyword_or_category,
        "domain": payload.get("domain"),
        "results": content.get("results") or content,
        "credits_consumed_this_call": max_pages,
        "credits_consumed_this_process": _results_spent_this_run,
    })


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
    Returns the ~8 top reviews Amazon surfaces inline on the product page,
    plus the 5-star distribution.

    Why not the spec's 100+ reviews:
      Oxylabs has an amazon_reviews source, but it's tier-gated on this
      account (verified: HTTP 400 ``Source amazon_reviews is not
      available``). This is the same effective ceiling ScrapingBee hit
      (Amazon's /product-reviews/ login wall). The pragmatic v1 path on
      both providers is to surface the inline product-page reviews.

    Output shape matches the ScrapingBee MCP exactly so the two are
    drop-in swappable.

    Args:
        asin: 10-character Amazon ASIN.
        country: Marketplace code, default 'us'.
        zip_code: Optional ZIP.
    """
    _check_budget(1)
    payload = _build_payload(
        "amazon_product", query=asin, country=country, zip_code=zip_code
    )
    status, body = await _oxy_post(payload)
    if status != 200:
        raise RuntimeError(
            f"Oxylabs amazon_product HTTP {status} for ASIN {asin}: {str(body)[:300]}"
        )
    _record_spend(1)
    content = _first_content(body)

    raw_reviews = content.get("reviews") or []
    normalized: list[dict] = []
    seen_ids: set[str] = set()
    for r in raw_reviews:
        rid = r.get("id")
        if rid and rid in seen_ids:
            continue
        if rid:
            seen_ids.add(rid)
        normalized.append({
            "rating": r.get("rating"),
            "title": _strip_html(r.get("title")),
            "body": _strip_html(r.get("content") or r.get("body")),
            "date": _parse_iso_date_loose(r.get("timestamp") or r.get("date")),
            "verified_purchase": r.get("is_verified") or r.get("verified_purchase"),
            "helpful_votes": r.get("helpful_count") or r.get("helpful_votes") or 0,
            "variant": r.get("product_attributes") or r.get("variant"),
            "reviewer_name": r.get("author") or r.get("reviewer_name"),
            "review_id": rid,
        })

    return _json({
        "asin": asin,
        "domain": payload.get("domain"),
        "reviews_count_total": content.get("reviews_count"),
        "rating_overall": content.get("rating"),
        "rating_stars_distribution": content.get("rating_stars_distribution"),
        "reviews_returned": len(normalized),
        "reviews": normalized,
        "note": (
            "Only the ~8 inline top reviews are returned; the dedicated "
            "amazon_reviews source is tier-gated on this account. For "
            "deeper review pulls, upgrade the Oxylabs plan or route via "
            "an authenticated SP-API session."
        ),
        "credits_consumed_this_call": 1,
        "credits_consumed_this_process": _results_spent_this_run,
    })


# --- Bonus tool (Oxylabs-only — outside the parity set) ----------------------


@mcp.tool(
    name="get_seller",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_seller(
    seller_id: str,
    country: str = "us",
) -> str:
    """
    BONUS (Oxylabs-only): Get seller storefront info via amazon_sellers.

    Track a competitor seller's product list, ratings, business details.
    Not part of the ScrapingBee parity set.

    Args:
        seller_id: Amazon seller ID (e.g. 'A2L77EE7U53NWQ' = Amazon.com).
        country: Marketplace code, default 'us'.
    """
    _check_budget(1)
    payload = _build_payload("amazon_sellers", query=seller_id, country=country)
    status, body = await _oxy_post(payload)
    if status != 200:
        raise RuntimeError(
            f"Oxylabs amazon_sellers HTTP {status}: {str(body)[:300]}"
        )
    _record_spend(1)
    content = _first_content(body)
    return _json({
        "seller_id": seller_id,
        "domain": payload.get("domain"),
        "content": content,
        "credits_consumed_this_call": 1,
        "credits_consumed_this_process": _results_spent_this_run,
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
        logger.info("Oxylabs MCP server ready on port %d", PORT)
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
