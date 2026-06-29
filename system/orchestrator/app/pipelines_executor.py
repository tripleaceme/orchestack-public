"""Pipeline executor + scheduler.

Two concerns in this module:

  1. compute_next_run(trigger_type, value, tz) — pure helper used by the
     API to project when a pipeline will next fire.

  2. run_loop(stop_event) — long-running coroutine the orchestrator
     launches on startup. Every TICK_INTERVAL seconds it scans for
     pipelines whose next_run_at has passed and fires them.

  3. fire_pipeline(pipeline_id, triggered_by, ...) — actually run one
     pipeline end-to-end: insert a pipeline_runs row, walk the steps
     in order, call docker_ops.start_service / stop_service, wait
     each step's buffer, mark the run succeeded/failed.

The executor never blocks the API — fire_pipeline returns the run_id
immediately and schedules the actual execution as an asyncio task.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import audit, db, docker_ops

log = logging.getLogger(__name__)

# How often the scheduler scans for pipelines to fire. Trades latency
# for DB load. 30s means the worst-case lateness for a cron-fired
# pipeline is 30s, which is well under the 5-minute buffer-period a
# wake-up pipeline typically uses.
TICK_INTERVAL_SECONDS = 30

# Hard cap on how long any single step's start_service / stop_service
# call may run. Most steps complete in seconds (cached image start)
# but a first-pull-during-start can take 10+ min. The bigger 30-min
# cap matches start_service's own ceiling for heavy images.
STEP_ACTION_TIMEOUT_SECONDS = 30 * 60


# ---------------------------------------------------------------------------
# Next-run computation
# ---------------------------------------------------------------------------
def compute_next_run(
    trigger_type: str,
    trigger_value: str | None,
    trigger_timezone: str = "UTC",
) -> datetime | None:
    """Return when this pipeline should next fire, or None if 'manual'.

    For 'once', returns the configured timestamp if still in the future,
    else None (already fired or in the past).

    For 'cron', returns the next match after now. Uses croniter if
    available — falls back to None and logs a warning if not (the
    scheduler will then never auto-fire that pipeline, but operators
    can still trigger it manually).
    """
    if trigger_type == "manual" or not trigger_value:
        return None
    if trigger_type == "once":
        try:
            dt = datetime.fromisoformat(trigger_value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            if dt > datetime.now(timezone.utc):
                return dt
            return None
        except ValueError as e:
            log.warning("compute_next_run: invalid once-trigger %r: %s", trigger_value, e)
            return None
    if trigger_type == "cron":
        try:
            from croniter import croniter   # type: ignore
        except ImportError:
            log.warning(
                "compute_next_run: croniter not installed; cron pipelines "
                "cannot auto-fire. Operators can still trigger manually."
            )
            return None
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(trigger_timezone) if trigger_timezone != "UTC" else timezone.utc
        except Exception:
            tz = timezone.utc
        try:
            base = datetime.now(tz)
            it = croniter(trigger_value, base)
            return it.get_next(datetime).astimezone(timezone.utc)
        except Exception as e:
            log.warning("compute_next_run: bad cron %r: %s", trigger_value, e)
            return None
    return None


# ---------------------------------------------------------------------------
# Firing a single pipeline
# ---------------------------------------------------------------------------
async def fire_pipeline(
    pipeline_id: int,
    triggered_by: str,
    triggered_by_user_id: int | None = None,
) -> int:
    """Spawn a background task to execute the pipeline; return run_id immediately.

    The dashboard's "Run now" button + the scheduler tick both call this.
    The actual execution is fire-and-forget so the caller (HTTP request or
    scheduler tick) doesn't block on what could be a multi-minute pipeline.
    """
    row = await db.fetchrow(
        """
        INSERT INTO platform.pipeline_runs
            (pipeline_id, triggered_by, triggered_by_user_id, status, started_at)
        VALUES ($1, $2, $3, 'running', now())
        RETURNING id
        """,
        pipeline_id, triggered_by, triggered_by_user_id,
    )
    run_id = row["id"]
    await db.fetch(
        "UPDATE platform.pipelines SET last_run_at = now(), last_run_status = 'running' WHERE id = $1",
        pipeline_id,
    )
    await audit.write(
        "pipeline_run_started",
        service_name=None,
        user_id=triggered_by_user_id,
        details={"pipeline_id": pipeline_id, "run_id": run_id, "triggered_by": triggered_by},
    )
    asyncio.create_task(
        _execute_run(pipeline_id, run_id),
        name=f"pipeline-run:{pipeline_id}:{run_id}",
    )
    return run_id


async def _execute_run(pipeline_id: int, run_id: int) -> None:
    """Walk the pipeline's steps in order; populate step_results; finalize status."""
    steps = await db.fetch(
        """
        SELECT order_index, service_name, action, buffer_seconds
        FROM platform.pipeline_steps
        WHERE pipeline_id = $1
        ORDER BY order_index
        """,
        pipeline_id,
    )

    step_results: list[dict[str, Any]] = []
    any_failed = False

    for s in steps:
        step_started_at = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "order_index":  s["order_index"],
            "service_name": s["service_name"],
            "action":       s["action"],
            "started_at":   step_started_at.isoformat(),
            "status":       "running",
        }
        try:
            if s["action"] == "start":
                op = await asyncio.wait_for(
                    docker_ops.start_service(s["service_name"]),
                    timeout=STEP_ACTION_TIMEOUT_SECONDS,
                )
            else:
                op = await asyncio.wait_for(
                    docker_ops.stop_service(s["service_name"]),
                    timeout=STEP_ACTION_TIMEOUT_SECONDS,
                )
            if op.ok:
                result["status"] = "succeeded"
            else:
                result["status"] = "failed"
                result["error"]  = op.short_stderr
                any_failed = True
        except asyncio.TimeoutError:
            result["status"] = "failed"
            result["error"]  = f"timeout after {STEP_ACTION_TIMEOUT_SECONDS}s"
            any_failed = True
        except Exception as e:
            log.warning(
                "pipeline %s run %s step %s failed: %s",
                pipeline_id, run_id, s["service_name"], e,
            )
            result["status"] = "failed"
            result["error"]  = f"{type(e).__name__}: {e}"
            any_failed = True

        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        step_results.append(result)

        # Buffer wait — give the service time to finish its own startup
        # (e.g. Airflow scheduler tick) before the next step fires. Skipped
        # on the last step (no following step to wait for).
        if s["order_index"] < len(steps) - 1:
            await asyncio.sleep(s["buffer_seconds"])

    final_status = "failed" if any_failed else "succeeded"
    summary = (
        f"{sum(1 for r in step_results if r['status'] == 'failed')}/{len(step_results)} step(s) failed"
        if any_failed else None
    )
    await db.fetch(
        """
        UPDATE platform.pipeline_runs
        SET status = $2, completed_at = now(),
            step_results = $3::jsonb, error_summary = $4
        WHERE id = $1
        """,
        run_id, final_status, json.dumps(step_results), summary,
    )
    await db.fetch(
        "UPDATE platform.pipelines SET last_run_status = $2 WHERE id = $1",
        pipeline_id, final_status,
    )
    await audit.write(
        "pipeline_run_succeeded" if not any_failed else "pipeline_run_failed",
        service_name=None,
        details={
            "pipeline_id": pipeline_id, "run_id": run_id,
            "step_count": len(step_results),
            "failed_count": sum(1 for r in step_results if r["status"] == "failed"),
        },
    )


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------
async def run_loop(stop_event: asyncio.Event) -> None:
    """Main scheduler. Lives for the lifetime of the orchestrator process.

    Every TICK_INTERVAL_SECONDS:
      1. SELECT pipelines that are enabled AND next_run_at <= now()
      2. For each: fire_pipeline() in background
      3. Recompute next_run_at (for cron) or NULL (for once — it's spent).

    Idempotency: a pipeline whose previous run is still 'running' is NOT
    re-fired even if its next_run_at has elapsed. Prevents overlap when
    a slow pipeline collides with its own next-trigger time.
    """
    log.info("pipelines_executor: scheduler started (tick=%ds)", TICK_INTERVAL_SECONDS)
    while not stop_event.is_set():
        try:
            await _tick()
        except Exception as e:
            log.exception("pipelines_executor: tick failed: %s", e)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=TICK_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass
    log.info("pipelines_executor: scheduler stopped")


async def _tick() -> None:
    due = await db.fetch(
        """
        SELECT p.id, p.trigger_type, p.trigger_value, p.trigger_timezone
        FROM platform.pipelines p
        WHERE p.enabled = TRUE
          AND p.next_run_at IS NOT NULL
          AND p.next_run_at <= now()
          AND NOT EXISTS (
              SELECT 1 FROM platform.pipeline_runs r
              WHERE r.pipeline_id = p.id AND r.status = 'running'
          )
        """
    )
    for p in due:
        pid = p["id"]
        try:
            await fire_pipeline(pid, triggered_by=p["trigger_type"])
        except Exception as e:
            log.warning("pipelines_executor: failed to fire pipeline %s: %s", pid, e)
            continue
        # Recompute next_run_at:
        #   - cron: next match after now
        #   - once: NULL (already fired; effectively becomes manual)
        new_next = compute_next_run(
            p["trigger_type"], p["trigger_value"], p["trigger_timezone"],
        )
        await db.fetch(
            "UPDATE platform.pipelines SET next_run_at = $2 WHERE id = $1",
            pid, new_next,
        )
