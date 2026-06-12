"""Docker operations — subprocess wrappers around `docker compose` and `docker`.

The orchestrator owns the lifecycle of cold-tier services. To start one, we
shell out to `docker compose -f services/<name>.yml -p orchestack-service-<name>
up -d`; to stop, we use the matching `stop` command. We use subprocess +
the docker CLI (installed in the orchestrator image) rather than the Python
Docker SDK for three reasons:

1. The CLI does proper API version negotiation with the daemon. We had a
   nasty class of bugs in M1 where Traefik's embedded Go client hardcoded
   v1.24 and got rejected by modern daemons — the CLI doesn't have that
   problem.
2. Operators can reproduce any orchestrator action by running the exact
   command from their own shell. Debuggability beats abstraction.
3. The Python SDK would have its own API-version pitfalls and would
   require us to re-implement `compose` semantics (multi-file, project
   namespaces, etc.) on top of the lower-level container API.

Every function here is async-aware: it runs the subprocess in a thread pool
via asyncio.to_thread so the FastAPI event loop isn't blocked while docker
does its thing (which can take a few seconds for image pulls).
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from dataclasses import dataclass

from . import config

log = logging.getLogger("orchestrator.docker")


@dataclass
class CommandResult:
    """Return value of every docker_ops call.

    `ok` is the headline — True iff the subprocess exited 0 AND we
    didn't catch an exception. `stdout`/`stderr` are captured so callers
    can pass them to audit logging on failure.
    """
    ok: bool
    returncode: int
    stdout: str
    stderr: str

    @property
    def short_stderr(self) -> str:
        """Last 500 chars of stderr for audit logging (full text would bloat the table)."""
        return self.stderr[-500:] if self.stderr else ""


def _env_file_usable() -> bool:
    """Return True if config.ENV_FILE is a regular, readable file.

    `os.path.exists()` is too permissive: when the operator's `./.env`
    doesn't exist on the host at orchestrator startup, Docker materialises
    the bind-mount target as an empty DIRECTORY at /etc/orchestack/.env
    rather than failing. `os.path.exists()` returns True for that
    directory, `os.path.isfile()` doesn't — and docker compose rejects
    `--env-file <dir>` with "couldn't read env file", landing the
    operator in the broken state seen during M3 testing where stop
    fails with returncode 1.
    """
    try:
        return os.path.isfile(config.ENV_FILE) and os.access(config.ENV_FILE, os.R_OK)
    except OSError:
        return False


def _service_compose_args(service: str, *, need_env: bool = True) -> list[str]:
    """Build the `docker compose` argument prefix for a managed service.

    `--env-file` is passed for subcommands that interpolate ${VARS} from
    .env at parse time (up, run, exec, config) — most importantly the
    `up -d` that starts a service like metabase which depends on
    ${ORCHESTACK_DB_PASSWORD} and ${PIPELINE_DB_*}. The `.env` file
    itself is bind-mounted into the orchestrator at config.ENV_FILE.

    Pass `need_env=False` for subcommands that don't interpolate (stop,
    rm, ps, logs, kill). Compose still validates --env-file at every
    invocation when the flag is set, so passing it on stop would fail
    when the .env mount is misshapen — even though stop doesn't actually
    use any variables. Omitting it on those subcommands is both correct
    and resilient to a broken .env mount.
    """
    compose_file = os.path.join(config.SERVICES_DIR, f"{service}.yml")
    project_name = f"{config.COMPOSE_PROJECT_PREFIX}-{service}"
    args = [
        "docker", "compose",
        "--file", compose_file,
        "--project-name", project_name,
    ]
    if need_env:
        if _env_file_usable():
            args += ["--env-file", config.ENV_FILE]
        else:
            log.warning(
                "env-file at %s is not a readable file — compose "
                "interpolation will fall back to the process environment. "
                "Common cause: the operator's ./.env was missing when the "
                "orchestrator started, so the bind-mount materialised as "
                "an empty directory. Restore the file and restart the "
                "orchestack-orchestrator container.",
                config.ENV_FILE,
            )
    return args


def _run_sync(args: list[str], timeout: int = 180) -> CommandResult:
    """Run a subprocess synchronously. Wrapped in to_thread() by callers."""
    log.debug("subprocess: %s", " ".join(args))
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(
            ok=(proc.returncode == 0),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as e:
        return CommandResult(
            ok=False,
            returncode=-1,
            stdout=e.stdout or "",
            stderr=f"timeout after {timeout}s: {e}",
        )
    except FileNotFoundError as e:
        # `docker` CLI not installed inside the container — should never
        # happen in production but useful diagnostic for dev / wrong image.
        return CommandResult(
            ok=False,
            returncode=-1,
            stdout="",
            stderr=f"command not found: {e}",
        )


async def ping() -> bool:
    """Returns True if `docker info` succeeds. Used by /api/health."""
    res = await asyncio.to_thread(_run_sync, ["docker", "info"], 10)
    return res.ok


# ===========================================================================
#  Pre-start hooks
#
# Some managed services depend on a sidecar Postgres database that must
# already exist before the container boots. Metabase v0.50+ is the example
# that surfaced in M3 testing — it stopped auto-creating its own DB and
# now hard-errors with FATAL: database "metabase" does not exist, falling
# into a restart loop. Each hook is best-effort + idempotent: CREATE
# DATABASE IF NOT EXISTS pattern via pg_database lookup, run once per
# start_service call. Add new managed services to PRE_START_HOOKS as
# M4 brings them online (airflow, airbyte_internal, openmetadata).
#
# Why hook in the orchestrator vs adding a postgres-init/*.sql file:
# postgres-init runs ONLY on first volume initialisation. Operators with
# an existing platform volume (every M3 tester) would need to manually
# drop the volume — which is a destructive ask. The orchestrator hook
# fixes both fresh installs AND existing volumes on the next start
# attempt, no operator action required.
# ===========================================================================
async def _ensure_metabase_database() -> None:
    """Create the `metabase` database in platform postgres if it doesn't exist.

    Metabase connects with the orchestack platform user (per the env vars
    in services/metabase.yml: MB_DB_USER / MB_DB_PASS = ORCHESTACK_DB_*).
    The DB is owned by that user; this matches what Metabase expects on
    its own first-run migration.
    """
    from . import db  # local import — avoids circular import at module load
    pool = db.get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'metabase'",
        )
        if exists:
            return
        # CREATE DATABASE can't run inside a transaction. asyncpg auto-wraps
        # execute() in one — but pg accepts the statement at top level
        # outside the implicit txn for this specific command. The owner
        # matches the user Metabase connects as.
        log.info("pre-start hook: creating 'metabase' database")
        await conn.execute('CREATE DATABASE "metabase"')


# Where the orchestrator drops pgadmin's pre-loaded servers.json. The
# matching bind-mount on the pgadmin side (services/pgadmin.yml) maps
# the host's ./config/pgadmin directory to /etc/orchestack/conf in the
# pgadmin container, and PGADMIN_SERVER_JSON_FILE points at the file
# pgadmin should import on first boot.
_PGADMIN_SERVERS_JSON = "/etc/orchestack/config/pgadmin/servers.json"
_PGADMIN_PGPASS_FILE  = "/etc/orchestack/config/pgadmin/.pgpass"
# pgAdmin's container UID. The image's Dockerfile creates a pgadmin user
# at UID 5050; the .pgpass file must be readable by that user. Without
# this chown the file would be root-owned (orchestrator runs as root)
# and pgAdmin's libpq client would refuse to read it.
_PGADMIN_CONTAINER_UID = 5050
_PGADMIN_CONTAINER_GID = 5050
# pgAdmin's view of where the shared config volume is mounted (the
# matching `volumes:` directive in services/pgadmin.yml mounts
# `orchestack_config:/etc/orchestack/conf:ro`).
_PGADMIN_INNER_CONF_DIR = "/etc/orchestack/conf"


def _read_env_file_or_empty() -> dict[str, str]:
    """Read .env into a dict. Returns {} on any error.

    Trivial parser — KEY=value, ignores comments and blank lines. We don't
    handle quoting or escaping because docker compose itself doesn't, and
    the orchestrator's _persist_credentials_to_env() writes raw KEY=value
    lines too. The parser only sees keys we wrote.
    """
    out: dict[str, str] = {}
    try:
        with open(config.ENV_FILE) as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


async def _ensure_pgadmin_servers_json() -> None:
    """Materialise pgAdmin's servers.json so the operator opens the tool
    and sees the OrcheStack platform DB and pipeline warehouse already
    listed in the navigator — no manual "Add Server" required.

    pgAdmin's auto-import deliberately does NOT carry passwords for
    security reasons; the operator types the password once at first
    connect (pgAdmin offers to remember it for the session). Pre-listing
    the SERVERS — name, host, port, user, default DB — is what removes
    the cognitive overhead of figuring out the right hostname / user
    from a stack the operator didn't build.

    Best-effort. If the .env can't be read or the file can't be written
    (mount missing, filesystem RO, etc.), pgAdmin starts with an empty
    server list and the operator adds entries manually — the path the
    operator was on before this hook existed. Hook failure never blocks
    pgAdmin starting.
    """
    env = _read_env_file_or_empty()
    servers: dict[str, object] = {"Servers": {}}
    by_idx = servers["Servers"]
    assert isinstance(by_idx, dict)

    # Materialise a pgpass file alongside servers.json so pgAdmin
    # auto-connects without prompting the operator for the warehouse
    # password every session. pgpass format: host:port:dbname:user:pw,
    # one entry per line. pgAdmin's libpq reads the path from the
    # PassFile attribute on each server entry below.
    pipeline_db   = env.get("PIPELINE_DB_NAME")
    pipeline_user = env.get("PIPELINE_DB_USER")
    pipeline_pass = env.get("PIPELINE_DB_PASSWORD")
    pgpass_inner_path: str | None = None
    if pipeline_db and pipeline_user and pipeline_pass:
        try:
            os.makedirs(os.path.dirname(_PGADMIN_PGPASS_FILE), exist_ok=True)
            # Strip embedded colons from the password — pgpass uses ':' as
            # the field separator. The wizard's password rules already
            # forbid ':' but defend in depth.
            safe_pw = pipeline_pass.replace(":", r"\:")
            # Use `*` for the dbname field — postgres lists every database
            # on the server in the navigator regardless of which one we
            # marked as MaintenanceDB, and pgAdmin/libpq does a per-database
            # auth lookup when the operator clicks a different DB. Without
            # a wildcard, clicking `metabase` or `orchestack` triggered
            # "fe_sendauth: no password supplied" because no pgpass row
            # matched. The DBRestriction setting below ALSO filters the
            # listing so the operator only sees their own DB — both
            # together: the wildcard handles "tried to connect anyway,"
            # the restriction handles "shouldn't even be visible."
            line = f"orchestack-postgres:5432:*:{pipeline_user}:{safe_pw}\n"
            with open(_PGADMIN_PGPASS_FILE, "w") as f:
                f.write(line)
            # pgAdmin enforces mode 0600 and refuses to read pgpass files
            # with looser permissions. Owner needs to match the pgadmin
            # user inside the container — without this chown the file is
            # root-owned and pgAdmin gets EACCES.
            os.chmod(_PGADMIN_PGPASS_FILE, 0o600)
            try:
                os.chown(_PGADMIN_PGPASS_FILE,
                          _PGADMIN_CONTAINER_UID,
                          _PGADMIN_CONTAINER_GID)
            except (OSError, PermissionError) as ce:
                # Best-effort. On rootless docker the orchestrator may
                # not have the privilege to chown; operator will see a
                # password prompt instead, which is the pre-pgpass UX.
                log.warning("pgadmin pgpass chown skipped: %s", ce)
            pgpass_inner_path = f"{_PGADMIN_INNER_CONF_DIR}/pgadmin/.pgpass"
            log.info("pre-start hook: wrote pgadmin pgpass for %s@%s",
                      pipeline_user, pipeline_db)
        except OSError as e:
            log.warning("pgadmin pgpass write failed: %s — operator will "
                          "be prompted for password on first connect", e)

    # Pipeline warehouse — the only server we pre-load by default.
    #
    # We deliberately do NOT pre-load the OrcheStack platform DB
    # (platform.users, platform.audit_log, etc). Day-2 admin access to
    # that DB is intentionally friction-laden: surfacing it in every
    # operator's pgAdmin navigator implies "this is for you to query,"
    # which it isn't — the platform schema is OrcheStack's own private
    # state, mutated through the dashboard's typed APIs, not by hand-
    # written UPDATEs. Admins who need direct access can add the
    # connection manually.
    #
    # The connection name is the actual database name (e.g.
    # "data_warehouse") rather than a friendly label like "Pipeline
    # warehouse" so operators don't conflate the pgAdmin connection
    # display name with a real PostgreSQL database name — surfacing the
    # actual name removes that ambiguity at the cost of a slightly less
    # operator-friendly label.
    if pipeline_db and pipeline_user:
        entry = {
            "Name":          pipeline_db,
            "Group":         "OrcheStack",
            "Host":          "orchestack-postgres",
            "Port":          5432,
            "MaintenanceDB": pipeline_db,
            "Username":      pipeline_user,
            "SSLMode":       "prefer",
            # DBRestriction filters the Object Explorer to only show this
            # database — without it, pgAdmin lists every database on the
            # postgres server (the platform's internal `orchestack`, the
            # `metabase` state DB, the bare `postgres` template DB) and
            # the operator clicks them by accident, gets "no password
            # supplied" or "permission denied" depending on what they
            # tried, and concludes "the system is broken." Limiting the
            # listing keeps the operator focused on what they actually
            # have rights to.
            "DBRestriction": pipeline_db,
            "Comment":       "Your pipeline warehouse. dbt writes here; "
                              "Metabase reads here. Click to connect — "
                              "password auto-filled from your .env via pgpass.",
        }
        if pgpass_inner_path is not None:
            # PassFile path is from inside the pgAdmin container — the
            # shared volume mounts at /etc/orchestack/conf there.
            entry["PassFile"] = pgpass_inner_path
        by_idx["1"] = entry

    target = _PGADMIN_SERVERS_JSON
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        import json as _json
        with open(target, "w") as f:
            _json.dump(servers, f, indent=2)
        log.info("pre-start hook: wrote pgadmin servers.json with %d server(s)",
                  len(by_idx))
    except OSError as e:
        log.warning(
            "pre-start hook: could not write %s: %s — pgadmin will start "
            "without pre-listed servers. Add the bind-mount of ./config to "
            "the orchestrator container (see system/docker/docker-compose.yml).",
            target, e,
        )


PRE_START_HOOKS = {
    "metabase": _ensure_metabase_database,
    "pgadmin":  _ensure_pgadmin_servers_json,
}


# ===========================================================================
#  Post-start hooks
#
# Pre-start hooks handle prerequisites for the CONTAINER (sidecar DBs,
# pre-loaded config files). Post-start hooks handle prerequisites for the
# APPLICATION inside the container — first-run setup, admin user
# creation, default workspace provisioning. Things that can only happen
# after the app is actually listening.
#
# Why this is its own framework rather than a continuation of
# start_service: post-start hooks need to poll for the app to be ready
# (Metabase's Liquibase migration can take 90+ seconds), and the user
# who clicked Open shouldn't be blocked while that runs. Each post-start
# hook runs in the background via asyncio.create_task; the start_service
# call returns as soon as compose is done so the dashboard's session
# heartbeat can begin immediately.
#
# Hook contract:
#   async def hook(): ...
#     - MUST be idempotent — if the app is already configured, return
#       without raising. Operators clicking Open multiple times must not
#       re-bootstrap.
#     - MUST log success / failure via the audit log so the operator can
#       see what happened on the dashboard's audit page.
#     - MUST NOT exceed POST_START_HOOK_TIMEOUT (5 minutes) — orphaned
#       hooks accumulate as zombie tasks otherwise.
# ===========================================================================
POST_START_HOOK_TIMEOUT = 420  # 7 minutes; matches the slowest bootstrap (Metabase).


async def _bootstrap_metabase() -> None:
    """Complete Metabase's first-run setup via /api/setup.

    Metabase exposes a one-time setup-token at /api/session/properties
    while it's unconfigured. We poll for that, then POST /api/setup with
    the credentials the operator entered in the wizard PLUS the pipeline
    warehouse details so Metabase opens with the warehouse already
    connected.

    If Metabase is already configured, /api/session/properties returns
    setup-token=null. We exit without acting — the hook is idempotent
    across repeated Open clicks.
    """
    import httpx  # local import: only loaded when this hook runs
    from . import audit, db as _db  # local imports avoid cycle at load

    env = _read_env_file_or_empty()
    admin_email    = env.get("METABASE_ADMIN_EMAIL", "").strip()
    admin_password = env.get("METABASE_ADMIN_PASSWORD", "").strip()
    if not (admin_email and admin_password):
        # CRITICAL: this is the silent-skip path that has been making
        # the dashboard's "bootstrapping…" toast loop forever. If the
        # operator's .env bind-mount went bad (became a directory rather
        # than a file — same root cause as the credentials 500 we fixed
        # earlier), _read_env_file_or_empty() returns {}, this skip
        # fires, and the dashboard /ready endpoint never sees the
        # setup-token disappear. Surface it in the audit log so the
        # operator can see the cause on /app/audit instead of staring
        # at the toast.
        from . import audit
        log.warning(
            "metabase bootstrap: missing METABASE_ADMIN_EMAIL or "
            "METABASE_ADMIN_PASSWORD in .env — skipping. Common cause: "
            ".env bind-mount unreadable inside the orchestrator container."
        )
        await audit.write(
            "metabase_bootstrap_skipped",
            service_name="metabase",
            user_id=None,
            details={
                "reason": "missing METABASE_ADMIN_EMAIL/PASSWORD in .env",
                "env_file": config.ENV_FILE,
                "env_keys_seen": len(env),
            },
        )
        return

    pipeline_name = env.get("PIPELINE_DB_NAME")
    pipeline_user = env.get("PIPELINE_DB_USER")
    pipeline_pass = env.get("PIPELINE_DB_PASSWORD")

    site_name = "OrcheStack"
    try:
        async with _db.get_pool().acquire() as conn:
            company = await conn.fetchval(
                "SELECT company_name FROM platform.users "
                "WHERE username != 'system' "
                "ORDER BY created_at ASC LIMIT 1",
            )
            if company:
                site_name = company
    except Exception:
        pass  # default to "OrcheStack"

    base = "http://orchestack-metabase:3000"
    setup_token: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Poll up to ~6 minutes for Metabase's setup page to come online.
        # Metabase's first-boot Liquibase migration runs ~420 changesets
        # against an empty postgres; on Docker Desktop's macOS file-system
        # layer that consistently takes 4-5 minutes (single-digit seconds
        # on bare-metal Linux). Three minutes wasn't enough — the hook
        # would time out before /api/setup became available, leaving
        # Metabase showing its built-in setup wizard instead of being
        # auto-configured.
        #
        # Match the POST_START_HOOK_TIMEOUT (300s) too — this 360s loop
        # would otherwise be cut off by the outer asyncio.wait_for.
        for attempt in range(180):
            try:
                r = await client.get(f"{base}/api/session/properties")
                if r.status_code == 200:
                    setup_token = r.json().get("setup-token")
                    if setup_token is not None:
                        log.info(
                            "metabase bootstrap: setup-token observed after "
                            "~%ds — proceeding to POST /api/setup",
                            attempt * 2,
                        )
                        break
                    else:
                        # 200 with NO setup-token means Metabase is already
                        # configured (probably from a previous bootstrap
                        # run). Nothing to do.
                        log.info(
                            "metabase bootstrap: setup-token already null at "
                            "~%ds — Metabase reports as configured already",
                            attempt * 2,
                        )
                        break
            except httpx.HTTPError:
                pass
            # Heartbeat every minute so docker-logs observers can see the
            # hook is making progress (not stuck) without spamming logs.
            if attempt > 0 and attempt % 30 == 0:
                log.info(
                    "metabase bootstrap: still waiting for setup-token (%ds elapsed)",
                    attempt * 2,
                )
            await asyncio.sleep(2)

        if setup_token is None:
            log.info("metabase bootstrap: no setup-token observed (already configured or timeout)")
            return

        # Step 1: complete first-run setup with the MINIMUM REQUIRED
        # payload. Metabase 0.51 has a strict schema for /api/setup —
        # extra fields can produce a 400 with a generic "Unable to set
        # up Metabase" message that gives no clue what's wrong. The
        # previous payload included `user.site_name` (which the schema
        # rejects) and an inline `database` block (engine-specific keys
        # tripped schema validation in some Metabase patch versions).
        # Cleaner separation: (a) setup with `database: null`,
        # (b) authenticate, (c) POST /api/database to register the
        # warehouse. Each step is independently observable in the audit
        # log so a partial failure is debuggable.
        setup_payload = {
            "token": setup_token,
            "user": {
                "first_name": "Admin",
                "last_name":  "User",
                "email":      admin_email,
                "password":   admin_password,
            },
            "prefs": {
                "site_name":      site_name,
                "allow_tracking": False,
            },
            # Explicit null — tells Metabase "no warehouse to add right
            # now" so it doesn't sit waiting for a database step.
            "database": None,
        }

        try:
            r = await client.post(f"{base}/api/setup", json=setup_payload, timeout=60.0)
        except httpx.HTTPError as e:
            log.warning("metabase bootstrap: POST /api/setup raised: %s", e)
            await audit.write(
                "metabase_bootstrap_failed",
                service_name="metabase",
                user_id=None,
                details={"phase": "setup", "error": str(e)},
            )
            return

        if r.status_code not in (200, 201, 204):
            # Surface the FULL response body in the audit log — that's
            # how an operator finds out their password failed a Metabase
            # rule (length/complexity) without needing docker logs.
            log.warning(
                "metabase bootstrap: /api/setup returned %d: %s",
                r.status_code, r.text[:500],
            )
            await audit.write(
                "metabase_bootstrap_failed",
                service_name="metabase",
                user_id=None,
                details={
                    "phase":  "setup",
                    "status": r.status_code,
                    "body":   r.text[:500],
                },
            )
            return

        log.info("metabase bootstrap: /api/setup succeeded (status %d)", r.status_code)
        await audit.write(
            "metabase_bootstrapped",
            service_name="metabase",
            user_id=None,
            details={"site_name": site_name, "setup_status_code": r.status_code},
        )

        # Step 2: warehouse registration. Best-effort — failure here
        # doesn't undo step 1's progress; operator can add the warehouse
        # by hand from Metabase Admin → Databases.
        if not (pipeline_name and pipeline_user and pipeline_pass):
            log.info(
                "metabase bootstrap: no PIPELINE_DB_* in .env; skipping "
                "warehouse register"
            )
            return

        # Sign in to get a session cookie for /api/database. POST /api/setup
        # returns the admin user id but no session token in some Metabase
        # versions, so re-authenticating is the portable path.
        try:
            session_resp = await client.post(
                f"{base}/api/session",
                json={"username": admin_email, "password": admin_password},
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            log.warning("metabase bootstrap: /api/session login raised: %s", e)
            return

        if session_resp.status_code != 200:
            log.warning(
                "metabase bootstrap: /api/session login failed (%d): %s",
                session_resp.status_code, session_resp.text[:300],
            )
            await audit.write(
                "metabase_bootstrap_failed",
                service_name="metabase",
                user_id=None,
                details={
                    "phase":  "warehouse_login",
                    "status": session_resp.status_code,
                    "body":   session_resp.text[:300],
                },
            )
            return

        session_id = session_resp.json().get("id")

        db_payload = {
            "engine": "postgres",
            "name":   "Pipeline warehouse",
            "details": {
                "host":     "orchestack-postgres",
                "port":     5432,
                "dbname":   pipeline_name,
                "user":     pipeline_user,
                "password": pipeline_pass,
                "ssl":      False,
            },
            "is_on_demand":     False,
            "is_full_sync":     True,
            "is_sample":        False,
            "auto_run_queries": True,
        }
        try:
            db_resp = await client.post(
                f"{base}/api/database",
                json=db_payload,
                headers={"X-Metabase-Session": session_id} if session_id else {},
                timeout=30.0,
            )
        except httpx.HTTPError as e:
            log.warning("metabase bootstrap: warehouse register raised: %s", e)
            await audit.write(
                "metabase_bootstrap_failed",
                service_name="metabase",
                user_id=None,
                details={"phase": "warehouse_register", "error": str(e)},
            )
            return

        if db_resp.status_code in (200, 201, 204):
            log.info(
                "metabase bootstrap: warehouse registered (status %d)",
                db_resp.status_code,
            )
            await audit.write(
                "metabase_warehouse_registered",
                service_name="metabase",
                user_id=None,
                details={"name": "Pipeline warehouse", "dbname": pipeline_name},
            )
        else:
            log.warning(
                "metabase bootstrap: warehouse register returned %d: %s",
                db_resp.status_code, db_resp.text[:500],
            )
            await audit.write(
                "metabase_bootstrap_failed",
                service_name="metabase",
                user_id=None,
                details={
                    "phase":  "warehouse_register",
                    "status": db_resp.status_code,
                    "body":   db_resp.text[:500],
                },
            )


POST_START_HOOKS = {
    "metabase": _bootstrap_metabase,
}


def _schedule_post_start_hook(service: str) -> None:
    """Fire-and-forget post-start hook. Bounded by POST_START_HOOK_TIMEOUT
    so a stuck hook can't accumulate as a zombie task forever."""
    hook = POST_START_HOOKS.get(service)
    if hook is None:
        return

    async def _run() -> None:
        try:
            await asyncio.wait_for(hook(), timeout=POST_START_HOOK_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning(
                "post-start hook for %s timed out after %ds",
                service, POST_START_HOOK_TIMEOUT,
            )
        except Exception as e:
            log.warning("post-start hook for %s raised: %s", service, e)

    # Name the task so it surfaces in `asyncio` debugging output.
    asyncio.create_task(_run(), name=f"post-start:{service}")


async def start_service(service: str) -> CommandResult:
    """Bring a cold-tier service up. Idempotent — `up -d` no-ops if already running.

    Timeout is 5 minutes, not the usual 3, because the first start of a
    fresh service pulls its image which can be 100-500 MB. On a
    Nigerian-affordable VPS with a ~10 Mbps link, a 200 MB image takes
    160 seconds just to pull. Subsequent starts are sub-second once the
    image is cached, so the bigger timeout is paid only on cold cache.

    Pre-start hook: for services that need a sidecar database to exist
    BEFORE the container boots (Metabase, Airflow when M4 lands, etc),
    the registered hook in PRE_START_HOOKS runs first. Hook failures are
    logged but don't block the start — the container's own startup will
    surface a clearer error message than the hook could.

    Self-heal on name conflict: when a previous bundle install left a
    container with the same name behind (typically because the operator
    re-extracted the bundle into a new directory), `docker compose up -d`
    fails with "container name '/orchestack-X' is already in use by
    container 'abc...'". Detect that specific error, `docker rm -f` the
    orphan, retry. Anything else returns the original error.
    """
    hook = PRE_START_HOOKS.get(service)
    if hook is not None:
        try:
            await hook()
        except Exception as e:
            # Best-effort. If the hook fails (e.g., DB pool not ready),
            # the container's own start will report a clearer error.
            log.warning("pre-start hook for %s failed: %s", service, e)

    up_args = _service_compose_args(service) + ["up", "-d", "--remove-orphans"]
    res = await asyncio.to_thread(_run_sync, up_args, 300)

    if res.ok:
        # Post-start hook runs in background — doesn't block the caller.
        _schedule_post_start_hook(service)
        return res

    if "is already in use by container" not in res.stderr:
        return res

    # Orphan-container conflict — clean up + retry once.
    container_name = f"orchestack-{service}"
    log.warning(
        "name conflict starting %s — removing orphan container %s and retrying",
        service, container_name,
    )
    rm_res = await asyncio.to_thread(
        _run_sync, ["docker", "rm", "-f", container_name], 30,
    )
    if not rm_res.ok:
        log.warning(
            "could not remove orphan %s: %s — returning original up failure",
            container_name, rm_res.short_stderr,
        )
        return res

    # Retry the up. Same timeout as the first attempt.
    retry = await asyncio.to_thread(_run_sync, up_args, 300)
    if retry.ok:
        _schedule_post_start_hook(service)
    return retry


async def stop_service(service: str) -> CommandResult:
    """Stop a cold-tier service. Keeps volumes + networks; only stops the container.

    We use `stop`, not `down`, so subsequent `start_service` is a fast
    container-start (~1-2s) instead of a full recreate (~10s).

    Pass `need_env=False`: stop doesn't interpolate any env vars at
    parse time, so requiring a usable .env file just to stop a service
    is over-strict. Operators who lost their .env between starts can
    still stop their containers cleanly.
    """
    return await asyncio.to_thread(
        _run_sync,
        _service_compose_args(service, need_env=False) + ["stop"],
        60,
    )


async def list_running_services() -> list[dict[str, str]]:
    """List every managed service container that's currently running.

    Returns a list of dicts:
        [{"service": "metabase", "container": "orchestack-metabase",
          "started_at": "2026-06-02T..."}]

    The filter `label=orchestack.service` is what scopes us to managed
    services — base control-plane containers (proxy, postgres, auth, etc.)
    don't carry this label so they're invisible to the reconciler.
    """
    res = await asyncio.to_thread(
        _run_sync,
        ["docker", "ps",
         "--filter", "label=orchestack.service",
         "--format", "{{.Label \"orchestack.service\"}}\t{{.Names}}\t{{.CreatedAt}}"],
        10,
    )
    if not res.ok:
        log.warning("list_running_services failed: %s", res.short_stderr)
        return []
    out: list[dict[str, str]] = []
    for line in res.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3:
            out.append({"service": parts[0], "container": parts[1], "started_at": parts[2]})
    return out


async def container_uptime_seconds(service: str) -> int | None:
    """How long has this service been running, in seconds? None if not running.

    Used by the reconciler's start-grace check — we don't want to stop a
    service that was just started 5 seconds ago because a session POST
    hasn't landed yet.
    """
    res = await asyncio.to_thread(
        _run_sync,
        ["docker", "ps",
         "--filter", f"label=orchestack.service={service}",
         "--format", "{{.RunningFor}}"],
        10,
    )
    # Docker prints something like "About a minute ago" or "2 hours ago" —
    # not directly parseable. Easier: ask for StartedAt as ISO timestamp.
    res2 = await asyncio.to_thread(
        _run_sync,
        ["docker", "inspect",
         "--format", "{{.State.StartedAt}}",
         f"orchestack-{service}"],
        10,
    )
    if not res2.ok or not res2.stdout.strip():
        return None
    # State.StartedAt format: 2026-06-02T03:14:09.123456789Z
    import datetime as _dt
    try:
        started = _dt.datetime.fromisoformat(res2.stdout.strip().replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        return int((now - started).total_seconds())
    except ValueError:
        return None
