import { connect, type Framer, isFrameNode, isTextNode, isWebPageNode } from "framer-api";
import { Hono, type Context } from "hono";
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
let needsReconnect = false;
let connectPromise: Promise<Framer> | null = null;

// Heuristic: does this error message indicate the WebSocket dropped underneath us?
function isConnectionError(msg: string): boolean {
    return /connection closed|websocket|disconnected|not connected|ECONNRESET|ECONNREFUSED|socket hang up|EPIPE/i.test(
        msg,
    );
}

function markFramerDirty(reason: string): void {
    if (!needsReconnect) {
        console.warn(`[framer-sidecar] marking cache dirty: ${reason}`);
    }
    needsReconnect = true;
}

export async function getFramer(): Promise<Framer> {
    if (framerInstance && !needsReconnect) return framerInstance;
    if (connectPromise) return connectPromise;

    connectPromise = (async () => {
        if (framerInstance) {
            try {
                await framerInstance.disconnect();
            } catch (err) {
                console.warn("[framer-sidecar] disconnect during reconnect failed:", err);
            }
            framerInstance = null;
        }
        const next = await connect(PROJECT_URL, API_KEY);
        framerInstance = next;
        needsReconnect = false;
        console.log(`[framer-sidecar] connected to ${PROJECT_URL}`);
        return next;
    })();

    try {
        return await connectPromise;
    } finally {
        connectPromise = null;
    }
}

// Centralized 500 helper — also invalidates the cached Framer connection
// when the failure looks like a dropped WebSocket so the next call reconnects.
function errResponse(c: Context, err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    if (isConnectionError(msg)) {
        markFramerDirty(msg);
    }
    return c.json({ ok: false, error: msg }, 500);
}

