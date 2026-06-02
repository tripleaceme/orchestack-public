"""Audit log helper.

Every state-changing operation (service start/stop, session events, pin/unpin,
wizard handoff) writes a row to platform.audit_log so M3 can render an
activity feed AND so M5 has time-series data to evaluate.

The helper is fire-and-forget: it logs to stderr on DB failures rather than
raising. The motivation is that audit-log failures must never block the
operation being audited — losing one row of history is better than failing
to stop an idle container because audit insertion happened to fail.

Schema mapping
--------------
Our API uses friendly parameter names; the schema uses generic ones. The
mapping in `write()` keeps callers from caring about the schema's column
choices:

  caller arg         schema column
  -----------------  ----------------
  action             event_type
  user_id            actor_user_id
  service_name       target
  details            details (jsonb)
"""

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
    """Append one row to platform.audit_log. Errors are logged, not raised.

    `action` is a short snake_case verb describing what happened, e.g.
    "service_started", "service_stopped_idle", "session_opened",
    "pipeline_db_created". Keep the action names stable across phases;
    M5's evaluation queries filter by action.

    `details` is a free-form dict — request body excerpts, container IDs,
    error stderr, anything useful for debugging after the fact. Stored as
    JSONB in the audit_log table.

    `user_id` falls back to config.DEFAULT_USER_ID (the seeded system user)
    when not provided, because audit_log.actor_user_id is a NULL-able FK
    but we'd rather track which user triggered each action — including the
    system user for background operations like reconciler ticks.
    """
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
