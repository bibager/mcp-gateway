"""
Framer MCP Frontend
===================
Dual-purpose Starlette app:
  - /mcp     FastMCP streamable-HTTP for Claude (CoWork or Code)
  - /health  unauthenticated health check

Auth: Authorization: Bearer {MCP_API_KEY} on every route except public OAuth/health.
The MCP OAuth flow is synthetic (auto-approves and returns MCP_API_KEY as the
access token), matching the pattern used by the other gateway services.

Tool calls are forwarded over localhost to the framer-sidecar Node process,
which holds the live framer-api WebSocket connection.

Env vars:
  MCP_API_KEY            required; protects all /mcp endpoints
  SIDECAR_INTERNAL_KEY   required; shared secret with sidecar (X-Sidecar-Key)
  SIDECAR_BASE_URL       optional; defaults to http://127.0.0.1:8006
  PORT                   optional; defaults to 8007
  SERVER_URL             optional; overrides base URL detection
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
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
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("framer_mcp")

# --- Config ------------------------------------------------------------------

MCP_API_KEY: str = os.environ["MCP_API_KEY"]
SIDECAR_INTERNAL_KEY: str = os.environ["SIDECAR_INTERNAL_KEY"]
SIDECAR_BASE_URL: str = os.getenv("SIDECAR_BASE_URL", "http://127.0.0.1:8006")
PORT: int = int(os.getenv("PORT", "8007"))

# --- Helpers -----------------------------------------------------------------


def _json(data: Any) -> str:
    return json.dumps(data, default=str, indent=2)


async def _call_sidecar(tool: str, args: dict[str, Any]) -> str:
    """Forward a tool call to the Node sidecar. Returns JSON-stringified result.
    Raises RuntimeError on transport failure or sidecar error."""
    url = f"{SIDECAR_BASE_URL}/tools/{tool}"
    async with httpx.AsyncClient(timeout=60.0) as client:
        try:
            r = await client.post(
                url,
                json=args,
                headers={"X-Sidecar-Key": SIDECAR_INTERNAL_KEY},
            )
        except httpx.RequestError as exc:
            logger.error("sidecar unreachable: %s", exc)
            raise RuntimeError(f"sidecar unreachable: {exc}") from exc
    try:
        data = r.json()
    except ValueError as exc:
        raise RuntimeError(
            f"sidecar returned non-JSON ({r.status_code}): {r.text[:200]}"
        ) from exc
    if not data.get("ok"):
        err = data.get("error", "unknown sidecar error")
        raise RuntimeError(f"sidecar error: {err}")
    return _json(data.get("result", {}))


# --- FastMCP instance --------------------------------------------------------

mcp = FastMCP(
    "framer_mcp",
    instructions=(
        "Tools to build, inspect, and ship pages in a Framer project via the Framer Server API. "
        "READ: get_current_page, get_node, get_children, get_parent, get_rect, get_nodes_with_type, "
        "get_project_info, get_publish_info, get_color_styles, get_text_styles, get_fonts, get_font, "
        "get_locales, get_default_locale, get_code_files, get_code_file. "
        "CREATE: create_web_page, create_design_page, create_frame, create_text_node, "
        "create_color_style, create_text_style, create_code_file, clone_node, clone_web_page. "
        "MUTATE: set_attributes (most powerful — change any editable trait by id), set_text, "
        "set_parent, set_frame_image, set_custom_code (inject <script>/<link> in head/body), "
        "add_redirects (URL rewrites), upload_image, delete_node. "
        "VISUAL FEEDBACK: screenshot (returns PNG/JPEG bytes as base64), export_svg. "
        "SHIP: publish (preview deployment), deploy (promote to production). "
        "HTML translation hint: walk the HTML, map <div>/<section> -> create_frame, "
        "<h1>-<h6>/<p> -> create_text_node with attributes.tag, <img> -> set_frame_image, "
        "CSS layout -> set_attributes with stack/grid/gap/padding traits. Use screenshot to "
        "self-verify what was rendered. "
        "CMS (content collections like blog posts, products, portfolio items): "
        "get_collections lists what exists; create_collection makes a new one; "
        "add_collection_fields defines schema (e.g. {name, type:'string'|'image'|'date'|'formattedText'|...}); "
        "add_collection_items populates with [{slug, fieldData:{<field_id>: value}}]; "
        "get_collection_items reads back; remove_collection_items deletes by id."
    ),
)

# --- MCP Tools ---------------------------------------------------------------


@mcp.tool(
    name="get_current_page",
    annotations={"readOnlyHint": True, "destructiveHint": False},
)
async def get_current_page() -> str:
    """Get metadata for the active Framer canvas page (id, name, type, path if a Web Page)."""
    return await _call_sidecar("get_current_page", {})


@mcp.tool(
    name="create_web_page",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def create_web_page(path: str) -> str:
    """
    Create a new Web Page in the Framer project.

    Args:
        path: URL path for the page, leading-slash style. Examples: "/about", "/blog/post-1".
    """
    return await _call_sidecar("create_web_page", {"path": path})


@mcp.tool(
    name="create_text_node",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def create_text_node(
    text: Optional[str] = None,
    attributes: Optional[dict[str, Any]] = None,
    parent_id: Optional[str] = None,
) -> str:
    """
    Create a TextNode on the canvas, optionally inside a parent frame.

    Args:
        text: Plain text content, applied via node.setText after creation.
        attributes: Partial Framer EditableTextNodeAttributes (font, size, position, etc.).
        parent_id: Existing frame's node id to insert the text into. Omit for canvas root.
    """
    args: dict[str, Any] = {}
    if text is not None:
        args["text"] = text
    if attributes is not None:
        args["attributes"] = attributes
    if parent_id is not None:
        args["parent_id"] = parent_id
    return await _call_sidecar("create_text_node", args)


@mcp.tool(
    name="create_design_page",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def create_design_page(name: str) -> str:
    """
    Create a new Design Page (component-library style page, not URL-routed).

    Args:
        name: Page name shown in the Framer pages panel.
    """
    return await _call_sidecar("create_design_page", {"name": name})


@mcp.tool(
    name="create_frame",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def create_frame(
    attributes: Optional[dict[str, Any]] = None,
    parent_id: Optional[str] = None,
) -> str:
    """
    Create a new FrameNode on the canvas. The workhorse for layout containers.

    Args:
        attributes: Partial Framer EditableFrameNodeAttributes. Common keys:
            width, height, x, y, backgroundColor, borderRadius, layout
            ("stack" | "grid" | "none"), stackDirection ("horizontal" | "vertical"),
            stackAlignment, stackDistribution, gap, padding, gridColumnCount,
            gridRowCount, etc. See Framer Plugin API trait docs.
        parent_id: Existing frame's node id to insert into. Omit for the canvas root.
    """
    args: dict[str, Any] = {}
    if attributes is not None:
        args["attributes"] = attributes
    if parent_id is not None:
        args["parent_id"] = parent_id
    return await _call_sidecar("create_frame", args)


@mcp.tool(
    name="set_attributes",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_attributes(node_id: str, attributes: dict[str, Any]) -> str:
    """
    Update editable attributes on any canvas node.

    Args:
        node_id: The node id (returned from create_frame, create_text_node, etc.).
        attributes: Partial attributes to set. Trait support depends on node type
            (e.g., backgroundColor on FrameNode, font on TextNode, layout on either).
    """
    return await _call_sidecar("set_attributes", {"node_id": node_id, "attributes": attributes})


@mcp.tool(
    name="set_text",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_text(node_id: str, text: str) -> str:
    """
    Replace the text content of an existing TextNode.

    Args:
        node_id: A TextNode id (errors with 400 if the node is not a TextNode).
        text: New plain text content.
    """
    return await _call_sidecar("set_text", {"node_id": node_id, "text": text})


@mcp.tool(
    name="delete_node",
    annotations={"readOnlyHint": False, "destructiveHint": True},
)
async def delete_node(node_id: str) -> str:
    """
    Delete a node from the canvas. This is destructive and cannot be undone via the API.

    Args:
        node_id: The id of the node to remove.
    """
    return await _call_sidecar("delete_node", {"node_id": node_id})


@mcp.tool(
    name="upload_image",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def upload_image(
    image_url: str,
    alt_text: Optional[str] = None,
    name: Optional[str] = None,
) -> str:
    """
    Upload an image from a URL to the project's asset library. Framer dedupes
    repeat uploads of the same content. Useful when you want the asset id/url
    for inspection before painting it onto a frame; otherwise prefer
    set_frame_image which combines upload + paint in one call.

    Args:
        image_url: HTTP(S) URL to the source image.
        alt_text: Optional alt text for accessibility.
        name: Optional asset name shown in the assets panel.
    """
    args: dict[str, Any] = {"image_url": image_url}
    if alt_text is not None:
        args["alt_text"] = alt_text
    if name is not None:
        args["name"] = name
    return await _call_sidecar("upload_image", args)


@mcp.tool(
    name="set_frame_image",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_frame_image(node_id: str, image_url: str) -> str:
    """
    Paint an image onto an existing FrameNode as its background. Uploads the image
    and applies it in one call. Errors with 400 if node_id is not a FrameNode.

    Args:
        node_id: A FrameNode id.
        image_url: HTTP(S) URL to the source image.
    """
    return await _call_sidecar("set_frame_image", {"node_id": node_id, "image_url": image_url})


@mcp.tool(
    name="publish",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def publish() -> str:
    """
    Publish a fresh preview deployment of the project. Returns the deployment id
    and a preview URL. Does not promote to production; call deploy(deployment_id)
    afterwards to push to custom domains.
    """
    return await _call_sidecar("publish", {})


@mcp.tool(
    name="deploy",
    annotations={"readOnlyHint": False, "destructiveHint": False},
)
async def deploy(deployment_id: str) -> str:
    """
    Promote an existing deployment to the project's production domain(s).
    Returns the list of hostnames now serving the deployment.

    Args:
        deployment_id: A deployment id from a previous publish() call.
    """
    return await _call_sidecar("deploy", {"deployment_id": deployment_id})


# --- v1.2 read tools -----------------------------------------------------

@mcp.tool(name="get_node", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_node(node_id: str) -> str:
    """Get any canvas node by id (id, name, type, path if a Web Page)."""
    return await _call_sidecar("get_node", {"node_id": node_id})


@mcp.tool(name="get_children", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_children(node_id: str) -> str:
    """List the direct children of a node."""
    return await _call_sidecar("get_children", {"node_id": node_id})


@mcp.tool(name="get_parent", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_parent(node_id: str) -> str:
    """Get a node's parent."""
    return await _call_sidecar("get_parent", {"node_id": node_id})


