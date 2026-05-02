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

let framerInstance: Framer | null = null;

export async function getFramer(): Promise<Framer> {
    if (framerInstance) return framerInstance;
    framerInstance = await connect(PROJECT_URL, API_KEY);
    console.log(`[framer-sidecar] connected to ${PROJECT_URL}`);
    return framerInstance;
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
