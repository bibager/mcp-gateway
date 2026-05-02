# Framer MCP Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship `framer.bibager.com/mcp` — a gateway-hosted MCP service that wraps the Framer Server API, so Claude can build native Framer pages from HTML the user provides.

**Architecture:** Python FastMCP frontend (clone of `services/gitlab/server.py`) handles auth + synthetic OAuth and forwards tool calls over `localhost:8006` to a Node 22 sidecar that owns the long-lived `framer-api` WebSocket connection. Both processes run under the existing supervisord on the existing DigitalOcean `apps-s-1vcpu-1gb` instance. See `docs/plans/2026-05-02-framer-mcp-design.md` for rationale.

**Tech Stack:** Python 3.12 + FastMCP 2.x + Starlette + httpx (frontend) · Node.js ≥22 + TypeScript + `framer-api@0.1.7` + `hono` (sidecar) · supervisord · Caddy · DigitalOcean App Platform.

**Project conventions** (read before starting): the existing 6 services have no automated test suite. Verification is "deploy + curl-probe live endpoints," which this plan follows. The only place this plan adds in-process tests is for the one piece of pure logic that benefits (the attributes-passthrough validator in the sidecar). Match the style of `services/gitlab/server.py` and `services/trackiq/server.py` exactly — same imports, same boilerplate, same comment headers.

---

## Phase 1 — Sidecar (Node) skeleton

### Task 1: Create the sidecar package layout

**Files:**
- Create: `services/framer/package.json`
- Create: `services/framer/tsconfig.json`
- Create: `services/framer/.gitignore`

**Step 1: Write `services/framer/package.json`**

```json
{
  "name": "framer-mcp-sidecar",
  "version": "0.1.0",
  "private": true,
  "type": "module",
  "engines": { "node": ">=22" },
  "scripts": {
    "build": "tsc",
    "start": "node dist/sidecar.js"
  },
  "dependencies": {
    "framer-api": "0.1.7",
    "hono": "^4.6.0",
    "@hono/node-server": "^1.13.0"
  },
  "devDependencies": {
    "typescript": "^5.6.0",
    "@types/node": "^22.0.0"
  }
}
```

**Step 2: Write `services/framer/tsconfig.json`**

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "outDir": "dist",
    "rootDir": "src",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "resolveJsonModule": true
  },
  "include": ["src/**/*.ts"]
}
```

**Step 3: Write `services/framer/.gitignore`**

```
node_modules/
dist/
```

**Step 4: Verify**

Run: `cd services/framer && ls`
Expected: `package.json  tsconfig.json  .gitignore`

**Step 5: Commit**

```bash
git add services/framer/package.json services/framer/tsconfig.json services/framer/.gitignore
git commit -m "feat(framer): scaffold sidecar package layout"
```

---

### Task 2: Sidecar entry point with framer-api connection lifecycle

**Files:**
- Create: `services/framer/src/sidecar.ts`

**Step 1: Write `services/framer/src/sidecar.ts`**

```typescript
import { connect, type Framer } from "framer-api";
import { Hono } from "hono";
import { serve } from "@hono/node-server";

const PORT = Number(process.env["PORT"] ?? 8006);
const PROJECT_URL = required("FRAMER_PROJECT_URL");
const API_KEY = required("FRAMER_API_KEY");
const INTERNAL_KEY = required("SIDECAR_INTERNAL_KEY");

function required(name: string): string {
    const v = process.env[name];
    if (!v) throw new Error(`Missing env: ${name}`);
    return v;
}

let framer: Framer | null = null;

async function getFramer(): Promise<Framer> {
    if (framer) return framer;
    framer = await connect(PROJECT_URL, API_KEY);
    console.log(`[framer-sidecar] connected to ${PROJECT_URL}`);
    return framer;
}

const app = new Hono();

