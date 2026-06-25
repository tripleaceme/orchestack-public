"""Wizard handoff — POST /api/setup/deploy."""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import audit, config, db

log = logging.getLogger("orchestrator.setup")

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Warehouse DB identifier regex — restrictive because these get interpolated
# into CREATE DATABASE / CREATE ROLE statements (asyncpg won't parameterise
# identifiers). Defence in depth — never trust the network.
_SAFE_IDENT = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,30}$")


class Profile(BaseModel):
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    username: str | None = None
    company_name: str | None = None


class DeployRequest(BaseModel):
    profile: Profile
    selections: dict[str, str] = Field(
        ...,
        description="Wizard layer -> tool display name (e.g. {'ingestion': 'Airbyte'})",
    )
    credentials: dict[str, str] = Field(
        ...,
        description="Flat map of env var name -> value",
    )
    user_id: int | None = Field(
        None,
        description="Actor user id. Defaults to system user.",
    )


def _validate_warehouse_db_inputs(creds: dict[str, str]) -> tuple[str, str, str]:
    """Extract + sanity-check warehouse DB credentials before any SQL."""
    user = creds.get("WAREHOUSE_DB_USER", "")
    name = creds.get("WAREHOUSE_DB_NAME", "")
    password = creds.get("WAREHOUSE_DB_PASSWORD", "")
    if not _SAFE_IDENT.fullmatch(user):
        raise HTTPException(400, f"WAREHOUSE_DB_USER {user!r} must match {_SAFE_IDENT.pattern}")
    if not _SAFE_IDENT.fullmatch(name):
        raise HTTPException(
            400,
            f"WAREHOUSE_DB_NAME {name!r} is not a valid PostgreSQL identifier. "
            f"Use letters, digits and underscores only — no hyphens, dots, or spaces. "
            f"Start with a letter; 3–31 chars total. Example: 'data_warehouse', "
            f"'raw_data' (NOT 'raw-data'). Internal regex: {_SAFE_IDENT.pattern}",
        )
    if len(password) < 12:
        raise HTTPException(400, "WAREHOUSE_DB_PASSWORD must be at least 12 chars")
    return user, name, password


@router.post("/deploy")
async def deploy(req: DeployRequest) -> dict[str, object]:
    """Materialise the wizard's plan into database state."""
    user_id = req.user_id if req.user_id is not None else config.DEFAULT_USER_ID
    user, name, password = _validate_warehouse_db_inputs(req.credentials)

    await audit.write(
        "setup_deploy_started", user_id=user_id,
        details={"selections": req.selections},
    )

    # PostgreSQL DDL statements DO NOT accept $N parameter placeholders —
    # CREATE ROLE ... PASSWORD $1 fails with "syntax error at or near $1".
    # We inject the password as a quoted literal. user/name are regex-validated
    # to be safe as double-quoted identifiers; password is wrapped in PostgreSQL's
    # standard single-quote literal form (surround with ' and double internal ').
    # CREATE DATABASE additionally cannot run inside a transaction.
    def _quote_literal(s: str) -> str:
        return "'" + s.replace("'", "''") + "'"

    try:
        async with db.get_pool().acquire() as conn:
            db_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", name,
            )
            role_exists = await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", user,
            )

            # Idempotent: if DB + role already exist, skip CREATE so re-entering
            # the wizard to add a service doesn't 409 on the existing DB.
            if role_exists and db_exists:
                pass
            elif db_exists and not role_exists:
                await conn.execute(
                    f'CREATE ROLE "{user}" WITH LOGIN PASSWORD {_quote_literal(password)}'
                )
                await conn.execute(
                    f'ALTER DATABASE "{name}" OWNER TO "{user}"'
                )
            elif role_exists and not db_exists:
                await conn.execute(f'CREATE DATABASE "{name}" OWNER "{user}"')
            else:
                await conn.execute(
                    f'CREATE ROLE "{user}" WITH LOGIN PASSWORD {_quote_literal(password)}'
                )
                await conn.execute(f'CREATE DATABASE "{name}" OWNER "{user}"')
    except HTTPException:
        raise
    except Exception as e:
        await audit.write(
            "setup_deploy_db_failed", user_id=user_id,
            details={"error": str(e)},
        )
        raise HTTPException(500, f"warehouse DB creation failed: {e}") from e

    await db.execute(
        """
        INSERT INTO platform.setup_state
            (user_id, current_step, selections, started_at, updated_at, completed_at)
        VALUES ($1, 'completed', $2::jsonb, now(), now(), now())
        ON CONFLICT (user_id) DO UPDATE SET
            current_step = 'completed',
            selections   = EXCLUDED.selections,
            updated_at   = now(),
            completed_at = now()
        """,
        user_id, json.dumps(req.selections),
    )

    registered: list[str] = []
    skipped: list[dict[str, str]] = []
    async with db.transaction() as conn:
        for wizard_layer, display_name in req.selections.items():
            if not display_name or display_name == "None":
                continue
            catalogue_key = config.tool_name_to_catalogue_key(display_name)
            if catalogue_key is None:
                skipped.append({
                    "wizard_layer": wizard_layer, "tool": display_name,
                    "reason": "not_in_catalogue",
                })
                continue
            meta = config.SERVICE_CATALOGUE[catalogue_key]
            schema_layer = config.WIZARD_LAYER_TO_SCHEMA.get(
                wizard_layer, meta["layer"]
            )
            await conn.execute(
                """
                INSERT INTO platform.installed_services
                    (name, display_name, layer, tier, enabled,
                     configured_at, configured_by_user_id)
                VALUES ($1, $2, $3, $4, TRUE, now(), $5)
                ON CONFLICT (name) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    layer        = EXCLUDED.layer,
                    tier         = EXCLUDED.tier,
                    enabled      = TRUE,
                    configured_at = now(),
                    configured_by_user_id = EXCLUDED.configured_by_user_id
                """,
                catalogue_key,
                meta["display_name"],
                schema_layer,
                meta["tier"],
                user_id,
            )
            registered.append(catalogue_key)

    # Credential persistence is best-effort — if the env-file write fails
    # (permissions, missing mount, read-only fs) the deploy still succeeds.
    # DB role and installed_services rows are already committed; a
    # half-deployed install is worse than re-entering credentials later.
    credentials_written: list[str] = []
    credentials_error: str | None = None
    try:
        credentials_written = _persist_credentials_to_env(req.credentials)
    except Exception as e:
        log.exception("credentials persistence failed (deploy continues)")
        credentials_error = f"{type(e).__name__}: {e}"
        await audit.write(
            "setup_credentials_persist_failed", user_id=user_id,
            details={"error": credentials_error},
        )

    await audit.write(
        "setup_deploy_complete", user_id=user_id,
        details={
            "warehouse_db": name,
            "pipeline_user": user,
            "registered": registered,
            "skipped": skipped,
            # Record only the KEYS, never the values.
            "credentials_written": credentials_written,
            "credentials_error": credentials_error,
        },
    )

    # Auto-start hot-tier services in the background. The wizard's
    # Configure copy promises "the images will be pulled and services
    # created" — kick off start_service for every registered hot-tier
    # service so by the time the operator reaches the dashboard the
    # tiles are at minimum in "Starting" state (rather than uniformly
    # "Stopped" with the operator unsure what to do). Cold-tier services
    # remain on-demand: the operator opens them from the dashboard when
    # they actually need them.
    #
    # Fire-and-forget — we don't await the start tasks because pulling
    # heavy images can take 10+ minutes and we'd rather not block the
    # /deploy response. The dashboard's service grid polls
    # /orchestrator/api/services and reflects starting/running state
    # as the background tasks complete.
    try:
        import asyncio
        from .. import docker_ops, config as _conf
        hot_tier_to_start = [
            svc for svc in registered
            if svc in _conf.SERVICE_CATALOGUE
            and _conf.SERVICE_CATALOGUE[svc].get("tier") == "hot"
            and not _conf.SERVICE_CATALOGUE[svc].get("control_plane", False)
        ]
        if hot_tier_to_start:
            log.info(
                "post-deploy: kicking off background start for hot-tier services: %s",
                hot_tier_to_start,
            )
            for svc in hot_tier_to_start:
                asyncio.create_task(
                    docker_ops.start_service(svc),
                    name=f"post-deploy-start:{svc}",
                )
    except Exception as e:
        # Never let an auto-start failure poison the deploy response.
        log.warning("post-deploy auto-start scheduling failed: %s", e)

    return {
        "status": "ready",
        "warehouse_db": name,
        "pipeline_user": user,
        "registered_services": registered,
        "skipped_services": skipped,
        "credentials_written": credentials_written,
        "credentials_error": credentials_error,
    }


