"""Phase 3.2 smoke test — exercise every dashboard route with a stub
orchestrator. Verifies routes wire up correctly, templates render without
errors, and the error-path branches handle a down orchestrator gracefully.

Run from system/dashboard/ with the .smoke-venv interpreter:
    .smoke-venv/bin/python _smoke_test.py

This file is a one-off; phase 3.6 will replace it with a proper pytest
suite under tests/. Keep it for now as a manual regression check during
M3 development.
"""
from __future__ import annotations
import asyncio, sys

import httpx
from httpx import ASGITransport

import app.main as appmod
from app.main import app

# --- Stub orchestrator: replace the OrchestratorClient with a fake. -----
class FakeOrchestrator:
    """Drop-in replacement for OrchestratorClient — covers the happy path
    and the unreachable path. State is configurable per test."""

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

    async def list_services(self):
        if self.unreachable:
            raise httpx.ConnectError("Connection refused (stub)")
        return {"services": self.services}

    async def get_service(self, name):
        if self.unreachable:
            raise httpx.ConnectError("Connection refused (stub)")
        for s in self.services:
            if s["name"] == name:
                return s
        return None

    async def start_service(self, name):
        if self.unreachable:
            raise httpx.ConnectError("Connection refused (stub)")
        for s in self.services:
            if s["name"] == name:
                s["state"] = "running"
                s["container"] = f"orchestack-{name}"
        return {"ok": True, "service": name, "state": "running"}

    async def stop_service(self, name):
        if self.unreachable:
            raise httpx.ConnectError("Connection refused (stub)")
        for s in self.services:
            if s["name"] == name:
                s["state"] = "stopped"
                s["container"] = None
        return {"ok": True, "service": name, "state": "stopped"}

    async def health(self):
        if self.unreachable:
            raise httpx.ConnectError("Connection refused (stub)")
        return {"ok": True, "checks": {"database": "ok", "docker": "ok"}}


fake = FakeOrchestrator()
appmod.orchestrator = fake  # type: ignore[assignment]


# --- Run requests against the in-process app. ---------------------------
async def main():
    failures = []

    def check(label, condition, detail=""):
        status = "✓" if condition else "✗"
        print(f"  {status} {label}{(' — ' + detail) if detail and not condition else ''}")
        if not condition:
            failures.append(label)

    transport = ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                   base_url="http://test") as client:
        print("\n1. Container liveness")
        r = await client.get("/healthz")
        check("/healthz returns 200 'ok'",
              r.status_code == 200 and r.text.strip() == "ok",
              f"got status={r.status_code} body={r.text!r}")

        print("\n2. Home page renders")
        r = await client.get("/")
        check("/ returns 200",
              r.status_code == 200, f"status={r.status_code}")
        check("/ HTML contains service grid container",
              'id="services-grid"' in r.text)
        check("/ HTML contains 'Service status' heading",
              'Service status' in r.text)
        check("/ HTML wires hx-get to the grid endpoint (with /app prefix)",
              "/app/api/dashboard/services/grid" in r.text)

        print("\n3. Service grid fragment (happy path)")
        r = await client.get("/api/dashboard/services/grid")
        check("grid returns 200", r.status_code == 200)
        check("grid renders Metabase card", "Metabase" in r.text)
        check("grid shows Metabase as Running", "Running" in r.text)
        check("grid renders pgAdmin card", "pgAdmin" in r.text)
        check("grid renders Airbyte (unmanaged) card", "Airbyte" in r.text)
        check("Airbyte button is disabled (unmanaged service)",
              "Unavailable" in r.text and "disabled" in r.text)
        check("Stop button posts to stop endpoint for Metabase",
              "/api/dashboard/services/metabase/stop" in r.text)
        check("Start button posts to start endpoint for pgAdmin",
              "/api/dashboard/services/pgadmin/start" in r.text)
        check("Card has stable id for HTMX outerHTML swap",
              'id="service-card-metabase"' in r.text)

        print("\n4. Start action")
        # pgadmin starts stopped; clicking start should make it running.
        r = await client.post("/api/dashboard/services/pgadmin/start")
        check("start returns 200", r.status_code == 200)
        check("returned fragment is a card (has stable id)",
              'id="service-card-pgadmin"' in r.text)
        check("returned card now shows Running", "Running" in r.text)
        check("returned card shows new container name",
              "orchestack-pgadmin" in r.text)

        print("\n5. Stop action")
        r = await client.post("/api/dashboard/services/pgadmin/stop")
        check("stop returns 200", r.status_code == 200)
        check("returned card now shows Stopped", "Stopped" in r.text)
        check("returned card has no container name (stopped state)",
              "orchestack-pgadmin" not in r.text)

        print("\n6. Unreachable orchestrator: grid renders error banner")
        fake.unreachable = True
        r = await client.get("/api/dashboard/services/grid")
        check("grid still returns 200 (graceful degradation)",
              r.status_code == 200)
        check("grid shows 'Orchestrator unreachable' banner",
              "Orchestrator unreachable" in r.text)
        check("grid does NOT render stale cards during outage",
              "Metabase" not in r.text)
        fake.unreachable = False

        print("\n7. Unreachable orchestrator: health fragment also degrades")
        fake.unreachable = True
        r = await client.get("/api/dashboard/health")
        check("health still returns 200", r.status_code == 200)
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
