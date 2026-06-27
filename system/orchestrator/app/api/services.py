"""Service control endpoints — list, start, stop, disable, enable, delete."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

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
            # Optional per-service caveat that the dashboard renders as a
            # banner on the detail page. Set when a service's tier
            # classification has a known footgun the operator should know
            # about (e.g. cold-tier Airflow + scheduled DAGs).
            "scheduling_warning": meta.get("scheduling_warning"),
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


@router.post("/{name}/disable")
async def disable_service(name: str) -> dict[str, object]:
    """Disable a service: stop it, flip enabled=FALSE, preserve config + volumes.

    Disable is the cheap reversible counterpart to Delete. The compose
    project is torn down (containers + network removed; volumes kept),
    the row in platform.installed_services has enabled flipped to FALSE
    so the dashboard hides the tile + the post-deploy auto-start loop
    skips it, and any open sessions are closed. The operator's .env
    entries for the service stay put. Re-enable restores everything
    bit-for-bit.
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")
    if config.SERVICE_CATALOGUE[name].get("control_plane"):
        raise HTTPException(400, "control-plane services cannot be disabled")

    # remove_service runs `compose down` without -v, so volumes survive.
    teardown = await docker_ops.remove_service(name, wipe_volumes=False)
    # Even if teardown fails (container may already be stopped/gone), we
    # still flip the DB flag — the catalogue+DB are the source of truth
    # for "is this service offered." A failed teardown just leaves
    # orphan containers the operator can reap; it doesn't undo the disable.
    await db.fetch(
        "UPDATE platform.installed_services SET enabled = FALSE WHERE name = $1",
        name,
    )
    closed = await db.fetch(
        "UPDATE platform.service_sessions SET closed_at = now() "
        "WHERE service_name = $1 AND closed_at IS NULL RETURNING id",
        name,
    )
    await audit.write(
        "service_disabled",
        service_name=name,
        details={
            "teardown_ok": teardown.ok,
            "teardown_stderr": teardown.short_stderr if not teardown.ok else None,
            "sessions_closed": len(closed),
        },
    )
    return {"ok": True, "service": name, "state": "disabled"}


@router.post("/{name}/enable")
async def enable_service(name: str) -> dict[str, object]:
    """Re-enable a previously-disabled service. Does NOT auto-start it.

    Sets enabled=TRUE so the dashboard tile reappears + auto-start
    eligibility returns. The operator brings the service up with the
    usual Open / Start click. We deliberately don't auto-start on
    enable because Disable → Enable should be a quiet operation — the
    operator decides when to consume resources again.
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")
    row = await db.fetch(
        "SELECT enabled FROM platform.installed_services WHERE name = $1",
        name,
    )
    if not row:
        raise HTTPException(404, f"service {name} was never configured")
    await db.fetch(
        "UPDATE platform.installed_services SET enabled = TRUE WHERE name = $1",
        name,
    )
    await audit.write("service_enabled", service_name=name, details={})
    return {"ok": True, "service": name, "state": "enabled"}


@router.delete("/{name}")
async def delete_service(
    name: str,
    wipe_volumes: Annotated[bool, Query()] = False,
) -> dict[str, object]:
    """Remove a service entirely: tear down + drop config row.

    With wipe_volumes=False: the compose project is torn down (containers
    + network) but named volumes survive. Re-configuring the service via
    the setup wizard restores its prior state.

    With wipe_volumes=True: the named volumes are dropped too — every
    dashboard, connection, and project file the service held is gone.
    Irreversible. The dashboard's confirm modal must obtain explicit
    operator opt-in for wipe_volumes=True (a checkbox, not a default).

    Either way, the row in platform.installed_services is deleted and
    any open sessions are closed. Per-tool databases inside
    orchestack-postgres (metabase_db, airflow_db, etc.) are NOT
    dropped — those live on the platform postgres volume and would
    require a separate operator action to wipe.
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")
    if config.SERVICE_CATALOGUE[name].get("control_plane"):
        raise HTTPException(400, "control-plane services cannot be removed")

    teardown = await docker_ops.remove_service(name, wipe_volumes=wipe_volumes)
    # Best-effort: even if teardown failed, drop the DB row so the
    # service can be re-configured from scratch. The orphan containers
    # the operator can clean up manually if needed.
    await db.fetch(
        "DELETE FROM platform.installed_services WHERE name = $1",
        name,
    )
    closed = await db.fetch(
        "UPDATE platform.service_sessions SET closed_at = now() "
        "WHERE service_name = $1 AND closed_at IS NULL RETURNING id",
        name,
    )
    await audit.write(
        "service_removed",
        service_name=name,
        details={
            "wipe_volumes": wipe_volumes,
            "teardown_ok": teardown.ok,
            "teardown_stderr": teardown.short_stderr if not teardown.ok else None,
            "sessions_closed": len(closed),
        },
    )
    return {
        "ok": True,
        "service": name,
        "state": "removed",
        "volumes_wiped": wipe_volumes,
    }
