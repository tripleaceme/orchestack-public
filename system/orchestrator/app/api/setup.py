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

    # Snapshot which services were already enabled BEFORE this deploy.
    # Used after the upsert loop to distinguish "newly added in THIS
    # deploy" from "already configured + just re-submitted because the
    # wizard sends every locked layer's prior pick along too." The
    # post-deploy hot-tier-start + cold-tier-pull should only fire for
    # the newly-added subset — restarting hot-tier services that are
    # already running is wasteful, and re-pulling cold-tier images
    # whose tags haven't moved is a wasted 5-15 minutes.
    pre_existing_rows = await db.fetch(
        "SELECT name FROM platform.installed_services WHERE enabled = TRUE"
    )
    previously_enabled: set[str] = {r["name"] for r in pre_existing_rows}

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
    # Only act on services that are NEWLY added in this deploy — not
    # the already-configured ones the wizard re-submits because their
    # layers are locked in add-more mode. Re-starting an already-running
    # hot service or re-pulling an unchanged cold service's image is
    # pure waste (and a 5-15 min waste for the cold case — the operator
    # sees "Pulling…" forever for services they already had on the
    # dashboard). Defined outside the try so it's available for the
    # response body even if the background-task scheduling fails.
    newly_added = [s for s in registered if s not in previously_enabled]

    try:
        import asyncio
        from .. import docker_ops, config as _conf, audit as _audit

        registered_managed = [
            svc for svc in newly_added
            if svc in _conf.SERVICE_CATALOGUE
            and not _conf.SERVICE_CATALOGUE[svc].get("control_plane", False)
        ]
        hot_tier_to_start = [
            svc for svc in registered_managed
            if _conf.SERVICE_CATALOGUE[svc].get("tier") == "hot"
        ]
        cold_tier_to_pull = [
            svc for svc in registered_managed
            if _conf.SERVICE_CATALOGUE[svc].get("tier") == "cold"
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

        # Cold-tier services aren't auto-started (that would defeat their
        # cold-tier semantics — they're supposed to sleep when idle). But
        # we DO eagerly pull their images in the background so the first
        # Open is instant instead of waiting on a 5-15min pull. Without
        # this, an operator who adds Airflow via the wizard then clicks
        # Open from the dashboard sits staring at the deploying spinner
        # while docker pulls 2.4 GB.
        if cold_tier_to_pull:
            log.info(
                "post-deploy: kicking off background pull for cold-tier services: %s",
                cold_tier_to_pull,
            )

            async def _pull_and_audit(svc_name: str) -> None:
                await _audit.write(
                    "service_pull_started",
                    service_name=svc_name,
                    details={"reason": "post-deploy eager pull (cold-tier)"},
                )
                result = await docker_ops.pull_service(svc_name)
                await _audit.write(
                    "service_pull_completed" if result.ok else "service_pull_failed",
                    service_name=svc_name,
                    details={
                        "returncode": result.returncode,
                        "stderr": result.short_stderr if not result.ok else None,
                    },
                )

            for svc in cold_tier_to_pull:
                asyncio.create_task(
                    _pull_and_audit(svc),
                    name=f"post-deploy-pull:{svc}",
                )
    except Exception as e:
        # Never let an auto-start failure poison the deploy response.
        log.warning("post-deploy auto-start scheduling failed: %s", e)

    return {
        "status": "ready",
        "warehouse_db": name,
        "pipeline_user": user,
        "registered_services": registered,
        # Subset of registered_services that didn't exist (or was disabled)
        # before this deploy. The deploying page filters its status table
        # to just these so the operator doesn't see "Pulling…" for
        # services that were already on their dashboard from a prior run.
        "newly_added_services": newly_added,
        "skipped_services": skipped,
        "credentials_written": credentials_written,
        "credentials_error": credentials_error,
    }


# ---------------------------------------------------------------------------
# .env file persistence — line-preserving update with backup
# ---------------------------------------------------------------------------
_VAR_REF_RE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")
_GITHUB_URL_RE = re.compile(r"^https://github\.com/")


def _concat_pat_into_repo_urls(incoming: dict[str, str]) -> dict[str, str]:
    """Concatenate per-service PATs into the corresponding *_REPO_URL.

    The wizard captures a plain HTTPS GitHub URL plus a separate PAT
    field (e.g. AIRFLOW_DAGS_REPO_URL + AIRFLOW_DAGS_REPO_PAT) — this
    function rewrites the URL to embed the PAT inline, since git's
    credential helper isn't available inside the service containers
    and the only auth path is the URL-embedded-PAT pattern:

      https://<PAT>@github.com/owner/repo.git

    Then the *_REPO_PAT key is REMOVED from the dict so the bare PAT
    never lands in .env on its own (where it'd be a second place to
    leak it from). The PAT-embedded URL is the only place it lives.

    Only rewrites github.com URLs — SSH URLs (git@github.com:...) and
    GitLab/Bitbucket variants pass through unchanged because their
    auth mechanisms are different (SSH key, deploy-token-in-URL).
    Empty PAT or URL stays as-is.
    """
    pairs = (
        ("AIRFLOW_DAGS_REPO_URL", "AIRFLOW_DAGS_REPO_PAT"),
        ("DBT_REPO_URL",          "DBT_REPO_PAT"),
    )
    out = dict(incoming)
    for url_key, pat_key in pairs:
        url = out.get(url_key, "")
        pat = out.get(pat_key, "")
        if url and pat and _GITHUB_URL_RE.match(url) and "@github.com" not in url:
            # Insert <PAT>@ right after https:// — the PAT in the URL
            # username field is git's HTTPS auth convention.
            out[url_key] = url.replace("https://", f"https://{pat}@", 1)
        # Always drop the PAT key — either consumed or empty, but never
        # written to .env on its own. PAT-embedded URLs are the single
        # source of truth.
        out.pop(pat_key, None)
    return out


def _resolve_credential_placeholders(incoming: dict[str, str]) -> dict[str, str]:
    """Substitute ${VAR} and *** placeholders in credential values.

    The wizard captures derived values as templates so the operator can
    SEE the structure before submitting (e.g. they can spot a typo in
    a host name). The two placeholder shapes:

      ${KEY}  — a reference to another credential being submitted in the
                same form. Resolved by looking up the matching key in
                `incoming`. If the key isn't in incoming, the ${KEY}
                stays literal (no surprise expansion against the host's
                env vars).

      ***     — a stand-in for WAREHOUSE_DB_PASSWORD (the wizard uses
                this specifically because the password field is masked
                and operators don't want to retype a long password into
                a URL field). Replaced with the actual warehouse password
                from incoming.

    Single-pass substitution — doesn't re-scan results, so a credential
    value can't transitively expand another. Sufficient for the current
    use case (GE_DATASOURCE_URL); tighten later if we add chained refs.
    """
    warehouse_pw = incoming.get("WAREHOUSE_DB_PASSWORD")
    resolved: dict[str, str] = {}
    for k, v in incoming.items():
        if not isinstance(v, str):
            resolved[k] = v
            continue
        out = _VAR_REF_RE.sub(
            lambda m: incoming.get(m.group(1), m.group(0)),
            v,
        )
        if warehouse_pw and "***" in out:
            out = out.replace("***", warehouse_pw)
        resolved[k] = out
    return resolved


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

    # Concatenate per-service PATs into the corresponding *_REPO_URL
    # BEFORE placeholder resolution. The wizard collects URL + PAT as
    # separate fields so the operator can paste each cleanly (and so
    # the PAT field can be type=password). The git client inside the
    # service containers needs the PAT embedded in the URL — there's
    # no git credential helper available there. Rewrites to:
    #   https://<PAT>@github.com/owner/repo.git
    # The PAT key itself is dropped so the bare token never lands in
    # .env on its own.
    incoming = _concat_pat_into_repo_urls(incoming)

    # Resolve ${VAR} and *** placeholders in credential values.
    # The configure-page wizard captures derived values as templates —
    # e.g. GE_DATASOURCE_URL = "postgresql://${WAREHOUSE_DB_USER}:***@host:5432/${WAREHOUSE_DB_NAME}"
    # — where `***` is a stand-in for the warehouse password (operators
    # don't type a password into a URL field) and ${VAR} references are
    # other credentials filled in elsewhere on the same form. Substitute
    # them so the on-disk .env value is the actual usable connection
    # string, not a template that would mislead the operator.
    incoming = _resolve_credential_placeholders(incoming)

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
