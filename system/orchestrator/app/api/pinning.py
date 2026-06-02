"""Pin/unpin endpoints — the keep-warm mechanism.

A pinned service is exempt from the reconciler's idle sweep — even if no
sessions are open, the orchestrator won't stop it. Pins can be permanent
(NULL expires_at) or time-bounded (expires_at in the future). Default TTL
when the wizard doesn't specify is 2 hours, which matches the typical
"open a tool, switch context for an hour, come back" pattern operators
have during active development.

Two endpoints:
    POST   /api/services/{name}/pin     { ttl_seconds: int | null }
    DELETE /api/services/{name}/pin
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException
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


@router.post("/{name}/pin")
async def pin_service(name: str, req: PinRequest) -> dict[str, object]:
    """Pin a service so the reconciler won't stop it."""
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    if req.ttl_seconds is None:
        expires_at = None
    else:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=req.ttl_seconds)

    # Upsert: one pin per service. If you re-pin, you extend the TTL.
    await db.execute(
        """
        INSERT INTO platform.service_pinning (service_name, expires_at, created_at)
        VALUES ($1, $2, now())
        ON CONFLICT (service_name)
        DO UPDATE SET expires_at = EXCLUDED.expires_at, created_at = now()
        """,
        name, expires_at,
    )
    await audit.write(
        "service_pinned",
        service_name=name,
        details={
            "ttl_seconds": req.ttl_seconds,
            "expires_at": expires_at.isoformat() if expires_at else "never",
        },
    )
    return {
        "ok": True,
        "service": name,
        "expires_at": expires_at.isoformat() if expires_at else None,
    }


@router.delete("/{name}/pin", status_code=204)
async def unpin_service(name: str) -> None:
    """Remove the pin from a service. Reconciler can stop it again at next idle."""
    if name not in config.SERVICE_CATALOGUE:
        raise HTTPException(404, f"unknown service: {name}")

    result = await db.execute(
        "DELETE FROM platform.service_pinning WHERE service_name = $1",
        name,
    )
    # No 404 on "not pinned" — DELETE is idempotent. We log the result anyway.
    await audit.write(
        "service_unpinned",
        service_name=name,
        details={"was_pinned": result.endswith("0") is False},
    )
