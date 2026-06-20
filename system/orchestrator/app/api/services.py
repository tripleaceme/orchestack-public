"""Service control endpoints — list, start, stop."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import audit, config, db, docker_ops

router = APIRouter(prefix="/api/services", tags=["services"])


@router.get("")
async def list_services() -> dict[str, object]:
    """Return state of every service in the catalogue."""
    running = await docker_ops.list_running_services()
    running_by_name = {r["service"]: r for r in running}

    rows = await db.fetch(
        "SELECT name FROM platform.installed_services WHERE enabled = TRUE"
    )
    configured_names = {r["name"] for r in rows}

    # Matches the reconciler's pin check: row exists AND (expires_at IS NULL
    # OR expires_at > now()). Expired pins are treated as unpinned.
    pin_rows = await db.fetch(
        """
        SELECT service_name, expires_at
        FROM platform.service_pinning
        WHERE expires_at IS NULL OR expires_at > now()
        """
    )
    pinned_by_name = {r["service_name"]: r for r in pin_rows}

    items = []
    for name, meta in config.SERVICE_CATALOGUE.items():
        is_control_plane = bool(meta.get("control_plane", False))
        # Control-plane services are owned by the base compose stack and
        # always running; list_running_services filters by the
        # orchestrack.service label which base containers don't carry,
        # so report them as running unconditionally.
        is_running = is_control_plane or (name in running_by_name)
        pin_row = pinned_by_name.get(name)
        running_info = running_by_name.get(name, {})
        items.append({
            "name": name,
            "display_name": meta["display_name"],
            "tier": meta["tier"],
            "layer": meta.get("layer"),
            "state": "running" if is_running else "stopped",
            "container": (
                f"orchestack-{name.replace('postgresql', 'postgres')}"
                if is_control_plane and is_running
                else running_info.get("container")
            ),
            "image":      running_info.get("image") or None,
            "started_at": running_info.get("started_at") or None,
            "managed": bool(meta.get("managed", False)),
            "control_plane": is_control_plane,
            "configured": name in configured_names,
            "pinned": pin_row is not None,
            "pin_expires_at": (
                pin_row["expires_at"].isoformat()
                if pin_row and pin_row["expires_at"] else None
            ),
            # Set for tools whose UI doesn't work cleanly under the
            # /app/<name> subpath (MinIO), and for control-plane services
            # with no UI that redirect to a related tool (PostgreSQL → pgAdmin).
            "external_url": meta.get("external_url"),
            # ready_probe is dropped here because tuples don't serialise
            # cleanly to JSON and the probe is only used inside the
            # dashboard's /ready handler, not by the JS.
            "actions": [
                {k: v for k, v in a.items() if k != "ready_probe"}
                for a in meta.get("actions", [])
            ] or None,
        })
    return {"services": items}


@router.post("/{name}/start")
async def start_service(name: str) -> dict[str, object]:
    """Bring service `name` up via `docker compose up -d` (idempotent)."""
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
    """Stop service `name` via `docker compose stop` (preserves volumes); also closes open sessions for it."""
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    result = await docker_ops.stop_service(name)
    await audit.write(
        "service_stopped" if result.ok else "service_stop_failed",
        service_name=name,
        details={"returncode": result.returncode, "stderr": result.short_stderr},
    )
    if result.ok:
        # Skip the audit write when no rows match to avoid spamming the
        # log with sessions_closed_on_stop=0 events.
        closed = await db.fetch(
            "UPDATE platform.service_sessions SET closed_at = now() "
            "WHERE service_name = $1 AND closed_at IS NULL "
            "RETURNING id, user_id, token",
            name,
        )
        if closed:
            await audit.write(
                "sessions_closed_on_stop",
                service_name=name,
                details={
                    "count": len(closed),
                    "user_ids": list({r["user_id"] for r in closed}),
                    "reason": "operator_initiated_stop",
                },
            )
    if not result.ok:
        raise HTTPException(500, {
            "error": "docker compose stop failed",
            "returncode": result.returncode,
            "stderr": result.short_stderr,
        })
    return {"ok": True, "service": name, "state": "stopped"}
