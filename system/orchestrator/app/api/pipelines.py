"""Pipelines API — lifecycle-only scheduling for cold-tier services.

A pipeline is an ORDERED list of services to wake up at a trigger time.
The pipeline DOES NOT run jobs — it just guarantees the services are
RUNNING so their own internal triggers (Airbyte connection schedules,
Airflow DAG schedules, dbt-cosmos DAGs, etc.) can fire as scheduled.

Triggers:
  - manual: only fires when operator clicks Run on the dashboard
  - once:   fires once at a specific datetime, then converts to manual
  - cron:   fires recurringly on a cron expression

The actual fire-loop lives in pipelines_scheduler.py — this module is
just the CRUD + manual-trigger HTTP surface.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit, config, db, pipelines_executor

router = APIRouter(prefix="/api/pipelines", tags=["pipelines"])


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------
class PipelineStepIn(BaseModel):
    service_name: str = Field(..., min_length=1)
    action: str = Field("start", pattern="^(start|stop)$")
    buffer_seconds: int = Field(300, ge=0, le=3600)


class PipelineIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: str | None = Field(None, max_length=1000)
    trigger_type: str = Field(..., pattern="^(manual|once|cron)$")
    # ISO-8601 timestamp for `once`, cron expression for `cron`, None for `manual`.
    trigger_value: str | None = None
    trigger_timezone: str = Field("UTC", max_length=64)
    enabled: bool = True
    steps: list[PipelineStepIn] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _validate_trigger(trigger_type: str, value: str | None) -> str | None:
    """Returns the validated value (raises HTTP 400 on invalid)."""
    if trigger_type == "manual":
        return None
    if not value:
        raise HTTPException(400, f"trigger_value required for trigger_type='{trigger_type}'")
    if trigger_type == "once":
        try:
            # Accept anything fromisoformat parses; normalise to UTC ISO.
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat()
        except ValueError as e:
            raise HTTPException(400, f"invalid once-trigger timestamp: {e}")
    if trigger_type == "cron":
        # croniter is installed alongside Airflow's deps but the orchestrator
        # may not have it; do a lazy import.
        try:
            from croniter import croniter   # type: ignore
        except ImportError:
            # Minimal manual check: 5 whitespace-separated fields.
            parts = value.split()
            if len(parts) != 5:
                raise HTTPException(400, "cron expression must have 5 fields (min hr dom mon dow)")
            return value
        if not croniter.is_valid(value):
            raise HTTPException(400, f"invalid cron expression: {value!r}")
        return value
    raise HTTPException(400, f"unknown trigger_type: {trigger_type}")


def _validate_steps(steps: list[PipelineStepIn]) -> None:
    seen_services: set[str] = set()
    for s in steps:
        if s.service_name not in config.SERVICE_CATALOGUE:
            raise HTTPException(400, f"unknown service in step: '{s.service_name}'")
        if s.service_name in seen_services:
            raise HTTPException(400, f"service '{s.service_name}' appears twice in steps")
        if config.SERVICE_CATALOGUE[s.service_name].get("control_plane"):
            raise HTTPException(400, f"control-plane services can't be in pipelines: '{s.service_name}'")
        seen_services.add(s.service_name)


def _pipeline_to_dict(row: dict, steps: list[dict] | None = None) -> dict:
    """Serialise a pipelines-row + optional steps list for the API response."""
    return {
        "id":               row["id"],
        "name":             row["name"],
        "description":      row.get("description"),
        "trigger_type":     row["trigger_type"],
        "trigger_value":    row.get("trigger_value"),
        "trigger_timezone": row["trigger_timezone"],
        "enabled":          row["enabled"],
        "next_run_at":      row["next_run_at"].isoformat() if row.get("next_run_at") else None,
        "last_run_at":      row["last_run_at"].isoformat() if row.get("last_run_at") else None,
        "last_run_status":  row.get("last_run_status"),
        "created_at":       row["created_at"].isoformat(),
        "updated_at":       row["updated_at"].isoformat(),
        "steps":            steps if steps is not None else [],
    }


def _step_to_dict(row: dict) -> dict:
    return {
        "id":             row["id"],
        "order_index":    row["order_index"],
        "service_name":   row["service_name"],
        "action":         row["action"],
        "buffer_seconds": row["buffer_seconds"],
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@router.get("")
async def list_pipelines() -> dict[str, object]:
    """All pipelines (admin-facing). Step counts but not step details."""
    rows = await db.fetch(
        """
        SELECT p.*, (SELECT COUNT(*) FROM platform.pipeline_steps s WHERE s.pipeline_id = p.id) AS step_count
        FROM platform.pipelines p
        ORDER BY p.created_at DESC
        """,
    )
    return {
        "pipelines": [
            {**_pipeline_to_dict(r), "step_count": r["step_count"]} for r in rows
        ],
    }


@router.get("/{pipeline_id}")
async def get_pipeline(pipeline_id: int) -> dict[str, object]:
    row = await db.fetchrow(
        "SELECT * FROM platform.pipelines WHERE id = $1", pipeline_id,
    )
    if not row:
        raise HTTPException(404, f"pipeline {pipeline_id} not found")
    steps = await db.fetch(
        "SELECT * FROM platform.pipeline_steps WHERE pipeline_id = $1 ORDER BY order_index",
        pipeline_id,
    )
    return _pipeline_to_dict(dict(row), [_step_to_dict(dict(s)) for s in steps])


@router.post("")
async def create_pipeline(
    body: PipelineIn,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, object]:
    trigger_value = _validate_trigger(body.trigger_type, body.trigger_value)
    _validate_steps(body.steps)

    next_run = pipelines_executor.compute_next_run(
        body.trigger_type, trigger_value, body.trigger_timezone,
    )

    async with db.transaction() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO platform.pipelines
                (name, description, trigger_type, trigger_value,
                 trigger_timezone, enabled, next_run_at, created_by_user_id)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            RETURNING *
            """,
            body.name, body.description, body.trigger_type, trigger_value,
            body.trigger_timezone, body.enabled, next_run, actor_user_id,
        )
        pid = row["id"]
        for idx, s in enumerate(body.steps):
            await conn.execute(
                """
                INSERT INTO platform.pipeline_steps
                    (pipeline_id, order_index, service_name, action, buffer_seconds)
                VALUES ($1, $2, $3, $4, $5)
                """,
                pid, idx, s.service_name, s.action, s.buffer_seconds,
            )

    await audit.write(
        "pipeline_created",
        service_name=None,
        user_id=actor_user_id,
        details={"pipeline_id": pid, "name": body.name,
                 "trigger_type": body.trigger_type, "step_count": len(body.steps)},
    )
    return await get_pipeline(pid)


