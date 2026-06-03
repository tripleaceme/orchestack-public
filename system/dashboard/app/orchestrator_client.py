"""Thin async HTTP wrapper around the orchestrator's REST API.

Why a dedicated module:
    - Centralised timeout policy (different per operation type).
    - Single place to wire session-cookie headers when phase 3.5 lands.
    - Tests can substitute a fake implementation without monkeypatching httpx.

Timeout policy:
    list_services()  →  3s   (UI read; should be sub-second normally)
    start_service()  →  180s (compose up may pull images on first run)
    stop_service()   →  60s  (compose stop is fast but allow for slow shutdown)
    list_health()    →  3s   (UI read)

All methods raise httpx exceptions on transport failure and
`httpx.HTTPStatusError` on a non-2xx response. Callers in the dashboard
catch these and render an error fragment — we don't swallow errors here.
"""

from __future__ import annotations

import httpx


class OrchestratorClient:
    """Async client for the OrcheStack orchestrator's HTTP API.

    Constructed once at module import in `main.py`. Stateless other than
    its base URL, so per-request `AsyncClient`s are safe — see module
    docstring for the rationale.
    """

    def __init__(self, base_url: str) -> None:
        # Strip a trailing slash so we can always concatenate "/api/..."
        self.base_url = base_url.rstrip("/")

    async def list_services(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{self.base_url}/api/services")
            resp.raise_for_status()
            return resp.json()

    async def get_service(self, name: str) -> dict[str, object] | None:
        """Return one service's dict from the catalogue list, or None.

        The orchestrator doesn't currently expose a per-service GET — we
        list and filter client-side. That's fine while the catalogue is
        small (~9 entries); add a server-side route if it grows.
        """
        data = await self.list_services()
        for svc in data.get("services", []):
            if svc.get("name") == name:
                return svc
        return None

    async def start_service(self, name: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{self.base_url}/api/services/{name}/start")
            resp.raise_for_status()
            return resp.json()

    async def stop_service(self, name: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{self.base_url}/api/services/{name}/stop")
            resp.raise_for_status()
            return resp.json()

    async def health(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{self.base_url}/api/health")
            resp.raise_for_status()
            return resp.json()
