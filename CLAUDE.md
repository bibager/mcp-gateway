# MCP Gateway

## Project Overview

Multi-service MCP (Model Context Protocol) gateway that provides Claude AI with standardized access to personal finance, task management, and analytics services. Acts as a unified reverse proxy routing requests to specialized Python microservices.

**Domain**: `mcp.bibager.com` (primary), with subdomains per service
**Deployment**: Docker container with Caddy reverse proxy + Supervisor process management

## Architecture

```
Internet → Caddy (port 8080) → Service routing by host/path
                ├── monarch.bibager.com  → Monarch Money (port 8000)
                ├── todoist.bibager.com  → Todoist (port 8001)
                ├── gitlab.bibager.com   → GitLab (port 8003, placeholder)
                └── mcp.bibager.com      → Google Analytics (port 8002)
```

### Key Files

```
├── Caddyfile              # Reverse proxy routing rules
├── Dockerfile             # Python 3.12-slim + Caddy + Supervisor
├── docker-compose.yml     # Single service, port 8080, .env file
├── supervisord.conf       # Process management for all services
├── .env.example           # Environment variable template
└── services/
    ├── monarch/server.py  # Monarch Money MCP server
    ├── todoist/server.py  # Todoist MCP server
    ├── ga/                # Google Analytics MCP server
    │   ├── server.py
    │   ├── auth.py
    │   └── search_console.py
    └── gitlab/            # Placeholder (empty)
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
- **Auth**: Todoist OAuth 2.0 flow, fallback to `TODOIST_API_TOKEN` env var
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
- **Auth**: `GITLAB_TOKEN` env var (personal access token with `read_api` + `read_repository` scopes)
- **Default Project**: `trackio1/track-app` (configurable via `GITLAB_PROJECT_ID`)
- **MCP Tools**: 11 tools (get_recent_commits, get_merge_requests, get_pipelines, get_issues, get_file_content, etc.)
- **Protocols**: FastMCP `/mcp`
- **OAuth**: Synthetic — auto-approves, returns `MCP_API_KEY` as access token

## Routing (Caddyfile)

Host-based routing takes priority (prevents cross-domain OAuth hijack):
1. `todoist.bibager.com` → port 8001
2. `monarch.bibager.com` → port 8000
3. `gitlab.bibager.com` → port 8003
4. GA OAuth endpoints (/.well-known/*, /authorize, /token, etc.) → port 8002
5. Path-based fallbacks: `/ga/*`, `/monarch/*`, `/todoist/*`, `/gitlab/*`
6. Default: 404

## Environment Variables

| Variable | Service | Description |
|----------|---------|-------------|
| `MCP_API_KEY` | All | Shared API key protecting all endpoints |
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
| `GITLAB_TOKEN` | GitLab | Personal access token (read_api + read_repository) |
| `GITLAB_PROJECT_ID` | GitLab | Default project (URL-encoded, e.g. `trackio1%2Ftrack-app`) |
| `SERVER_URL` | All | Base URL override (e.g., `https://mcp.bibager.com`) |

## Conventions

- **Language**: Python 3.12 for all services
- **MCP Framework**: FastMCP 2.0+
- **Dual Protocol**: Each service exposes FastMCP `/mcp` endpoint + optional REST `/api/*`
- **OAuth Pattern**: Synthetic OAuth for Todoist/Monarch (auto-approve, never-expiring tokens), full OAuth for GA
- **Process Management**: Supervisor runs all services + Caddy in a single container
- **Commit Style**: Conventional commits (`feat:`, `fix:`) with descriptive messages

## Deployment

- **Platform**: DigitalOcean App Platform (app ID: `02a49797-290a-42fa-b71c-2d5dbe4fe107`)
- **Git**: Deploys from `origin/main` on push. Local work happens on `master`, push to main with `git push origin master:main`
- **CLI**: `doctl` is available and authenticated for managing the DO app

## n8n Automations

Two n8n workflows built for daily task management (hosted on bibager-n8n):

### Workflow 1: Todoist Task Enhancer (ID: `IgreqAeTK2wv2be6`)
- **Schedule**: 8:30am CST daily
- **Flow**: Cron → Todoist API (P1+P2 tasks) → Filter unpolished (description < 20 chars) → Claude Haiku (enhance title+description) → Todoist API (update task)
- **Credentials**: Todoist OAuth2, Anthropic Header Auth

### Workflow 2: Daily Digest Email (ID: `OhQIlpOxtmBPCuV9`)
- **Schedule**: 9:00am CST daily
- **Flow**: Cron → Todoist API (P1+P2 tasks) → Format summary → Claude Haiku (craft HTML email) → Gmail OAuth2 → jheinz@gmail.com
- **Credentials**: Todoist OAuth2, Anthropic Header Auth, Gmail OAuth2

## Current Task

Todoist MCP + n8n automations configured. Workflows need manual testing and activation in n8n UI.

## Session Log (2026-04-03)

1. Examined full codebase and documented architecture in CLAUDE.md
2. Tested Todoist MCP — server healthy, MCP handshake working, but Todoist API returning 401
3. Diagnosed: commit `8aaad7e` (Todoist OAuth flow) was on `master` but never pushed to `main` (deployed branch)
4. Pushed `master` to `origin/main` triggering deployment
5. Created Todoist developer app, added `TODOIST_CLIENT_ID` and `TODOIST_CLIENT_SECRET` to DO env vars
6. Completed Todoist OAuth flow — token `0d379c...` obtained and persisted as `TODOIST_API_TOKEN` in DO
7. Verified: `get_projects` and `get_labels` MCP tools returning live data successfully
8. Installed Todoist MCP as Claude Connector at `todoist.bibager.com/mcp`
9. Discovered Claude scheduled tasks can't access custom MCP connectors
10. Built two n8n workflows: Task Enhancer (8:30am) and Daily Digest Email (9:00am)
11. Configured workflows to use existing n8n credentials (Todoist OAuth2, Gmail OAuth2, Anthropic Header Auth)
12. Built GitLab MCP service (11 tools) for monitoring trackio1/track-app repository
13. Added GITLAB_TOKEN and GITLAB_PROJECT_ID to DO env vars, deployed

## Next Steps

- **Install GitLab MCP as Claude Connector** at `gitlab.bibager.com/mcp`
- **Test and activate n8n workflows** in n8n UI
- Test Monarch Money and Google Analytics MCP services
- Review security: in-memory token stores, shared MCP_API_KEY across services