@router.put("/{pipeline_id}")
async def update_pipeline(
    pipeline_id: int,
    body: PipelineIn,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, object]:
    existing = await db.fetchrow(
        "SELECT id FROM platform.pipelines WHERE id = $1", pipeline_id,
    )
    if not existing:
        raise HTTPException(404, f"pipeline {pipeline_id} not found")
    trigger_value = _validate_trigger(body.trigger_type, body.trigger_value)
    _validate_steps(body.steps)
    next_run = pipelines_executor.compute_next_run(
        body.trigger_type, trigger_value, body.trigger_timezone,
    )

    async with db.transaction() as conn:
        await conn.execute(
            """
            UPDATE platform.pipelines
            SET name = $2, description = $3, trigger_type = $4,
                trigger_value = $5, trigger_timezone = $6, enabled = $7,
                next_run_at = $8
            WHERE id = $1
            """,
            pipeline_id, body.name, body.description, body.trigger_type,
            trigger_value, body.trigger_timezone, body.enabled, next_run,
        )
        # Replace steps wholesale — simpler than diffing. Pipelines are
        # small (typically 1-5 steps) so the rewrite cost is negligible.
        await conn.execute("DELETE FROM platform.pipeline_steps WHERE pipeline_id = $1", pipeline_id)
        for idx, s in enumerate(body.steps):
            await conn.execute(
                """
                INSERT INTO platform.pipeline_steps
                    (pipeline_id, order_index, service_name, action, buffer_seconds)
                VALUES ($1, $2, $3, $4, $5)
                """,
                pipeline_id, idx, s.service_name, s.action, s.buffer_seconds,
            )

    await audit.write(
        "pipeline_updated",
        service_name=None,
        user_id=actor_user_id,
        details={"pipeline_id": pipeline_id, "name": body.name},
    )
    return await get_pipeline(pipeline_id)