# ---------------------------------------------------------------------------
# .env file persistence — line-preserving update with backup
# ---------------------------------------------------------------------------
def _persist_credentials_to_env(credentials: dict) -> list[str]:
    """Append/update each credential key in the operator's .env; returns keys written."""
    if not credentials:
        return []

    env_path = Path(config.ENV_FILE)
    if not env_path.exists():
        log.warning(
            "credentials persist skipped — env file not found at %s. "
            "Was system/docker/.env bind-mounted into the orchestrator?",
            env_path,
        )
        return []

    KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
    incoming = {
        k: v for k, v in credentials.items()
        if k and KEY_RE.match(k) and v not in (None, "")
    }
    if not incoming:
        return []

    lines = env_path.read_text().splitlines()
    written: list[str] = []

    matched: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0]
        if key in incoming:
            lines[i] = f"{key}={incoming[key]}"
            matched.add(key)
            written.append(key)

    appended_keys = [k for k in incoming.keys() if k not in matched]
    if appended_keys:
        lines.append("")
        lines.append("# Added by the setup wizard")
        for k in appended_keys:
            lines.append(f"{k}={incoming[k]}")
            written.append(k)

    try:
        backup_path = env_path.parent / f"{env_path.name}.bak.{int(env_path.stat().st_mtime)}"
        if not backup_path.exists():
            backup_path.write_text(env_path.read_text())
    except OSError as e:
        log.warning("env backup write failed (continuing): %s", e)

    # Direct write — cannot use "tmpfile + os.replace" because .env is
    # bind-mounted from the host as a SINGLE FILE; renaming over a
    # bind-mounted file fails with EBUSY on Linux (mount locks the inode).
    env_path.write_text("\n".join(lines) + "\n")

    log.info("persisted %d credentials to %s", len(written), env_path)
    return written


@router.get("/state")
async def get_setup_state(user_id: int | None = None) -> dict[str, object]:
    """Return the wizard state for a user; defaults to system user."""
    uid = user_id if user_id is not None else config.DEFAULT_USER_ID
    row = await db.fetchrow(
        """
        SELECT current_step, selections, started_at, updated_at, completed_at
        FROM platform.setup_state
        WHERE user_id = $1
        """,
        uid,
    )
    if row is None:
        return {"user_id": uid, "current_step": "welcome", "selections": {}}
    return {
        "user_id": uid,
        "current_step": row["current_step"],
        "selections": json.loads(row["selections"]) if isinstance(row["selections"], str) else (row["selections"] or {}),
        "started_at":   row["started_at"].isoformat()   if row["started_at"]   else None,
        "updated_at":   row["updated_at"].isoformat()   if row["updated_at"]   else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
    }
