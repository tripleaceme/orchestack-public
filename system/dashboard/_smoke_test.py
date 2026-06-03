"""Dashboard smoke test — phases 3.1 → 3.6.

Drives every dashboard route against a stubbed OrchestratorClient.
Verifies routes wire up, templates render, error paths degrade gracefully,
and the auth dependency redirects unauthenticated users to /login.

Run from system/dashboard/ with the .smoke-venv interpreter:
    .smoke-venv/bin/python _smoke_test.py
"""
from __future__ import annotations
import asyncio, sys

import httpx
from httpx import ASGITransport

import app.main as appmod
from app.main import app


# ============================================================================
#  Stub orchestrator — drop-in replacement for OrchestratorClient.
# ============================================================================
class FakeOrchestrator:
    def __init__(self):
        self.unreachable = False
        self.services = [
            {"name": "metabase", "display_name": "Metabase",
             "tier": "hot", "layer": "bi",
             "state": "running", "container": "orchestack-metabase",
             "managed": True},
            {"name": "pgadmin", "display_name": "pgAdmin",
             "tier": "cold", "layer": "admin-ui",
             "state": "stopped", "container": None,
             "managed": True},
            {"name": "airbyte", "display_name": "Airbyte",
             "tier": "hot", "layer": "ingestion",
             "state": "stopped", "container": None,
             "managed": False},
        ]
        self.next_session_token = "abc-123-token"
        self.sessions_db = []
        self.pins = {}  # service → pin record
        self.audit_events = [
            {"id": 1, "event_type": "service_started", "target": "metabase",
             "actor_username": "system", "actor_full_name": "System",
             "actor_user_id": 1, "details": {"returncode": 0},
             "ip_address": None, "created_at": "2026-06-03T12:00:00+00:00"},
            {"id": 2, "event_type": "session_opened", "target": "pgadmin",
             "actor_username": "ayoade", "actor_full_name": "Ayoade",
             "actor_user_id": 2, "details": {"auto_start": True},
             "ip_address": None, "created_at": "2026-06-03T11:55:00+00:00"},
        ]
        self.users = {}
        self.session_cookies = {}  # cookie_value → user dict
        self.test_user = {
            "user_id": 1, "username": "tester", "email": "test@test.com",
            "full_name": "Test User", "roles": ["Admin"],
        }

    async def list_services(self):
        if self.unreachable:
            raise httpx.ConnectError("stub unreachable")
        return {"services": self.services}

    async def get_service(self, name):
        for s in self.services:
            if s["name"] == name:
                return s
        return None

    async def start_service(self, name):
        for s in self.services:
            if s["name"] == name:
                s["state"] = "running"
                s["container"] = f"orchestack-{name}"
        return {"ok": True, "service": name, "state": "running"}

    async def stop_service(self, name):
        for s in self.services:
            if s["name"] == name:
                s["state"] = "stopped"
                s["container"] = None
        return {"ok": True, "service": name, "state": "stopped"}

    async def health(self):
        if self.unreachable:
            raise httpx.ConnectError("stub unreachable")
        return {"ok": True, "checks": {"postgres": True, "docker": True}}

    # Sessions
    async def open_session(self, service, **kwargs):
        token = self.next_session_token
        self.sessions_db.append({
            "token": token, "service": service, "user_id": 1,
            "username": "tester", "full_name": "Test User",
            "opened_at": "2026-06-03T12:00:00+00:00",
            "last_heartbeat_at": "2026-06-03T12:00:00+00:00",
            "closed_at": None, "idle_seconds": 0,
        })
        return {"token": token, "service": service, "started": True}

    async def checkin_session(self, token):
        return {"ok": True, "matched": True}

    async def close_session(self, token):
        for s in self.sessions_db:
            if s["token"] == token:
                s["closed_at"] = "2026-06-03T12:01:00+00:00"

    async def list_sessions(self, **kwargs):
        if self.unreachable:
            raise httpx.ConnectError("stub unreachable")
        active = [s for s in self.sessions_db if s["closed_at"] is None]
        return {"sessions": active, "total": len(active)}

    # Pinning
    async def pin_service(self, name, **kwargs):
        self.pins[name] = {
            "service": name, "expires_at": "2026-06-03T14:00:00+00:00",
            "pinned_by_username": "tester",
        }
        return {"ok": True, "service": name}

    async def unpin_service(self, name):
        self.pins.pop(name, None)

    async def get_pin(self, name):
        return self.pins.get(name)

    # Audit
    async def list_audit(self, **kwargs):
        if self.unreachable:
            raise httpx.ConnectError("stub unreachable")
        target = kwargs.get("target")
        events = self.audit_events
        if target:
            events = [e for e in events if e["target"] == target]
        return {
            "events": events, "total": len(events),
            "limit": kwargs.get("limit", 50), "offset": kwargs.get("offset", 0),
        }

    # Auth
    async def auth_login(self, username_or_email, password):
        if username_or_email == "tester" and password == "correct_horse_battery":
            cookie = "Set-Cookie: orchestack_session=fake-cookie-value; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200"
            return self.test_user, cookie
        # Mimic a real 401 raised by raise_for_status()
        request = httpx.Request("POST", "http://stub/api/auth/login")
        response = httpx.Response(401, request=request)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    async def auth_logout(self, session_cookie):
        return None

    async def auth_me(self, session_cookie):
        if session_cookie == "fake-cookie-value":
            return self.test_user
        return None


