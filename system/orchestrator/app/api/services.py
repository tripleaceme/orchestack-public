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

    # Which services are currently keep-warm pinned? Matches the
    # reconciler's pin check: row exists AND (expires_at IS NULL OR
    # expires_at > now()). Expired pins are treated as unpinned.
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
        # Control-plane services (PostgreSQL) are owned by the base
        # compose stack and always running — if they weren't, the
        # orchestrator wouldn't be able to answer this request. Report
        # them as running unconditionally so the tile reflects reality
        # instead of grey-out (which happens when list_running_services
        # filters by the orchestrack.service label that base containers
        # don't carry).
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
                # For control-plane services use the base container name
                # (orchestack-postgres). For managed services use whatever
                # docker ps reported.
                f"orchestack-{name.replace('postgresql', 'postgres')}"
                if is_control_plane and is_running
                else running_info.get("container")
            ),
            # `image` and `started_at` come from docker ps for managed
            # services; control-plane containers don't carry the
            # orchestack.service label so they don't appear in
            # running_info — return None for those (dashboard hides the
            # bit instead of showing em-dash).
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
            # external_url is set in the catalogue for tools whose UI
            # doesn't work cleanly under the /app/<name> subpath
            # (MinIO is the canonical case), and for control-plane
            # services that have no UI of their own and need to redirect
            # to a related tool (PostgreSQL → pgAdmin). The dashboard's
            # Open handler reads this and overrides the default tool URL.
            "external_url": meta.get("external_url"),
            # actions[] is set for services that expose multiple
            # operator-facing surfaces (dbt: docs + terminal). When
            # present, the dashboard renders one Open button per
            # action; when absent, single Open button using
            # external_url. Each action carries its own ready_probe
            # so the dashboard can wait for the correct port before
            # opening that action's tool URL. ready_probe is dropped
            # here because tuples don't serialise cleanly to JSON and
            # the probe is only used inside the dashboard's /ready
            # handler — not by the JS.
            "actions": [
                {k: v for k, v in a.items() if k != "ready_probe"}
                for a in meta.get("actions", [])
            ] or None,
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

    On a successful stop, ALSO close every open session for this
    service — the container is gone, the operator's tab can no longer
    reach the tool, so the session rows that referenced it should
    reflect that reality. Without this step, the Active sessions KPI
    and the service-detail Open sessions card would keep showing
    stale rows that look like work-in-progress.
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    result = await docker_ops.stop_service(name)
    await audit.write(
        "service_stopped" if result.ok else "service_stop_failed",
        service_name=name,
        details={"returncode": result.returncode, "stderr": result.short_stderr},
    )
    if result.ok:
        # Close all currently-open sessions for this service in one
        # UPDATE. RETURNING gives us the affected ids so we can emit
        # one audit row per closed session — useful for M5 evaluation
        # ("how often does an operator-initiated stop terminate active
        # work?"). If no rows match, we skip the audit write — no
        # need to spam the log with sessions_closed_on_stop=0 events.
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
