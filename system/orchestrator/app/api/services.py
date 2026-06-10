"""Service control endpoints — list, start, stop.

Three endpoints:
    GET    /api/services             → list every catalogue service with state
    POST   /api/services/{name}/start → bring it up via docker compose
    POST   /api/services/{name}/stop  → take it down (preserves volumes)

State is DERIVED at request time, not stored: we combine the static
SERVICE_CATALOGUE entries with the live `docker ps` output. If a service
is in the catalogue but not running, state="stopped". If it's running,
state="running". Future phases will add "starting" and "error" states
once we track in-flight operations.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import audit, config, db, docker_ops

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
async def list_services() -> dict[str, object]:
    """Return state of every service in the catalogue.

    Fields per service:
      name          — catalogue key (lowercase, no spaces; used in URLs)
      display_name  — human-readable name for UI ("Apache Airflow")
      tier          — "hot" or "cold" (governs reconciler behaviour)
      layer         — pipeline layer the service belongs to ("bi", "ingestion",
                      "warehouse", etc.). Useful for UIs that group services.
      state         — "running" or "stopped" (derived from docker ps)
      container     — Docker container name if running, else null
      managed       — True iff the orchestrator has a compose snippet for this
                      service and can actually start/stop it. False means
                      the service is catalogue-registered but M4-pending —
                      the dashboard should disable start/stop buttons in that
                      case to avoid a 500 from the orchestrator.
      configured    — True iff the operator selected this service in the
                      setup wizard (i.e. there's a row in
                      platform.installed_services). The dashboard uses this
                      to differentiate "not yet picked by the operator"
                      (offer a Configure link) from "M4-pending" (just
                      grey out).
    """
    running = await docker_ops.list_running_services()
    running_by_name = {r["service"]: r for r in running}

    # Which services has the operator already configured? Single query +
    # in-memory lookup keeps this cheap regardless of catalogue size.
    rows = await db.fetch(
        "SELECT name FROM platform.installed_services WHERE enabled = TRUE"
    )
    configured_names = {r["name"] for r in rows}

    items = []
    for name, meta in config.SERVICE_CATALOGUE.items():
        is_running = name in running_by_name
        items.append({
            "name": name,
            "display_name": meta["display_name"],
            "tier": meta["tier"],
            "layer": meta.get("layer"),
            "state": "running" if is_running else "stopped",
            "container": running_by_name.get(name, {}).get("container"),
            "managed": bool(meta.get("managed", False)),
            "configured": name in configured_names,
        })
    return {"services": items}


@router.post("/{name}/start")
async def start_service(name: str) -> dict[str, object]:
    """Bring service `name` up via `docker compose up -d`.

    Idempotent — if the service is already running, returns 200 without
    re-running compose. We let docker compose itself be the source of
    truth rather than maintaining a separate "intent" state we'd then
    have to keep synced.
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    result = await docker_ops.start_service(name)
    await audit.write(
        "service_started" if result.ok else "service_start_failed",
        service_name=name,
        details={"returncode": result.returncode, "stderr": result.short_stderr},
    )
    if not result.ok:
        raise HTTPException(500, {
            "error": "docker compose up failed",
            "returncode": result.returncode,
            "stderr": result.short_stderr,
        })
    return {"ok": True, "service": name, "state": "running"}


@router.post("/{name}/stop")
async def stop_service(name: str) -> dict[str, object]:
    """Stop service `name`. Equivalent to `docker compose stop` (NOT down).

    Volumes and the network are preserved so the next start is fast
    (container start, not container recreate).
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    result = await docker_ops.stop_service(name)
    await audit.write(
        "service_stopped" if result.ok else "service_stop_failed",
        service_name=name,
        details={"returncode": result.returncode, "stderr": result.short_stderr},
    )
    if not result.ok:
        raise HTTPException(500, {
            "error": "docker compose stop failed",
            "returncode": result.returncode,
            "stderr": result.short_stderr,
        })
    return {"ok": True, "service": name, "state": "stopped"}
