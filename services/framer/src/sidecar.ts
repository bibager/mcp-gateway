import { connect, type Framer, isFrameNode, isTextNode, isWebPageNode } from "framer-api";
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

let framerInstance: Framer | null = null;

export async function getFramer(): Promise<Framer> {
    if (framerInstance) return framerInstance;
    framerInstance = await connect(PROJECT_URL, API_KEY);
    console.log(`[framer-sidecar] connected to ${PROJECT_URL}`);
    return framerInstance;
}

// Flatten any AnyNode to a JSON-safe shape for tool responses.
function serializeNode(n: unknown): Record<string, unknown> | null {
    if (!n || typeof n !== "object") return null;
    const node = n as Record<string, unknown>;
    const out: Record<string, unknown> = {
        id: node["id"] ?? null,
        name: node["name"] ?? null,
        type: (n as { constructor?: { name?: string } }).constructor?.name ?? "Unknown",
    };
    if (typeof node["path"] === "string") out["path"] = node["path"];
    return out;
}

const app = new Hono();

// Internal-key guard: skip /health, require X-Sidecar-Key on everything else.
app.use("*", async (c, next) => {
    if (c.req.path === "/health") return next();
    if (c.req.header("x-sidecar-key") !== INTERNAL_KEY) {
        return c.json({ ok: false, error: "unauthorized" }, 401);
    }
    return next();
});

app.get("/health", (c) => c.json({ status: "ok" }));