@mcp.tool(name="get_rect", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_rect(node_id: str) -> str:
    """Get the bounding rect of a node ({x, y, width, height} in canvas coords)."""
    return await _call_sidecar("get_rect", {"node_id": node_id})


@mcp.tool(name="get_nodes_with_type", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_nodes_with_type(type: str) -> str:
    """
    Find all nodes of a given class. Useful for "list every text node on the page" etc.

    Args:
        type: One of "FrameNode", "TextNode", "SVGNode", "ComponentInstanceNode",
              "WebPageNode", "DesignPageNode", "ComponentNode".
    """
    return await _call_sidecar("get_nodes_with_type", {"type": type})


# --- v1.2 manipulation tools ---------------------------------------------

@mcp.tool(name="clone_node", annotations={"readOnlyHint": False, "destructiveHint": False})
async def clone_node(node_id: str) -> str:
    """Duplicate any node. Returns the new node's id."""
    return await _call_sidecar("clone_node", {"node_id": node_id})


@mcp.tool(name="clone_web_page", annotations={"readOnlyHint": False, "destructiveHint": False})
async def clone_web_page(node_id: str) -> str:
    """Duplicate a Web Page (its full canvas tree). Returns the new page's id and path."""
    return await _call_sidecar("clone_web_page", {"node_id": node_id})


@mcp.tool(
    name="set_parent",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_parent(node_id: str, parent_id: str, index: Optional[int] = None) -> str:
    """
    Move a node to a new parent (or reorder among existing children).

    Args:
        node_id: Node to move.
        parent_id: New parent.
        index: Position among the parent's children (0-based). Omit for end.
    """
    args: dict[str, Any] = {"node_id": node_id, "parent_id": parent_id}
    if index is not None:
        args["index"] = index
    return await _call_sidecar("set_parent", args)


# --- v1.2 site settings tools --------------------------------------------

@mcp.tool(name="add_redirects", annotations={"readOnlyHint": False, "destructiveHint": False})
async def add_redirects(redirects: list[dict[str, Any]]) -> str:
    """
    Add URL rewrites. `from` paths can include `*` wildcards captured as `:1`, `:2` in `to`.

    Args:
        redirects: List of {from, to, expandToAllLocales?} entries. Example:
            [{"from": "/business", "to": "/enterprise", "expandToAllLocales": true},
             {"from": "/posts/*", "to": "/blog/:1"}]
    """
    return await _call_sidecar("add_redirects", {"redirects": redirects})


@mcp.tool(
    name="set_custom_code",
    annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True},
)
async def set_custom_code(location: str, html: Optional[str] = None) -> str:
    """
    Install or clear a custom HTML/JS snippet at a fixed location in every page.

    Args:
        location: Where to inject. One of "headStart", "headEnd", "bodyStart", "bodyEnd".
        html: HTML to install (e.g. <script src=...></script>). Pass None to clear.
    """
    return await _call_sidecar("set_custom_code", {"location": location, "html": html})


# --- v1.2 design system tools --------------------------------------------

@mcp.tool(name="get_color_styles", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_color_styles() -> str:
    """List all color styles in the project."""
    return await _call_sidecar("get_color_styles", {})


@mcp.tool(name="create_color_style", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_color_style(attributes: dict[str, Any]) -> str:
    """
    Create a reusable color style.

    Args:
        attributes: ColorStyleAttributes — at minimum {name, light}. Optional dark mode value.
            Example: {"name": "Brand Primary", "light": "#3b82f6", "dark": "#60a5fa"}
    """
    return await _call_sidecar("create_color_style", {"attributes": attributes})


@mcp.tool(name="get_text_styles", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_text_styles() -> str:
    """List all text styles (typography presets) in the project."""
    return await _call_sidecar("get_text_styles", {})


@mcp.tool(name="create_text_style", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_text_style(attributes: dict[str, Any]) -> str:
    """
    Create a reusable text style.

    Args:
        attributes: TextStyleAttributes — name + typography (font, fontSize, lineHeight,
            letterSpacing, transform, alignment, decoration, tag).
    """
    return await _call_sidecar("create_text_style", {"attributes": attributes})


@mcp.tool(name="get_fonts", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_fonts() -> str:
    """List all fonts available to the project (one entry per family/weight/style combo)."""
    return await _call_sidecar("get_fonts", {})


@mcp.tool(name="get_font", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_font(family: str, weight: Optional[int] = None, style: Optional[str] = None) -> str:
    """
    Look up a specific font by family + optional weight/style.

    Args:
        family: Font family name (e.g. "Inter", "Noto Sans"). Case-insensitive.
        weight: Numeric weight (e.g. 400, 700). Defaults to normal.
        style: "normal" or "italic". Defaults to "normal".
    """
    args: dict[str, Any] = {"family": family}
    if weight is not None:
        args["weight"] = weight
    if style is not None:
        args["style"] = style
    return await _call_sidecar("get_font", args)


# --- v1.2 project + visual feedback tools --------------------------------

@mcp.tool(name="get_project_info", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_project_info() -> str:
    """Get project name, id, and api version 1 id."""
    return await _call_sidecar("get_project_info", {})


@mcp.tool(name="get_publish_info", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_publish_info() -> str:
    """Get current production + staging deployment info (urls, deployment time, optimization status)."""
    return await _call_sidecar("get_publish_info", {})


@mcp.tool(name="screenshot", annotations={"readOnlyHint": True, "destructiveHint": False})
async def screenshot(
    node_id: str,
    format: Optional[str] = None,
    scale: Optional[float] = None,
) -> str:
    """
    Capture a PNG/JPEG of any canvas node. Returns base64-encoded bytes.

    Args:
        node_id: Node to screenshot.
        format: "png" (default) or "jpeg".
        scale: Pixel-density multiplier — 0.5, 1, 1.5, 2, 3, or 4. Default 1.
    """
    args: dict[str, Any] = {"node_id": node_id}
    if format is not None:
        args["format"] = format
    if scale is not None:
        args["scale"] = scale
    return await _call_sidecar("screenshot", args)


@mcp.tool(name="export_svg", annotations={"readOnlyHint": True, "destructiveHint": False})
async def export_svg(node_id: str) -> str:
    """Export a node as an SVG string."""
    return await _call_sidecar("export_svg", {"node_id": node_id})


# --- v1.2 locale tools ---------------------------------------------------

@mcp.tool(name="get_locales", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_locales() -> str:
    """List all locales in the project (excludes the default locale)."""
    return await _call_sidecar("get_locales", {})


@mcp.tool(name="get_default_locale", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_default_locale() -> str:
    """Get the project's default locale."""
    return await _call_sidecar("get_default_locale", {})


@mcp.tool(name="get_active_locale", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_active_locale() -> str:
    """
    Get the currently active locale. NOTE: This method is plugin-only in framer-api 0.1.7
    and will return a `not_supported_via_framer_api` error from the sidecar. Use
    get_default_locale + get_locales instead.
    """
    return await _call_sidecar("get_active_locale", {})


# --- v1.2 code file tools ------------------------------------------------

@mcp.tool(name="create_code_file", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_code_file(name: str, code: str, edit_via_plugin: Optional[bool] = None) -> str:
    """
    Create a new code file (React component / override) in the project.

    Args:
        name: Filename including extension. Use `.tsx` for components.
        code: Source code (typically a default export).
        edit_via_plugin: When True, the "Edit Code" UI action opens this plugin. Optional.
    """
    args: dict[str, Any] = {"name": name, "code": code}
    if edit_via_plugin is not None:
        args["edit_via_plugin"] = edit_via_plugin
    return await _call_sidecar("create_code_file", args)


@mcp.tool(name="get_code_files", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_code_files() -> str:
    """List all code files in the project (id, name, path, content)."""
    return await _call_sidecar("get_code_files", {})


@mcp.tool(name="get_code_file", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_code_file(id: str) -> str:
    """
    Fetch a specific code file by id.

    Args:
        id: The code file id (from get_code_files or create_code_file).
    """
    return await _call_sidecar("get_code_file", {"id": id})


# --- v1.3 CMS tools ------------------------------------------------------

@mcp.tool(name="get_collections", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_collections() -> str:
    """List all CMS collections in the project (id, name, managed_by, readonly)."""
    return await _call_sidecar("get_collections", {})


@mcp.tool(name="get_collection", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_collection(id: str) -> str:
    """
    Get metadata for a single collection.

    Args:
        id: Collection id (from get_collections).
    """
    return await _call_sidecar("get_collection", {"id": id})


@mcp.tool(name="get_collection_fields", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_collection_fields(collection_id: str) -> str:
    """
    List the field schema (columns) defined on a collection.

    Args:
        collection_id: Collection id.
    """
    return await _call_sidecar("get_collection_fields", {"collection_id": collection_id})


@mcp.tool(name="get_collection_items", annotations={"readOnlyHint": True, "destructiveHint": False})
async def get_collection_items(collection_id: str) -> str:
    """
    List all items in a collection (id, slug, field_data).

    Args:
        collection_id: Collection id.
    """
    return await _call_sidecar("get_collection_items", {"collection_id": collection_id})


@mcp.tool(name="create_collection", annotations={"readOnlyHint": False, "destructiveHint": False})
async def create_collection(name: str) -> str:
    """
    Create a new (user-facing) CMS collection. Returns the new collection's id and name.
    Define its schema afterwards with add_collection_fields, then populate with add_collection_items.

    Args:
        name: Collection display name (e.g. "Blog Posts").
    """
    return await _call_sidecar("create_collection", {"name": name})


@mcp.tool(name="add_collection_fields", annotations={"readOnlyHint": False, "destructiveHint": False})
async def add_collection_fields(collection_id: str, fields: list[dict[str, Any]]) -> str:
    """
    Add fields (columns) to a collection.

    Args:
        collection_id: Collection id.
        fields: Array of field definitions. Each entry must have at least
            {name, type}. Common types: "string", "formattedText", "number",
            "boolean", "color", "image", "file", "link", "date", "enum",
            "collectionReference", "multiCollectionReference", "array".
            Type-specific fields (e.g. enum.cases, collectionReference.collectionId)
            depend on the field type — see Framer's CMS field docs.
            Example: [{"name": "Title", "type": "string"},
                     {"name": "Body", "type": "formattedText"},
                     {"name": "Cover", "type": "image"}]
    """
    return await _call_sidecar("add_collection_fields", {"collection_id": collection_id, "fields": fields})


@mcp.tool(name="add_collection_items", annotations={"readOnlyHint": False, "destructiveHint": False})
async def add_collection_items(collection_id: str, items: list[dict[str, Any]]) -> str:
    """
    Add items (rows) to a collection.

    Args:
        collection_id: Collection id.
        items: Array of item entries. Each entry must have a `slug` (URL-safe
            unique identifier) and may include a `fieldData` map keyed by
            field id with type-specific values.
            Example: [{"slug": "first-post",
                       "fieldData": {"<title-field-id>": {"type": "string", "value": "Hello"},
                                     "<body-field-id>": {"type": "formattedText", "value": "<p>Hi</p>"}}}]
            (Run get_collection_fields first to discover the field ids.)
    """
    return await _call_sidecar("add_collection_items", {"collection_id": collection_id, "items": items})


@mcp.tool(name="remove_collection_items", annotations={"readOnlyHint": False, "destructiveHint": True})
async def remove_collection_items(collection_id: str, item_ids: list[str]) -> str:
    """
    Permanently delete items from a collection. Cannot be undone via the API.

    Args:
        collection_id: Collection id.
        item_ids: Array of item ids to remove.
    """
    return await _call_sidecar("remove_collection_items", {"collection_id": collection_id, "item_ids": item_ids})


# --- OAuth 2.0 with PKCE (synthetic — issues MCP_API_KEY as access_token) ---

_oauth_codes: dict[str, dict[str, Any]] = {}
_oauth_clients: dict[str, dict[str, Any]] = {}
OAUTH_CODE_TTL = 300


def _cleanup_expired_codes() -> None:
    now = time.time()
    expired = [c for c, m in _oauth_codes.items() if m["expires_at"] < now]
    for c in expired:
        del _oauth_codes[c]


# --- Auth Middleware ----------------------------------------------------------

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


# --- Utility Routes ----------------------------------------------------------


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


# --- OAuth 2.0 Endpoints (synthetic PKCE flow) -------------------------------


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
    return JSONResponse({
        "resource": base,
        "authorization_servers": [base],
    })


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


async def oauth_authorize(request: Request) -> JSONResponse:
    from starlette.responses import RedirectResponse

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

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")

    if grant_type != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)

    _cleanup_expired_codes()
    code_meta = _oauth_codes.pop(code, None)
    if not code_meta:
        return JSONResponse(
            {"error": "invalid_grant", "error_description": "Code expired or invalid"},
            status_code=400,
        )

    if code_meta["code_challenge"]:
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if computed != code_meta["code_challenge"]:
            return JSONResponse(
                {"error": "invalid_grant", "error_description": "PKCE verification failed"},
                status_code=400,
            )

    return JSONResponse({
        "access_token": MCP_API_KEY,
        "token_type": "Bearer",
    })


# --- App Assembly ------------------------------------------------------------

mcp_asgi = mcp.http_app()


@asynccontextmanager
async def lifespan(app: Starlette):
    async with mcp_asgi.lifespan(app):
        logger.info("Framer MCP frontend ready on port %d", PORT)
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
