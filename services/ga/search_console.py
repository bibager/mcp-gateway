"""Google Search Console API tools for MCP server."""

import json
import os
import tempfile

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


def _get_service():
    """Build a Search Console API service using ADC credentials."""
    adc_path = os.environ.get(
        "GOOGLE_APPLICATION_CREDENTIALS",
        os.path.join(tempfile.gettempdir(), "adc.json"),
    )
    credentials = Credentials.from_authorized_user_file(
        adc_path,
        scopes=["https://www.googleapis.com/auth/webmasters.readonly"],
    )
    return build("searchconsole", "v1", credentials=credentials)


def list_search_console_sites() -> str:
    """List all Google Search Console properties (sites) the user has access to.

    Returns a JSON array of sites with their URL and permission level.
    """
    service = _get_service()
    response = service.sites().list().execute()
    sites = response.get("siteEntry", [])
    return json.dumps(sites, indent=2)


def query_search_analytics(
    site_url: str,
    start_date: str,
    end_date: str,
    dimensions: list[str] | None = None,
    row_limit: int = 100,
    dimension_filters: list[dict] | None = None,
) -> str:
    """Query Google Search Console search analytics data.

    Returns search performance data including clicks, impressions, CTR, and
    average position for the specified site and date range.

    Args:
        site_url: The Search Console property URL (e.g. "https://trackiq.com/" or "sc-domain:trackiq.com").
        start_date: Start date in YYYY-MM-DD format.
        end_date: End date in YYYY-MM-DD format.
        dimensions: List of dimensions to group by. Options: "query", "page", "date", "device", "country", "searchAppearance". Defaults to ["query"].
        row_limit: Max rows to return (1-25000). Defaults to 100.
        dimension_filters: Optional list of filters, each with keys "dimension", "operator" ("contains", "equals", "notContains", "notEquals", "includingRegex", "excludingRegex"), and "expression".
    """
    service = _get_service()

    if dimensions is None:
        dimensions = ["query"]

    body = {
        "startDate": start_date,
        "endDate": end_date,
        "dimensions": dimensions,
        "rowLimit": min(row_limit, 25000),
        "startRow": 0,
    }

    if dimension_filters:
        body["dimensionFilterGroups"] = [{
            "groupType": "and",
            "filters": dimension_filters,
        }]

    response = service.searchanalytics().query(
        siteUrl=site_url,
        body=body,
    ).execute()

    rows = response.get("rows", [])
    results = []
    for row in rows:
        entry = {
            "keys": dict(zip(dimensions, row["keys"])),
            "clicks": row["clicks"],
            "impressions": row["impressions"],
            "ctr": round(row["ctr"] * 100, 2),
            "position": round(row["position"], 1),
        }
        results.append(entry)

    return json.dumps({
        "row_count": len(results),
        "rows": results,
    }, indent=2)


def inspect_url(
    site_url: str,
    inspection_url: str,
) -> str:
    """Inspect a URL's Google Search index status.

    Returns indexing status, last crawl time, canonical URL, and mobile
    usability for a specific page.

    Args:
        site_url: The Search Console property URL (e.g. "https://trackiq.com/").
        inspection_url: The full URL to inspect (e.g. "https://trackiq.com/pricing").
    """
    service = _get_service()

    result = service.urlInspection().index().inspect(body={
        "inspectionUrl": inspection_url,
        "siteUrl": site_url,
    }).execute()

    return json.dumps(result, indent=2)