app.use("*", async (c, next) => {
    if (c.req.path === "/health") return next();
    if (c.req.header("x-sidecar-key") !== INTERNAL_KEY) {
        return c.json({ ok: false, error: "unauthorized" }, 401);
    }
    return next();
});

app.get("/health", (c) => c.json({ status: "ok" }));

serve({ fetch: app.fetch, port: PORT }, (info) => {
    console.log(`[framer-sidecar] listening on ${info.port}`);
});

// Graceful shutdown
for (const sig of ["SIGINT", "SIGTERM"] as const) {
    process.on(sig, async () => {
        console.log(`[framer-sidecar] ${sig} — disconnecting`);
        await framer?.disconnect();
        process.exit(0);
    });
}
```

**Step 2: Build and run sanity check**

Local sanity check is optional during development if you don't have Node 22 installed. The Dockerfile build will catch type errors. Skip if not feasible locally; rely on CI build.

If you do have Node locally:
```bash
cd services/framer && npm install && npm run build
```
Expected: no TypeScript errors. `dist/sidecar.js` exists.

**Step 3: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): sidecar boot with health + auth + framer connect"
```

---

### Task 3: Implement read tools (`list_pages`)

**Files:**
- Modify: `services/framer/src/sidecar.ts`

**Step 1: Add list_pages route after `/health`**

```typescript
app.post("/tools/list_pages", async (c) => {
    try {
        const f = await getFramer();
        const pages = await f.getPages();
        return c.json({
            ok: true,
            result: pages.map((p) => ({
                id: p.id,
                name: p.name,
                path: p.path,
                type: p.type,
            })),
        });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});
```

Confirm `getPages()` exists on the `Framer` type. If the actual SDK method name differs (likely `f.getWebPages()` / `f.getDesignPages()`, see `framer-api` types), substitute the correct call. Check `services/framer/node_modules/framer-api/dist/index.d.ts` after `npm install` — search for `Page`.

