# MCP Gateway

## Project Overview

Multi-service MCP (Model Context Protocol) gateway that gives Claude (CoWork and Code) standardized access to personal finance, task management, analytics, dev-ops, weather, marketplace, and design-tooling services. Acts as a unified reverse proxy in front of specialized microservices, all running in a single Docker container on DigitalOcean App Platform.

**Domain**: `mcp.bibager.com` (primary), with subdomains per service.
**Deployment**: Single Docker container with Caddy reverse proxy + Supervisor process management.

## Architecture

```
Internet → Caddy (port 8080) → Service routing by host/path
                ├── monarch.bibager.com  → Monarch Money     (port 8000, Python)
                ├── todoist.bibager.com  → Todoist           (port 8001, Python)
                ├── mcp.bibager.com      → Google Analytics  (port 8002, Python)
                ├── gitlab.bibager.com   → GitLab repo MCP   (port 8003, Python)
                ├── weather.bibager.com  → Weather           (port 8004, Python)
                ├── trackiq.bibager.com  → TrackIQ proxy     (port 8005, Python)
                ├── framer.bibager.com   → Framer MCP        (port 8007 frontend → 8006 sidecar)
                │                             Python frontend ─▶ Node 22 sidecar ─▶ Framer Server API
                ├── pacvue.bibager.com   → Pacvue proxy      (port 8008, Python)
                ├── alpaca.bibager.com   → Alpaca trading    (port 8009 proxy → 8010 alpaca-mcp-server, Python)
                │                             Bearer-guarded proxy ─▶ localhost-only FastMCP ─▶ Alpaca API
                ├── ta.bibager.com       → Technical analysis (port 8011, Python)
                │                             Pivots, VWAP, Volume Profile computed locally from Alpaca data
                ├── uw.bibager.com       → Unusual Whales     (port 8012, Python)
                │                             HTTP-streaming proxy ─▶ api.unusualwhales.com/api/mcp
                ├── datarova.bibager.com → Datarova rank tracker (port 8013, Python)
                │                             Cognito refresh-token flow ─▶ api.datarova.com
                ├── keepa.bibager.com    → Keepa historical data (port 8014, Python)
                │                             keepa PyPI lib wrapper ─▶ api.keepa.com
                └── scrapingbee.bibager.com → ScrapingBee Amazon content (port 8015, Python)
                                              httpx ─▶ app.scrapingbee.com/api/v1/amazon/*
```

### Key Files

```
├── Caddyfile              # Reverse proxy routing rules
├── Dockerfile             # Python 3.12-slim + Node 22 + Caddy + Supervisor
├── docker-compose.yml     # Single service, port 8080, .env file
├── supervisord.conf       # Process management (14 programs: caddy + 11 services + framer's 2 + alpaca's 2)
├── .env.example           # Environment variable template
├── docs/plans/            # Design + implementation plans (Framer build, etc.)
└── services/
    ├── monarch/server.py
    ├── todoist/server.py
    ├── ga/                # Google Analytics
    │   ├── server.py
    │   ├── auth.py
    │   └── search_console.py
    ├── gitlab/server.py
    ├── weather/server.py  # Open-Meteo wrapper, no API key
    ├── trackiq/server.py  # HTTP-streaming proxy to app.trackiq.com/mcp
    ├── framer/            # Polyglot — Python frontend + Node sidecar
    │   ├── server.py      # Python FastMCP frontend (auth, OAuth, MCP tools)
    │   ├── src/sidecar.ts # Node sidecar holding the framer-api WebSocket
    │   ├── package.json
    │   ├── tsconfig.json
    │   └── requirements.txt
    ├── pacvue/server.py   # HTTP-streaming proxy to mcp.pacvue.com/mcp
    ├── alpaca/server.py   # Bearer-guarded proxy to localhost alpaca-mcp-server (live trading)
    ├── ta/server.py       # Technical analysis (pivots, VWAP, volume profile) over Alpaca data
    ├── uw/server.py       # HTTP-streaming proxy to api.unusualwhales.com/api/mcp
    ├── datarova/server.py # Datarova rank tracker (Cognito refresh-token flow)
    ├── keepa/server.py    # Keepa historical data (price/BSR/buybox time series)
    └── scrapingbee/server.py # ScrapingBee Amazon: product, search, reviews, offers
```

## Services

### Monarch Money (port 8000)
- **Purpose**: Personal finance — accounts, transactions, budgets, net worth, investments
- **Auth**: `MONARCH_TOKEN` env var OR email/password + optional TOTP MFA
- **MCP Tools**: 12 tools (get_accounts, get_transactions, get_cashflow, get_budgets, etc.)
- **Protocols**: FastMCP `/mcp` + REST `/api/*` (n8n compatible)
- **OAuth**: Synthetic — auto-approves, returns `MCP_API_KEY` as access token

