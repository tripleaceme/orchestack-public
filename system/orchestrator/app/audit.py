"""Audit log helper — fire-and-forget writes to platform.audit_log."""

from __future__ import annotations

import json
import logging
from typing import Any

from . import config, db

log = logging.getLogger("orchestrator.audit")


async def write(
    action: str,
    *,
    service_name: str | None = None,
    user_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Append one row to platform.audit_log. Errors are logged, not raised."""
    try:
        await db.execute(
            """
            INSERT INTO platform.audit_log (event_type, actor_user_id, target, details, created_at)
            VALUES ($1, $2, $3, $4::jsonb, now())
            """,
            action,
            user_id if user_id is not None else config.DEFAULT_USER_ID,
            service_name,
            json.dumps(details or {}),
        )
    except Exception as e:
        # Never raise — caller's operation continues even if audit fails.
        log.warning(
            "audit write failed action=%s service=%s err=%s",
            action, service_name, e,
        )
