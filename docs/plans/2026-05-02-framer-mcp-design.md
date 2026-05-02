# Framer MCP — Design

**Status:** approved 2026-05-02
**Goal:** expose the Framer Server API as a gateway-hosted MCP service so Claude (Code and CoWork) can read an HTML file and build a native Framer page in a target project (`framer.bibager.com/mcp`).

## Background

The Framer Server API (open beta, free during beta) is a Node.js SDK distributed as the npm package `framer-api`. It speaks WebSocket to Framer and shares the Plugin API's full canvas-write surface — verified by inspecting `dist/index.d.ts` in v0.1.7: `createWebPage`, `createDesignPage`, `createFrameNode`, `addText`, `addImage`, `uploadImage`, `setAttributes`, `setText`, `cloneNode`, `removeNode`, `setParent`, `createTextStyle`, `getFonts`. Layout traits (stack/grid/padding/gap) and border traits ship in v3.6.0 of the Plugin API. Framer's announcement confirms server-side canvas mutation runs fully headless.

There is no native HTML import. Claude does the HTML → Framer-node-tree mapping by orchestrating MCP tool calls. The MCP exposes primitive node operations; semantic interpretation lives in Claude.

## Approach

**Python frontend + Node sidecar**, both inside the existing DigitalOcean container under supervisord.

```
Claude --Bearer MCP_API_KEY-->  framer.bibager.com/mcp        (Caddy)
                            |
                            v
                  services/framer/server.py                    (Python, port 8007)
                  - synthetic OAuth (auto-approve)
                  - APIKeyMiddleware (Bearer MCP_API_KEY)
                  - FastMCP /mcp; tool handlers POST to:
                            |
                            v
                  services/framer/sidecar.ts                   (Node 22, port 8006, localhost-only)
                  - holds long-lived `framer-api` connection
                  - small REST surface: POST /tools/<name>
                            |
                            v websocket
                  Framer Server API (api.framer.com)
```

The Python frontend reuses our battle-tested OAuth/auth pattern (clone of `services/gitlab/server.py`). The Node sidecar's only job is talking to Framer; it never sees external traffic.

**Why not all-TypeScript?** OAuth boilerplate is already proven in Python across five services. Reimplementing in TS doubles the maintenance surface for no win. Cross-language overhead between two localhost processes is a single JSON POST per tool call — negligible.

## Tool surface (v1)

Exactly the primitives Claude needs to build pages from HTML. Tool names are stable; argument shapes follow Framer's `attributes` object convention.

| Tool | Args | Returns |
|---|---|---|
| `list_pages` | — | `[{id, name, path, type}]` |
| `create_web_page` | `name`, `parent_path?` | `{id, path}` |
| `create_frame` | `parent_id`, `attributes?` | `{id}` |
| `add_text` | `parent_id`, `text`, `attributes?` | `{id}` |
| `add_image` | `parent_id`, `image_url`, `attributes?` | `{id}` |
| `set_attributes` | `node_id`, `attributes` | `{ok: true}` |
| `set_text` | `node_id`, `text` | `{ok: true}` |
| `delete_node` | `node_id` | `{ok: true}` |
| `publish` | — | `{deployment_id, preview_url}` |
| `deploy` | `deployment_id` | `{hostnames: [...]}` |