### Todoist (port 8001)
- **Purpose**: Task management — tasks, projects, sections, labels, comments
- **Auth**: Todoist OAuth 2.0 flow at `/todoist/auth`, fallback to `TODOIST_API_TOKEN` env var
- **MCP Tools**: 12+ tools (CRUD tasks, projects, sections, labels, comments)
- **Protocols**: FastMCP `/mcp`
- **OAuth**: Two-layer — synthetic MCP OAuth + real Todoist OAuth via `/todoist/auth`

### Google Analytics (port 8002)
- **Purpose**: GA4 analytics + Google Search Console
- **Auth**: Full Google OAuth 2.0 with PKCE + email allowlist (`ALLOWED_EMAIL`)
- **MCP Tools**: 9 tools (reports, realtime, search console, URL inspection)
- **Protocols**: FastMCP `/mcp`
- **OAuth**: Full OAuth 2.0 with token refresh, 1-hour access token expiry

### GitLab (port 8003)
- **Purpose**: Repository monitoring — commits, merge requests, pipelines, issues, file browsing
- **Auth**: `GITLAB_TOKEN` env var (PAT with `read_api` + `read_repository`)
- **Default Project**: `trackio1/track-app` (URL-encoded via `GITLAB_PROJECT_ID`)
- **MCP Tools**: 11 read-only tools
- **Protocols**: FastMCP `/mcp`
- **OAuth**: Synthetic

### Weather (port 8004)
- **Purpose**: Forecasts and daily summaries (sunrise/sunset, hourly temps, highs/lows, precipitation)
- **Auth**: None for upstream (Open-Meteo is keyless); `MCP_API_KEY` for the gateway
- **Data sources**: `api.open-meteo.com` + `geocoding-api.open-meteo.com`
- **MCP Tools**: 2 (`get_daily_summary`, `get_forecast`)
- **OAuth**: Synthetic

### TrackIQ (port 8005)
- **Purpose**: Amazon advertising/marketplace analytics for the Manuka Doctor brand
- **Auth**: `TRACKIQ_API_KEY` env var (Bearer, injected server-side)
- **Architecture**: Transparent HTTP-streaming proxy to `app.trackiq.com/mcp` — we just rewrite `Authorization`. The MCP session lives directly between Claude and TrackIQ.
- **MCP Tools**: 16 (forwarded from TrackIQ — `list_marketplaces`, `get_campaigns`, `get_search_terms`, `get_dsp_campaigns`, etc.)
- **OAuth**: Synthetic at our side

### Framer (port 8007 frontend → port 8006 sidecar)
- **Purpose**: Build native Framer pages from HTML/structured input via the Framer Server API
- **Auth**: `FRAMER_API_KEY` (project-scoped Server API key, set in Framer Site Settings); `FRAMER_PROJECT_URL` points at the target project
- **Architecture**: First polyglot service. Python FastMCP frontend at 8007 handles MCP/OAuth/auth; forwards each tool call over `localhost:8006` to a Node 22 sidecar holding the long-lived `framer-api@0.1.7` WebSocket connection. `SIDECAR_INTERNAL_KEY` guards the localhost RPC.
- **MCP Tools (46)**:
  - **Read / inspect**: `get_current_page`, `get_node`, `get_children`, `get_parent`, `get_rect`, `get_nodes_with_type`, `get_project_info`, `get_publish_info`
  - **Pages**: `create_web_page(path)`, `create_design_page(name)`, `clone_web_page(node_id)`, `clone_node(node_id)`
  - **Layout**: `create_frame(attributes, parent_id?)`, `create_text_node(text/attributes, parent_id?)`
  - **Mutate**: `set_attributes(node_id, attributes)`, `set_text(node_id, text)`, `set_parent(node_id, parent_id, index?)`, `delete_node(node_id)`
  - **Images**: `upload_image(url)`, `set_frame_image(node_id, image_url)`
  - **Design system**: `get_color_styles`, `create_color_style`, `get_text_styles`, `create_text_style`, `get_fonts`, `get_font(family, weight?, style?)`
  - **Site settings**: `add_redirects([{from, to, expandToAllLocales?}])`, `set_custom_code({location, html|null})`
  - **Visual feedback**: `screenshot(node_id, format?, scale?)` (PNG/JPEG bytes base64), `export_svg(node_id)`
  - **Locales**: `get_locales`, `get_default_locale`, `get_active_locale` (latter returns 501 — plugin-only in `framer-api@0.1.7`)
  - **Code files** (React components / overrides): `create_code_file(name, code)`, `get_code_files`, `get_code_file(id)`
  - **CMS**: `get_collections`, `get_collection(id)`, `get_collection_fields(collection_id)`, `get_collection_items(collection_id)`, `create_collection(name)`, `add_collection_fields(collection_id, fields)`, `add_collection_items(collection_id, items)`, `remove_collection_items(collection_id, item_ids)` — drives blog posts, products, portfolio etc.
  - **Ship**: `publish()`, `deploy(deployment_id)`
