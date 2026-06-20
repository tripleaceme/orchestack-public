"""Reconciler loop — shutdown-only hot/cold tier sweeper."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from . import audit, config, db, docker_ops

log = logging.getLogger("orchestrator.reconciler")


async def reconcile_once() -> dict[str, int]:
    """One tick. Returns a count summary suitable for the health endpoint."""
    summary = {"running": 0, "active": 0, "pinned": 0, "stopped": 0}

    try:
        running = await docker_ops.list_running_services()
    except Exception as e:
        log.warning("docker ps failed during reconcile: %s", e)
        return summary
    summary["running"] = len(running)

    if not running:
        return summary

    try:
        # Compute cutoff in Python rather than as a SQL interval: asyncpg's
        # binary protocol doesn't auto-cast int→text so `$1 || ' seconds'`
        # would error; passing a precomputed TIMESTAMPTZ also matches the
        # column type so postgres can use the partial index on
        # last_heartbeat_at directly.
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

    for entry in running:
        svc = entry["service"]
        meta = config.SERVICE_CATALOGUE.get(svc)

        if meta is None:
            # Skip containers labelled orchestack.service but not in our
            # catalogue — don't touch what we don't own.
            continue

        if meta["tier"] == "hot":
            continue

        if svc in pinned:
            continue

        if active_by_service.get(svc, 0) > 0:
            continue

        # Start-grace: a session POST may still be in flight just after start.
        uptime = await docker_ops.container_uptime_seconds(svc)
        if uptime is not None and uptime < config.START_GRACE:
            log.debug("skipping %s — uptime %ds within grace window", svc, uptime)
            continue

        # Without IDLE_THRESHOLD the reconciler would stop services almost
        # immediately after start, before any session has opened.
        if uptime is not None and uptime < config.IDLE_THRESHOLD:
            continue

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
            # Race-defence: a session could have opened between the
            # active_by_service check above and the docker stop landing here.
            # Closing unconditionally is harmless at zero rows and
            # load-bearing if one slipped in.
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
    """Background task — ticks until stop_event is set on shutdown."""
    log.info(
        "reconciler starting interval=%ss idle_threshold=%ss",
        config.RECONCILE_INTERVAL, config.IDLE_THRESHOLD,
    )
    # First tick fires immediately so the operator sees activity at startup.
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
            pass

    log.info("reconciler stopped")