@router.delete("/{pipeline_id}")
async def delete_pipeline(
    pipeline_id: int,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, object]:
    row = await db.fetchrow(
        "SELECT name FROM platform.pipelines WHERE id = $1", pipeline_id,
    )
    if not row:
        raise HTTPException(404, f"pipeline {pipeline_id} not found")
    await db.fetch("DELETE FROM platform.pipelines WHERE id = $1", pipeline_id)
    await audit.write(
        "pipeline_deleted",
        service_name=None,
        user_id=actor_user_id,
        details={"pipeline_id": pipeline_id, "name": row["name"]},
    )
    return {"ok": True, "deleted_id": pipeline_id}


@router.post("/{pipeline_id}/run")
async def run_pipeline_now(
    pipeline_id: int,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, object]:
    """Fire-and-forget manual trigger. Returns immediately with the run id."""
    p = await db.fetchrow(
        "SELECT * FROM platform.pipelines WHERE id = $1", pipeline_id,
    )
    if not p:
        raise HTTPException(404, f"pipeline {pipeline_id} not found")
    if not p["enabled"]:
        raise HTTPException(400, "pipeline is disabled — enable it first")
    run_id = await pipelines_executor.fire_pipeline(
        pipeline_id, triggered_by="manual", triggered_by_user_id=actor_user_id,
    )
    return {"ok": True, "run_id": run_id, "pipeline_id": pipeline_id}


@router.post("/runs/{run_id}/cancel")
async def cancel_run(
    run_id: int,
    actor_user_id: Annotated[int | None, Query()] = None,
) -> dict[str, object]:
    """Cancel a currently-running pipeline run.

    Marks status='cancelled' in the DB. The executor checks this flag
    between steps; if the run is cancelled mid-flight, the in-flight
    step is allowed to complete (docker compose up/stop is already
    racing) but no more steps fire. Idempotent — cancelling a
    succeeded/failed/already-cancelled run is a no-op.
    """
    row = await db.fetchrow(
        "SELECT pipeline_id, status FROM platform.pipeline_runs WHERE id = $1",
        run_id,
    )
    if not row:
        raise HTTPException(404, f"pipeline run {run_id} not found")
    if row["status"] != "running":
        return {"ok": True, "run_id": run_id, "status": row["status"], "noop": True}
    await db.fetch(
        """
        UPDATE platform.pipeline_runs
        SET status = 'cancelled',
            completed_at = now(),
            error_summary = 'cancelled by operator'
        WHERE id = $1 AND status = 'running'
        """,
        run_id,
    )
    await audit.write(
        "pipeline_run_cancelled",
        service_name=None,
        user_id=actor_user_id,
        details={"pipeline_id": row["pipeline_id"], "run_id": run_id},
    )
    return {"ok": True, "run_id": run_id, "status": "cancelled"}


@router.get("/{pipeline_id}/runs")
async def list_runs(
    pipeline_id: int,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> dict[str, object]:
    runs = await db.fetch(
        """
        SELECT id, pipeline_id, triggered_by, status, started_at,
               completed_at, error_summary, step_results
        FROM platform.pipeline_runs
        WHERE pipeline_id = $1
        ORDER BY started_at DESC
        LIMIT $2
        """,
        pipeline_id, limit,
    )
    return {
        "runs": [
            {
                "id":            r["id"],
                "pipeline_id":   r["pipeline_id"],
                "triggered_by":  r["triggered_by"],
                "status":        r["status"],
                "started_at":    r["started_at"].isoformat(),
                "completed_at":  r["completed_at"].isoformat() if r["completed_at"] else None,
                "error_summary": r["error_summary"],
                "step_results":  r["step_results"],
            }
            for r in runs
        ],
    }
