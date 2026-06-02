"""Wizard handoff — POST /api/setup/deploy.

Called once when the operator clicks "Create services" on deploying.html.
This is the moment localStorage state becomes real database state.

What this does:
  1. Validate request body shape (Pydantic) + pipeline DB inputs (regex).
  2. Create the pipeline database + scoped role inside postgres.
  3. Upsert platform.setup_state for the actor user (current_step='completed').
  4. Upsert one platform.installed_services row per selected, catalogued tool.
  5. Return 200 OK with the deploy summary the client can render.

What this does NOT do (deferred):
  - Image pulls. The operator can let the next session-open trigger that.
  - Per-service .env file writes. M4 picks this up when the per-service
    compose snippets actually need wired-up env_files.
  - Starting any service. Services start lazily on first session.

Schema notes
------------
platform.setup_state is keyed on user_id and tracks wizard progress
(current_step + selections). It is NOT keyed on a deploy_id. Each user
has exactly one row that the wizard updates as they move through steps.
M3 will redirect users with current_step='completed' away from the wizard.

platform.installed_services is the registry of *what's been chosen* —
the orchestrator reads it on startup to know which compose snippets to
make available. tier/layer must satisfy CHECK constraints; we look those
up from SERVICE_CATALOGUE.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import audit, config, db

router = APIRouter(prefix="/api/setup", tags=["setup"])

# Pipeline DB identifier regex — restrictive because these get interpolated
# into CREATE DATABASE / CREATE ROLE statements (asyncpg won't parameterise
# identifiers). The wizard's client-side validation enforces this already;
# re-validating here is defence in depth — never trust the network.
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
        description="Actor user id. Defaults to system user during M2.",
    )


def _validate_pipeline_db_inputs(creds: dict[str, str]) -> tuple[str, str, str]:
    """Extract + sanity-check pipeline DB credentials before any SQL."""
    user = creds.get("PIPELINE_DB_USER", "")
    name = creds.get("PIPELINE_DB_NAME", "")
    password = creds.get("PIPELINE_DB_PASSWORD", "")
    if not _SAFE_IDENT.fullmatch(user):
        raise HTTPException(400, f"PIPELINE_DB_USER {user!r} must match {_SAFE_IDENT.pattern}")
    if not _SAFE_IDENT.fullmatch(name):
        raise HTTPException(400, f"PIPELINE_DB_NAME {name!r} must match {_SAFE_IDENT.pattern}")
    if len(password) < 12:
        raise HTTPException(400, "PIPELINE_DB_PASSWORD must be at least 12 chars")
    return user, name, password


@router.post("/deploy")
async def deploy(req: DeployRequest) -> dict[str, object]:
    """Materialise the wizard's plan into database state."""
    user_id = req.user_id if req.user_id is not None else config.DEFAULT_USER_ID
    user, name, password = _validate_pipeline_db_inputs(req.credentials)

    await audit.write(
        "setup_deploy_started", user_id=user_id,
        details={"selections": req.selections},
    )

    # ---- 1. Create the pipeline database + scoped role ----------------
    # PostgreSQL DDL statements DO NOT accept $N parameter placeholders —
    # the parameterized-query protocol works only on DML grammar slots.
    # CREATE ROLE ... PASSWORD $1 fails with "syntax error at or near $1".
    # We have to inject the password as a quoted literal at the SQL level.
    #
    # Safety: the user/name are regex-validated upstream to match
    # ^[a-zA-Z][a-zA-Z0-9_]{2,30}$ so they're safe as double-quoted
    # identifiers. The password is wrapped in PostgreSQL's standard single-
    # quote literal form: surround with ' and double any internal '. This
    # is what pg_catalog.quote_literal() produces; we just do it in Python
    # because asyncpg won't expose it through parameter substitution.
    #
    # CREATE DATABASE additionally cannot run inside a transaction.
    def _quote_literal(s: str) -> str:
        """PostgreSQL string literal escape — see comments above."""
        return "'" + s.replace("'", "''") + "'"

    try:
        async with db.get_pool().acquire() as conn:
            db_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", name,
            )
            if db_exists:
                raise HTTPException(
                    409,
                    f"pipeline database {name!r} already exists. M2 does not "
                    "auto-drop existing databases; remove it manually or pick a "
                    "different name and re-submit the wizard.",
                )

            role_exists = await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", user,
            )
            if not role_exists:
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
        raise HTTPException(500, f"pipeline DB creation failed: {e}") from e

    # ---- 2. Mark this user's wizard as completed ---------------------
    # setup_state has user_id as primary key — UPSERT semantics. We store
    # the selections in the JSONB column so M3 can render "what they
    # picked" without re-querying installed_services.
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

    # ---- 3. Register each selected, catalogued tool ------------------
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

    await audit.write(
        "setup_deploy_complete", user_id=user_id,
        details={
            "pipeline_db": name,
            "pipeline_user": user,
            "registered": registered,
            "skipped": skipped,
        },
    )
    return {
        "status": "ready",
        "pipeline_db": name,
        "pipeline_user": user,
        "registered_services": registered,
        "skipped_services": skipped,
    }


@router.get("/state")
async def get_setup_state(user_id: int | None = None) -> dict[str, object]:
    """Return the wizard state for a user. Defaults to system user.

    Used by route guards (M3) to decide whether to bounce a user to /setup/*
    or to /app/. current_step='completed' means onboarding done.
    """
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