- **OAuth**: Synthetic

### Pacvue (port 8008)
- **Purpose**: Amazon/Walmart/Instacart/Kroger/DoorDash/Sam's/Target/Criteo/Citrus/Chewy/Bol retail-media reporting via the Pacvue Console
- **Auth**: `PACVUE_API_KEY` env var (raw `pv_…` token, NO `Bearer` prefix — Pacvue spec is `Authorization: pv_<token>`)
- **Architecture**: Transparent HTTP-streaming proxy to `mcp.pacvue.com/mcp` (same pattern as TrackIQ); we just rewrite `Authorization`. The MCP session lives directly between Claude and Pacvue.
- **MCP Tools**: 5 (forwarded from Pacvue) — `fetch_report_list`, `fetch_report_schema`, `fetch_materials`, `run_report`, `fetch_report_result`. Async report flow: `run_report` returns `taskId`, `fetch_report_result` polls. Hard caps: 50k rows, 24h download URL, 24h taskId, 50 active tokens, 180-day token lifetime.
- **OAuth**: Synthetic at our side

### Alpaca (port 8009 proxy → 8010 upstream)
- **Purpose**: Stocks, ETFs, crypto, and options trading via Alpaca Markets — orders, positions, watchlists, market data, portfolio history
- **Auth**: `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` env vars (read by upstream `alpaca-mcp-server` at startup); `ALPACA_PAPER_TRADE=false` selects **live** trading (real money)
- **Architecture**: Two-process polyglot pattern (both Python this time). Upstream `alpaca-mcp-server@2.0.1` runs locally on `127.0.0.1:8010` as `--transport streamable-http` — bound to localhost only because it has no built-in auth. Our Bearer-guarded proxy on `0.0.0.0:8009` enforces `MCP_API_KEY`, terminates synthetic OAuth, and forwards `/mcp` upstream. Anyone reaching `alpaca.bibager.com/mcp` directly without our Bearer is rejected at the perimeter.
- **MCP Tools**: 43 — Account (1), Positions (2), Portfolio history (1), Assets (2), Watchlist (8), Corporate actions/Calendar/Clock (3), Market data: Stock (7) / Crypto (7) / Options (3), Trading: Orders (5), Position management (3). Tool list comes verbatim from upstream — see `alpaca-mcp-server` README for signatures.
- **OAuth**: Synthetic at our side
- **⚠ Live trading**: tool calls move real money. `ALPACA_PAPER_TRADE` must be flipped to `true` to switch to the simulator.

### TA / Technical Analysis (port 8011)
- **Purpose**: Compute pivot points, session VWAP, anchored VWAP, and volume profile (POC/VAH/VAL) locally from Alpaca's bar data — same numbers as TradingView Premium indicators, no licensing or scraping involved.
- **Auth**: Reuses `ALPACA_API_KEY` + `ALPACA_SECRET_KEY` (DO env vars are app-scoped, all programs see them)
- **Architecture**: Native FastMCP service (no proxy layer). Uses `alpaca-py` SDK directly to fetch daily/minute bars, then runs pure-Python math for the indicators.
- **MCP Tools (4)**:
  - `get_pivot_points(symbol, type='standard'|'camarilla'|'woodie', session_date?)` — P, R1-R4, S1-S4 from prior session H/L/C
  - `get_session_vwap(symbol, session='regular'|'extended', session_date?, timeframe='1Min'|'5Min')` — single-session VWAP
  - `get_anchored_vwap(symbol, anchor_date, anchor_time?='09:30', end_date?, timeframe?)` — cumulative VWAP from anchor point with daily snapshots
  - `get_volume_profile(symbol, session_date?, session?, value_area_pct=70, num_buckets=50)` — POC, VAH, VAL + price-volume histogram (uniform per-bar distribution)
- **OAuth**: Synthetic at our side
- **Caveat**: Volume Profile uses minute-bar uniform distribution (good approximation; tick-data version possible later via `get_stock_trades`). Pivot computation doesn't account for US market holidays — pass an explicit `session_date` if accuracy around holidays matters.

### Unusual Whales (port 8012)
- **Purpose**: Options flow, dark pool data, congressional trades, ETF holdings, and broader institutional-flow market analysis from Unusual Whales
- **Auth**: `UW_API_KEY` env var (UUID format from `unusualwhales.com/settings/api-dashboard`); upstream expects `Authorization: Bearer <uuid>`
- **Architecture**: Transparent HTTP-streaming proxy to `https://api.unusualwhales.com/api/mcp` (mirrors TrackIQ pattern). The MCP session lives directly between Claude and Unusual Whales — we just rewrite `Authorization`. **NOTE:** the public docs URL `unusualwhales.com/public-api/mcp` is the documentation page, NOT the MCP endpoint — the real endpoint is `api.unusualwhales.com/api/mcp`.
- **MCP Tools**: forwarded from Unusual Whales (count varies by subscription tier)
- **OAuth**: Synthetic at our side

