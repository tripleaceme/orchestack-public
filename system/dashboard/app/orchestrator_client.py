"""Thin async HTTP wrapper around the orchestrator's REST API."""

from __future__ import annotations

import httpx


class OrchestratorClient:
    """Async client for the OrcheStack orchestrator's HTTP API."""

    def __init__(self, base_url: str) -> None:
        # Strip a trailing slash so we can always concatenate "/api/..."
        self.base_url = base_url.rstrip("/")

    async def list_services(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(f"{self.base_url}/api/services")
            resp.raise_for_status()
            return resp.json()

    async def get_service(self, name: str) -> dict[str, object] | None:
        """Return one service's dict from the catalogue list, or None."""
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

    async def disable_service(self, name: str) -> dict[str, object]:
        # Longer timeout: disable runs `compose down` which can take ~30s
        # if the container has slow shutdown hooks.
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.post(f"{self.base_url}/api/services/{name}/disable")
            resp.raise_for_status()
            return resp.json()

    async def enable_service(self, name: str) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{self.base_url}/api/services/{name}/enable")
            resp.raise_for_status()
            return resp.json()

    # ---- Pipelines ---------------------------------------------------
    async def list_pipelines(self) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/api/pipelines")
            r.raise_for_status()
            return r.json()

    async def get_pipeline(self, pipeline_id: int) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/api/pipelines/{pipeline_id}")
            r.raise_for_status()
            return r.json()

    async def create_pipeline(self, body: dict, *, actor_user_id: int | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {"actor_user_id": actor_user_id} if actor_user_id is not None else {}
            r = await client.post(f"{self.base_url}/api/pipelines", json=body, params=params)
            r.raise_for_status()
            return r.json()

    async def update_pipeline(self, pipeline_id: int, body: dict, *, actor_user_id: int | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            params = {"actor_user_id": actor_user_id} if actor_user_id is not None else {}
            r = await client.put(f"{self.base_url}/api/pipelines/{pipeline_id}", json=body, params=params)
            r.raise_for_status()
            return r.json()

    async def delete_pipeline(self, pipeline_id: int, *, actor_user_id: int | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"actor_user_id": actor_user_id} if actor_user_id is not None else {}
            r = await client.delete(f"{self.base_url}/api/pipelines/{pipeline_id}", params=params)
            r.raise_for_status()
            return r.json()

    async def run_pipeline_now(self, pipeline_id: int, *, actor_user_id: int | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"actor_user_id": actor_user_id} if actor_user_id is not None else {}
            r = await client.post(f"{self.base_url}/api/pipelines/{pipeline_id}/run", params=params)
            r.raise_for_status()
            return r.json()

    async def cancel_pipeline_run(self, run_id: int, *, actor_user_id: int | None = None) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            params = {"actor_user_id": actor_user_id} if actor_user_id is not None else {}
            r = await client.post(f"{self.base_url}/api/pipelines/runs/{run_id}/cancel", params=params)
            r.raise_for_status()
            return r.json()

    async def list_pipeline_runs(self, pipeline_id: int, limit: int = 20) -> dict[str, object]:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{self.base_url}/api/pipelines/{pipeline_id}/runs", params={"limit": limit})
            r.raise_for_status()
            return r.json()

    async def delete_service(self, name: str, *, wipe_volumes: bool = False) -> dict[str, object]:
        # Volume wipe can take longer for large volumes (Airflow logs, Airbyte
        # workspace cache); keep the timeout generous.
        async with httpx.AsyncClient(timeout=180.0) as client:
            resp = await client.delete(
                f"{self.base_url}/api/services/{name}",
                params={"wipe_volumes": str(wipe_volumes).lower()},
            )
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
        # Long timeout because auto_start may trigger a compose-up that pulls an image.
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
        """Return the current pin record for `name`, or None if not pinned."""
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

    # ---- Auth -------------------------------------------------------------
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

    # ---- Credentials ------------------------------------------------------
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

    async def test_credential(self, key: str, value: str) -> dict[str, object]:
        """Live-test a credential before saving; non-DB keys return testable=False."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{self.base_url}/api/credentials/{key}/test",
                json={"value": value},
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

    # Self-service profile (works for any signed-in user, not just Admin).
    async def get_my_profile(self, session_cookie: str | None) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.get(f"{self.base_url}/api/users/me")
            r.raise_for_status()
            return r.json()

    async def update_my_profile(
        self, session_cookie: str | None,
        full_name: str | None = None,
        email: str | None = None,
        company_name: str | None = None,
        current_password: str | None = None,
        new_password: str | None = None,
    ) -> dict:
        # Only include keys the operator wants to change — backend treats
        # missing keys as "no change," so blank form fields must not be sent.
        payload: dict[str, str] = {}
        if full_name is not None:    payload["full_name"]    = full_name
        if email is not None:        payload["email"]        = email
        if company_name is not None: payload["company_name"] = company_name
        if current_password is not None and new_password is not None:
            payload["current_password"] = current_password
            payload["new_password"]     = new_password
        async with httpx.AsyncClient(timeout=10.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.patch(f"{self.base_url}/api/users/me", json=payload)
            r.raise_for_status()
            return r.json()

    # Role-based service permissions — gating signal for non-admin users
    # (admins always see everything).
    async def list_my_service_permissions(self, session_cookie: str | None) -> dict:
        async with httpx.AsyncClient(timeout=5.0,
                                       cookies=self._admin_cookies(session_cookie)) as c:
            r = await c.get(f"{self.base_url}/api/users/me/services")
            r.raise_for_status()
            return r.json()
