"""Pin/unpin endpoints — the keep-warm mechanism.

A pinned service is exempt from the reconciler's idle sweep — even if no
sessions are open, the orchestrator won't stop it. Pins can be permanent
(NULL expires_at) or time-bounded (expires_at in the future). Default TTL
when the wizard doesn't specify is 2 hours, which matches the typical
"open a tool, switch context for an hour, come back" pattern operators
have during active development.

Schema mapping
--------------
platform.service_pinning's columns are pinned_by_user_id, pinned_at,
expires_at, reason. Our API parameter `user_id` maps to pinned_by_user_id;
`ttl_seconds` becomes expires_at via Python timedelta arithmetic.
"""

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

    # Upsert: one pin per service. If you re-pin, you extend the TTL.
    # Note: ON CONFLICT requires the unique constraint on service_name,
    # which the schema has (TEXT NOT NULL UNIQUE).
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
    """Remove the pin from a service. Reconciler can stop it again at next idle.

    Note on the 204 dance: FastAPI 0.115's APIRoute assertion fires for
    `status_code=204` on the decorator regardless of `response_class`. The
    only working pattern is to set NO `status_code` on the decorator and
    return a `Response(status_code=204)` object explicitly — FastAPI passes
    it through as-is, the client sees the 204 with no body, RFC 7230 is
    honoured, and the assertion is sidestepped because the decorator's
    status_code stays at its default (allowed by is_body_allowed_for_status_code).
    """
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    # Use RETURNING to tell whether a row was actually pinned vs. already
    # absent — asyncpg execute() command-tag parsing is fragile for
    # multi-digit row counts.
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
