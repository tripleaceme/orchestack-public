"""Session check-in API — the input layer of the hot/cold tier mechanism.

When a user opens a tool's UI, the dashboard POSTs `/api/sessions` with the
service name + their user_id. The orchestrator inserts a row in
platform.service_sessions (which auto-generates a UUID token). While the
user has the tab open, the dashboard sends `POST /api/sessions/{token}/checkin`
every ~60s — the orchestrator refreshes `last_heartbeat_at`. When the user
closes the tab, DELETE sets `closed_at`.

The reconciler reads service_sessions every 30s and stops services with
no recent heartbeats AND no pin.

User identity: until M3.5 wires up real session cookies, every session
defaults to the seeded system user (config.DEFAULT_USER_ID = 1) so the
NOT NULL FK on service_sessions.user_id is satisfied. The dashboard at
M3.5 will pass the actual authenticated user id.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from pydantic import BaseModel, Field

from .. import audit, config, db, docker_ops

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    active: bool = Query(True, description="If true (default), only sessions with closed_at IS NULL."),
    service: str | None = Query(None, description="Filter by service name."),
    user_id: int | None = Query(None, description="Filter by user_id."),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, object]:
    """List service sessions, joined with users for display purposes.

    Used by the dashboard's `/app/sessions` page and the audit-log
    correlation flow. Default is active-only because the active set is
    small (n ≈ open browser tabs); pass `active=false` to see history.

    The `idle_seconds` field is computed at query time as
    `now() - last_heartbeat_at` — operators care about "is this session
    still being touched" more than the raw heartbeat timestamp.
    """
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

    # Total count for pagination headers. Cheap because of the partial
    # index `idx_service_sessions_active` when active=true.
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
    """Open a session against a service. Auto-starts the service by default.

    Returns the session token the client must use for subsequent checkins
    and the eventual DELETE. The token is generated by postgres via the
    DEFAULT gen_random_uuid() on service_sessions.token.
    """
    if req.service not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {req.service}")

    user_id = req.user_id if req.user_id is not None else config.DEFAULT_USER_ID

    # RETURNING gives us the auto-generated token from the schema's
    # DEFAULT clause — no need to generate it on the client side.
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
            result = await docker_ops.start_service(req.service)
            started = result.ok
            await audit.write(
                "session_autostart" if result.ok else "session_autostart_failed",
                service_name=req.service, user_id=user_id,
                details={"returncode": result.returncode, "stderr": result.short_stderr},
            )
        else:
            # Service is in the catalogue but not yet "managed" (no compose
            # snippet — M4 work). Record the session anyway; nothing to start.
            await audit.write(
                "session_autostart_skipped_unmanaged",
                service_name=req.service, user_id=user_id,
            )

    return {"token": str(token), "service": req.service, "started": started}


@router.post("/{token}/checkin")
async def checkin(token: UUID) -> dict[str, object]:
    """Refresh last_heartbeat_at for an open session. Idempotent.

    No-op if the session doesn't exist or has been closed — the reconciler
    only counts sessions where closed_at IS NULL, so a closed session
    wouldn't extend a service's life even if we did update it.
    """
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
    """Close a session. We don't physically delete the row — setting closed_at
    leaves history visible to the M3 dashboard's audit view AND to M5's
    evaluation queries (which count session durations to estimate usage).

    Returns HTTP 204 via an explicit Response object. See the same comment
    in pinning.py — the decorator's status_code=204 can't be used directly
    in FastAPI 0.115 because of a route-registration assertion.
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
