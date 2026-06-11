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

    # ---- Sessions ---------------------------------------------------------
    async def open_session(
        self, service: str, *, auto_start: bool = True, user_id: int | None = None
    ) -> dict[str, object]:
        """Open a service session against the orchestrator.

        Long timeout because auto_start may trigger a compose-up that pulls
        an image. Same number as start_service() for consistency.
        """
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/sessions",
                json={"service": service, "auto_start": auto_start, "user_id": user_id},
            )
            resp.raise_for_status()
            return resp.json()

    async def checkin_session(self, token: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.post(f"{self.base_url}/api/sessions/{token}/checkin")
            resp.raise_for_status()
            return resp.json()

    async def close_session(self, token: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(f"{self.base_url}/api/sessions/{token}")
            resp.raise_for_status()

    async def list_sessions(
        self, *, active: bool = True, limit: int = 100, offset: int = 0
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/sessions",
                params={"active": str(active).lower(), "limit": limit, "offset": offset},
            )
            resp.raise_for_status()
            return resp.json()

    # ---- Pinning ----------------------------------------------------------
    async def pin_service(
        self, name: str, *, ttl_seconds: int | None = 7200, reason: str | None = None
    ) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/services/{name}/pin",
                json={"ttl_seconds": ttl_seconds, "reason": reason},
            )
            resp.raise_for_status()
            return resp.json()

    async def unpin_service(self, name: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.delete(f"{self.base_url}/api/services/{name}/pin")
            resp.raise_for_status()

    async def get_pin(self, name: str) -> dict[str, object] | None:
        """Return the current pin record for `name`, or None if not pinned.

        Relies on the orchestrator's GET /api/services/{name}/pin endpoint
        (added in M3.4) — 404 means not pinned, 200 means pinned with
        details.
        """
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{self.base_url}/api/services/{name}/pin")
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()

    # ---- Audit ------------------------------------------------------------
    async def list_audit(
        self, *, event_type: str | None = None, target: str | None = None,
        since: str | None = None, until: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> dict[str, object]:
        params: dict[str, object] = {"limit": limit, "offset": offset}
        if event_type:
            params["event_type"] = event_type
        if target:
            params["target"] = target
        if since:
            params["since"] = since
        if until:
            params["until"] = until
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{self.base_url}/api/audit", params=params)
            resp.raise_for_status()
            return resp.json()

    # ---- Auth (M3.5) ------------------------------------------------------
    async def auth_login(
        self, username_or_email: str, password: str
    ) -> tuple[dict[str, object], str | None]:
        """Login. Returns (json_body, set_cookie_header)."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/auth/login",
                json={"username_or_email": username_or_email, "password": password},
            )
            resp.raise_for_status()
            return resp.json(), resp.headers.get("set-cookie")

    async def auth_logout(self, session_cookie: str | None) -> None:
        cookies = {"orchestack_session": session_cookie} if session_cookie else {}
        async with httpx.AsyncClient(timeout=5.0, cookies=cookies) as client:
            resp = await client.post(f"{self.base_url}/api/auth/logout")
            # 204 is success, 401 is "already logged out" — neither is a problem.
            if resp.status_code not in (200, 204, 401):
                resp.raise_for_status()

    async def auth_me(self, session_cookie: str | None) -> dict[str, object] | None:
        """Return the user identity for `session_cookie`, or None if invalid."""
        if not session_cookie:
            return None
        cookies = {"orchestack_session": session_cookie}
        async with httpx.AsyncClient(timeout=3.0, cookies=cookies) as client:
            resp = await client.get(f"{self.base_url}/api/auth/me")
            if resp.status_code == 401:
                return None
            resp.raise_for_status()
            return resp.json()

    # ---- Credentials (M3.6 polish) ----------------------------------------
    async def list_credentials(self, reveal: bool = False) -> dict[str, object]:
        """Return every variable in the operator's `.env` with metadata."""
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(
                f"{self.base_url}/api/credentials",
                params={"reveal": str(reveal).lower()},
            )
            resp.raise_for_status()
            return resp.json()

    async def update_credential(
        self, key: str, value: str, *, actor_user_id: int | None = None
    ) -> dict[str, object]:
        """Update a single .env variable. Audit log records the key only."""
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.put(
                f"{self.base_url}/api/credentials/{key}",
                json={"value": value, "actor_user_id": actor_user_id},
            )
            resp.raise_for_status()
            return resp.json()

    # ---- Admin: users / roles / role-permissions --------------------------
    def _admin_cookies(self, session_cookie: str | None) -> dict[str, str]:
        return {"orchestack_session": session_cookie} if session_cookie else {}

    async def admin_list_users(self, session_cookie: str | None) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.get(f"{self.base_url}/api/admin/users")
            r.raise_for_status()
            return r.json()

    async def admin_invite_user(
        self, session_cookie: str | None,
        username: str, email: str, full_name: str,
        role_names: list[str] | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.post(
                f"{self.base_url}/api/admin/users/invite",
                json={
                    "username": username, "email": email,
                    "full_name": full_name,
                    "role_names": role_names or [],
                },
            )
            r.raise_for_status()
            return r.json()

    async def admin_toggle_user(
        self, session_cookie: str | None, user_id: int, enable: bool,
    ) -> dict:
        action = "enable" if enable else "disable"
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.post(f"{self.base_url}/api/admin/users/{user_id}/{action}")
            r.raise_for_status()
            return r.json()

    async def admin_grant_user_role(
        self, session_cookie: str | None, user_id: int, role_id: int,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.post(
                f"{self.base_url}/api/admin/user-roles",
                json={"user_id": user_id, "role_id": role_id},
            )
            r.raise_for_status()
            return r.json()

    async def admin_revoke_user_role(
        self, session_cookie: str | None, user_id: int, role_id: int,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.delete(
                f"{self.base_url}/api/admin/user-roles/{user_id}/{role_id}",
            )
            r.raise_for_status()
            return r.json()

    async def admin_list_roles(self, session_cookie: str | None) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.get(f"{self.base_url}/api/admin/roles")
            r.raise_for_status()
            return r.json()

    async def admin_create_role(
        self, session_cookie: str | None, name: str, description: str | None = None,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.post(
                f"{self.base_url}/api/admin/roles",
                json={"name": name, "description": description},
            )
            r.raise_for_status()
            return r.json()

    async def admin_delete_role(self, session_cookie: str | None, role_id: int) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.delete(f"{self.base_url}/api/admin/roles/{role_id}")
            r.raise_for_status()
            return r.json()

    async def admin_list_permissions(
        self, session_cookie: str | None, role_id: int | None = None,
    ) -> dict:
        params = {"role_id": role_id} if role_id is not None else {}
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.get(f"{self.base_url}/api/admin/role-permissions", params=params)
            r.raise_for_status()
            return r.json()

    async def admin_grant_permission(
        self, session_cookie: str | None, role_id: int, service_name: str,
        can_start: bool, can_use: bool, can_force_stop: bool, can_edit_config: bool,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.post(
                f"{self.base_url}/api/admin/role-permissions",
                json={
                    "role_id": role_id,
                    "service_name": service_name,
                    "can_start": can_start, "can_use": can_use,
                    "can_force_stop": can_force_stop, "can_edit_config": can_edit_config,
                },
            )
            r.raise_for_status()
            return r.json()

    async def admin_revoke_permission(
        self, session_cookie: str | None, permission_id: int,
    ) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.delete(
                f"{self.base_url}/api/admin/role-permissions/{permission_id}",
            )
            r.raise_for_status()
            return r.json()
