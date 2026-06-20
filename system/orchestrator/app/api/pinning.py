"""Pin/unpin endpoints for the keep-warm mechanism."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from .. import audit, config, db

router = APIRouter(prefix="/api/services", tags=["pinning"])

DEFAULT_PIN_TTL_SECONDS = 7200  # 2 hours


class PinRequest(BaseModel):
    ttl_seconds: int | None = Field(
        DEFAULT_PIN_TTL_SECONDS,
        description=(
            "How long the pin lasts. Pass null for a permanent pin "
            "(operator must DELETE to clear). Default 7200s (2h)."
        ),
        ge=60,  # 1 minute minimum; shorter would be indistinguishable from "no pin"
    )
    user_id: int | None = Field(None, description="Defaults to system user")
    reason: str | None = Field(None, description="Optional human-readable reason for the pin")


@router.get("/{name}/pin")
async def get_pin(name: str) -> dict[str, object]:
    """Return the current pin record for `name`, or 404 if not pinned."""
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    row = await db.fetchrow(
        """
        SELECT
          sp.service_name,
          sp.pinned_by_user_id,
          sp.pinned_at,
          sp.expires_at,
          sp.reason,
          u.username AS pinned_by_username,
          u.full_name AS pinned_by_full_name
        FROM platform.service_pinning sp
        LEFT JOIN platform.users u ON u.id = sp.pinned_by_user_id
        WHERE sp.service_name = $1
          AND (sp.expires_at IS NULL OR sp.expires_at > now())
        """,
        name,
    )
    if row is None:
        raise HTTPException(404, f"no active pin for {name}")
    return {
        "service": row["service_name"],
        "pinned_by_user_id": row["pinned_by_user_id"],
        "pinned_by_username": row["pinned_by_username"],
        "pinned_by_full_name": row["pinned_by_full_name"],
        "pinned_at": row["pinned_at"].isoformat() if row["pinned_at"] else None,
        "expires_at": row["expires_at"].isoformat() if row["expires_at"] else None,
        "reason": row["reason"],
    }


@router.post("/{name}/pin")
async def pin_service(name: str, req: PinRequest) -> dict[str, object]:
    """Pin a service so the reconciler won't stop it."""
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    user_id = req.user_id if req.user_id is not None else config.DEFAULT_USER_ID
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=req.ttl_seconds)
        if req.ttl_seconds is not None
        else None
    )

    # ON CONFLICT requires the unique constraint on service_name (schema: TEXT NOT NULL UNIQUE).
    await db.execute(
        """
        INSERT INTO platform.service_pinning
            (service_name, pinned_by_user_id, pinned_at, expires_at, reason)
        VALUES ($1, $2, now(), $3, $4)
        ON CONFLICT (service_name) DO UPDATE SET
            pinned_by_user_id = EXCLUDED.pinned_by_user_id,
            pinned_at         = now(),
            expires_at        = EXCLUDED.expires_at,
            reason            = EXCLUDED.reason
        """,
        name, user_id, expires_at, req.reason,
    )
    await audit.write(
        "service_pinned", service_name=name, user_id=user_id,
        details={
            "ttl_seconds": req.ttl_seconds,
            "expires_at": expires_at.isoformat() if expires_at else "never",
            "reason": req.reason,
        },
    )
    return {
        "ok": True,
        "service": name,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.delete("/{name}/pin")
async def unpin_service(name: str) -> Response:
    # FastAPI 0.115's APIRoute assertion fires for `status_code=204` on the decorator
    # regardless of `response_class`; instead, leave the decorator's status_code at its
    # default and return Response(status_code=204) explicitly so the client sees 204 no-body.
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    # RETURNING distinguishes pinned vs already-absent; asyncpg execute() command-tag parsing
    # is fragile for multi-digit row counts.
    row = await db.fetchrow(
        "DELETE FROM platform.service_pinning WHERE service_name = $1 RETURNING service_name",
        name,
    )
    # No 404 on "not pinned" — DELETE is idempotent. We log the result.
    await audit.write(
        "service_unpinned", service_name=name,
        details={"was_pinned": row is not None},
    )
    return Response(status_code=204)
