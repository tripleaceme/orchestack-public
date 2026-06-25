"""Session check-in API for the hot/cold tier mechanism."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import audit, config, db, docker_ops

log = logging.getLogger("orchestrator.sessions")

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    active: bool = Query(True, description="If true (default), only sessions with closed_at IS NULL."),
    service: str | None = Query(None, description="Filter by service name."),
    user_id: int | None = Query(None, description="Filter by user_id."),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """List service sessions, joined with users for display purposes."""
    where = ["1=1"]
    params: list[object] = []
    if active:
        where.append("ss.closed_at IS NULL")
    if service is not None:
        params.append(service)
        where.append(f"ss.service_name = ${len(params)}")
    if user_id is not None:
        params.append(user_id)
        where.append(f"ss.user_id = ${len(params)}")

    params.append(limit)
    params.append(offset)
    sql = f"""
        SELECT
          ss.token::text       AS token,
          ss.service_name      AS service_name,
          ss.user_id           AS user_id,
          u.username           AS username,
          u.full_name          AS full_name,
          ss.opened_at         AS opened_at,
          ss.last_heartbeat_at AS last_heartbeat_at,
          ss.closed_at         AS closed_at,
          EXTRACT(EPOCH FROM (now() - ss.last_heartbeat_at))::int AS idle_seconds
        FROM platform.service_sessions ss
        LEFT JOIN platform.users u ON u.id = ss.user_id
        WHERE {' AND '.join(where)}
        ORDER BY ss.opened_at DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
    """
    rows = await db.fetch(sql, *params)

    sessions = []
    for r in rows:
        sessions.append({
            "token": r["token"],
            "service": r["service_name"],
            "user_id": r["user_id"],
            "username": r["username"],
            "full_name": r["full_name"],
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
            "last_heartbeat_at": r["last_heartbeat_at"].isoformat() if r["last_heartbeat_at"] else None,
            "closed_at": r["closed_at"].isoformat() if r["closed_at"] else None,
            "idle_seconds": r["idle_seconds"],
        })

    count_where = where[:-0] if False else where
    count_sql = f"SELECT count(*) AS n FROM platform.service_sessions ss WHERE {' AND '.join(where)}"
    # Strip the LIMIT/OFFSET placeholders from params for the count query.
    count_row = await db.fetchrow(count_sql, *params[:-2])
    return {"sessions": sessions, "total": count_row["n"]}


class SessionOpenRequest(BaseModel):
    service: str = Field(..., description="Service name from SERVICE_CATALOGUE")
    user_id: int | None = Field(None, description="Optional — defaults to system user")
    auto_start: bool = Field(
        True,
        description=(
            "If true, the orchestrator starts the service in the background "
            "when the session opens. Set False for sessions where you just "
            "want to record interest without spinning anything up."
        ),
    )


@router.post("", status_code=201)
async def open_session(req: SessionOpenRequest) -> dict[str, object]:
    """Open a session against a service. Auto-starts the service by default."""
    if req.service not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {req.service}")

    user_id = req.user_id if req.user_id is not None else config.DEFAULT_USER_ID

    # Enforce one open session per (user, service); reuse existing token and
    # refresh heartbeat to avoid duplicate rows and stale idle timestamps.
    existing = await db.fetchrow(
        """
        SELECT token FROM platform.service_sessions
        WHERE service_name = $1 AND user_id = $2 AND closed_at IS NULL
        ORDER BY opened_at DESC LIMIT 1
        """,
        req.service, user_id,
    )
    if existing:
        token = existing["token"]
        await db.execute(
            "UPDATE platform.service_sessions SET last_heartbeat_at = now() "
            "WHERE token = $1",
            token,
        )
        await audit.write(
            "session_reused",
            service_name=req.service, user_id=user_id,
            details={"token": str(token)},
        )
    else:
        row = await db.fetchrow(
            """
            INSERT INTO platform.service_sessions
                (service_name, user_id)
            VALUES ($1, $2)
            RETURNING token
            """,
            req.service, user_id,
        )
        token = row["token"]
        await audit.write(
            "session_opened",
            service_name=req.service, user_id=user_id,
            details={"token": str(token), "auto_start": req.auto_start},
        )

    started = False
    if req.auto_start:
        meta = config.SERVICE_CATALOGUE[req.service]
        if meta.get("managed", False):
            # Fire-and-forget background task. Previously this awaited
            # start_service synchronously, which blocked the HTTP response
            # for the full duration of the docker compose up (up to 15min
            # on first-pull of heavy images like orchestack-airflow at
            # 2.4 GB). The browser's HTMX request would time out or the
            # operator would give up and reload, leaving the orchestrator
            # mid-pull with no operator feedback.
            #
            # Now: kick off start_service in the background, return
            # immediately. The dashboard's service grid (which polls
            # /api/services every few seconds) will see the
            # starting → running transition. The audit log gets
            # session_autostart or session_autostart_failed when the
            # background task completes. Service-pinning for cold-tier
            # services also moves into the background task.
            async def _run_autostart() -> None:
                try:
                    result = await docker_ops.start_service(req.service)
                    await audit.write(
                        "session_autostart" if result.ok else "session_autostart_failed",
                        service_name=req.service, user_id=user_id,
                        details={"returncode": result.returncode, "stderr": result.short_stderr},
                    )
                    if result.ok and meta.get("tier") == "cold":
                        existing_pin = await db.fetchval(
                            "SELECT 1 FROM platform.service_pinning "
                            "WHERE service_name = $1 AND "
                            "(expires_at IS NULL OR expires_at > now())",
                            req.service,
                        )
                        if not existing_pin:
                            auto_pin_expires = (
                                datetime.now(timezone.utc) + timedelta(hours=4)
                            )
                            await db.execute(
                                """
                                INSERT INTO platform.service_pinning
                                    (service_name, pinned_by_user_id, pinned_at,
                                      expires_at, reason)
                                VALUES ($1, $2, now(), $3, $4)
                                ON CONFLICT (service_name) DO NOTHING
                                """,
                                req.service, user_id, auto_pin_expires,
                                "auto-pin on Open (4hr default)",
                            )
                            await audit.write(
                                "service_auto_pinned",
                                service_name=req.service, user_id=user_id,
                                details={
                                    "expires_at": auto_pin_expires.isoformat(),
                                    "trigger": "session_open",
                                },
                            )
                except Exception as e:
                    await audit.write(
                        "session_autostart_failed",
                        service_name=req.service, user_id=user_id,
                        details={"error": f"{type(e).__name__}: {e}"},
                    )

            asyncio.create_task(_run_autostart(), name=f"autostart:{req.service}")
            # Optimistic — the background task will write the real
            # success/failure audit event when it finishes.
            started = True
        else:
            # Service is catalogued but not "managed" (no compose snippet);
            # record the session anyway, nothing to start.
            await audit.write(
                "session_autostart_skipped_unmanaged",
                service_name=req.service, user_id=user_id,
            )

    # Cascade sessions for declared `requires` upstreams (e.g. pgAdmin → PostgreSQL)
    # so the reconciler won't stop an in-use upstream. Failures are non-fatal.
    meta = config.SERVICE_CATALOGUE[req.service]
    for required in meta.get("requires", []) or []:
        if required not in config.SERVICE_CATALOGUE:
            continue
        try:
            existing_req = await db.fetchrow(
                """
                SELECT token FROM platform.service_sessions
                WHERE service_name = $1 AND user_id = $2 AND closed_at IS NULL
                ORDER BY opened_at DESC LIMIT 1
                """,
                required, user_id,
            )
            if existing_req:
                await db.execute(
                    "UPDATE platform.service_sessions SET last_heartbeat_at = now() "
                    "WHERE token = $1",
                    existing_req["token"],
                )
            else:
                await db.execute(
                    "INSERT INTO platform.service_sessions (service_name, user_id) "
                    "VALUES ($1, $2)",
                    required, user_id,
                )
                await audit.write(
                    "session_opened_cascade",
                    service_name=required, user_id=user_id,
                    details={"triggered_by": req.service},
                )
        except Exception as e:
            log.warning("cascade session for %s required by %s failed: %s",
                        required, req.service, e)

    return {"token": str(token), "service": req.service, "started": started}


@router.post("/{token}/checkin")
async def checkin(token: UUID) -> dict[str, object]:
    """Refresh last_heartbeat_at for an open session. Idempotent."""
    row = await db.fetchrow(
        """
        UPDATE platform.service_sessions
        SET last_heartbeat_at = now()
        WHERE token = $1 AND closed_at IS NULL
        RETURNING token
        """,
        token,
    )
    return {"ok": True, "matched": row is not None}


@router.delete("/{token}")
async def close_session(token: UUID) -> Response:
    """Close a session by setting closed_at; row is preserved for history.

    Returns 204 via an explicit Response — FastAPI 0.115's
    decorator status_code=204 trips a route-registration assertion.
    """
    row = await db.fetchrow(
        """
        UPDATE platform.service_sessions
        SET closed_at = now()
        WHERE token = $1 AND closed_at IS NULL
        RETURNING service_name, user_id
        """,
        token,
    )
    if row is not None:
        await audit.write(
            "session_closed",
            service_name=row["service_name"], user_id=row["user_id"],
            details={"token": str(token)},
        )
    return Response(status_code=204)
