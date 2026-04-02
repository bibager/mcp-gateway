"""Google Analytics MCP Server — Streamable HTTP transport for CoWork."""

import json
import os
import tempfile

from mcp.server.fastmcp import FastMCP

from analytics_mcp.tools.admin.info import (
    get_account_summaries,
    list_google_ads_links,
    get_property_details,
    list_property_annotations,
)
from analytics_mcp.tools.reporting.core import (
    run_report,
    _run_report_description,
)
from analytics_mcp.tools.reporting.realtime import (
    run_realtime_report,
    _run_realtime_report_description,
)
from analytics_mcp.tools.reporting.metadata import (
    get_custom_dimensions_and_metrics,
)
from search_console import (
    list_search_console_sites,
    query_search_analytics,
    inspect_url,
)


def _setup_adc():
    """Write ADC credentials file from env vars if not already present."""
    refresh_token = os.environ.get("GOOGLE_REFRESH_TOKEN")
    client_id = os.environ.get("GA_CLIENT_ID", os.environ.get("GOOGLE_CLIENT_ID"))
    client_secret = os.environ.get("GA_CLIENT_SECRET", os.environ.get("GOOGLE_CLIENT_SECRET"))
    if refresh_token and client_id and client_secret:
        adc_path = os.path.join(tempfile.gettempdir(), "adc.json")
        adc_data = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "type": "authorized_user",
        }
        with open(adc_path, "w") as f:
            json.dump(adc_data, f)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = adc_path


_setup_adc()

# Create FastMCP server
mcp = FastMCP(
    "Google Analytics MCP Server",
    host="0.0.0.0",
    stateless_http=True,
)

# Register GA tools directly so FastMCP can introspect their signatures
mcp.tool()(get_account_summaries)
mcp.tool()(list_google_ads_links)
mcp.tool()(get_property_details)
mcp.tool()(list_property_annotations)
mcp.tool()(get_custom_dimensions_and_metrics)
mcp.tool(description=_run_report_description())(run_report)
mcp.tool(description=_run_realtime_report_description())(run_realtime_report)
mcp.tool()(list_search_console_sites)
mcp.tool()(query_search_analytics)
mcp.tool()(inspect_url)


# --- Auth middleware and composed app ---
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse as StarletteJSONResponse
from starlette.routing import Mount, Route
from auth import auth_routes, validate_token


class AuthMiddleware(BaseHTTPMiddleware):
    """Require valid Bearer token on /mcp endpoints."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/register",
            "/authorize",
            "/callback",
            "/token",
            "/health",
        ):
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token_data = validate_token(auth_header)
        if not token_data:
            base = os.environ.get("SERVER_URL", "http://localhost:8080").rstrip("/")
            return StarletteJSONResponse(
                {"error": "unauthorized"},
                status_code=401,
                headers={
                    "WWW-Authenticate": f'Bearer resource_metadata="{base}/.well-known/oauth-protected-resource"',
                },
            )
        return await call_next(request)


async def health(request):
    return StarletteJSONResponse({"status": "ok"})


mcp_app = mcp.streamable_http_app()


@asynccontextmanager
async def lifespan(app):
    """Chain the MCP app's lifespan so its task group is initialized."""
    async with mcp_app.router.lifespan_context(app):
        yield


app = Starlette(
    routes=[
        *auth_routes,
        Route("/health", health, methods=["GET"]),
        Mount("/", app=mcp_app),
    ],
    middleware=[Middleware(AuthMiddleware)],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8002"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