fake = FakeOrchestrator()
appmod.orchestrator = fake


# ============================================================================
#  Test runner
# ============================================================================
async def main():
    failures = []

    def check(label, condition, detail=""):
        status = "✓" if condition else "✗"
        print(f"  {status} {label}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(label)

    transport = ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test",
                                   follow_redirects=False) as client:

        # ---- 0. Container liveness ----------------------------------------
        print("\n0. Container liveness (no auth needed)")
        r = await client.get("/healthz")
        check("/healthz", r.status_code == 200 and r.text.strip() == "ok")

        # ---- 1. Unauthenticated access bounces to /login ------------------
        print("\n1. Auth gate — unauthenticated users redirected")
        r = await client.get("/")
        check("/ redirects when no cookie", r.status_code == 307,
              f"status={r.status_code}")
        check("redirect Location includes /login",
              "/login" in (r.headers.get("location") or ""))
        r = await client.get("/sessions")
        check("/sessions also redirects", r.status_code == 307)
        r = await client.get("/audit")
        check("/audit also redirects", r.status_code == 307)
        r = await client.get("/services/metabase")
        check("/services/metabase also redirects", r.status_code == 307)

        # ---- 2. Login page renders ----------------------------------------
        print("\n2. Login page (unauth OK)")
        r = await client.get("/login")
        check("/login returns 200", r.status_code == 200)
        check("/login has username_or_email input",
              'name="username_or_email"' in r.text)
        check("/login has password input", 'name="password"' in r.text)

        # ---- 3. Login flow ------------------------------------------------
        print("\n3. Login flow")
        r = await client.post("/api/dashboard/auth/login",
                              data={"username_or_email": "tester",
                                    "password": "correct_horse_battery",
                                    "next": "/"})
        check("login returns 303 on success", r.status_code == 303)
        check("login Set-Cookie header forwarded",
              "orchestack_session" in (r.headers.get("set-cookie") or ""))
        check("login redirects to /app/", r.headers.get("location") == "/app/")

        r = await client.post("/api/dashboard/auth/login",
                              data={"username_or_email": "tester",
                                    "password": "wrong",
                                    "next": "/"})
        check("login returns 401 on wrong password", r.status_code == 401)
        check("login error page mentions 'Invalid'",
              "Invalid" in r.text)

        # ---- 4. Authenticated requests -----------------------------------
        print("\n4. Authenticated dashboard pages (with cookie)")
        client.cookies.set("orchestack_session", "fake-cookie-value")

        r = await client.get("/")
        check("/ returns 200 with valid cookie", r.status_code == 200)
        check("/ renders 'Service status'", "Service status" in r.text)
        check("/ shows logged-in user", "Test User" in r.text)
        check("/ shows Sign out form",
              '/api/dashboard/auth/logout' in r.text)
        check("/ nav links to /sessions",
              "/app/sessions" in r.text)
        check("/ nav links to /audit",
              "/app/audit" in r.text)

        r = await client.get("/sessions")
        check("/sessions returns 200", r.status_code == 200)
        check("/sessions page mentions 'Active sessions'",
              "Active sessions" in r.text)

        r = await client.get("/audit")
        check("/audit returns 200", r.status_code == 200)
        check("/audit page has filter form",
              'name="event_type"' in r.text)

        r = await client.get("/services/metabase")
        check("/services/metabase returns 200", r.status_code == 200)
        check("/services/metabase shows display name",
              "Metabase" in r.text)
        check("/services/metabase has pin section",
              "Keep warm" in r.text)

        # ---- 5. HTMX fragments --------------------------------------------
        print("\n5. HTMX fragments")
        r = await client.get("/api/dashboard/services/grid")
        check("grid fragment 200", r.status_code == 200)
        check("grid card has Open button",
              "orchestackOpenService" in r.text)
        check("grid card link to detail page",
              "Details · pin · activity" in r.text)

        r = await client.get("/api/dashboard/sessions/active")
        check("sessions fragment 200", r.status_code == 200)
        check("sessions empty-state when none",
              "No open sessions" in r.text)

        # Open a session, refetch
        r = await client.post("/api/dashboard/services/metabase/open")
        check("open service returns 200 + JSON", r.status_code == 200)
        body = r.json()
        check("open response has token", "token" in body)
        check("open response has tool_url", body.get("tool_url") == "/app/metabase")

        r = await client.get("/api/dashboard/sessions/active")
        check("sessions fragment renders open session now",
              "metabase" in r.text and "No open sessions" not in r.text)

        r = await client.get("/api/dashboard/audit/table")
        check("audit fragment 200", r.status_code == 200)
        check("audit fragment shows service_started",
              "service_started" in r.text)
        check("audit fragment shows actor",
              "ayoade" in r.text)

        # ---- 6. Action endpoints ------------------------------------------
        print("\n6. Action endpoints")
        r = await client.post("/api/dashboard/services/pgadmin/start")
        check("start action returns 200", r.status_code == 200)
        check("returned card shows Running", "Running" in r.text)
        check("returned card has Open button now",
              "orchestackOpenService" in r.text)

        r = await client.post("/api/dashboard/services/pgadmin/stop")
        check("stop action returns 200", r.status_code == 200)
        check("returned card shows Stopped after stop", "Stopped" in r.text)

        # Pin lifecycle
        r = await client.get("/api/dashboard/services/metabase/pin-button")
        check("pin button 200", r.status_code == 200)
        check("pin button (unpinned) shows 'Pin'", "Pin" in r.text)

        r = await client.post("/api/dashboard/services/metabase/pin")
        check("POST pin 200", r.status_code == 200)
        check("after pin, button shows 'Pinned'", "Pinned" in r.text)

        r = await client.delete("/api/dashboard/services/metabase/pin")
        check("DELETE pin 200", r.status_code == 200)
        check("after unpin, button shows 'Pin' again",
              "Pinned" not in r.text and "Pin" in r.text)

        # ---- 7. Heartbeat + close -----------------------------------------
        print("\n7. Session lifecycle endpoints")
        token = body["token"]
        r = await client.post(f"/api/dashboard/sessions/{token}/heartbeat")
        check("heartbeat 200", r.status_code == 200)

        r = await client.post(f"/api/dashboard/sessions/{token}/close")
        check("close returns 204", r.status_code == 204)

        # ---- 8. Logout ----------------------------------------------------
        print("\n8. Logout")
        r = await client.post("/api/dashboard/auth/logout")
        check("logout 303", r.status_code == 303)
        check("logout redirects to /app/login",
              r.headers.get("location") == "/app/login")
        check("logout clears cookie",
              "Max-Age=0" in (r.headers.get("set-cookie") or "")
              or "expires" in (r.headers.get("set-cookie") or "").lower())

        # ---- 9. Degraded mode -------------------------------------------
        print("\n9. Orchestrator unreachable → graceful degradation")
        fake.unreachable = True
        client.cookies.set("orchestack_session", "fake-cookie-value")

        r = await client.get("/api/dashboard/services/grid")
        check("grid still 200 when orchestrator down", r.status_code == 200)
        check("grid shows error banner",
              "Orchestrator unreachable" in r.text)
        r = await client.get("/api/dashboard/sessions/active")
        check("sessions still 200", r.status_code == 200)
        check("sessions shows error",
              "Couldn't load sessions" in r.text)
        r = await client.get("/api/dashboard/audit/table")
        check("audit still 200", r.status_code == 200)
        check("audit shows error",
              "Couldn't load audit events" in r.text)
        fake.unreachable = False

    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print("ALL CHECKS PASSED")


asyncio.run(main())
