from __future__ import annotations

import os
from typing import Any, Dict

import httpx
from fastmcp import FastMCP


mcp = FastMCP("__SERVER_NAME__")

API_BASE_URL = os.getenv("API_BASE_URL", "https://api.example.com")
API_TOKEN = os.getenv("API_TOKEN")
REQUEST_TIMEOUT = float(os.getenv("API_TIMEOUT_SECONDS", "20"))


def _headers() -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if API_TOKEN:
        headers["Authorization"] = f"Bearer {API_TOKEN}"
    return headers


def _request(method: str, path: str, *, params: Dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE_URL.rstrip('/')}/{path.lstrip('/')}"
    with httpx.Client(timeout=REQUEST_TIMEOUT, headers=_headers()) as client:
        response = client.request(method, url, params=params)
        response.raise_for_status()
        return response.json()


@mcp.tool
def health_check() -> Dict[str, Any]:
    """Check whether the upstream API is reachable."""
    payload = _request("GET", "/health")
    return {"base_url": API_BASE_URL, "result": payload}


@mcp.tool
def get_resource(resource_id: str) -> Dict[str, Any]:
    """Fetch one resource by ID from the upstream API."""
    payload = _request("GET", f"/resources/{resource_id}")
    return {"resource_id": resource_id, "data": payload}


@mcp.tool
def search_resources(query: str, limit: int = 10) -> Dict[str, Any]:
    """Search upstream resources by query string."""
    payload = _request("GET", "/resources", params={"q": query, "limit": limit})
    return {"query": query, "limit": limit, "results": payload}


if __name__ == "__main__":
    mcp.run()
