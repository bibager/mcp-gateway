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
                └── pacvue.bibager.com   → Pacvue proxy      (port 8008, Python)
```

### Key Files

```
├── Caddyfile              # Reverse proxy routing rules
├── Dockerfile             # Python 3.12-slim + Node 22 + Caddy + Supervisor
├── docker-compose.yml     # Single service, port 8080, .env file
├── supervisord.conf       # Process management (10 programs: caddy + 8 services + framer's 2)
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
    └── pacvue/server.py   # HTTP-streaming proxy to mcp.pacvue.com/mcp
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

## Routing (Caddyfile)

Host-based routing takes priority (prevents cross-domain OAuth hijack):
1. `todoist.bibager.com` → 8001
2. `monarch.bibager.com` → 8000
3. `gitlab.bibager.com` → 8003
4. `weather.bibager.com` → 8004
5. `trackiq.bibager.com` → 8005
6. `framer.bibager.com` → 8007 (Python frontend; sidecar at 8006 is localhost-only)
7. `pacvue.bibager.com` → 8008
8. GA OAuth endpoints (`/.well-known/*`, `/authorize`, `/token`, etc.) → 8002
9. Path-based fallbacks: `/ga/*`, `/monarch/*`, `/todoist/*`, `/gitlab/*`, `/weather/*`, `/trackiq/*`, `/framer/*`, `/pacvue/*`
10. Default: 404

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
- **Instance**: `apps-s-1vcpu-1gb` (~$12/mo) — has headroom for all 8 services
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

## Open follow-ups

- Investigate sidecar process robustness — observed one crash when `framer-api` threw "API key does not have access" (the SDK may exit on certain auth errors instead of throwing).
- Custom font support for Framer (`upload_font` / `create_text_style`) — out of scope for v1.1.
- SVG node creation for Framer — out of scope for v1.1.
- Component instance creation for Framer — out of scope for v1.1.
- Security review: in-memory token stores, shared `MCP_API_KEY` across services.
- Test/activate the two n8n workflows in n8n UI.
