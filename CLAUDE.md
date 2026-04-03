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
- **Status**: Placeholder — directory exists but no implementation

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
| `SERVER_URL` | All | Base URL override (e.g., `https://mcp.bibager.com`) |

## Conventions

- **Language**: Python 3.12 for all services
- **MCP Framework**: FastMCP 2.0+
- **Dual Protocol**: Each service exposes FastMCP `/mcp` endpoint + optional REST `/api/*`
- **OAuth Pattern**: Synthetic OAuth for Todoist/Monarch (auto-approve, never-expiring tokens), full OAuth for GA
- **Process Management**: Supervisor runs all services + Caddy in a single container
- **Commit Style**: Conventional commits (`feat:`, `fix:`) with descriptive messages

## Current Task

Initial codebase examination complete. Project understood and documented.

## Next Steps

- Implement GitLab MCP service (placeholder exists)
- Consider adding README.md for external documentation
- Review security: in-memory token stores, shared MCP_API_KEY across services
- Consider persistent token storage (currently in-memory for Todoist OAuth)
