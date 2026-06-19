"""Reconciler loop — the hot/cold tier engine.

Single async task that runs every ORCHESTRATOR_RECONCILE_INTERVAL seconds.
Reads three sources of truth and decides which services should be stopped:

    1. platform.service_sessions  — who's actively using what?
    2. platform.service_pinning   — what's protected from idle sweeps?
    3. `docker ps --filter=label`  — what's actually running right now?

Every running service that has zero active sessions AND isn't pinned AND
has been up past the start-grace window gets stopped. Hot-tier services
(per SERVICE_CATALOGUE) are exempt from the sweep — they stay running.

The loop is shutdown-only by design. Services start via explicit POST
calls (from /api/sessions when a user opens a tool, or from
/api/services/{name}/start). The reconciler never starts anything. This
one-directional design keeps the algorithm simple and means a buggy
reconciler can at worst stop things — it can never fight an operator
who's trying to keep something stopped.

Failure handling: the loop catches all exceptions per-tick. A failed tick
logs and waits for the next interval. The orchestrator never crashes
because reconciliation hit a temporary error.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import audit, config, db, docker_ops

log = logging.getLogger("orchestrator.reconciler")


async def reconcile_once() -> dict[str, int]:
    """One tick. Returns a count summary suitable for the health endpoint."""
    summary = {"running": 0, "active": 0, "pinned": 0, "stopped": 0}

    # ---- 1. Read sources of truth -------------------------------------
    try:
        running = await docker_ops.list_running_services()
    except Exception as e:
        log.warning("docker ps failed during reconcile: %s", e)
        return summary
    summary["running"] = len(running)

    if not running:
        return summary  # nothing to sweep

    try:
        # Sessions are "active" if their last_heartbeat_at is recent enough
        # AND they haven't been closed by an explicit DELETE. We compute the
        # cutoff in Python rather than as a SQL interval expression for two
        # reasons: (a) asyncpg's binary protocol doesn't auto-cast int→text
        # so `$1 || ' seconds'` would error; (b) passing a precomputed
        # TIMESTAMPTZ matches the column type, so postgres can use the
        # partial index on last_heartbeat_at directly.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=config.SESSION_ACTIVE_WINDOW)
        rows = await db.fetch(
            """
            SELECT service_name, COUNT(*) AS n
            FROM platform.service_sessions
            WHERE last_heartbeat_at > $1
              AND closed_at IS NULL
            GROUP BY service_name
            """,
            cutoff,
        )
        active_by_service = {r["service_name"]: r["n"] for r in rows}
        summary["active"] = sum(active_by_service.values())

        pinned_rows = await db.fetch(
            """
            SELECT service_name FROM platform.service_pinning
            WHERE expires_at IS NULL OR expires_at > now()
            """,
        )
        pinned = {r["service_name"] for r in pinned_rows}
        summary["pinned"] = len(pinned)
    except Exception as e:
        log.warning("reconciler DB read failed: %s", e)
        return summary

    # ---- 2. Decide + act per running service --------------------------
    for entry in running:
        svc = entry["service"]
        meta = config.SERVICE_CATALOGUE.get(svc)

        if meta is None:
            # Container has the orchestack.service label but isn't in our
            # catalogue. Could be a leftover from an older snippet that's
            # since been removed. Skip — don't touch what we don't own.
            continue

        if meta["tier"] == "hot":
            continue  # hot-tier services never get swept

        if svc in pinned:
            continue  # operator wants this kept warm

        if active_by_service.get(svc, 0) > 0:
            continue  # someone's using it

        # Start-grace: don't kill a service that's < 60s old, the session
        # POST might just be in flight.
        uptime = await docker_ops.container_uptime_seconds(svc)
        if uptime is not None and uptime < config.START_GRACE:
            log.debug("skipping %s — uptime %ds within grace window", svc, uptime)
            continue

        # Idle-threshold: only stop if uptime > IDLE_THRESHOLD. (If we
        # didn't have this, the reconciler would stop services almost
        # immediately after start when no session has opened yet.)
        if uptime is not None and uptime < config.IDLE_THRESHOLD:
            continue

        # All checks pass — stop the service.
        log.info("reconciler stopping idle service: %s (uptime=%ss)", svc, uptime)
        result = await docker_ops.stop_service(svc)
        await audit.write(
            "service_stopped_idle" if result.ok else "service_stop_failed",
            service_name=svc,
            details={
                "uptime_seconds": uptime,
                "returncode": result.returncode,
                "stderr": result.short_stderr,
                "reason": "no_active_sessions",
            },
        )
        if result.ok:
            summary["stopped"] += 1
            # Race-defence: between the active_by_service count check
            # at the top of this loop and the docker stop landing
            # here, a session could have opened. Closing on stop
            # unconditionally is harmless if there are zero rows and
            # load-bearing if there's one.
            closed = await db.fetch(
                "UPDATE platform.service_sessions SET closed_at = now() "
                "WHERE service_name = $1 AND closed_at IS NULL "
                "RETURNING id",
                svc,
            )
            if closed:
                await audit.write(
                    "sessions_closed_on_stop",
                    service_name=svc,
                    details={"count": len(closed), "reason": "reconciler_idle_stop"},
                )

    return summary


async def run_loop(stop_event: asyncio.Event) -> None:
    """Background task — ticks until the stop_event is set on shutdown."""
    log.info(
        "reconciler starting interval=%ss idle_threshold=%ss",
        config.RECONCILE_INTERVAL, config.IDLE_THRESHOLD,
    )
    # First tick fires immediately so the operator sees activity at startup,
    # then every RECONCILE_INTERVAL after that.
    while not stop_event.is_set():
        try:
            summary = await reconcile_once()
            if summary["stopped"] > 0:
                log.info("reconciler tick: %s", summary)
            else:
                log.debug("reconciler tick: %s", summary)
        except Exception as e:
            # Catch-all so unhandled exceptions don't kill the loop.
            log.exception("reconciler tick raised — continuing: %s", e)

        try:
            await asyncio.wait_for(stop_event.wait(), config.RECONCILE_INTERVAL)
        except asyncio.TimeoutError:
            pass  # normal — interval elapsed, time for next tick

    log.info("reconciler stopped")
