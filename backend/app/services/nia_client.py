from __future__ import annotations

import os
from typing import Any

import httpx

_BASE_URL = "https://apigcp.trynia.ai/v2"


class NiaClient:
    """Async HTTP client for the Nia code-search API."""

    def __init__(self) -> None:
        api_key = os.environ.get("NIA_API_KEY", "")
        self._http = httpx.AsyncClient(
            base_url=_BASE_URL,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=30.0,
        )

    async def index_repository(self, owner_repo: str) -> dict[str, Any]:
        """POST /v2/repositories — trigger indexing of a GitHub repo."""
        r = await self._http.post("/repositories", json={"repository": owner_repo})
        r.raise_for_status()
        return r.json()

    async def check_status(self, owner_repo: str) -> dict[str, Any]:
        """GET /v2/repositories/{owner_repo} — poll indexing status."""
        r = await self._http.get(f"/repositories/{owner_repo}")
        r.raise_for_status()
        return r.json()

    async def search(
        self, query: str, repositories: list[str]
    ) -> dict[str, Any]:
        """POST /v2/universal-search — semantic code search across repos."""
        r = await self._http.post(
            "/universal-search",
            json={"query": query, "repositories": repositories},
        )
        r.raise_for_status()
        return r.json()

    async def aclose(self) -> None:
        await self._http.aclose()