### Datarova (port 8013)
- **Purpose**: Amazon brand keyword rank tracker reverse-engineered from `app.datarova.com` (no public API exists). Surfaces the Rank Tracker (project / ASIN / keyword rank time series) and Keyword Spy / ASIN Insights cross-section. Built initially for tracking the Manuka brand on amazon.com.
- **Auth**: AWS Cognito (us-west-2 user pool `us-west-2_0CKqzWHDX`, client_id `5dcti49pggi19uqs3df8iae3o1`). We hold the long-lived refresh token in `DATAROVA_REFRESH_TOKEN` and call `cognito-idp.us-west-2.amazonaws.com` with `REFRESH_TOKEN_AUTH` on demand to mint fresh access+id JWTs. Refresh tokens last ~30 days; access/id tokens last 1 hour. Token cache keys off the access JWT's `exp` claim with a 60-second safety margin.
- **Architecture**: Native FastMCP service. Sends 3 headers per request to `api.datarova.com`: `Authorization: Bearer <access>`, `x-id-token: <id>`, and Origin/Referer spoofed to `https://app.datarova.com`. The `x-plan` header is documented but **verified NOT required** — endpoints accept requests without it.
- **Recon source**: `tools/datarova-api-recon/` Chrome extension (Manifest V3, fetch/XHR monkey-patcher) used to capture API calls. Diagnostic JSONs got us the auth model + endpoints in two passes.
- **MCP Tools (11)**:
  - **Discovery**: `get_account_summary` (plan/limits/usage), `get_latest_data_date` (when data last refreshed)
  - **Inventory**: `list_projects(search?)`, `list_keywords(marketplace)`, `list_asins(marketplace)`, `get_keyword_tags(project_id)`
  - **Rank tracker (core)**: `get_rank_history_by_asin(project_id, asin, range_type, from?, to?)`, `get_keyword_rank_history(project_id, asin, keyword, from?, to?)`
  - **Market data**: `get_keyword_market_data(keyword, asin?, top_asin?, from, to, range_type, marketplace)` — drives both Keyword Spy AND ASIN Insights
  - **Product enrichment**: `get_asin_details(asin, marketplace)` — title/price/image for any ASIN
  - **Mutation**: `add_keyword_to_project(project_id, asin, keyword, marketplace)` — counts vs `keywordsLimit`
- **ASIN Insights gotcha** (Basic tier): There is no separate "list keywords this competitor ASIN ranks for" endpoint surfaced. On Basic plan, ASIN Insights = iterating your existing tracked keywords through `/records/record` with the competitor as `topAsin`. The bulk-keywords-for-arbitrary-ASIN feature appears paywalled behind the PostHog flag `asin-insights-additional-options`.
- **OAuth**: Synthetic at our side

