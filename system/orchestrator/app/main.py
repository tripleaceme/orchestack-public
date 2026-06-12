"""OrcheStack orchestrator — FastAPI control-plane service.

Wires together:
  - The four API routers (services, sessions, pinning, setup)
  - The asyncpg connection pool (lifespan-managed)
  - The reconciler background task (lifespan-managed)
  - The /api/health endpoint

The lifespan handler is the canonical FastAPI pattern for managing
resources whose lifetime tracks the app's: open them on startup, close
them on shutdown, expose them through app.state if route handlers need
direct access (we use module-level singletons instead — db.get_pool()).

See design/m2-orchestrator.md for the architecture overview.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI

from . import config, db, docker_ops, reconciler
from .api import admin, audit_api, auth, credentials, pinning, services, sessions, setup as setup_api, users

# ---------- Logging --------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, config.LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("orchestrator")


# ---------- Lifespan -------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Open the DB pool + launch the reconciler at startup; close on shutdown.

    Exception handling here is intentional: if the DB can't be reached at
    startup, we log loudly and continue — the orchestrator can still serve
    /api/health (reporting postgres: false) so an operator can see what's
    wrong. Better than crash-loop, which gives no useful feedback.
    """
    pool_ready = False
    try:
        await db.init_pool()
        pool_ready = True
    except Exception as e:
        log.error("startup: DB pool init failed — running in degraded mode: %s", e)

    # .env readability check.
    #
    # The bind-mount-as-empty-directory trap has bitten three subsystems
    # so far (stop_service, /api/credentials, the Metabase bootstrap
    # hook). Catch the trap ONCE at startup and surface it as an audit
    # event so the operator sees the cause on /app/audit rather than
    # being puzzled by silently-broken downstream features.
    if pool_ready:
        try:
            import os as _os
            env_path = config.ENV_FILE
            env_ok = _os.path.isfile(env_path) and _os.access(env_path, _os.R_OK)
            if not env_ok:
                # Treat this as an audit event (visible on /app/audit)
                # and a loud log line. Don't fail startup — degraded
                # mode is more useful than a crash loop.
                from . import audit
                kind = (
                    "directory" if _os.path.isdir(env_path)
                    else "missing" if not _os.path.exists(env_path)
                    else "unreadable"
                )
                log.error(
                    "startup: env-file at %s is %s. Credentials writes, "
                    "stop_service env interpolation, and the Metabase "
                    "bootstrap will all silently misbehave. Re-extract "
                    "the bundle, restore ./.env on the host, and "
                    "`docker compose up -d` to re-mount.",
                    env_path, kind,
                )
                await audit.write(
                    "env_file_unreadable",
                    user_id=None,
                    details={"path": env_path, "kind": kind},
                )
        except Exception as e:
            log.warning("startup: env-file check raised: %s", e)

    # Auto-heal orphaned first-admin assignment.
    #
    # Earlier signups (commits before 601b06f) counted the seeded system
    # row when deciding whether the new user was the "first" user, so
    # is_first was always False and the Admin role was never granted.
    # Operators on those installs end up locked out of /app/users and
    # /app/roles even though they ARE the only real user. On startup,
    # detect "exactly one non-system user with zero roles" and grant
    # them Admin so they recover without losing data.
    #
    # Guarded by user-count == 1: we never silently elevate a user in a
    # multi-user install. Idempotent ON CONFLICT DO NOTHING so repeat
    # startups don't double-grant.
    if pool_ready:
        try:
            row = await db.fetchrow(
                """
                SELECT u.id
                FROM platform.users u
                LEFT JOIN platform.user_roles ur ON ur.user_id = u.id
                WHERE u.username != 'system'
                GROUP BY u.id
                HAVING count(ur.role_id) = 0
                """,
            )
            user_count = await db.fetchval(
                "SELECT count(*) FROM platform.users WHERE username != 'system'"
            )
            if row and user_count == 1:
                await db.execute(
                    """
                    INSERT INTO platform.user_roles (user_id, role_id)
                    SELECT $1, id FROM platform.roles WHERE name = 'Admin'
                    ON CONFLICT DO NOTHING
                    """,
                    row["id"],
                )
                log.warning(
                    "startup: auto-healed missing Admin role for user_id=%s "
                    "(only real user, had no roles)", row["id"],
                )
        except Exception as e:
            # Heal-on-startup is best-effort — never block app start on it.
            log.warning("startup: admin auto-heal skipped: %s", e)

    stop_event = asyncio.Event()
    reconciler_task: asyncio.Task | None = None
    if pool_ready:
        reconciler_task = asyncio.create_task(
            reconciler.run_loop(stop_event), name="reconciler",
        )

    log.info(
        "orchestack-orchestrator ready — phase=2.6 db=%s reconciler=%s",
        "ok" if pool_ready else "degraded",
        "running" if reconciler_task else "skipped",
    )

    try:
        yield
    finally:
        # Shutdown: stop the reconciler, then close the pool.
        if reconciler_task is not None:
            log.info("shutdown: signalling reconciler to stop")
            stop_event.set()
            try:
                await asyncio.wait_for(reconciler_task, timeout=10)
            except asyncio.TimeoutError:
                log.warning("reconciler did not stop within 10s, cancelling")
                reconciler_task.cancel()
        await db.close_pool()
        log.info("shutdown complete")


# ---------- App ------------------------------------------------------------
app = FastAPI(
    title="OrcheStack orchestrator",
    description=(
        "Control-plane service for OrcheStack. Implements event-driven hot/cold "
        "tier service orchestration. See design/m2-orchestrator.md for the full "
        "design."
    ),
    version="0.1.0",
    lifespan=lifespan,
)

# Mount the API routers.
app.include_router(services.router)
app.include_router(sessions.router)
app.include_router(pinning.router)
app.include_router(setup_api.router)
app.include_router(audit_api.router)
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(credentials.router)
app.include_router(admin.router)


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Readiness state, including subsystem checks.

    Schema is additive — new keys may appear over time. Clients should
    treat the absence of a key as "not yet checked" rather than "failed".
    The top-level `ok` is True only when ALL critical subsystems are ok.
    """
    postgres_ok = await db.ping()
    docker_ok = await docker_ops.ping()
    return {
        "ok": postgres_ok and docker_ok,
        "service": "orchestack-orchestrator",
        "version": app.version,
        "phase": "2.6",
        "checks": {
            "postgres": postgres_ok,
            "docker": docker_ok,
        },
    }