**Step 2: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): list_pages tool in sidecar"
```

---

### Task 4: Implement page + frame creation (`create_web_page`, `create_frame`)

**Files:**
- Modify: `services/framer/src/sidecar.ts`

**Step 1: Add routes**

```typescript
app.post("/tools/create_web_page", async (c) => {
    const { name, parent_path } = await c.req.json<{ name: string; parent_path?: string }>();
    try {
        const f = await getFramer();
        const page = await f.createWebPage({ name, parentPath: parent_path });
        return c.json({ ok: true, result: { id: page.id, path: page.path } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});

app.post("/tools/create_frame", async (c) => {
    const { parent_id, attributes } = await c.req.json<{
        parent_id: string;
        attributes?: Record<string, unknown>;
    }>();
    try {
        const f = await getFramer();
        const node = await f.createFrameNode({ parentId: parent_id, ...(attributes ?? {}) });
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});
```

If `createWebPage` / `createFrameNode` argument shapes differ from this guess, consult `framer-api/dist/index.d.ts` and adjust. Do NOT invent fields — pass through what the user provides in `attributes`.

**Step 2: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): create_web_page + create_frame tools"
```

---

### Task 5: Implement text + image tools (`add_text`, `add_image`, `set_text`)

**Files:**
- Modify: `services/framer/src/sidecar.ts`

**Step 1: Add routes**

```typescript
app.post("/tools/add_text", async (c) => {
    const { parent_id, text, attributes } = await c.req.json<{
        parent_id: string;
        text: string;
        attributes?: Record<string, unknown>;
    }>();
    try {
        const f = await getFramer();
        const node = await f.addText({ parentId: parent_id, text, ...(attributes ?? {}) });
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});

app.post("/tools/add_image", async (c) => {
    const { parent_id, image_url, attributes } = await c.req.json<{
        parent_id: string;
        image_url: string;
        attributes?: Record<string, unknown>;
    }>();
    try {
        const f = await getFramer();
        // uploadImage takes the URL or buffer per Framer docs; verify exact signature.
        const asset = await f.uploadImage({ url: image_url });
        const node = await f.addImage({ parentId: parent_id, image: asset, ...(attributes ?? {}) });
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});

app.post("/tools/set_text", async (c) => {
    const { node_id, text } = await c.req.json<{ node_id: string; text: string }>();
    try {
        const f = await getFramer();
        const node = await f.getNode(node_id);
        await node.setText(text);
        return c.json({ ok: true, result: { ok: true } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});
```

Verify `f.getNode(id)` exists; if the SDK uses a different lookup path (e.g., walking the tree), adjust. The `addImage` flow needs `uploadImage` first — confirm the chained shape against the d.ts.

**Step 2: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): add_text, add_image, set_text tools"
```

---

### Task 6: Implement node mutation tools (`set_attributes`, `delete_node`)

**Files:**
- Modify: `services/framer/src/sidecar.ts`

**Step 1: Add routes**

```typescript
app.post("/tools/set_attributes", async (c) => {
    const { node_id, attributes } = await c.req.json<{
        node_id: string;
        attributes: Record<string, unknown>;
    }>();
    try {
        const f = await getFramer();
        await f.setAttributes(node_id, attributes);
        return c.json({ ok: true, result: { ok: true } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});

app.post("/tools/delete_node", async (c) => {
    const { node_id } = await c.req.json<{ node_id: string }>();
    try {
        const f = await getFramer();
        await f.removeNode(node_id);
        return c.json({ ok: true, result: { ok: true } });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});
```

**Step 2: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): set_attributes + delete_node tools"
```

---

### Task 7: Implement publish/deploy tools

**Files:**
- Modify: `services/framer/src/sidecar.ts`

**Step 1: Add routes (mirror `examples/publish/publish.ts` from framer/server-api-examples)**

```typescript
app.post("/tools/publish", async (c) => {
    try {
        const f = await getFramer();
        const { deployment } = await f.publish();
        return c.json({
            ok: true,
            result: { deployment_id: deployment.id, preview_url: deployment.url ?? null },
        });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});

app.post("/tools/deploy", async (c) => {
    const { deployment_id } = await c.req.json<{ deployment_id: string }>();
    try {
        const f = await getFramer();
        const deployed = await f.deploy(deployment_id);
        return c.json({
            ok: true,
            result: { hostnames: deployed.map((d) => d.hostname) },
        });
    } catch (err) {
        return c.json({ ok: false, error: String(err) }, 500);
    }
});
```

**Step 2: Commit**

```bash
git add services/framer/src/sidecar.ts
git commit -m "feat(framer): publish + deploy tools"
```

---

## Phase 2 — Python frontend

### Task 8: Python frontend skeleton (clone of gitlab/server.py)

**Files:**
- Create: `services/framer/server.py`
- Create: `services/framer/requirements.txt`

**Step 1: Write `services/framer/requirements.txt`** (identical to gitlab's)

```
fastmcp>=2.0
uvicorn[standard]
httpx
starlette
python-multipart
```

**Step 2: Write `services/framer/server.py`**

Clone `services/gitlab/server.py` line-for-line, then make these changes:

- Replace logger name `"gitlab_mcp"` with `"framer_mcp"`.
- Replace `MCP` instructions string with: `"Tools to create and modify a Framer site by calling the Framer Server API. Build pages from HTML by calling create_web_page, then create_frame, add_text, add_image, set_attributes etc. Publish with publish() and promote with deploy(deployment_id)."`
- Replace the `httpx.AsyncClient` GitLab calls inside each tool with a single helper `_call_sidecar(tool, args)` that POSTs to `http://127.0.0.1:8006/tools/<tool>` with `X-Sidecar-Key` header.
- Replace the GitLab tool list with the 10 tools from the design doc. Each tool just delegates to `_call_sidecar`.
- Set `PORT` env default to `8007`.
- Read `SIDECAR_INTERNAL_KEY` env at boot.
- Drop GitLab-specific env vars (`GITLAB_TOKEN`, `GITLAB_PROJECT_ID`).

The auth middleware, OAuth endpoints, lifespan, and route assembly stay byte-identical to `services/gitlab/server.py`.

Tool wrapper template:

```python
@mcp.tool(name="create_web_page", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_web_page(name: str, parent_path: Optional[str] = None) -> str:
    """Create a new Web Page in the Framer project. Returns its id and path."""
    return await _call_sidecar("create_web_page", {"name": name, "parent_path": parent_path})
```

`_call_sidecar` returns the JSON-stringified `result` field from the sidecar, or raises with the `error` field.

**Step 3: Syntax check**

Run: `py -c "import ast; ast.parse(open('services/framer/server.py').read()); print('OK')"`
Expected: `OK`

**Step 4: Commit**

```bash
git add services/framer/server.py services/framer/requirements.txt
git commit -m "feat(framer): python frontend forwarding to sidecar"
```

---

## Phase 3 — Container integration

### Task 9: Add Node.js to the Dockerfile

**Files:**
- Modify: `Dockerfile`

**Step 1: Insert Node 22 install after the apt-get caddy/supervisor block**

Add the following lines just before the `WORKDIR /app` line:

```dockerfile
# --- Node.js 22 (for the framer service) ---
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
```

Then add a build step for the framer sidecar after the existing `pip install` block:

```dockerfile
# --- Build the framer sidecar (TypeScript -> dist/) ---
COPY services/framer/package.json services/framer/tsconfig.json /app/services/framer/
COPY services/framer/src /app/services/framer/src
RUN cd /app/services/framer && npm ci && npm run build && npm prune --omit=dev
```

Also add the framer requirements.txt to the existing pip block:

```dockerfile
COPY services/framer/requirements.txt /tmp/framer-requirements.txt

RUN pip install --no-cache-dir \
    -r /tmp/monarch-requirements.txt \
    -r /tmp/todoist-requirements.txt \
    -r /tmp/ga-requirements.txt \
    -r /tmp/gitlab-requirements.txt \
    -r /tmp/weather-requirements.txt \
    -r /tmp/trackiq-requirements.txt \
    -r /tmp/framer-requirements.txt
```

**Step 2: Commit**

```bash
git add Dockerfile
git commit -m "build(framer): install Node 22 and build framer sidecar"
```

---

### Task 10: Add supervisord programs for both framer processes

**Files:**
- Modify: `supervisord.conf`

**Step 1: Append after the `[program:trackiq]` block**

```ini
[program:framer-sidecar]
command=node /app/services/framer/dist/sidecar.js
directory=/app/services/framer
environment=PORT="8006"
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0

[program:framer-frontend]
command=python /app/services/framer/server.py
directory=/app/services/framer
environment=PORT="8007",SERVER_URL="https://framer.bibager.com"
autostart=true
autorestart=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
```

`FRAMER_API_KEY`, `FRAMER_PROJECT_URL`, `SIDECAR_INTERNAL_KEY` are inherited from container env (set via DO app spec in Task 12).

**Step 2: Commit**

```bash
git add supervisord.conf
git commit -m "build(framer): supervisord programs for sidecar + frontend"
```

---

### Task 11: Add Caddy route

**Files:**
- Modify: `Caddyfile`

**Step 1: Insert after the `@trackiq` host block**

```caddy
    # --- Framer MCP: framer.bibager.com ---
    @framer header Host framer.bibager.com
    handle @framer {
        reverse_proxy localhost:8007
    }
```

**Step 2: Insert after the `handle_path /trackiq/*` block**

```caddy
    handle_path /framer/* {
        reverse_proxy localhost:8007
    }
```

**Step 3: Commit**

```bash
git add Caddyfile
git commit -m "build(framer): caddy route for framer.bibager.com"
```

---

## Phase 4 — DigitalOcean deployment

### Task 12: Update DO app spec with env vars + alias domain

**Files:**
- (no repo changes; doctl invocation only)

**Step 1: Generate a fresh internal sidecar key**

Run: `py -c "import secrets; print(secrets.token_urlsafe(32))"`
Save the output as `<SIDECAR_INTERNAL_KEY_VALUE>` — needed below.

**Step 2: Fetch current spec, patch, apply**

```bash
SPEC_CUR="$HOME/app-spec-current.yaml"
SPEC_NEW="$HOME/app-spec-new.yaml"
doctl apps spec get 02a49797-290a-42fa-b71c-2d5dbe4fe107 --format yaml > "$SPEC_CUR"
```

Then run a Python script identical in shape to the one used in the TrackIQ session:

- Anchor for env vars: the `TRACKIQ_API_KEY` block
- Append three new entries: `FRAMER_API_KEY` (SECRET), `FRAMER_PROJECT_URL` (plain), `SIDECAR_INTERNAL_KEY` (SECRET)
- Anchor for domains: the `trackiq.bibager.com` ALIAS block
- Append `framer.bibager.com` ALIAS

```bash
doctl apps update 02a49797-290a-42fa-b71c-2d5dbe4fe107 --spec "$SPEC_NEW"
```

Expected: `Notice: App updated`. A new deployment with phase DEPLOYING.

**Step 3: Verify**

```bash
doctl apps spec get 02a49797-290a-42fa-b71c-2d5dbe4fe107 --format yaml | grep -E "FRAMER_API_KEY|FRAMER_PROJECT_URL|framer.bibager.com"
```

Expected: three matches (env keys + domain), values redacted.

---

### Task 13: Push code → trigger deploy

**Files:** none new

**Step 1: Push**

```bash
git push origin master:main
```

Expected: GitHub accepts; DO auto-triggers a new deploy on top of the spec-update deploy.

**Step 2: Poll deploy phase**

Background poll (mirror the trackiq pattern):

```bash
for i in $(seq 1 24); do
  PHASE=$(doctl apps list-deployments 02a49797-290a-42fa-b71c-2d5dbe4fe107 --format Phase 2>/dev/null | sed -n '2p')
  echo "[t=$((i*15))s] deploy=$PHASE"
  if [ "$PHASE" = "ACTIVE" ]; then break; fi
  sleep 15
done
```

Expected: ACTIVE within ~4 minutes.

**Step 3: Sanity probes**

```bash
curl -sS --resolve framer.bibager.com:443:172.66.0.96 -o /dev/null -w "HTTP %{http_code}\n" https://framer.bibager.com/health
curl -sS --resolve framer.bibager.com:443:172.66.0.96 https://framer.bibager.com/.well-known/oauth-authorization-server | head -1
```

Expected: HTTP 200, OAuth metadata JSON pointing at `https://framer.bibager.com`.

If sidecar logs show framer connection errors:
```bash
doctl apps logs 02a49797-290a-42fa-b71c-2d5dbe4fe107 --type run --tail 100 | grep -i framer | tail -30
```

---

### Task 14: User-side DNS step

**Files:** none

The user adds a Cloudflare CNAME record:
- Type: CNAME
- Name: `framer`
- Content: `mcp-gateway-pph44.ondigitalocean.app`
- Proxy: DNS only
- TTL: 1 min

Wait for the user to confirm. Verify with:

```bash
nslookup framer.bibager.com 1.1.1.1 | tail -8
```

Expected: alias to `mcp-gateway-pph44.ondigitalocean.app`.

---

### Task 15: End-to-end verification through the proxy

**Files:** none

**Step 1: Synthetic OAuth dance to mint MCP_API_KEY-as-token** (mirror the trackiq verification pattern):

```bash
RESOLVE="--resolve framer.bibager.com:443:172.66.0.96"
VERIFIER="diag-test-$(date +%s)"
CHALLENGE=$(py -c "import hashlib,base64; v='$VERIFIER'; print(base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b'=').decode())")
CODE=$(curl -sS $RESOLVE -o /dev/null -w "%{redirect_url}" \
  "https://framer.bibager.com/authorize?response_type=code&client_id=diag&redirect_uri=https://example.com/cb&code_challenge=$CHALLENGE&code_challenge_method=S256&state=s" \
  | grep -oE 'code=[^&]+' | cut -d= -f2)
TOKEN=$(curl -sS $RESOLVE -X POST https://framer.bibager.com/token \
  -d "grant_type=authorization_code&code=$CODE&code_verifier=$VERIFIER" \
  | py -c "import sys,json; print(json.load(sys.stdin)['access_token'])")
```

**Step 2: Initialize and list tools**

```bash
SESSION=$(curl -sS $RESOLVE -D - -o /dev/null -X POST https://framer.bibager.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"diag","version":"1"}}}' \
  | grep -i "^mcp-session-id:" | tr -d '\r' | awk '{print $2}')

curl -sS $RESOLVE -X POST https://framer.bibager.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","method":"notifications/initialized"}'

curl -sS $RESOLVE -X POST https://framer.bibager.com/mcp \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "Mcp-Session-Id: $SESSION" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | sed -n 's/^data: //p' | py -c "import sys,json; d=json.load(sys.stdin); print(len(d['result']['tools']),'tools'); [print(' -',t['name']) for t in d['result']['tools']]"
```

Expected: `10 tools` listed, each one of the 10 names from the design doc.

**Step 3: Live round-trip**

Call `list_pages` against the actual TrackIQ-V2 Framer project. The MCP should return existing pages. If yes, the wiring is end-to-end correct.

---

### Task 16: User-side connector step + final docs update

**Files:**
- Modify: `CLAUDE.md`

**Step 1: User adds connector in Claude.ai**

| Field | Value |
|---|---|
| Name | Framer |
| URL | `https://framer.bibager.com/mcp` |
| OAuth Client ID/Secret | blank |

**Step 2: Update `CLAUDE.md`**

In the Architecture diagram, add the framer subdomain. Append a new ### Framer section under ## Services with: purpose, auth, MCP tools list, protocols, OAuth pattern. Add `framer.bibager.com` to the Routing list. Add `FRAMER_API_KEY`, `FRAMER_PROJECT_URL`, `SIDECAR_INTERNAL_KEY` to the Environment Variables table.

**Step 3: Commit + push**

```bash
git add CLAUDE.md
git commit -m "docs: document framer MCP service in CLAUDE.md"
git push origin master:main
```

---

## Acceptance criteria

The plan is done when, in order:

1. `https://framer.bibager.com/health` returns 200.
2. `tools/list` over MCP returns exactly the 10 design-doc tools.
3. Calling `list_pages` returns the actual page list of the TrackIQ-V2 Framer project.
4. A Claude prompt — "Create a new Framer web page named 'Hello Test', add a heading 'Hello' and a paragraph 'World', publish a preview, return the preview URL" — completes and the preview URL loads in a browser showing the page.
5. The other 5 services (`monarch`, `todoist`, `gitlab`, `weather`, `trackiq`, `ga`) still respond on their respective subdomains (regression check).
6. `CLAUDE.md` reflects the new service.

If any of those fail, do NOT mark the plan complete — investigate logs (`doctl apps logs ... --type run | grep -i framer`) and iterate.

## Known follow-ups (out of scope for this plan)

- Custom font upload tooling (`upload_font`)
- SVG node creation (`add_svg_node`)
- Component instance creation
- Canvas reading / introspection beyond `list_pages`
- Multi-project switching at runtime
- HTML→Framer translation evaluation harness (would let us regression-test page-build quality as we add tools)