`attributes` is a flat dict that the Node sidecar passes to the Framer SDK verbatim. Common keys (per Framer's reference): `width`, `height`, `x`, `y`, `backgroundColor`, `backgroundGradient`, `borderRadius`, `borderWidth`, `borderColor`, `rotation`, `opacity`, `visible`, `layout` (`"stack"` | `"grid"` | `"none"`), `stackDirection`, `stackAlignment`, `stackDistribution`, `gap`, `padding`, `gridColumnCount`, `gridRowCount`.

Out of scope for v1 (defer until we hit a real need):
- `upload_font` / `create_text_style` — start with system fonts; address custom fonts when a project requires it
- `add_svg_node` — most HTML conversion can route through frame + text + image
- `add_code_component` — not needed for HTML translation
- Component instances (Framer Components) — v1 builds raw layouts only
- Reading/walking existing canvas — `list_pages` only; node introspection waits for a use case

## Internal protocol (Python ↔ Node sidecar)

Single endpoint on the sidecar:

```
POST http://127.0.0.1:8006/tools/<tool_name>
Content-Type: application/json
X-Sidecar-Key: <SIDECAR_INTERNAL_KEY>

{...args...}
```

Response: `{"ok": true, "result": {...}}` or `{"ok": false, "error": "...", "code": "..."}`.

The internal key is generated at boot (random) and shared via env var; it just guards against accidental external exposure if someone misconfigures Caddy. Both processes share `MCP_API_KEY`-derived secret or read from a shared file written by the Python frontend.

Connection lifecycle: the sidecar calls `connect(FRAMER_PROJECT_URL, FRAMER_API_KEY)` at boot, holds the connection, and reconnects on WebSocket close. No connection-per-request.

## Auth & secrets

| Env var | Purpose | Type |
|---|---|---|
| `MCP_API_KEY` | gateway Bearer (existing) | SECRET (already set) |
| `FRAMER_API_KEY` | upstream Framer key | SECRET (new) |
| `FRAMER_PROJECT_URL` | target Framer project | non-secret env var |
| `SIDECAR_INTERNAL_KEY` | python ↔ node guard | generated, non-secret |

User confirms `FRAMER_PROJECT_URL` per project — for now, the TrackIQ-V2 project URL the user shared.

Public OAuth at `framer.bibager.com` is the same synthetic flow as Todoist/GitLab/Weather/TrackIQ: auto-approves, returns `MCP_API_KEY` as a never-expiring access token.

## Deployment

1. New files:
   - `services/framer/server.py` (Python frontend, ~150 lines, mirrors `gitlab/server.py`)
   - `services/framer/requirements.txt`
   - `services/framer/sidecar.ts` (Node sidecar, ~200 lines)
   - `services/framer/package.json`, `tsconfig.json`
2. `Dockerfile`: install Node 22 alongside Python; run `npm ci` in `services/framer/` during build.
3. `supervisord.conf`: add two programs — `framer-frontend` (Python, 8007) and `framer-sidecar` (Node, 8006).
4. `Caddyfile`: add `framer.bibager.com` host route → `localhost:8007`, plus `/framer/*` path fallback.
5. DO app spec update: add `FRAMER_API_KEY` (SECRET) + `FRAMER_PROJECT_URL` env vars; add `framer.bibager.com` alias domain.
6. Cloudflare DNS: CNAME `framer` → `mcp-gateway-pph44.ondigitalocean.app` (DNS only).
7. Verification: `/health` returns 200, OAuth metadata returns valid JSON, `tools/list` over MCP returns the 10 tools, end-to-end `create_web_page` round-trip against the real Framer project.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Framer Server API is open beta — surface may change | Pin `framer-api` exact version; bump explicitly with regression checks. |
| Translation lossiness (CSS animations, custom fonts) | Document the fidelity boundary in MCP tool descriptions. Out of scope for v1; users see "approximation, not pixel-perfect." |
| WebSocket disconnect mid-session | Sidecar auto-reconnects on `disconnect`; Python frontend returns 503 on transient errors so Claude can retry. |
| Node sidecar crash | supervisord auto-restart; Python frontend returns clean error if sidecar is unreachable. |
| Resource budget on 1 GB instance | Estimated +170 MB resident, leaves comfortable headroom (~400 MB free). Re-evaluate if we add another Node service. |
| API key in chat history (already done) | User rotates after build complete. |

## Verification before "done"

- `curl https://framer.bibager.com/health` → 200
- Connector dialog accepts URL, OAuth completes, tools list shows 10 tools
- A Claude prompt of "create a new web page named 'Test' in the project, add a heading 'Hello' and a paragraph 'World', publish a preview" round-trips successfully and the preview URL is reachable
- Existing Todoist/GitLab/Weather/TrackIQ services still healthy after deploy

## Out of scope

- HTML parsing on the server side (Claude does it)
- Image hosting beyond what Framer's `uploadImage` provides
- Multi-project switching at runtime (one project per deploy)
- Component library / design-system reuse
- Rolling back published deployments
- Telemetry / usage tracking