// Resolve attribute fields that Framer's plugin API expects as real class
// instances (TextStyle, Font, etc.) but that arrive over JSON as bare ID
// strings or plain objects. Without this, set_attributes silently no-ops on
// inlineTextStyle, and createTextStyle stores a null font.
async function resolveAttributes(
    f: Framer,
    attrs: Record<string, unknown>,
): Promise<Record<string, unknown>> {
    const out: Record<string, unknown> = { ...attrs };

    // inlineTextStyle: a string ID -> real TextStyle object
    const its = out["inlineTextStyle"];
    if (typeof its === "string" && its) {
        const ts = await f.getTextStyle(its);
        if (ts) out["inlineTextStyle"] = ts;
    } else if (its && typeof its === "object" && typeof (its as { id?: unknown }).id === "string"
               && !((its as { selector?: unknown }).selector)) {
        const ts = await f.getTextStyle((its as { id: string }).id);
        if (ts) out["inlineTextStyle"] = ts;
    }

    // font: plain object {family, weight?, style?} -> real Font via getFont.
    // A real Font from framer-api carries a `selector` field; user-supplied
    // plain objects do not, which is how we tell them apart.
    const font = out["font"];
    if (font && typeof font === "object") {
        const fobj = font as Record<string, unknown>;
        const isPlain = !fobj["selector"] && typeof fobj["family"] === "string";
        if (isPlain) {
            const family = fobj["family"] as string;
            const fontArgs: Record<string, unknown> = {};
            if (typeof fobj["weight"] === "number") fontArgs["weight"] = fobj["weight"];
            if (fobj["style"] === "italic" || fobj["style"] === "normal") {
                fontArgs["style"] = fobj["style"];
            }
            const resolved = await f.getFont(
                family,
                Object.keys(fontArgs).length
                    ? (fontArgs as Parameters<typeof f.getFont>[1])
                    : undefined,
            );
            if (resolved) out["font"] = resolved;
        }
    }

    return out;
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

function serializeColorStyle(s: unknown): Record<string, unknown> | null {
    if (!s || typeof s !== "object") return null;
    const o = s as Record<string, unknown>;
    return {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
        // Common ColorStyleAttributes shape: { name, light, dark }
        light: o["light"] ?? null,
        dark: o["dark"] ?? null,
    };
}

function serializeTextStyle(s: unknown): Record<string, unknown> | null {
    if (!s || typeof s !== "object") return null;
    const o = s as Record<string, unknown>;
    // Surface obvious scalar fields; skip nested class instances.
    const out: Record<string, unknown> = {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
    };
    for (const k of ["fontSize", "fontWeight", "fontStyle", "lineHeight",
                      "letterSpacing", "textAlign", "textDecoration", "textTransform", "tag"]) {
        if (o[k] !== undefined) out[k] = o[k];
    }
    return out;
}

function serializeFont(f: unknown): Record<string, unknown> | null {
    if (!f || typeof f !== "object") return null;
    const o = f as Record<string, unknown>;
    return {
        id: o["id"] ?? null,
        family: o["family"] ?? null,
        weight: o["weight"] ?? null,
        style: o["style"] ?? null,
    };
}

function serializeLocale(l: unknown): Record<string, unknown> | null {
    if (!l || typeof l !== "object") return null;
    const o = l as Record<string, unknown>;
    return {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
        code: o["code"] ?? null,
        slug: o["slug"] ?? null,
        fallback_locale_id: o["fallbackLocaleId"] ?? null,
    };
}

function serializeCodeFile(cf: unknown): Record<string, unknown> | null {
    if (!cf || typeof cf !== "object") return null;
    const o = cf as Record<string, unknown>;
    const out: Record<string, unknown> = {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
        path: o["path"] ?? null,
        version_id: o["versionId"] ?? null,
    };
    // Surface source content if present (could be huge — caller should expect this).
    if (typeof o["content"] === "string") out["content"] = o["content"];
    return out;
}

function serializeCollection(c: unknown): Record<string, unknown> | null {
    if (!c || typeof c !== "object") return null;
    const o = c as Record<string, unknown>;
    return {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
        // Surface plugin-managed flag if present
        managed_by: o["managedBy"] ?? null,
        readonly: o["readonly"] ?? null,
    };
}

function serializeCollectionField(f: unknown): Record<string, unknown> | null {
    if (!f || typeof f !== "object") return null;
    const o = f as Record<string, unknown>;
    const out: Record<string, unknown> = {
        id: o["id"] ?? null,
        name: o["name"] ?? null,
        type: o["type"] ?? null,
    };
    // Some fields have additional scalar metadata — surface a few common ones.
    for (const k of ["userEditable", "cases", "collectionId"]) {
        if (o[k] !== undefined) out[k] = o[k];
    }
    return out;
}

function serializeCollectionItem(it: unknown): Record<string, unknown> | null {
    if (!it || typeof it !== "object") return null;
    const o = it as Record<string, unknown>;
    return {
        id: o["id"] ?? null,
        slug: o["slug"] ?? null,
        // fieldData is a record of field_id -> { type, value } typically
        field_data: o["fieldData"] ?? null,
    };
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        // Setting `inlineTextStyle` directly on createTextNode causes Framer
        // to drop the node's text content from the project save (the canvas
        // renders it but publish emits an empty <br>). Hold the style aside
        // and apply it via setAttributes after the text is set.
        const attrsRest: Record<string, unknown> = { ...attributes };
        const inlineTextStyle = attrsRest["inlineTextStyle"];
        delete attrsRest["inlineTextStyle"];
        const resolved = await resolveAttributes(f, attrsRest);
        const node = await f.createTextNode(
            resolved as Parameters<typeof f.createTextNode>[0],
            parentId,
        );
        if (!node) {
            return c.json({ ok: false, error: "createTextNode returned null" }, 500);
        }
        if (text !== undefined) {
            await node.setText(text);
        }
        if (inlineTextStyle !== undefined && inlineTextStyle !== null) {
            const styleAttrs = await resolveAttributes(f, { inlineTextStyle });
            await f.setAttributes(
                node.id,
                styleAttrs as Parameters<typeof f.setAttributes>[1],
            );
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        const resolved = await resolveAttributes(f, attributes);
        const node = await f.createFrameNode(
            resolved as Parameters<typeof f.createFrameNode>[0],
            parentId,
        );
        if (!node) {
            return c.json({ ok: false, error: "createFrameNode returned null" }, 500);
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return errResponse(c, err);
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
        const resolved = await resolveAttributes(f, attributes as Record<string, unknown>);
        const node = await f.setAttributes(
            nodeId,
            resolved as Parameters<typeof f.setAttributes>[1],
        );
        if (!node) {
            return c.json({ ok: false, error: "setAttributes returned null" }, 500);
        }
        return c.json({ ok: true, result: { id: node.id } });
    } catch (err) {
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
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
        return errResponse(c, err);
    }
});

app.post("/tools/get_color_styles", async (c) => {
    try {
        const f = await getFramer();
        const styles = await f.getColorStyles();
        return c.json({ ok: true, result: styles.map(serializeColorStyle) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/create_color_style", async (c) => {
    let body: { attributes?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const attrs = body.attributes;
    if (typeof attrs !== "object" || attrs === null) {
        return c.json({ ok: false, error: "missing_or_invalid_attributes (expected {name, light, dark?})" }, 400);
    }
    try {
        const f = await getFramer();
        const style = await f.createColorStyle(attrs as Parameters<typeof f.createColorStyle>[0]);
        return c.json({ ok: true, result: serializeColorStyle(style) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_text_styles", async (c) => {
    try {
        const f = await getFramer();
        const styles = await f.getTextStyles();
        return c.json({ ok: true, result: styles.map(serializeTextStyle) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/create_text_style", async (c) => {
    let body: { attributes?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const attrs = body.attributes;
    if (typeof attrs !== "object" || attrs === null) {
        return c.json({ ok: false, error: "missing_or_invalid_attributes" }, 400);
    }
    try {
        const f = await getFramer();
        const resolved = await resolveAttributes(f, attrs as Record<string, unknown>);
        const style = await f.createTextStyle(resolved as Parameters<typeof f.createTextStyle>[0]);
        return c.json({ ok: true, result: serializeTextStyle(style) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_fonts", async (c) => {
    try {
        const f = await getFramer();
        const fonts = await f.getFonts();
        return c.json({ ok: true, result: fonts.map(serializeFont) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_font", async (c) => {
    let body: { family?: unknown; weight?: unknown; style?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const family = body.family;
    if (typeof family !== "string" || !family) {
        return c.json({ ok: false, error: "missing_or_invalid_family" }, 400);
    }
    const fontAttrs: Record<string, unknown> = {};
    if (typeof body.weight === "number") fontAttrs["weight"] = body.weight;
    if (body.style === "normal" || body.style === "italic") fontAttrs["style"] = body.style;
    try {
        const f = await getFramer();
        const font = await f.getFont(family, Object.keys(fontAttrs).length ? fontAttrs as Parameters<typeof f.getFont>[1] : undefined);
        return c.json({ ok: true, result: serializeFont(font) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_project_info", async (c) => {
    try {
        const f = await getFramer();
        const info = await f.getProjectInfo();
        return c.json({
            ok: true,
            result: {
                id: info.id,
                name: info.name,
                api_version_1_id: info.apiVersion1Id ?? null,
            },
        });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_publish_info", async (c) => {
    try {
        const f = await getFramer();
        const info = await f.getPublishInfo();
        // PublishInfo shape varies — surface the raw object after a JSON round-trip
        // to filter out non-serializable fields (functions, symbols, class instances).
        const safe = JSON.parse(JSON.stringify(info, (_k, v) => {
            if (typeof v === "function" || typeof v === "symbol") return undefined;
            return v;
        }));
        return c.json({ ok: true, result: safe });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/screenshot", async (c) => {
    let body: { node_id?: unknown; format?: unknown; scale?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    const options: Record<string, unknown> = {};
    if (typeof body.format === "string") options["format"] = body.format;
    if (typeof body.scale === "number") options["scale"] = body.scale;
    try {
        const f = await getFramer();
        const r = await f.screenshot(nodeId, Object.keys(options).length
            ? options as Parameters<typeof f.screenshot>[1]
            : undefined);
        // Handle the various shapes ScreenshotResult might take:
        const result: Record<string, unknown> = {};
        if (r && typeof r === "object") {
            const ro = r as unknown as Record<string, unknown>;
            // Common fields:
            if (typeof ro["url"] === "string") result["url"] = ro["url"];
            if (typeof ro["mimeType"] === "string") result["mime_type"] = ro["mimeType"];
            if (typeof ro["width"] === "number") result["width"] = ro["width"];
            if (typeof ro["height"] === "number") result["height"] = ro["height"];
            // Binary data: base64-encode if present.
            const data = ro["data"];
            if (data instanceof Uint8Array) {
                result["data_base64"] = Buffer.from(data).toString("base64");
                result["byte_length"] = data.byteLength;
            } else if (typeof data === "string") {
                // Already-encoded (data URL or base64)
                result["data"] = data;
            }
        }
        return c.json({ ok: true, result });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/export_svg", async (c) => {
    let body: { node_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const nodeId = body.node_id;
    if (typeof nodeId !== "string" || !nodeId) {
        return c.json({ ok: false, error: "missing_or_invalid_node_id" }, 400);
    }
    try {
        const f = await getFramer();
        const svg = await f.exportSVG(nodeId);
        return c.json({ ok: true, result: { svg, length: svg.length } });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_locales", async (c) => {
    try {
        const f = await getFramer();
        const locales = await f.getLocales();
        return c.json({ ok: true, result: locales.map(serializeLocale) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_default_locale", async (c) => {
    try {
        const f = await getFramer();
        const l = await f.getDefaultLocale();
        return c.json({ ok: true, result: serializeLocale(l) });
    } catch (err) {
        return errResponse(c, err);
    }
});

// get_active_locale: NOT exposed.
// `getActiveLocale` is in framer-api's BlockedMethods (line ~7171 of dist/index.d.ts)
// and is not in $framerApiOnly. It is only callable from inside an actual Framer
// plugin (the running editor), not via the headless framer-api connection.
// Returning a 501 so the frontend wrapper can surface a clear error.
app.post("/tools/get_active_locale", async (c) => {
    return c.json({
        ok: false,
        error: "not_supported_via_framer_api: getActiveLocale is plugin-only (BlockedMethods); use get_default_locale or get_locales instead",
    }, 501);
});

app.post("/tools/create_code_file", async (c) => {
    let body: { name?: unknown; code?: unknown; edit_via_plugin?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const name = body.name;
    const code = body.code;
    if (typeof name !== "string" || !name) {
        return c.json({ ok: false, error: "missing_or_invalid_name (e.g. 'MyComponent.tsx')" }, 400);
    }
    if (typeof code !== "string") {
        return c.json({ ok: false, error: "missing_or_invalid_code" }, 400);
    }
    const options: { editViaPlugin?: boolean } | undefined =
        typeof body.edit_via_plugin === "boolean"
            ? { editViaPlugin: body.edit_via_plugin }
            : undefined;
    try {
        const f = await getFramer();
        const cf = await f.createCodeFile(name, code, options);
        return c.json({ ok: true, result: serializeCodeFile(cf) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_code_files", async (c) => {
    try {
        const f = await getFramer();
        const files = await f.getCodeFiles();
        return c.json({ ok: true, result: files.map(serializeCodeFile) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_code_file", async (c) => {
    let body: { id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const id = body.id;
    if (typeof id !== "string" || !id) {
        return c.json({ ok: false, error: "missing_or_invalid_id" }, 400);
    }
    try {
        const f = await getFramer();
        const cf = await f.getCodeFile(id);
        return c.json({ ok: true, result: serializeCodeFile(cf) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_collections", async (c) => {
    try {
        const f = await getFramer();
        const cols = await f.getCollections();
        return c.json({ ok: true, result: cols.map(serializeCollection) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_collection", async (c) => {
    let body: { id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const id = body.id;
    if (typeof id !== "string" || !id) {
        return c.json({ ok: false, error: "missing_or_invalid_id" }, 400);
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(id);
        return c.json({ ok: true, result: serializeCollection(col) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_collection_fields", async (c) => {
    let body: { collection_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const cid = body.collection_id;
    if (typeof cid !== "string" || !cid) {
        return c.json({ ok: false, error: "missing_or_invalid_collection_id" }, 400);
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(cid);
        if (!col) return c.json({ ok: false, error: "collection_not_found" }, 404);
        const fields = await col.getFields();
        return c.json({ ok: true, result: fields.map(serializeCollectionField) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/get_collection_items", async (c) => {
    let body: { collection_id?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const cid = body.collection_id;
    if (typeof cid !== "string" || !cid) {
        return c.json({ ok: false, error: "missing_or_invalid_collection_id" }, 400);
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(cid);
        if (!col) return c.json({ ok: false, error: "collection_not_found" }, 404);
        const items = await col.getItems();
        return c.json({ ok: true, result: items.map(serializeCollectionItem) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/create_collection", async (c) => {
    let body: { name?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const name = body.name;
    if (typeof name !== "string" || !name) {
        return c.json({ ok: false, error: "missing_or_invalid_name" }, 400);
    }
    try {
        const f = await getFramer();
        const col = await f.createCollection(name);
        return c.json({ ok: true, result: serializeCollection(col) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/add_collection_fields", async (c) => {
    let body: { collection_id?: unknown; fields?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const cid = body.collection_id;
    const fields = body.fields;
    if (typeof cid !== "string" || !cid) {
        return c.json({ ok: false, error: "missing_or_invalid_collection_id" }, 400);
    }
    if (!Array.isArray(fields) || fields.length === 0) {
        return c.json({ ok: false, error: "missing_or_invalid_fields (non-empty array)" }, 400);
    }
    // Light validation — each field needs at least `name` and `type`
    for (const fd of fields as Array<Record<string, unknown>>) {
        if (typeof fd["name"] !== "string" || typeof fd["type"] !== "string") {
            return c.json({ ok: false, error: "each field needs string name + type" }, 400);
        }
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(cid);
        if (!col) return c.json({ ok: false, error: "collection_not_found" }, 404);
        const added = await col.addFields(fields as Parameters<typeof col.addFields>[0]);
        return c.json({ ok: true, result: added.map(serializeCollectionField) });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/add_collection_items", async (c) => {
    let body: { collection_id?: unknown; items?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const cid = body.collection_id;
    const items = body.items;
    if (typeof cid !== "string" || !cid) {
        return c.json({ ok: false, error: "missing_or_invalid_collection_id" }, 400);
    }
    if (!Array.isArray(items) || items.length === 0) {
        return c.json({ ok: false, error: "missing_or_invalid_items (non-empty array)" }, 400);
    }
    // Light validation — each item needs at least a `slug`
    for (const it of items as Array<Record<string, unknown>>) {
        if (typeof it["slug"] !== "string" || !it["slug"]) {
            return c.json({ ok: false, error: "each item needs string slug" }, 400);
        }
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(cid);
        if (!col) return c.json({ ok: false, error: "collection_not_found" }, 404);
        await col.addItems(items as Parameters<typeof col.addItems>[0]);
        return c.json({ ok: true, result: { added: items.length } });
    } catch (err) {
        return errResponse(c, err);
    }
});

app.post("/tools/remove_collection_items", async (c) => {
    let body: { collection_id?: unknown; item_ids?: unknown };
    try { body = await c.req.json(); } catch { return c.json({ ok: false, error: "invalid_json" }, 400); }
    const cid = body.collection_id;
    const itemIds = body.item_ids;
    if (typeof cid !== "string" || !cid) {
        return c.json({ ok: false, error: "missing_or_invalid_collection_id" }, 400);
    }
    if (!Array.isArray(itemIds) || itemIds.length === 0 || !itemIds.every((x) => typeof x === "string")) {
        return c.json({ ok: false, error: "missing_or_invalid_item_ids (non-empty string array)" }, 400);
    }
    try {
        const f = await getFramer();
        const col = await f.getCollection(cid);
        if (!col) return c.json({ ok: false, error: "collection_not_found" }, 404);
        await col.removeItems(itemIds as string[]);
        return c.json({ ok: true, result: { removed: itemIds.length } });
    } catch (err) {
        return errResponse(c, err);
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

// Safety net — framer-api can throw async errors out-of-band when the
// WebSocket drops. Log + invalidate the cache so the next tool call
// reconnects cleanly, instead of letting the process crash.
process.on("unhandledRejection", (reason) => {
    const msg = reason instanceof Error ? reason.message : String(reason);
    console.error("[framer-sidecar] unhandledRejection:", msg);
    if (isConnectionError(msg)) {
        markFramerDirty(`unhandledRejection: ${msg}`);
    }
});

process.on("uncaughtException", (err) => {
    console.error("[framer-sidecar] uncaughtException:", err);
    if (isConnectionError(err.message)) {
        markFramerDirty(`uncaughtException: ${err.message}`);
    }
});

export { app };
