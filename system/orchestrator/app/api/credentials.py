"""Credentials API — read + update the operator's `.env`."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Iterator

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .. import audit, config

log = logging.getLogger("orchestrator.credentials")

router = APIRouter(prefix="/api/credentials", tags=["credentials"])


# Lock anything the operator never types into a form: platform bootstrap
# state, internal Docker hostnames/ports, image tags, and the
# <service>_admin DB roles that pre-start hooks provision by exact name
# (renaming on the dashboard without also renaming in postgres breaks
# the next service start). ORCHESTACK_* are frozen after first volume
# creation — editing them later fails auth against postgres and forces a
# destructive drop-volume recovery.
READ_ONLY_PATTERNS = (
    re.compile(r"_TAG$"),
    re.compile(r"_HOST$"),
    re.compile(r"_PORT$"),
    re.compile(r"^ORCHESTACK_"),
    re.compile(r"^WAREHOUSE_DB_USER$"),
    re.compile(r"^DBT_DB_USER$"),
    re.compile(r"^AIRBYTE_DB_USER$"),
    re.compile(r"^AIRBYTE_DB_NAME$"),
    re.compile(r"^AIRFLOW_DB_USER$"),
    re.compile(r"^AIRFLOW_DB_NAME$"),
    re.compile(r"^METABASE_DB_USER$"),
    re.compile(r"^METABASE_DB_NAME$"),
    re.compile(r"^OPENMETADATA_DB_USER$"),
    re.compile(r"^OPENMETADATA_DB_NAME$"),
    re.compile(r"^GE_DB_USER$"),
    re.compile(r"^GE_DB_NAME$"),
)

# Values masked in GET responses unless reveal=true.
SENSITIVE_PATTERNS = (
    re.compile(r"_PASSWORD$"),
    re.compile(r"_PASS$"),
    re.compile(r"_SECRET$"),
    re.compile(r"_KEY$"),
    re.compile(r"_TOKEN$"),
    re.compile(r"^ORCHESTACK_DB_PASSWORD$"),
)

LINE_RE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _is_readonly(key: str) -> bool:
    return any(p.search(key) for p in READ_ONLY_PATTERNS)


def _is_sensitive(key: str) -> bool:
    return any(p.search(key) for p in SENSITIVE_PATTERNS)


def _iter_env_lines() -> Iterator[tuple[int, str]]:
    """Yield (line_no, raw_line) for the operator's .env, 1-indexed."""
    # Defend against the bind-mount-as-empty-directory trap: if .env
    # didn't exist on the host at startup, Docker silently created a
    # directory at the bind-mount target. Path.exists() returns True
    # but read_text() then raises IsADirectoryError. is_file() rejects
    # the directory case so the page renders an empty list cleanly.
    p = Path(config.ENV_FILE)
    if not p.is_file():
        return
    try:
        text = p.read_text()
    except OSError:
        return
    for i, line in enumerate(text.splitlines(), start=1):
        yield i, line


@router.get("")
async def list_credentials(
    reveal: bool = Query(False, description="If true, return raw values for sensitive keys"),
) -> dict[str, object]:
    items: list[dict[str, object]] = []
    for _line_no, line in _iter_env_lines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        m = LINE_RE.match(stripped)
        if not m:
            continue
        key, value = m.group(1), m.group(2)
        sensitive = _is_sensitive(key)
        readonly = _is_readonly(key)
        displayed = value if (not sensitive or reveal) else ("•" * 10 if value else "")
        items.append({
            "key": key,
            "value": displayed,
            "is_sensitive": sensitive,
            "is_readonly": readonly,
            "is_set": bool(value),
        })
    return {"credentials": items, "env_file": config.ENV_FILE}


class UpdateRequest(BaseModel):
    value: str = Field(..., description="New value for the variable. Empty string clears it.")
    actor_user_id: int | None = Field(None, description="User triggering the change.")


class TestRequest(BaseModel):
    value: str = Field(..., description="The proposed new value to test BEFORE saving.")


# Live connection tests for DB-typed credentials. Only WAREHOUSE_DB_PASSWORD
# and METABASE_DB_PASSWORD are testable; pgAdmin's admin lives in its own
# SQLite store and ORCHESTACK_DB_PASSWORD would only confirm the OLD value
# is still good (rotation requires a stack restart). Everything else returns
# testable=False rather than faking a green light.
_DB_TEST_TABLE: dict[str, tuple[str, str, str]] = {
    # key: (user_key, db_key, user_default — used if user_key is unset)
    "WAREHOUSE_DB_PASSWORD":  ("WAREHOUSE_DB_USER",  "WAREHOUSE_DB_NAME", ""),
    "METABASE_DB_PASSWORD":  ("__literal_metabase__", "__literal_metabase__", "metabase"),
}


@router.post("/{key}/test")
async def test_credential(key: str, req: TestRequest) -> dict[str, object]:
    if key not in _DB_TEST_TABLE:
        return {"testable": False,
                 "reason": f"No live connection test available for {key}."}

    user_key, db_key, default_user = _DB_TEST_TABLE[key]
    # Re-read .env every call so we test against the operator's CURRENT
    # linked-key values, not values cached from a prior request.
    env_map: dict[str, str] = {}
    for _, line in _iter_env_lines():
        m = LINE_RE.match(line.strip())
        if m:
            env_map[m.group(1)] = m.group(2)

    if user_key == "__literal_metabase__":
        # Metabase's role + DB names are fixed (`metabase`) and not in .env.
        pg_user, pg_db = "metabase", "metabase"
    else:
        pg_user = env_map.get(user_key) or default_user
        pg_db   = env_map.get(db_key)
        if not (pg_user and pg_db):
            return {
                "testable": False,
                "reason": f"can't test {key}: {user_key} or {db_key} "
                           "isn't set in .env",
            }

    try:
        import asyncpg
        conn = await asyncpg.connect(
            host=config.DB_HOST, port=config.DB_PORT,
            user=pg_user, password=req.value, database=pg_db,
            timeout=5.0,
        )
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()
        return {"testable": True, "ok": True, "tested_as": pg_user, "tested_db": pg_db}
    except Exception as e:
        # Surface the postgres-side error class + message so the operator
        # can distinguish bad-password from unreachable-host etc.
        return {
            "testable": True,
            "ok": False,
            "tested_as": pg_user,
            "tested_db": pg_db,
            "error_class": type(e).__name__,
            "error": str(e)[:300],
        }


@router.put("/{key}")
async def update_credential(key: str, req: UpdateRequest) -> dict[str, object]:
    """Update a single variable in .env. Read-only keys are rejected."""
    # Audit log records the key but NEVER the value — keeps secrets out
    # of the audit table.
    if not LINE_RE.match(f"{key}=x"):
        raise HTTPException(400, f"invalid key shape: {key!r}")
    if _is_readonly(key):
        raise HTTPException(409, f"{key} is not editable from the dashboard")

    p = Path(config.ENV_FILE)
    if not p.exists():
        raise HTTPException(500, f"env file not found at {config.ENV_FILE}")

    lines = p.read_text().splitlines()
    updated = False
    for i, line in enumerate(lines):
        m = LINE_RE.match(line.strip())
        if m and m.group(1) == key:
            lines[i] = f"{key}={req.value}"
            updated = True
            break

    if not updated:
        lines.append(f"{key}={req.value}")

    p.write_text("\n".join(lines) + "\n")

    await audit.write(
        "credential_updated",
        user_id=req.actor_user_id,
        details={"key": key, "was_set": updated, "appended": not updated},
    )

    return {
        "ok": True,
        "key": key,
        "is_sensitive": _is_sensitive(key),
    }
