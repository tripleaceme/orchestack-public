"""Credentials API — read + update the operator's `.env`.

The orchestrator owns the operator's `.env` (bind-mounted at
config.ENV_FILE — see system/docker/docker-compose.yml). This module
exposes:

    GET  /api/credentials             → list all variables, with sensitivity
                                        flags and a coarse `editable` hint
    PUT  /api/credentials/{key}       → update a single variable; persists
                                        back to the bind-mounted file

Why a per-key PUT rather than a bulk POST: each update produces an
audit-log row and the partial-update semantics let the dashboard's
HTMX swap target a single row at a time.

Sensitivity / editability is decided from the variable NAME, not from
its value:
  - Always read-only: `*_TAG` (image tags — bundle-update concern, not
    a credential rotation), and `ORCHESTACK_DB_PASSWORD` (rotating
    breaks the platform without a co-ordinated restart; M4 will add a
    proper rotation flow).
  - Sensitive (masked in GET unless `?reveal=true`): anything matching
    `*_PASSWORD`, `*_PASS`, `*_SECRET`, `*_KEY`, `*_TOKEN`.
  - Everything else: plain value.

The .env file is parsed and rewritten with care:
  - Comments and blank lines are preserved.
  - Variable VALUES are rewritten in place — line numbers stay stable.
  - Unknown / unexpected keys in the file are returned to the dashboard
    so the operator can see EVERYTHING in their .env, not just what
    OrcheStack knows about.
"""

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


# Read-only keys — the dashboard renders these as informational only.
READ_ONLY_PATTERNS = (
    re.compile(r"_TAG$"),                  # AUTH_TAG, ORCHESTRATOR_TAG, …
    re.compile(r"^ORCHESTACK_DB_PASSWORD$"),  # rotating breaks the platform
)

# Sensitive patterns — values masked in GET responses unless reveal=true.
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
    """Yield (line_no, raw_line) for the operator's .env, 1-indexed.

    Defensive against the bind-mount-as-empty-directory trap: if the
    operator's .env didn't exist on the host when the orchestrator
    started, Docker silently created a directory at the bind-mount
    target instead of failing. Path.exists() returns True for that
    directory, then read_text() raises IsADirectoryError and 500s the
    credentials page. Treat missing OR not-a-file as "no .env" — the
    credentials page then renders an empty list cleanly, and the
    operator can see the orchestrator's warning logs to understand why.
    """
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
    """Return every variable in .env with sensitivity + editability metadata."""
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


# ----------------------------------------------------------------------
# Live connection tests for database-typed credentials.
#
# What the test does, key-by-key:
#
#   PIPELINE_DB_PASSWORD — open a PostgreSQL connection as
#     PIPELINE_DB_USER against PIPELINE_DB_NAME on orchestack-postgres
#     using the PROPOSED password. Close immediately. Success means the
#     password is correct from postgres's perspective.
#
#   METABASE_DB_PASSWORD — same shape, against the `metabase` role and
#     the `metabase` database.
#
#   PGADMIN_DEFAULT_PASSWORD — no test. pgAdmin's admin user lives in
#     its own SQLite store, not in our postgres, and we don't read that
#     store. Returns testable=False.
#
#   ORCHESTACK_DB_PASSWORD — never tested. This is the platform admin
#     password and changing it requires a stack restart anyway; the test
#     would only confirm the OLD password is still good. Returns
#     testable=False.
#
#   Anything else — testable=False. Don't fake a green light by saying
#     "all good" when there's actually nothing to verify.
#
# This is what the dashboard's per-service Edit-config form calls to
# satisfy the docs' "live connection test before save" promise, and what
# the global /app/credentials page calls when the operator hits Save on
# a DB credential.
# ----------------------------------------------------------------------
_DB_TEST_TABLE: dict[str, tuple[str, str, str]] = {
    # key: (user_key, db_key, user_default — used if user_key is unset)
    "PIPELINE_DB_PASSWORD":  ("PIPELINE_DB_USER",  "PIPELINE_DB_NAME", ""),
    "METABASE_DB_PASSWORD":  ("__literal_metabase__", "__literal_metabase__", "metabase"),
}


@router.post("/{key}/test")
async def test_credential(key: str, req: TestRequest) -> dict[str, object]:
    """Live-test a credential value before the operator persists it."""
    if key not in _DB_TEST_TABLE:
        return {"testable": False,
                 "reason": f"No live connection test available for {key}."}

    user_key, db_key, default_user = _DB_TEST_TABLE[key]
    # Re-read .env every call — the operator may have updated other
    # related vars in the same session and we want to test against the
    # CURRENT linked-key values, not stale ones.
    env_map: dict[str, str] = {}
    for _, line in _iter_env_lines():
        m = LINE_RE.match(line.strip())
        if m:
            env_map[m.group(1)] = m.group(2)

    if user_key == "__literal_metabase__":
        # Metabase uses fixed role + DB names (`metabase`); they're not
        # in .env so we hardcode them here.
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
        # asyncpg.exceptions.InvalidPasswordError, ConnectionFailure,
        # etc — surface the postgres-side error so the operator knows
        # what's actually wrong.
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
    """Update a single variable in .env. Read-only keys are rejected.

    Audit log captures the change with the key but NEVER the value — we
    don't want secrets surfacing in the audit table. The presence of an
    update is sufficient evidence.
    """
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
        # Append at the end — new key.
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