app.post("/tools/get_current_page", async (c) => {
    try {
        const f = await getFramer();
        const root = await f.getCanvasRoot();
        const result: Record<string, unknown> = {
            id: (root as { id?: string }).id ?? null,
            name: (root as { name?: string }).name ?? null,
            type: root.constructor?.name ?? "Unknown",
        };
        if ("path" in root && typeof (root as { path?: unknown }).path === "string") {
            result.path = (root as { path: string }).path;
        }
        return c.json({ ok: true, result });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/create_web_page", async (c) => {
    let body: { path?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const path = body.path;
    if (typeof path !== "string" || !path) {
        return c.json({ ok: false, error: "missing_or_invalid_path" }, 400);
    }
    try {
        const f = await getFramer();
        const page = await f.createWebPage(path);
        return c.json({
            ok: true,
            result: {
                id: page.id,
                path: page.path,
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/create_text_node", async (c) => {
    let body: { attributes?: unknown; text?: unknown; parent_id?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const attributes = (body.attributes ?? {}) as Record<string, unknown>;
    const parentId = typeof body.parent_id === "string" ? body.parent_id : undefined;
    const text = typeof body.text === "string" ? body.text : undefined;

    try {
        const f = await getFramer();
        const node = await f.createTextNode(
            attributes as Parameters<typeof f.createTextNode>[0],
            parentId,
        );
        if (!node) {
            return c.json({ ok: false, error: "createTextNode returned null" }, 500);
        }
        if (text !== undefined) {
            await node.setText(text);
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/create_design_page", async (c) => {
    let body: { name?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const name = body.name;
    if (typeof name !== "string" || !name) {
        return c.json({ ok: false, error: "missing_or_invalid_name" }, 400);
    }
    try {
        const f = await getFramer();
        const page = await f.createDesignPage(name);
        return c.json({ ok: true, result: { id: page.id, name: page.name ?? null } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/create_frame", async (c) => {
    let body: { attributes?: unknown; parent_id?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const attributes = (body.attributes ?? {}) as Record<string, unknown>;
    const parentId = typeof body.parent_id === "string" ? body.parent_id : undefined;

    try {
        const f = await getFramer();
        const node = await f.createFrameNode(
            attributes as Parameters<typeof f.createFrameNode>[0],
            parentId,
        );
        if (!node) {
            return c.json({ ok: false, error: "createFrameNode returned null" }, 500);
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/set_attributes", async (c) => {
    let body: { node_id?: unknown; attributes?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const nodeId = body.node_id;
    const attributes = body.attributes;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    if (typeof attributes !== "object" || attributes === null) {
        return c.json({ ok: false, error: "missing_or_invalid_attributes" }, 400);
    }
    try {
        const f = await getFramer();
        const node = await f.setAttributes(
            nodeId,
            attributes as Parameters<typeof f.setAttributes>[1],
        );
        if (!node) {
            return c.json({ ok: false, error: "setAttributes returned null" }, 500);
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/set_text", async (c) => {
    let body: { node_id?: unknown; text?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const nodeId = body.node_id;
    const text = body.text;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    if (typeof text !== "string") {
        return c.json({ ok: false, error: "missing_or_invalid_text" }, 400);
    }
    try {
        const f = await getFramer();
        const node = await f.getNode(nodeId);
        if (!node) {
            return c.json({ ok: false, error: "node_not_found" }, 404);
        }
        if (!isTextNode(node)) {
            return c.json({ ok: false, error: "node_is_not_a_text_node" }, 400);
        }
        await node.setText(text);
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/delete_node", async (c) => {
    let body: { node_id?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        await f.removeNode(nodeId);
        return c.json({ ok: true, result: { ok: true } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/upload_image", async (c) => {
    let body: { image_url?: unknown; alt_text?: unknown; name?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const imageUrl = body.image_url;
    if (typeof imageUrl !== "string" || !imageUrl) {
        return c.json({ ok: false, error: "missing_or_invalid_image_url" }, 400);
    }
    const altText = typeof body.alt_text === "string" ? body.alt_text : undefined;
    const name = typeof body.name === "string" ? body.name : undefined;

    try {
        const f = await getFramer();
        const input: Record<string, unknown> = { image: imageUrl };
        if (altText !== undefined) input.altText = altText;
        if (name !== undefined) input.name = name;
        // Cast via `unknown` to satisfy TS (NamedImageAssetInput | File literal mismatch)
        const asset = await f.uploadImage(input as unknown as Parameters<typeof f.uploadImage>[0]);
        return c.json({
            ok: true,
            result: {
                id: asset.id,
                url: asset.url,
                thumbnail_url: asset.thumbnailUrl,
                alt_text: asset.altText ?? null,
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/set_frame_image", async (c) => {
    let body: { node_id?: unknown; image_url?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const nodeId = body.node_id;
    const imageUrl = body.image_url;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    if (typeof imageUrl !== "string" || !imageUrl) {
        return c.json({ ok: false, error: "missing_or_invalid_image_url" }, 400);
    }
    try {
        const f = await getFramer();
        const node = await f.getNode(nodeId);
        if (!node) {
            return c.json({ ok: false, error: "node_not_found" }, 404);
        }
        if (!isFrameNode(node)) {
            return c.json({ ok: false, error: "node_is_not_a_frame_node" }, 400);
        }
        // Upload, then set the resulting asset as backgroundImage on the frame.
        const uploadInput = { image: imageUrl } as unknown as Parameters<typeof f.uploadImage>[0];
        const asset = await f.uploadImage(uploadInput);
        const updated = await f.setAttributes(
            nodeId,
            { backgroundImage: asset } as unknown as Parameters<typeof f.setAttributes>[1],
        );
        if (!updated) {
            return c.json({ ok: false, error: "setAttributes returned null" }, 500);
        }
        return c.json({
            ok: true,
            result: {
                id: updated.id,
                asset_id: asset.id,
                asset_url: asset.url,
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/publish", async (c) => {
    try {
        const f = await getFramer();
        const { deployment, hostnames } = await f.publish();
        // Find a usable preview URL — prefer "version" (the freshly published preview),
        // fall back to "default", then any published hostname.
        const preview = hostnames.find((h) => h.type === "version")
            ?? hostnames.find((h) => h.type === "default")
            ?? hostnames.find((h) => h.isPublished)
            ?? null;
        return c.json({
            ok: true,
            result: {
                deployment_id: deployment.id,
                created_at: deployment.createdAt,
                preview_url: preview ? `https://${preview.hostname}` : null,
                hostnames: hostnames.map((h) => ({
                    hostname: h.hostname,
                    type: h.type,
                    is_primary: h.isPrimary,
                    is_published: h.isPublished,
                })),
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/deploy", async (c) => {
    let body: { deployment_id?: unknown };
    try {
        body = await c.req.json();
    } catch {
        return c.json({ ok: false, error: "invalid_json" }, 400);
    }
    const deploymentId = body.deployment_id;
    if (typeof deploymentId !== "string" || !deploymentId) {
        return c.json({ ok: false, error: "missing_or_invalid_deployment_id" }, 400);
    }
    try {
        const f = await getFramer();
        const hostnames = await f.deploy(deploymentId);
        return c.json({
            ok: true,
            result: {
                hostnames: hostnames.map((h) => ({
                    hostname: h.hostname,
                    type: h.type,
                    is_primary: h.isPrimary,
                })),
                count: hostnames.length,
            },
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/get_node", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const node = await f.getNode(nodeId);
        return c.json({ ok: true, result: serializeNode(node) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/get_children", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const children = await f.getChildren(nodeId);
        return c.json({ ok: true, result: children.map(serializeNode) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/get_parent", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const parent = await f.getParent(nodeId);
        return c.json({ ok: true, result: serializeNode(parent) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/get_rect", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const rect = await f.getRect(nodeId);
        if (!rect) return c.json({ ok: true, result: null });
        return c.json({ ok: true, result: { x: rect.x, y: rect.y, width: rect.width, height: rect.height } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/get_nodes_with_type", async (c) => {
    let body: { type?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const type = body.type;
    const validTypes = ["FrameNode", "TextNode", "SVGNode", "ComponentInstanceNode",
                        "WebPageNode", "DesignPageNode", "ComponentNode"];
    if (typeof type !== "string" || !validTypes.includes(type)) {
        return c.json({ ok: false, error: `invalid_type — must be one of ${validTypes.join(", ")}` }, 400);
    }
    try {
        const f = await getFramer();
        const nodes = await (f.getNodesWithType as (t: string) => Promise<unknown[]>)(type);
        return c.json({ ok: true, result: nodes.map(serializeNode) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/clone_node", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const cloned = await f.cloneNode(nodeId);
        return c.json({ ok: true, result: serializeNode(cloned) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/clone_web_page", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const node = await f.getNode(nodeId);
        if (!node) {
            return c.json({ ok: false, error: "node_not_found" }, 404);
        }
        if (!isWebPageNode(node)) {
            return c.json({ ok: false, error: "node_is_not_a_web_page_node" }, 400);
        }
        const page = await node.clone();
        return c.json({ ok: true, result: serializeNode(page) });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/set_parent", async (c) => {
    let body: { node_id?: unknown; parent_id?: unknown; index?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    const parentId = body.parent_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    if (typeof parentId !== "string" || !parentId) {
        return c.json({ ok: false, error: "missing_or_invalid_parent_id" }, 400);
    }
    const index = typeof body.index === "number" ? body.index : undefined;
    try {
        const f = await getFramer();
        await f.setParent(nodeId, parentId, index);
        return c.json({ ok: true, result: { ok: true } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/add_redirects", async (c) => {
    let body: { redirects?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const list = body.redirects;
    if (!Array.isArray(list) || list.length === 0) {
        return c.json({ ok: false, error: "missing_or_invalid_redirects (must be non-empty array)" }, 400);
    }
    // Light validation — each entry must have string `from` and `to`. expandToAllLocales is optional.
    for (const r of list as Array<Record<string, unknown>>) {
        if (typeof r["from"] !== "string" || typeof r["to"] !== "string") {
            return c.json({ ok: false, error: "each redirect must have string from + to" }, 400);
        }
    }
    try {
        const f = await getFramer();
        const added = await f.addRedirects(list as Parameters<typeof f.addRedirects>[0]);
        return c.json({
            ok: true,
            result: added.map((r) => ({
                id: r.id,
                from: r.from,
                to: r.to,
                expandToAllLocales: r.expandToAllLocales ?? false,
            })),
        });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

app.post("/tools/set_custom_code", async (c) => {
    let body: { html?: unknown; location?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    // html may be null (clears the snippet) or a string.
    const html = body.html;
    const location = body.location;
    const validLocations = ["headStart", "headEnd", "bodyStart", "bodyEnd"];
    if (typeof location !== "string" || !validLocations.includes(location)) {
        return c.json({ ok: false, error: `invalid_location — must be one of ${validLocations.join(", ")}` }, 400);
    }
    if (html !== null && typeof html !== "string") {
        return c.json({ ok: false, error: "html must be a string or null" }, 400);
    }
    try {
        const f = await getFramer();
        await f.setCustomCode({ html, location } as Parameters<typeof f.setCustomCode>[0]);
        return c.json({ ok: true, result: { ok: true, location, cleared: html === null } });
    } catch (err) {
        const msg = err instanceof Error ? err.message : String(err);
        return c.json({ ok: false, error: msg }, 500);
    }
});

serve({ fetch: app.fetch, port: PORT }, (info) => {
    console.log(`[framer-sidecar] listening on ${info.port}`);
});

// Graceful shutdown — close the framer-api WebSocket on signal.
for (const sig of ["SIGINT", "SIGTERM"] as const) {
    process.on(sig, async () => {
        console.log(`[framer-sidecar] ${sig} — disconnecting`);
        try {
            await framerInstance?.disconnect();
        } catch (err) {
            console.error("[framer-sidecar] disconnect failed:", err);
        }
        process.exit(0);
    });
}

export { app };
