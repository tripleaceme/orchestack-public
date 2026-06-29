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

from . import audit, config, db, docker_ops

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

    # Manual fire subsumes the upcoming scheduled fire — operators
    # don't want a pipeline they manually triggered at 16:15 to ALSO
    # fire at the configured 16:30. Skip the PENDING fire by advancing
    # next_run_at past it:
    #   - 'once': clear it (operator already got their one fire).
    #   - 'cron': next match strictly AFTER the current next_run_at,
    #     not next-after-now (which would re-pick the same pending slot).
    #   - 'manual': no schedule to skip (next_run_at is NULL already).
    if triggered_by == "manual":
        pinfo = await db.fetchrow(
            "SELECT trigger_type, trigger_value, trigger_timezone, next_run_at "
            "FROM platform.pipelines WHERE id = $1",
            pipeline_id,
        )
        if pinfo and pinfo["trigger_type"] in ("once", "cron"):
            new_next = None
            if pinfo["trigger_type"] == "cron" and pinfo["next_run_at"]:
                try:
                    from croniter import croniter   # type: ignore
                    try:
                        from zoneinfo import ZoneInfo
                        tz = ZoneInfo(pinfo["trigger_timezone"]) if pinfo["trigger_timezone"] != "UTC" else timezone.utc
                    except Exception:
                        tz = timezone.utc
                    base = pinfo["next_run_at"].astimezone(tz) if pinfo["next_run_at"] else datetime.now(tz)
                    it = croniter(pinfo["trigger_value"], base)
                    new_next = it.get_next(datetime).astimezone(timezone.utc)
                except Exception as e:
                    log.warning("manual-fire cron advance failed for pipeline %s: %s", pipeline_id, e)
                    new_next = compute_next_run(
                        pinfo["trigger_type"], pinfo["trigger_value"], pinfo["trigger_timezone"],
                    )
            await db.fetch(
                "UPDATE platform.pipelines SET next_run_at = $2 WHERE id = $1",
                pipeline_id, new_next,
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
    cancelled = False

    async def _persist_step_results() -> None:
        """Snapshot step_results to the DB so the dashboard's polling
        endpoint sees in-flight progress. Called twice per step (start +
        end) and once mid-buffer. The whole list is rewritten because
        jsonb columns don't support targeted array updates without
        gymnastics; the list is small (<=20 entries) so this is cheap."""
        try:
            await db.fetch(
                "UPDATE platform.pipeline_runs SET step_results = $2::jsonb WHERE id = $1",
                run_id, json.dumps(step_results),
            )
        except Exception as e:
            # Persistence failure shouldn't kill the run — log + continue.
            log.warning("pipelines_executor: persist step_results failed: %s", e)

    async def _check_cancelled() -> bool:
        """Re-fetch the run's status; an operator can cancel mid-flight
        via the API which sets status='cancelled' in the DB. We poll
        between steps so the in-flight step finishes (docker compose
        is already racing) but no MORE steps fire."""
        row = await db.fetchrow(
            "SELECT status FROM platform.pipeline_runs WHERE id = $1", run_id,
        )
        return row is not None and row["status"] == "cancelled"

    for s in steps:
        if await _check_cancelled():
            cancelled = True
            break
        step_started_at = datetime.now(timezone.utc)
        result: dict[str, Any] = {
            "order_index":   s["order_index"],
            "service_name":  s["service_name"],
            "action":        s["action"],
            "buffer_seconds": s["buffer_seconds"],
            "started_at":    step_started_at.isoformat(),
            "status":        "running",
        }
        # Append the in-flight result + persist BEFORE the docker call so
        # the dashboard sees the step transition from queued -> starting
        # on its next poll. Without this, step_results was empty for the
        # entire duration of the run (executor only wrote at completion),
        # so the runs page rendered every pill as queued even though one
        # was actively running.
        step_results.append(result)
        await _persist_step_results()
        try:
            # Hot-tier services (Postgres, Metabase, pgAdmin) are always
            # on — start_service would no-op via `docker compose up -d`
            # but we still want stop steps on them to NOT actually stop
            # them (stopping Postgres breaks the warehouse). Short-circuit
            # both actions to an instant success with a note. The
            # dashboard editor flags hot-tier picks with a "(always-on)"
            # suffix and a faded action picker, so operators are warned
            # before they save.
            tier = config.SERVICE_CATALOGUE.get(s["service_name"], {}).get("tier")
            if tier == "hot":
                result["status"] = "succeeded"
                result["note"]   = f"hot-tier (always-on) — {s['action']} no-op"
            elif s["action"] == "start":
                op = await asyncio.wait_for(
                    docker_ops.start_service(s["service_name"]),
                    timeout=STEP_ACTION_TIMEOUT_SECONDS,
                )
                if op.ok:
                    result["status"] = "succeeded"
                else:
                    result["status"] = "failed"
                    result["error"]  = op.short_stderr
                    any_failed = True
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
        # result is already in step_results (appended pre-docker); just
        # persist the post-docker mutation (status + completed_at + error)
        # so the dashboard sees the step transition from starting ->
        # succeeded/failed on its next poll.
        await _persist_step_results()

        # Bail on first failure. Operators model pipelines as dependency
        # chains: if "start MinIO" fails, running "start dbt" after it
        # is meaningless (dbt's connection target would be unreachable).
        # Continuing past failures was the original behaviour but
        # produced confusing partial-success states on the dashboard.
        # Operators wanting independent steps should create separate
        # pipelines.
        if result["status"] == "failed":
            log.info(
                "pipelines_executor: pipeline %s run %s step %d (%s) failed — bailing on remaining steps",
                pipeline_id, run_id, s["order_index"], s["service_name"],
            )
            break

        # Buffer wait — give the service time to finish its own startup
        # (e.g. Airflow scheduler tick) before the next step fires. Skipped
        # on the last step (no following step to wait for). Also broken
        # into 5s polls so a cancel signal is honoured mid-sleep instead
        # of waiting out the full buffer.
        if s["order_index"] < len(steps) - 1:
            slept = 0
            while slept < s["buffer_seconds"]:
                await asyncio.sleep(min(5, s["buffer_seconds"] - slept))
                slept += 5
                if await _check_cancelled():
                    cancelled = True
                    break
            if cancelled:
                break

    if cancelled:
        # The cancel API already set status='cancelled' + completed_at +
        # error_summary. We just persist whatever step_results we collected
        # before the cancel signal was observed.
        await db.fetch(
            "UPDATE platform.pipeline_runs SET step_results = $2::jsonb WHERE id = $1 AND status = 'cancelled'",
            run_id, json.dumps(step_results),
        )
        await db.fetch(
            "UPDATE platform.pipelines SET last_run_status = 'cancelled' WHERE id = $1",
            pipeline_id,
        )
        return

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
