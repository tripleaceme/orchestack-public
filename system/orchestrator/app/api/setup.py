"""Wizard handoff — POST /api/setup/deploy.

Called once when the operator clicks "Create services" on deploying.html.
This is the moment localStorage state becomes real database state.

Phase 2.5 scope (what this file does):
1. Validate the request body shape (Pydantic)
2. Create the pipeline database + scoped role inside postgres
3. Insert one row into platform.setup_state with the full configuration
4. Insert one platform.installed_services row per selected tool
5. Return 202 Accepted with a deploy_id the client can use to poll status

What this DOES NOT do (deferred):
- Image pulls. The operator can let the next session-open trigger that lazily.
  (M5 may revisit if perceived first-tool-open latency is too slow.)
- Writing per-service .env files to a shared volume. M4 picks this up when
  the per-service compose snippets actually need wired-up env_files.
- Starting any service. Services start lazily on first session.

Deliberate non-feature: idempotency. Re-submitting the wizard is treated
as a fresh deploy. If the operator wants to reconfigure, they re-run.
Avoids the "what should the orchestrator do if the pipeline DB already
exists with different credentials" question — which is real but not M2.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import audit, db

router = APIRouter(prefix="/api/setup", tags=["setup"])


class Profile(BaseModel):
    full_name: str = Field(..., min_length=1)
    email: str = Field(..., min_length=3)
    username: str | None = None
    company_name: str | None = None


class DeployRequest(BaseModel):
    profile: Profile
    selections: dict[str, str] = Field(
        ...,
        description="Layer -> tool name, e.g. {'ingestion': 'Airbyte', 'warehouse': 'PostgreSQL'}",
    )
    credentials: dict[str, str] = Field(
        ...,
        description="Flat map of env var name to value (PIPELINE_DB_USER, AIRFLOW_FERNET_KEY, etc.)",
    )


def _validate_pipeline_db_inputs(creds: dict[str, str]) -> tuple[str, str, str]:
    """Extract + sanity-check the pipeline DB credentials before SQL.

    These get interpolated into CREATE DATABASE / CREATE ROLE statements
    (asyncpg won't let us parameterise identifiers), so we restrict them
    to a strict character class. The wizard already enforces this on
    the client side but we re-validate here — never trust the network.
    """
    import re
    safe = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,30}$")
    user = creds.get("PIPELINE_DB_USER", "")
    name = creds.get("PIPELINE_DB_NAME", "")
    password = creds.get("PIPELINE_DB_PASSWORD", "")
    if not safe.fullmatch(user):
        raise HTTPException(400, f"PIPELINE_DB_USER {user!r} must match {safe.pattern}")
    if not safe.fullmatch(name):
        raise HTTPException(400, f"PIPELINE_DB_NAME {name!r} must match {safe.pattern}")
    if len(password) < 12:
        raise HTTPException(400, "PIPELINE_DB_PASSWORD must be at least 12 chars")
    return user, name, password


@router.post("/deploy", status_code=202)
async def deploy(req: DeployRequest) -> dict[str, object]:
    """Materialise the wizard's plan into database state.

    Returns 202 Accepted with a deploy_id; the client polls
    /api/setup/deploy/{id} to learn the outcome. (For M2.5 the deploy
    finishes synchronously inside this handler, so polling immediately
    returns "ready" — but the contract is async so we can move expensive
    operations like image pulls behind it later.)
    """
    deploy_id = str(uuid4())
    user, name, password = _validate_pipeline_db_inputs(req.credentials)

    await audit.write(
        "setup_deploy_started",
        details={"deploy_id": deploy_id, "selections": req.selections},
    )

    # ---- 1. Create the pipeline database and its scoped role -----------
    # We connect as the bootstrap superuser (config.DB_USER) and issue
    # CREATE DATABASE outside any transaction (postgres requires this).
    # If the DB already exists from a previous deploy, that's a 409 — we
    # don't silently reuse, because credentials might differ.
    try:
        # asyncpg won't let us parameterise identifiers, so we double-quote
        # the validated names. The password IS parameterised — safe even
        # if it contains quotes.
        async with db.get_pool().acquire() as conn:
            exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1",
                name,
            )
            if exists:
                raise HTTPException(
                    409,
                    f"pipeline database {name!r} already exists. "
                    "Re-deploying with a different name or dropping the "
                    "existing one are both options. M2 does not auto-drop.",
                )

            role_exists = await conn.fetchval(
                "SELECT 1 FROM pg_roles WHERE rolname = $1", user,
            )
            if not role_exists:
                # Quote the role name; parameterise the password literal.
                await conn.execute(
                    f'CREATE ROLE "{user}" WITH LOGIN PASSWORD $1', password,
                )
            await conn.execute(f'CREATE DATABASE "{name}" OWNER "{user}"')

    except HTTPException:
        raise
    except Exception as e:
        await audit.write(
            "setup_deploy_db_failed",
            details={"deploy_id": deploy_id, "error": str(e)},
        )
        raise HTTPException(500, f"pipeline DB creation failed: {e}") from e

    # ---- 2. Persist the full setup state (one row, latest wins) --------
    import json
    await db.execute(
        """
        INSERT INTO platform.setup_state (status, payload, deploy_id, created_at)
        VALUES ('ready', $1::jsonb, $2, now())
        """,
        json.dumps({
            "profile": req.profile.model_dump(),
            "selections": req.selections,
            # Don't store password material in setup_state; it's already in
            # postgres' own auth tables. Storing it twice would be a second
            # leak surface.
            "credentials_keys": sorted(req.credentials.keys()),
        }),
        deploy_id,
    )

    # ---- 3. Mark each selected tool as installed -----------------------
    async with db.transaction() as conn:
        for layer, tool in req.selections.items():
            if tool == "None" or not tool:
                continue
            await conn.execute(
                """
                INSERT INTO platform.installed_services
                    (service_name, layer, installed_at)
                VALUES ($1, $2, now())
                ON CONFLICT (service_name) DO UPDATE
                    SET layer = EXCLUDED.layer, installed_at = now()
                """,
                tool, layer,
            )

    await audit.write(
        "setup_deploy_complete",
        details={
            "deploy_id": deploy_id,
            "pipeline_db": name,
            "pipeline_user": user,
            "selections": req.selections,
        },
    )
    return {
        "deploy_id": deploy_id,
        "status": "ready",
        "pipeline_db": name,
        "pipeline_user": user,
    }


@router.get("/deploy/{deploy_id}")
async def get_deploy_status(deploy_id: str) -> dict[str, object]:
    """Poll the status of a deploy. Used by deploying.html during M2.5."""
    row = await db.fetchrow(
        "SELECT status, created_at FROM platform.setup_state WHERE deploy_id = $1",
        deploy_id,
    )
    if row is None:
        raise HTTPException(404, "deploy not found")
    return {
        "deploy_id": deploy_id,
        "status": row["status"],
        "created_at": row["created_at"].isoformat(),
    }
