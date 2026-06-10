"""Audit log read API — `GET /api/audit`.

The audit log is append-only (writes happen via `audit.write()` from every
state-changing endpoint). This module exposes the read side for the
dashboard's `/app/audit` page: paginated, filterable by event_type,
target, and date range.

Why a separate file from `audit.py`:
    audit.py    — the WRITE side, used internally by every endpoint
    audit_api.py — the READ side, public HTTP API

The write side stays simple and untouched; the read side has all the
filter/pagination plumbing without cluttering it.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query

from .. import db

router = APIRouter(prefix="/api/audit", tags=["audit"])


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # Accept both "2026-06-03T10:00:00Z" and "2026-06-03T10:00:00+00:00".
        # datetime.fromisoformat handles +00:00 natively; we normalise Z→+00:00.
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("")
async def list_audit(
    event_type: str | None = Query(None, description="Exact match (e.g. 'service_started')."),
    target: str | None = Query(None, description="Exact match on the 'target' field."),
    since: str | None = Query(None, description="ISO 8601 lower bound on created_at (inclusive)."),
    until: str | None = Query(None, description="ISO 8601 upper bound on created_at (exclusive)."),
    limit: int = Query(20, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Paginated audit-log query.

    The `details` JSONB column is returned as a dict so the dashboard
    can render specific keys (e.g. `details.returncode` for failed
    compose runs). asyncpg auto-decodes JSONB if pgjsonb codec is
    registered; otherwise we json.loads() it here.
    """
    where: list[str] = []
    params: list[object] = []

    if event_type is not None:
        params.append(event_type)
        where.append(f"al.event_type = ${len(params)}")
    if target is not None:
        params.append(target)
        where.append(f"al.target = ${len(params)}")

    since_dt = _parse_iso(since)
    until_dt = _parse_iso(until)
    if since_dt is not None:
        params.append(since_dt)
        where.append(f"al.created_at >= ${len(params)}")
    if until_dt is not None:
        params.append(until_dt)
        where.append(f"al.created_at < ${len(params)}")

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    # Total count for pagination, with the same WHERE.
    count_row = await db.fetchrow(
        f"SELECT count(*) AS n FROM platform.audit_log al{where_sql}",
        *params,
    )
    total = count_row["n"] if count_row else 0

    # Page query.
    params.append(limit)
    params.append(offset)
    rows = await db.fetch(
        f"""
        SELECT
          al.id,
          al.event_type,
          al.actor_user_id,
          al.target,
          al.details,
          al.ip_address,
          al.created_at,
          u.username AS actor_username,
          u.full_name AS actor_full_name
        FROM platform.audit_log al
        LEFT JOIN platform.users u ON u.id = al.actor_user_id
        {where_sql}
        ORDER BY al.created_at DESC, al.id DESC
        LIMIT ${len(params) - 1} OFFSET ${len(params)}
        """,
        *params,
    )

    events = []
    for r in rows:
        # asyncpg may return JSONB as str OR dict depending on codec
        # registration — handle both for robustness.
        details = r["details"]
        if isinstance(details, str):
            try:
                details = json.loads(details)
            except json.JSONDecodeError:
                details = {"raw": details}
        events.append({
            "id": r["id"],
            "event_type": r["event_type"],
            "actor_user_id": r["actor_user_id"],
            "actor_username": r["actor_username"],
            "actor_full_name": r["actor_full_name"],
            "target": r["target"],
            "details": details or {},
            "ip_address": str(r["ip_address"]) if r["ip_address"] else None,
            "created_at": r["created_at"].isoformat() if r["created_at"] else None,
        })

    return {"events": events, "total": total, "limit": limit, "offset": offset}