### Keepa (port 8014)
- **Purpose**: Amazon historical data — price/BSR (sales rank)/buy-box/rating time series and competitive discovery (product finder, deals, bestsellers, seller lookup). The historical data layer for the Manuka Doctor ad-optimization workflow.
- **Auth**: `KEEPA_API_KEY` env var (private key from `keepa.com/#!api`). Token-metered subscription (20 tokens/min at the €49/mo tier we're on).
- **Architecture**: Native FastMCP service. Built on the `keepa` PyPI package — never hand-roll the KeepaTime conversion, CSV-history index map, or token management since the lib already abstracts them. Sync Keepa API wrapped in `asyncio.to_thread`. Lazy client init so the service starts cleanly even when the subscription is "Payment Pending" (constructor pings the API).
- **Normalization (mandatory)**: All timestamps → ISO 8601 UTC; all prices → decimal dollars from integer cents; all `-1` values → null (Keepa's "no data / out of stock" sentinel). Agents NEVER see raw Keepa encodings.
- **Cache**: Simple SQLite kv store at `KEEPA_CACHE_PATH` (default `/tmp/keepa-cache.db`, TTL via `KEEPA_CACHE_TTL_SECONDS`, default 24h). Saves tokens on repeated history pulls within a single deploy. **Ephemeral on DO App Platform** — resets across deploys; persist responses externally if a durable historical baseline is needed.
- **MCP Tools (12)**:
  - **Core / history**: `get_product_snapshot(asins, domain)` — token-light current state batch (≤100 ASINs); `get_price_history(asin, price_type, start?, end?, domain)` where `price_type ∈ {amazon, new, used, buybox, fbm, fba, list_price}`; `get_sales_rank_history(asin, start?, end?, domain)` with category_node; `get_buybox_history(asin, start?, end?, domain)` price + seller (needs offers, more tokens); `get_rating_history(asin, start?, end?, domain)`; `get_product_stats(asins, days=90, domain)` cheap interval summary across many ASINs
  - **Discovery**: `find_products(filters, domain, per_page)`; `get_bestsellers(category_node, domain)`; `get_deals(filters, domain)` promo monitoring; `get_seller(seller_id, domain)`; `search_products(term, domain, per_page)`
  - **Ops**: `get_token_status()` — balance, refill rate, time-to-refill, subscription expiry
- **Domain mapping**: US=1 (default), UK=2, DE=3, FR=4, JP=5, CA=6, CN=7, IT=8, ES=9, IN=10, MX=11, BR=12, AU=13. Default US per the Manuka Doctor US Seller account; only US validated for v1.
- **OAuth**: Synthetic at our side
- **⚠ Payment Pending caveat**: At time of build the Keepa subscription showed "Payment Pending" with 0 available tokens. The service deploys and `get_token_status` returns gracefully, but tool calls that require tokens will error until the subscription activates (€49/mo for 20 tokens/min).

### ScrapingBee (port 8015)
- **Purpose**: Live, point-in-time Amazon competitor content — product listings (price/buybox/A+/coupon), live SERP rank (organic vs sponsored), and inline review text + 5-star distribution. The "live competitor-content" layer alongside Keepa (history) and SP-API (own catalog).
- **Auth**: `SCRAPINGBEE_API_KEY` env (1000 credits/mo on the free tier we're on; renewal date in `get_account_usage` response).
- **Architecture**: Native FastMCP service. Direct httpx calls to `app.scrapingbee.com/api/v1/amazon/*` (Product, Search endpoints) plus `/api/v1/usage`. **No SDK dependency** — ScrapingBee's Python SDK is a thin wrapper, so we keep it minimal.
- **Localization rule (verified)**: ScrapingBee REJECTS `country=us` + `domain=com` ("Invalid localization combination"). Use **`zip_code` for matching-country localization**, `country` only when routing from a non-matching country. `_build_localization()` enforces this; default US ZIP `30301` (Atlanta) keeps weekly competitor prices comparable.
- **Credit costs (verified live)**:
  - `/amazon/product`, `/amazon/search` light_request: 5 credits per page
  - `/amazon/*` with light_request=false (full JS render): 15 credits
  - General `/api/v1` (HTML API): 1 no-JS / 5 with-JS / 25 premium+JS / 75 stealth+JS
  - AI extraction adds +5 credits
  - `/usage` is free
- **Credit budget guard**: `SCRAPINGBEE_CREDIT_BUDGET` env caps per-process spend; each tool response includes `credits_consumed_this_call` and `credits_consumed_this_process` so callers can track usage.
- **MCP Tools (6)**:
  - **Core**: `get_product(asin, country, zip_code, include_aplus)` snapshot — title/bullets/price/rating/buybox/BSR ladder; `get_offers(asin, ...)` buybox projection; `get_reviews(asin, ...)` — see deviation below; `search_keyword(keyword, sort_by, max_pages, ...)` — **organic vs sponsored always separated**, organic_rank integer on organic only
  - **Discovery**: `get_bestsellers(keyword_or_category, ...)` — wraps search with sort=bestsellers
  - **Ops**: `get_account_usage()` — free, surfaces remaining + per-process spend
- **⚠ Reviews limitation (major spec deviation)**: The original spec called for 100+ reviews per ASIN via a "dedicated Review API." That doesn't exist (`/amazon/reviews` returns 404), AND the full `/product-reviews/{ASIN}` page on amazon.com requires login from any ScrapingBee proxy IP (confirmed with HTTP 500 + `"help": "Redirected to login"` on standard, premium, AND stealth proxies). **Pivot**: `get_reviews` returns the ~8 top reviews that Amazon surfaces inline on the product page (cheap — 5cr, reliable), plus `rating_stars_distribution`. For deeper review pulls, route via Oxylabs (different anti-bot model) or an authenticated SP-API session in v2.
- **Defensive shape coercion**: ScrapingBee's product response returns `buybox` and `delivery` as either dict OR list depending on the ASIN. `_as_dict()` helper coerces both, preserving the raw subtree as `buybox_raw` / `delivery_raw` for callers that need full structure.
- **Cache**: SQLite kv at `SCRAPINGBEE_CACHE_PATH` (default `/tmp/scrapingbee-cache.db`, TTL `SCRAPINGBEE_CACHE_TTL_SECONDS` default 7 days). Pure-JSON responses round-trip cleanly (no numpy headache like Keepa had).
- **Compliance gut-check** (per spec): Treat scraped review/content data as internal CI for ad optimization — not for republication. Keep volumes reasonable.
- **OAuth**: Synthetic at our side

## Routing (Caddyfile)

Host-based routing takes priority (prevents cross-domain OAuth hijack):
1. `todoist.bibager.com` → 8001
2. `monarch.bibager.com` → 8000
3. `gitlab.bibager.com` → 8003
4. `weather.bibager.com` → 8004
5. `trackiq.bibager.com` → 8005
6. `framer.bibager.com` → 8007 (Python frontend; sidecar at 8006 is localhost-only)
7. `pacvue.bibager.com` → 8008
8. `alpaca.bibager.com` → 8009 (proxy; upstream alpaca-mcp-server on 8010 is localhost-only)
9. `ta.bibager.com` → 8011 (technical analysis service)
10. `uw.bibager.com` → 8012 (Unusual Whales proxy)
11. `datarova.bibager.com` → 8013 (Amazon brand rank tracker)
12. `keepa.bibager.com` → 8014 (Keepa historical data)
13. `scrapingbee.bibager.com` → 8015 (ScrapingBee Amazon content)
14. GA OAuth endpoints (`/.well-known/*`, `/authorize`, `/token`, etc.) → 8002
15. Path-based fallbacks: `/ga/*`, `/monarch/*`, `/todoist/*`, `/gitlab/*`, `/weather/*`, `/trackiq/*`, `/framer/*`, `/pacvue/*`, `/alpaca/*`, `/ta/*`, `/uw/*`, `/datarova/*`, `/keepa/*`, `/scrapingbee/*`
16. Default: 404

## Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `MCP_API_KEY` | All | Shared API key protecting all gateway endpoints |
| `MONARCH_TOKEN` | Monarch | Direct auth token (preferred) |
| `MONARCH_EMAIL` | Monarch | Email login (fallback) |
| `MONARCH_PASSWORD` | Monarch | Password login (fallback) |
| `MONARCH_MFA_SECRET` | Monarch | TOTP base32 key for 2FA |
| `TODOIST_API_TOKEN` | Todoist | Static API token (fallback) |
| `TODOIST_CLIENT_ID` | Todoist | OAuth client ID |
| `TODOIST_CLIENT_SECRET` | Todoist | OAuth client secret |
| `GOOGLE_CLIENT_ID` | GA | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | GA | Google OAuth client secret |
| `GOOGLE_REFRESH_TOKEN` | GA | Refresh token for ADC |
| `ALLOWED_EMAIL` | GA | Email allowlist for OAuth |
| `GITLAB_TOKEN` | GitLab | Personal access token |
| `GITLAB_PROJECT_ID` | GitLab | Default project (URL-encoded, e.g. `trackio1%2Ftrack-app`) |
| `TRACKIQ_API_KEY` | TrackIQ | Upstream Bearer (`tiq_live_...`) |
| `FRAMER_API_KEY` | Framer | Framer Server API key (project-scoped, from Site Settings) |
| `FRAMER_PROJECT_URL` | Framer | Target Framer project URL (e.g. `https://framer.com/projects/<slug-id>`) |
| `SIDECAR_INTERNAL_KEY` | Framer | Localhost shared secret between Python frontend and Node sidecar |
| `PACVUE_API_KEY` | Pacvue | Upstream Pacvue API token (raw `pv_…`, no Bearer prefix) |
| `ALPACA_API_KEY` | Alpaca | Alpaca Trading API key ID (live or paper) |
| `ALPACA_SECRET_KEY` | Alpaca | Alpaca Trading API secret key |
| `ALPACA_PAPER_TRADE` | Alpaca | `"true"` for paper-trading simulator, `"false"` for live trading |
| `UW_API_KEY` | Unusual Whales | UUID from `unusualwhales.com/settings/api-dashboard` (Bearer-prefix) |
| `DATAROVA_REFRESH_TOKEN` | Datarova | Long-lived Cognito refresh token (~30 day TTL); captured via `tools/datarova-api-recon/` extension |
| `DATAROVA_COGNITO_CLIENT_ID` | Datarova | Optional override; defaults to `5dcti49pggi19uqs3df8iae3o1` |
| `DATAROVA_X_PLAN` | Datarova | Optional `x-plan` JWT fallback; verified NOT required in production |
| `KEEPA_API_KEY` | Keepa | Private API key from `keepa.com/#!api` (paid subscription, token-metered) |
| `KEEPA_DEFAULT_DOMAIN` | Keepa | Optional; defaults to `US` (Keepa domainId 1) |
| `KEEPA_CACHE_PATH` | Keepa | Optional SQLite cache path (default `/tmp/keepa-cache.db`; ephemeral on DO App Platform) |
| `KEEPA_CACHE_TTL_SECONDS` | Keepa | Optional cache TTL in seconds (default 86400 = 24h) |
| `SCRAPINGBEE_API_KEY` | ScrapingBee | API key from `app.scrapingbee.com/api-keys` |
| `SCRAPINGBEE_DEFAULT_ZIP` | ScrapingBee | Optional; default `30301` (Atlanta — keeps US prices comparable week-over-week) |
| `SCRAPINGBEE_DEFAULT_COUNTRY` | ScrapingBee | Optional; only set when routing from non-matching country |
| `SCRAPINGBEE_CREDIT_BUDGET` | ScrapingBee | Optional soft per-process credit cap (default unlimited) |
| `SCRAPINGBEE_CACHE_PATH` | ScrapingBee | Optional SQLite cache path (default `/tmp/scrapingbee-cache.db`; ephemeral on DO) |
| `SCRAPINGBEE_CACHE_TTL_SECONDS` | ScrapingBee | Optional cache TTL (default 604800 = 7d) |
| `SERVER_URL` | All | Per-service base URL override (e.g. `https://framer.bibager.com`) |

## Conventions

- **Languages**: Python 3.12 for everything except the Framer service, which adds a Node.js 22 sidecar (TypeScript via `tsc`).
- **MCP Framework**: FastMCP 2.0+ on the Python side; `@modelcontextprotocol/sdk` is intentionally avoided in the Node sidecar (we use Hono + plain JSON RPC over localhost — the public MCP surface is Python).
- **Dual Protocol**: Each service exposes FastMCP `/mcp` endpoint; Monarch additionally exposes REST `/api/*` for n8n.
- **OAuth Pattern**: Synthetic OAuth (auto-approve, return `MCP_API_KEY` as never-expiring access token) for everything except GA (full OAuth with token refresh) and Todoist (synthetic at our side, real Todoist OAuth at `/todoist/auth`).
- **Process Management**: Supervisor runs all services + Caddy in a single container.
- **Commit Style**: Conventional commits (`feat:`, `fix:`, `build:`, `docs:`). No `Co-Authored-By` lines per repo policy.

## Deployment

- **Platform**: DigitalOcean App Platform (app id `02a49797-290a-42fa-b71c-2d5dbe4fe107`)
- **Instance**: `apps-s-1vcpu-1gb` (~$12/mo) — has headroom for all 11 services
- **Git**: Deploys from `origin/main` on push. Local work happens on `master`; push to main with `git push origin master:main`.
- **CLI**: `doctl` is available and authenticated for spec updates and log inspection.
- **DNS**: Each subdomain has a Cloudflare CNAME (DNS-only, gray cloud) pointing at `mcp-gateway-pph44.ondigitalocean.app`. Add a new subdomain by (1) registering it as an `ALIAS` domain on the DO app, (2) adding the CNAME in Cloudflare.

## Working with the Framer service

- **`framer-api@0.1.7` is pinned exactly** in `services/framer/package.json`. Don't update casually — Framer's Server API is in open beta and the type surface changes.
- **The d.ts is gnarly** (~7000 lines of intersected types). Before adding a new tool, grep `node_modules/framer-api/dist/index.d.ts` for the exact method signature; treat docs as guidance, types as truth.
- **`addText` and `addImage` are traps** — they're plugin-API helpers that operate on the current selection and return `void`. Use `createTextNode(attrs, parentId?)` and the upload-then-`setAttributes({backgroundImage: asset})` flow instead.
- **API keys must be project-scoped**: a workspace/account-level Framer key returns "API key does not have access to this project". Generate the key from inside the target project's Site Settings → Server API.
- **`isTextNode` / `isFrameNode` type guards** from framer-api are the right way to discriminate node types before calling per-node methods.

## n8n Automations

Two n8n workflows for daily Todoist task management (hosted on bibager-n8n):

### Workflow 1: Todoist Task Enhancer (`IgreqAeTK2wv2be6`)
Daily 8:30am CST. Cron → Todoist (P1+P2) → filter unpolished → Claude Haiku → Todoist update.

### Workflow 2: Daily Digest Email (`OhQIlpOxtmBPCuV9`)
Daily 9:00am CST. Cron → Todoist (P1+P2) → format → Claude Haiku → Gmail → jheinz@gmail.com.

## Recent additions

- **Weather** (Apr 2026): Open-Meteo wrapper, 2 tools, no API key.
- **TrackIQ proxy** (Apr 2026): HTTP-streaming proxy to TrackIQ's MCP, 16 tools, Bearer-rewrite.
- **Framer v1.0** (May 2026): 3 read/create tools — proved the polyglot Python+Node pattern.
- **Framer v1.1** (May 2026): 12 tools total. Frame creation, layout traits, text mutation, image upload+paint, publish/deploy. Live integration verified end-to-end against TrackIQ-V2 project.
- **Framer v1.2** (May 2026): 38 tools total. Added tree navigation (get_node/get_children/get_parent/get_rect/get_nodes_with_type), node manipulation (clone_node/clone_web_page/set_parent), site settings (add_redirects/set_custom_code), design system (color/text styles + fonts), visual feedback (screenshot/export_svg), project info, locales, and code files.
- **Framer v1.3** (May 2026): 46 tools total. Added CMS data plane via the `Collection` class — list/create collections, add/list fields, add/list/remove items. End-to-end verified by creating a "Claude CMS Test" collection in TrackIQ V2 with Title (string) + Body (formattedText) fields and 2 populated items.
- **Pacvue proxy** (May 2026): HTTP-streaming proxy to `mcp.pacvue.com/mcp`, 5 tools (report list/schema, materials, run/fetch). Mirrors the TrackIQ proxy pattern but uses raw `pv_<token>` (no `Bearer` prefix) per Pacvue spec.
- **Alpaca trading** (May 2026): First self-hosted upstream — `alpaca-mcp-server@2.0.1` runs locally on `127.0.0.1:8010` (no built-in auth, hence localhost-only); our Bearer-guarded proxy on 8009 fronts it with synthetic OAuth. 61 tools (live count) across orders, positions, watchlists, stock/crypto/options market data. Started in **live** mode (`ALPACA_PAPER_TRADE=false`) — flip to `true` for paper.
- **TA service** (May 2026): Local-compute technical analysis over Alpaca bars. 4 tools — Standard/Camarilla/Woodie pivot points, session VWAP, anchored VWAP, and volume profile (POC/VAH/VAL with uniform per-bar volume distribution). Reuses `ALPACA_API_KEY`/`SECRET_KEY` env vars; first non-proxy gateway service that consumes another gateway service's underlying data source.
- **Unusual Whales proxy** (May 2026): HTTP-streaming proxy to `api.unusualwhales.com/api/mcp` with Bearer rewrite. Surfaces options flow, dark pool, congressional trades, ETF holdings. Critical setup gotcha: the public-facing `unusualwhales.com/public-api/mcp` URL is the docs page, NOT the MCP endpoint — use `api.unusualwhales.com/api/mcp` upstream.
- **Datarova rank tracker** (Jun 2026): Reverse-engineered Amazon brand keyword rank tracker (`app.datarova.com` has no public API). First service to do **server-side AWS Cognito refresh-token flow** — we hold a ~30-day refresh token in env and mint fresh access/id JWTs on demand instead of asking the user to recapture hourly. 11 tools across rank tracker, market data (Keyword Spy / ASIN Insights), product enrichment. Endpoint discovery powered by `tools/datarova-api-recon/` Chrome extension. Key finding: on Basic tier, ASIN Insights isn't a separate "list keywords for any ASIN" endpoint — it's `/records/record` iterated over your tracked keywords with the competitor as `topAsin`; the bulk-keywords feature is gated behind a PostHog flag.
- **Keepa historical data** (Jun 2026): Amazon historical price / BSR / buy-box / rating time series + competitive discovery via the official `keepa` PyPI package. 12 tools. Heavy normalization at the wrapper layer (KeepaTime → ISO 8601, integer cents → decimal dollars, `-1` sentinel → null) so agents never see raw Keepa encodings. Lazy Keepa client init means the service deploys cleanly even when the subscription is "Payment Pending" (constructor pings the API). Simple SQLite kv cache at `/tmp/keepa-cache.db` saves tokens on repeated history pulls within a deploy — ephemeral on DO App Platform.
- **ScrapingBee Amazon content** (Jun 2026): Live competitor content layer — product listings, SERP rank (organic vs sponsored), inline reviews. 6 tools. Pure-httpx wrapper around `/api/v1/amazon/{product,search}` plus `/api/v1/usage`. Two material spec deviations forced by ScrapingBee's actual API behavior: (1) `country=us` + `domain=com` is rejected — use `zip_code` for matching-country localization; (2) the spec's "dedicated Review API" doesn't exist (`/amazon/reviews` is 404) AND the full `/product-reviews/{ASIN}` page on Amazon requires login from any ScrapingBee proxy IP (confirmed across standard, premium, AND stealth tiers — all return HTTP 500 with `"Redirected to login"`). Pivot: `get_reviews` returns the ~8 reviews Amazon surfaces inline on the product page (5cr, reliable) plus `rating_stars_distribution`. Deep review pulls await an Oxylabs or SP-API integration in v2.

## Open follow-ups

- Investigate sidecar process robustness — observed one crash when `framer-api` threw "API key does not have access" (the SDK may exit on certain auth errors instead of throwing).
- Custom font support for Framer (`upload_font` / `create_text_style`) — out of scope for v1.1.
- SVG node creation for Framer — out of scope for v1.1.
- Component instance creation for Framer — out of scope for v1.1.
- Security review: in-memory token stores, shared `MCP_API_KEY` across services.
- Test/activate the two n8n workflows in n8n UI.
