"""Docker operations — subprocess wrappers around `docker compose` and `docker`."""

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
    """Return value of every docker_ops call."""
    ok: bool
    returncode: int
    stdout: str
    stderr: str

    @property
    def short_stderr(self) -> str:
        """Last 500 chars of stderr for audit logging (full text would bloat the table)."""
        return self.stderr[-500:] if self.stderr else ""


def _env_file_usable() -> bool:
    """Return True if config.ENV_FILE is actually openable + readable.

    Must detect stale bind-mount inodes: when the host's .env is edited via
    atomic rename, isfile() and os.access(R_OK) still return True (cached
    directory entry) but read attempts fail with ENOENT. Detected by
    st_nlink == 0 and confirmed by an actual open()+read(). Recovery is
    operator-side: restart the orchestrator container to re-resolve the
    bind mount. Returning False makes stop_service fall back to direct
    `docker stop <container>` so the dashboard's Stop button still works.
    """
    try:
        if not os.path.isfile(config.ENV_FILE):
            return False
        # st_nlink == 0 = directory entry exists but the inode was
        # deleted on the host. Cheap check before the open() attempt.
        st = os.stat(config.ENV_FILE)
        if st.st_nlink == 0:
            log.warning(
                "env-file at %s has nlink=0 — host .env was edited via "
                "atomic rename and the bind-mount is now stale. Restart "
                "the orchestack-orchestrator container to recover.",
                config.ENV_FILE,
            )
            return False
        # Actually open + read a byte — catches any other case where
        # the directory entry exists but the file can't actually be
        # read (permission changes on host, broken symlink chains, etc).
        with open(config.ENV_FILE, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def _service_compose_args(service: str, *, need_env: bool = True) -> list[str]:
    """Build the `docker compose` argument prefix for a managed service.

    `--env-file` is passed for subcommands that interpolate ${VARS} from
    .env at parse time (up, run, exec, config) — most importantly the
    `up -d` that starts a service like metabase which depends on
    ${ORCHESTACK_DB_PASSWORD} and ${WAREHOUSE_DB_*}. The `.env` file
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
    """Run a subprocess synchronously; wrapped in to_thread() by callers."""
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
# Each managed service that needs a sidecar Postgres DB to exist BEFORE
# the container boots registers a hook here. Hook in the orchestrator
# rather than postgres-init/*.sql because postgres-init runs ONLY on first
# volume initialisation; the orchestrator hook fixes both fresh installs
# AND existing volumes on the next start attempt with no operator action.
# ===========================================================================
async def _ensure_metabase_database() -> None:
    """Provision Metabase's PG role + dedicated `metabase_db` database.

    Metabase gets its own DB (not a schema in `services`) because Metabase
    0.51's first-boot Liquibase changeset `v00.00-000` HARDCODES
    `public.<tablename>` in every CREATE TABLE; MB_DB_SCHEMA is ignored
    during initialization. Per-DB isolation is therefore the only working
    arrangement — its hardcoded `public.<table>` writes land in a
    Metabase-owned database we control.

    Idempotent: checks role + DB independently, creates only what's
    missing, updates the password unconditionally so .env-driven rotation
    works on next start.
    """
    from . import db  # local import — avoids circular import at module load
    env = _read_env_file_or_empty()
    password = env.get("METABASE_DB_PASSWORD", "").strip()
    if not password:
        platform_pw = env.get("ORCHESTACK_DB_PASSWORD", "")
        if not platform_pw:
            log.warning(
                "metabase pre-start: no METABASE_DB_PASSWORD and no "
                "ORCHESTACK_DB_PASSWORD to derive from; skipping role/DB "
                "setup. Run the wizard or set METABASE_DB_PASSWORD in .env."
            )
            return
        # Deterministic seed — same input always produces the same hash.
        # This is intentional so subsequent restarts use the same password
        # without rewriting .env.
        import hashlib
        password = hashlib.sha256(
            f"metabase:{platform_pw}".encode("utf-8")
        ).hexdigest()[:32]
        # Persist back to .env so the metabase container sees it on next
        # `docker compose up` (the compose file reads MB_DB_PASS via
        # ${METABASE_DB_PASSWORD}). Without this the role exists with the
        # derived password but the container has MB_DB_PASS="" and login
        # fails. Best-effort — failures here are noisy in logs but don't
        # break this hook's main job.
        try:
            _append_or_update_env_key("METABASE_DB_PASSWORD", password)
            log.info(
                "metabase pre-start: derived METABASE_DB_PASSWORD and "
                "wrote it to .env for future container starts"
            )
        except Exception as e:
            log.warning(
                "metabase pre-start: couldn't persist derived password to "
                ".env (%s); metabase container will fail to connect until "
                "METABASE_DB_PASSWORD is set manually",
                e,
            )

    pool = db.get_pool()
    quoted_pw = password.replace("'", "''")

    async with pool.acquire() as conn:
        # Backward-compat: rename legacy "metabase" → "metabase_admin" then
        # ensure the canonical name exists with the chosen password.
        legacy_role = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'metabase'"
        )
        new_role = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'metabase_admin'"
        )
        if legacy_role and not new_role:
            log.info("pre-start hook (metabase): renaming legacy 'metabase' role → 'metabase_admin'")
            await conn.execute('ALTER ROLE "metabase" RENAME TO "metabase_admin"')
            new_role = True
        if not new_role:
            log.info("pre-start hook (metabase): creating 'metabase_admin' role")
            await conn.execute(
                f"CREATE ROLE metabase_admin WITH LOGIN PASSWORD '{quoted_pw}'"
            )
        else:
            await conn.execute(
                f"ALTER ROLE metabase_admin WITH LOGIN PASSWORD '{quoted_pw}'"
            )

        # Backward-compat: rename legacy "metabase" DB → "metabase_db",
        # then ensure metabase_db exists owned by metabase_admin.
        legacy_db = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'metabase'"
        )
        new_db = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'metabase_db'"
        )
        if legacy_db and not new_db:
            log.info("pre-start hook (metabase): renaming legacy 'metabase' DB → 'metabase_db'")
            await conn.execute('ALTER DATABASE "metabase" RENAME TO "metabase_db"')
            new_db = True
        if not new_db:
            log.info("pre-start hook (metabase): creating 'metabase_db' owned by metabase_admin")
            await conn.execute('CREATE DATABASE "metabase_db" OWNER metabase_admin')
        else:
            # Ensure ownership is correct after the rename or for
            # pre-existing DBs from very old installs.
            owner = await conn.fetchval(
                "SELECT pg_get_userbyid(datdba) "
                "FROM pg_database WHERE datname = 'metabase_db'"
            )
            if owner != "metabase_admin":
                log.info(
                    "pre-start hook (metabase): transferring metabase_db "
                    "ownership from %s to metabase_admin", owner,
                )
                await conn.execute(
                    "ALTER DATABASE metabase_db OWNER TO metabase_admin"
                )

    # If the database already had objects owned by the platform admin,
    # REASSIGN them to metabase_admin. REASSIGN OWNED only acts on the
    # connected database, so we open a fresh connection to metabase_db.
    platform_user = env.get("ORCHESTACK_DB_USER", "orchestack_admin")
    if platform_user != "metabase_admin":
        try:
            import asyncpg
            from . import config as _cfg
            mb_conn = await asyncpg.connect(
                host=_cfg.DB_HOST, port=_cfg.DB_PORT,
                user=_cfg.DB_USER, password=_cfg.DB_PASSWORD,
                database="metabase_db",
            )
            try:
                await mb_conn.execute(
                    f'REASSIGN OWNED BY "{platform_user}" TO metabase_admin'
                )
                await mb_conn.execute(
                    "GRANT ALL ON SCHEMA public TO metabase_admin"
                )
            finally:
                await mb_conn.close()
        except Exception as e:
            # Database doesn't exist yet on fresh installs, or no objects
            # owned by the platform user. Both are fine — best-effort.
            log.info(
                "pre-start hook (metabase): REASSIGN OWNED skipped (%s) — "
                "expected on fresh installs",
                type(e).__name__,
            )


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


def _append_or_update_env_key(key: str, value: str) -> None:
    """Write `KEY=value` into the operator's .env, replacing in place or appending."""
    path = config.ENV_FILE
    if not (os.path.isfile(path) and os.access(path, os.W_OK)):
        raise OSError(f".env at {path} is not a writable file")
    lines = open(path).read().splitlines(True)  # keepends
    prefix = f"{key}="
    new_line = f"{key}={value}\n"
    found = False
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            lines[i] = new_line
            found = True
            break
    if not found:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(new_line)
    with open(path, "w") as f:
        f.writelines(lines)


def _read_env_file_or_empty() -> dict[str, str]:
    """Read .env into a dict. Returns {} on any error. No quoting/escaping — docker compose doesn't either."""
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
    """Materialise pgAdmin's servers.json so the navigator pre-lists the warehouse server.

    Best-effort — failure never blocks pgAdmin starting; operator adds entries manually.
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
    warehouse_db   = env.get("WAREHOUSE_DB_NAME")
    pipeline_user = env.get("WAREHOUSE_DB_USER")
    pipeline_pass = env.get("WAREHOUSE_DB_PASSWORD")
    pgpass_inner_path: str | None = None
    if warehouse_db and pipeline_user and pipeline_pass:
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
                # password prompt instead.
                log.warning("pgadmin pgpass chown skipped: %s", ce)
            pgpass_inner_path = f"{_PGADMIN_INNER_CONF_DIR}/pgadmin/.pgpass"
            log.info("pre-start hook: wrote pgadmin pgpass for %s@%s",
                      pipeline_user, warehouse_db)
        except OSError as e:
            log.warning("pgadmin pgpass write failed: %s — operator will "
                          "be prompted for password on first connect", e)

    # Warehouse is the only server we pre-load. We deliberately do NOT
    # pre-load the OrcheStack platform DB — the platform schema is private
    # state mutated through the dashboard's typed APIs, not by hand-written
    # UPDATEs. Admins who need direct access can add the connection manually.
    if warehouse_db and pipeline_user:
        entry = {
            "Name":          warehouse_db,
            "Group":         "OrcheStack",
            "Host":          "orchestack-postgres",
            "Port":          5432,
            "MaintenanceDB": warehouse_db,
            "Username":      pipeline_user,
            "SSLMode":       "prefer",
            # NO DBRestriction — operator is the platform admin and gets
            # to see every database the postgres server hosts. The
            # wildcard pgpass entry handles auth for all of them.
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

    # pgAdmin imports servers.json ONLY on first start (when its internal
    # SQLite cache is empty). Subsequent starts ignore the JSON and read
    # from the cache. So if the operator renames a role (e.g.
    # warehouse_user → warehouse_admin via the convention pass), the
    # JSON gets updated but pgAdmin's stored server config doesn't —
    # and the operator sees a password prompt for the now-nonexistent
    # legacy role.
    #
    # Best-effort: patch the pgAdmin SQLite cache directly so the
    # stored username matches what's in servers.json. Runs only if the
    # pgadmin volume + database exist (skipped silently on fresh
    # installs where pgAdmin hasn't started yet).
    if warehouse_db and pipeline_user:
        # pgAdmin's data dir is volume-mounted; we reach it via the
        # platform's shared bind-mount. Standard location inside the
        # pgadmin container is /var/lib/pgadmin/pgadmin4.db — but the
        # orchestrator doesn't bind that volume. Instead we shell out
        # via the docker CLI to run a tiny python snippet inside the
        # pgadmin container. Same pattern as start_service uses for
        # compose; no new tooling needed.
        sync_script = (
            "import sqlite3, os; "
            "p='/var/lib/pgadmin/pgadmin4.db'; "
            "os.path.exists(p) or exit(0); "
            "con=sqlite3.connect(p); cur=con.cursor(); "
            "cur.execute(\"UPDATE server SET username=? WHERE name=?\", "
            f"  ('{pipeline_user}', '{warehouse_db}')); "
            "con.commit(); con.close()"
        )
        try:
            res = await asyncio.to_thread(
                _run_sync,
                ["docker", "exec", "orchestack-pgadmin",
                 "python3", "-c", sync_script],
                15,
            )
            if res.ok:
                log.info(
                    "pre-start hook: synced pgadmin sqlite cache "
                    "(server '%s' username=%s)", warehouse_db, pipeline_user,
                )
            else:
                log.debug(
                    "pre-start hook: pgadmin sqlite sync skipped — "
                    "container not running yet (%s)", res.short_stderr,
                )
        except Exception as e:
            log.debug("pre-start hook: pgadmin sqlite sync skipped: %s", e)


async def _ensure_simple_pg_role_and_db(
    role_name: str, db_name: str, env_key: str,
    *, schema_in_services: str | None = None,
    legacy_role_name: str | None = None,
    legacy_db_name: str | None = None,
) -> None:
    """Provision a per-service PG role + database (or a schema inside `services`).

    legacy_role_name / legacy_db_name: if set and the legacy name exists
    but the new name doesn't, ALTER ROLE/DATABASE RENAME migrates in
    place. Postgres tracks ownership/grants by OID so the rename is
    non-destructive. ALTER DATABASE RENAME requires zero active
    connections — safe only because the pre-start hook runs before
    compose up, when the dependent container is stopped.
    """
    from . import db as _db
    env = _read_env_file_or_empty()
    password = env.get(env_key, "").strip()
    if not password:
        platform_pw = env.get("ORCHESTACK_DB_PASSWORD", "")
        if not platform_pw:
            log.warning(
                "pre-start hook (%s): no %s in .env and no ORCHESTACK_DB_PASSWORD "
                "to derive from; skipping role/DB setup.",
                role_name, env_key,
            )
            return
        import hashlib
        password = hashlib.sha256(
            f"{role_name}:{platform_pw}".encode("utf-8")
        ).hexdigest()[:32]
        try:
            _append_or_update_env_key(env_key, password)
            log.info(
                "pre-start hook (%s): derived %s and wrote to .env",
                role_name, env_key,
            )
        except Exception as e:
            log.warning(
                "pre-start hook (%s): couldn't persist derived password: %s",
                role_name, e,
            )

    quoted_pw = password.replace("'", "''")
    pool = _db.get_pool()
    async with pool.acquire() as conn:
        role_exists = await conn.fetchval(
            f"SELECT 1 FROM pg_roles WHERE rolname = '{role_name}'"
        )
        # Backward-compat: rename legacy role in place rather than
        # creating a new one. Postgres tracks ownership + grants by OID
        # so the rename preserves every owned object.
        if legacy_role_name and not role_exists:
            legacy_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_roles WHERE rolname = '{legacy_role_name}'"
            )
            if legacy_exists:
                log.info(
                    "pre-start hook (%s): renaming legacy role '%s' → '%s'",
                    role_name, legacy_role_name, role_name,
                )
                await conn.execute(
                    f'ALTER ROLE "{legacy_role_name}" RENAME TO "{role_name}"'
                )
                role_exists = True
        if not role_exists:
            log.info("pre-start hook (%s): creating role", role_name)
            await conn.execute(
                f'CREATE ROLE "{role_name}" WITH LOGIN PASSWORD \'{quoted_pw}\''
            )
        else:
            await conn.execute(
                f'ALTER ROLE "{role_name}" WITH LOGIN PASSWORD \'{quoted_pw}\''
            )

        if schema_in_services:
            services_exists = await conn.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = 'services'"
            )
            if not services_exists:
                log.info("pre-start hook (%s): creating 'services' database", role_name)
                await conn.execute('CREATE DATABASE "services"')
            await conn.execute(
                f'GRANT CONNECT ON DATABASE services TO "{role_name}"'
            )

    if schema_in_services:
        import asyncpg
        services_conn = await asyncpg.connect(
            host=config.DB_HOST, port=config.DB_PORT,
            user=config.DB_USER, password=config.DB_PASSWORD,
            database="services",
        )
        try:
            await services_conn.execute(f'CREATE SCHEMA IF NOT EXISTS "{schema_in_services}" '
                                          f'AUTHORIZATION "{role_name}"')
            await services_conn.execute(
                f'GRANT USAGE, CREATE ON SCHEMA "{schema_in_services}" TO "{role_name}"'
            )
            await services_conn.execute(
                f'GRANT USAGE ON SCHEMA public TO "{role_name}"'
            )
        finally:
            await services_conn.close()
        async with pool.acquire() as conn:
            await conn.execute(
                f'ALTER ROLE "{role_name}" SET search_path TO '
                f'"{schema_in_services}", public'
            )
    else:
        async with pool.acquire() as conn:
            if legacy_db_name and legacy_db_name != db_name:
                new_exists = await conn.fetchval(
                    f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"
                )
                legacy_exists = await conn.fetchval(
                    f"SELECT 1 FROM pg_database WHERE datname = '{legacy_db_name}'"
                )
                if legacy_exists and not new_exists:
                    log.info(
                        "pre-start hook (%s): renaming legacy database '%s' → '%s'",
                        role_name, legacy_db_name, db_name,
                    )
                    await conn.execute(
                        f'ALTER DATABASE "{legacy_db_name}" RENAME TO "{db_name}"'
                    )
            db_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{db_name}'"
            )
            if not db_exists:
                log.info("pre-start hook (%s): creating '%s' database", role_name, db_name)
                await conn.execute(
                    f'CREATE DATABASE "{db_name}" OWNER "{role_name}"'
                )


async def _ensure_airflow_setup() -> None:
    """Airflow: dedicated airflow_db database, plus migration from the
    legacy `services` DB (drop without dump-and-restore — Airflow re-runs
    its Liquibase migrations; DAG files live in a separate volume).
    """
    from . import db as _db
    import asyncpg
    await _ensure_simple_pg_role_and_db(
        "airflow_admin", "airflow_db", "AIRFLOW_DB_PASSWORD",
        legacy_role_name="airflow",
    )
    # Only drop legacy `services` DB if it has an `airflow` schema —
    # that's the signal it was created by an old version of OrcheStack
    # for Airflow's metadata. Any other use of `services` is preserved.
    pool = _db.get_pool()
    async with pool.acquire() as conn:
        services_exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = 'services'"
        )
        if not services_exists:
            return
    # Connect to services to check if airflow schema is there.
    try:
        svc_conn = await asyncpg.connect(
            host=config.DB_HOST, port=config.DB_PORT,
            user=config.DB_USER, password=config.DB_PASSWORD,
            database="services",
        )
    except Exception as e:
        log.warning("airflow pre-start: couldn't connect to legacy services DB: %s", e)
        return
    try:
        airflow_schema_exists = await svc_conn.fetchval(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = 'airflow'"
        )
    finally:
        await svc_conn.close()
    if not airflow_schema_exists:
        return
    # DROP DATABASE requires no active connections — pre-start hook
    # context guarantees this for the Airflow side, and nothing else
    # in OrcheStack uses `services`.
    async with pool.acquire() as conn:
        log.info(
            "airflow pre-start: dropping legacy `services` database "
            "(airflow_db is the new home; Airflow will re-init its "
            "Liquibase migrations on next start)"
        )
        try:
            await conn.execute('DROP DATABASE IF EXISTS "services"')
        except Exception as e:
            log.warning("airflow pre-start: couldn't drop services DB: %s", e)


async def _ensure_airbyte_setup() -> None:
    """Airbyte: airbyte_admin role + airbyte_db + two extra DBs for Temporal
    (workflow history and visibility); all three DBs share the airbyte_admin role.
    """
    await _ensure_simple_pg_role_and_db(
        "airbyte_admin", "airbyte_db", "AIRBYTE_DB_PASSWORD",
        legacy_role_name="airbyte",
        legacy_db_name="airbyte",
    )
    from . import db as _db
    pool = _db.get_pool()
    # Temporal's upstream binary hardcodes the unsuffixed database
    # names "temporal" + "temporal_visibility" (via its compose snippet's
    # POSTGRES_DEFAULT_DB env defaults). We have to provision those
    # exact names — the platform-wide <service>_db naming convention
    # we use elsewhere DOES NOT APPLY HERE because Temporal isn't
    # configurable on this point. See GitHub issue #2.
    #
    # The legacy slot now holds the buggy "<name>_db" form that earlier
    # v0.1.x installs created, so the rename branch below migrates
    # stuck installs automatically on next start (no operator action).
    for new_name, legacy_name in (
        ("temporal", "temporal_db"),
        ("temporal_visibility", "temporal_visibility_db"),
    ):
        async with pool.acquire() as conn:
            new_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{new_name}'"
            )
            legacy_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{legacy_name}'"
            )
            if legacy_exists and new_exists:
                # Both exist — partial migration from an earlier install
                # left the legacy DB orphaned. Drop it so pgAdmin
                # doesn't show the red-X "can't connect" tile.
                log.info(
                    "pre-start hook (airbyte): legacy '%s' AND new '%s' both exist — dropping legacy (orphaned from previous install)",
                    legacy_name, new_name,
                )
                await conn.execute(f'DROP DATABASE IF EXISTS "{legacy_name}"')
            elif legacy_exists and not new_exists:
                log.info(
                    "pre-start hook (airbyte): renaming legacy '%s' → '%s'",
                    legacy_name, new_name,
                )
                await conn.execute(
                    f'ALTER DATABASE "{legacy_name}" RENAME TO "{new_name}"'
                )
            elif not new_exists:
                log.info("pre-start hook (airbyte): creating '%s' for Temporal", new_name)
                await conn.execute(
                    f'CREATE DATABASE "{new_name}" OWNER "airbyte_admin"'
                )


async def _ensure_openmetadata_setup() -> None:
    """OpenMetadata: own role + own DB (hardcodes public schema)."""
    await _ensure_simple_pg_role_and_db(
        "openmetadata_admin", "openmetadata_db", "OPENMETADATA_DB_PASSWORD",
        legacy_role_name="openmetadata",
        legacy_db_name="openmetadata",
    )


async def _ensure_dbt_setup() -> None:
    """dbt_admin role with CONNECT on target DB, ownership of target schema,
    ALL on existing + DEFAULT PRIVILEGES on future objects in that schema,
    and USAGE/SELECT (+ defaults) on the `raw` schema in the pipeline DB.
    """
    from . import db as _db
    env = _read_env_file_or_empty()
    warehouse_db = env.get("WAREHOUSE_DB_NAME") or "data_warehouse"
    target_db = env.get("DBT_DATABASE", "").strip() or warehouse_db
    target_schema = env.get("DBT_SCHEMA", "").strip() or "marts"

    password = env.get("DBT_DB_PASSWORD", "").strip()
    if not password:
        platform_pw = env.get("ORCHESTACK_DB_PASSWORD", "")
        if not platform_pw:
            log.warning("dbt pre-start: no DBT_DB_PASSWORD; skipping")
            return
        import hashlib
        password = hashlib.sha256(
            f"dbt:{platform_pw}".encode("utf-8")
        ).hexdigest()[:32]
        try:
            _append_or_update_env_key("DBT_DB_PASSWORD", password)
        except Exception as e:
            log.warning("dbt pre-start: couldn't persist password: %s", e)
    quoted_pw = password.replace("'", "''")

    pool = _db.get_pool()
    async with pool.acquire() as conn:
        # Backward-compat rename `dbt_user` → `dbt_admin`. ALTER ROLE
        # RENAME preserves all object ownership (stored by OID).
        old_exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'dbt_user'"
        )
        new_exists = await conn.fetchval(
            "SELECT 1 FROM pg_roles WHERE rolname = 'dbt_admin'"
        )
        if old_exists and not new_exists:
            log.info("dbt pre-start: renaming legacy 'dbt_user' role to 'dbt_admin'")
            await conn.execute("ALTER ROLE dbt_user RENAME TO dbt_admin")
            new_exists = True

        if not new_exists:
            await conn.execute(
                f"CREATE ROLE dbt_admin WITH LOGIN PASSWORD '{quoted_pw}'"
            )
        else:
            await conn.execute(
                f"ALTER ROLE dbt_admin WITH LOGIN PASSWORD '{quoted_pw}'"
            )

        if target_db != warehouse_db:
            db_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{target_db}'"
            )
            if not db_exists:
                log.info("dbt pre-start: creating target DB '%s'", target_db)
                await conn.execute(f'CREATE DATABASE "{target_db}"')

        await conn.execute(
            f'GRANT CONNECT ON DATABASE "{target_db}" TO dbt_admin'
        )
        # dbt sources may reference `raw.*` in the warehouse DB even
        # when writing marts to a different DB, so grant CONNECT there too.
        if target_db != warehouse_db:
            await conn.execute(
                f'GRANT CONNECT ON DATABASE "{warehouse_db}" TO dbt_admin'
            )

    # Connect as platform superuser so we can GRANT on objects owned by anyone.
    import asyncpg
    wh_conn = await asyncpg.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASSWORD,
        database=target_db,
    )
    try:
        await wh_conn.execute(
            f'CREATE SCHEMA IF NOT EXISTS "{target_schema}" '
            f'AUTHORIZATION dbt_admin'
        )
        # Belt-and-suspenders for the case where someone else creates a
        # table in this schema: GRANT ALL on existing + DEFAULT PRIVILEGES
        # for future objects so dbt_admin keeps full access.
        await wh_conn.execute(
            f'GRANT ALL ON SCHEMA "{target_schema}" TO dbt_admin'
        )
        await wh_conn.execute(
            f'GRANT ALL ON ALL TABLES IN SCHEMA "{target_schema}" TO dbt_admin'
        )
        await wh_conn.execute(
            f'GRANT ALL ON ALL SEQUENCES IN SCHEMA "{target_schema}" TO dbt_admin'
        )
        await wh_conn.execute(
            f'GRANT ALL ON ALL FUNCTIONS IN SCHEMA "{target_schema}" TO dbt_admin'
        )
        await wh_conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{target_schema}" '
            f'GRANT ALL ON TABLES TO dbt_admin'
        )
        await wh_conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{target_schema}" '
            f'GRANT ALL ON SEQUENCES TO dbt_admin'
        )
        await wh_conn.execute(
            f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{target_schema}" '
            f'GRANT EXECUTE ON FUNCTIONS TO dbt_admin'
        )

        # warehouse_admin owns the warehouse DB but not the marts schema
        # (dbt_admin owns it after this hook), so without explicit USAGE
        # + SELECT grants pgAdmin sessions hit "permission denied for
        # schema marts" even though the user owns the database.
        warehouse_role = "warehouse_admin"
        try:
            await wh_conn.execute(
                f'GRANT USAGE ON SCHEMA "{target_schema}" TO {warehouse_role}'
            )
            await wh_conn.execute(
                f'GRANT SELECT ON ALL TABLES IN SCHEMA "{target_schema}" '
                f'TO {warehouse_role}'
            )
            await wh_conn.execute(
                f'GRANT SELECT ON ALL SEQUENCES IN SCHEMA "{target_schema}" '
                f'TO {warehouse_role}'
            )
            # FOR ROLE dbt_admin is required: ALTER DEFAULT PRIVILEGES
            # is per-creating-role, so it must target the role that will
            # actually create the future objects.
            await wh_conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE dbt_admin '
                f'IN SCHEMA "{target_schema}" '
                f'GRANT SELECT ON TABLES TO {warehouse_role}'
            )
            await wh_conn.execute(
                f'ALTER DEFAULT PRIVILEGES FOR ROLE dbt_admin '
                f'IN SCHEMA "{target_schema}" '
                f'GRANT SELECT ON SEQUENCES TO {warehouse_role}'
            )
        except Exception as e:
            # warehouse_admin role may not exist on a fresh install where
            # the wizard hasn't run yet; grants will apply on next start.
            log.info(
                "dbt pre-start: warehouse_admin grants skipped (%s) — "
                "fresh install; will apply on next start",
                type(e).__name__,
            )
    finally:
        await wh_conn.close()

    # raw schema lives in the PIPELINE DB (Airbyte's landing zone).
    raw_conn = await asyncpg.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASSWORD,
        database=warehouse_db,
    )
    try:
        await raw_conn.execute("CREATE SCHEMA IF NOT EXISTS raw")
        await raw_conn.execute("GRANT USAGE ON SCHEMA raw TO dbt_admin")
        await raw_conn.execute(
            "GRANT SELECT ON ALL TABLES IN SCHEMA raw TO dbt_admin"
        )
        await raw_conn.execute(
            "ALTER DEFAULT PRIVILEGES IN SCHEMA raw "
            "GRANT SELECT ON TABLES TO dbt_admin"
        )
    finally:
        await raw_conn.close()


PRE_START_HOOKS = {
    "metabase":     _ensure_metabase_database,
    "pgadmin":      _ensure_pgadmin_servers_json,
    "airflow":      _ensure_airflow_setup,
    "airbyte":      _ensure_airbyte_setup,
    "openmetadata": _ensure_openmetadata_setup,
    "dbt":          _ensure_dbt_setup,
    # MinIO and GE need no PG provisioning — MinIO is filesystem-based,
    # GE writes checkpoints to disk.
}


# ===========================================================================
#  Post-start hooks
#
# Hook contract (per service in POST_START_HOOKS):
#   - MUST be idempotent (operators click Open repeatedly)
#   - MUST log success / failure via the audit log
#   - MUST NOT exceed POST_START_HOOK_TIMEOUT
#
# Runs in the background via asyncio.create_task so the user clicking
# Open isn't blocked by Metabase's multi-minute first-boot migration.
# ===========================================================================
POST_START_HOOK_TIMEOUT = 420  # 7 minutes; matches the slowest bootstrap (Metabase).


async def _bootstrap_metabase() -> None:
    """Complete Metabase's first-run setup via /api/setup; idempotent."""
    import httpx  # local import: only loaded when this hook runs
    from . import audit, db as _db  # local imports avoid cycle at load

    env = _read_env_file_or_empty()
    admin_email    = env.get("METABASE_ADMIN_EMAIL", "").strip()
    admin_password = env.get("METABASE_ADMIN_PASSWORD", "").strip()
    if not (admin_email and admin_password):
        # If .env bind-mount goes bad, _read_env_file_or_empty() returns
        # {} and this silent-skip would make the dashboard's
        # "bootstrapping…" toast loop forever. Surface it via the audit
        # log so the operator can see the cause on /app/audit.
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

    pipeline_name = env.get("WAREHOUSE_DB_NAME")
    pipeline_user = env.get("WAREHOUSE_DB_USER")
    pipeline_pass = env.get("WAREHOUSE_DB_PASSWORD")

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
        # Poll up to ~6 minutes — Metabase's first-boot Liquibase migration
        # (~420 changesets) takes 4-5 minutes on Docker Desktop's macOS FS
        # layer. Must stay under POST_START_HOOK_TIMEOUT or the outer
        # asyncio.wait_for cancels this loop mid-poll.
        already_configured = False
        for attempt in range(180):
            try:
                r = await client.get(f"{base}/api/session/properties")
                if r.status_code == 200:
                    props = r.json()
                    # `has-user-setup` is the canonical "already configured"
                    # signal. `setup-token` lingers in the in-memory store
                    # even after /api/setup completes, so checking it
                    # instead would make the bootstrap spin forever.
                    if props.get("has-user-setup"):
                        log.info(
                            "metabase bootstrap: has-user-setup=true at ~%ds — "
                            "already configured, nothing to do",
                            attempt * 2,
                        )
                        already_configured = True
                        break
                    setup_token = props.get("setup-token")
                    if setup_token is not None:
                        log.info(
                            "metabase bootstrap: setup-token observed after "
                            "~%ds — proceeding to POST /api/setup",
                            attempt * 2,
                        )
                        break
            except httpx.HTTPError:
                pass
            if attempt > 0 and attempt % 30 == 0:
                log.info(
                    "metabase bootstrap: still waiting for setup-token (%ds elapsed)",
                    attempt * 2,
                )
            await asyncio.sleep(2)

        if already_configured:
            return
        if setup_token is None:
            log.info("metabase bootstrap: timed out before setup-token appeared")
            return

        # Metabase 0.51 has a strict schema for /api/setup — extra fields
        # produce a generic 400 with no clue what's wrong. Keep this
        # payload to the minimum required and register the warehouse in
        # a separate POST /api/database after authenticating.
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
            # Explicit null — without this Metabase sits waiting for a
            # database block instead of completing setup.
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
            # Surface full body in audit log so password-rule failures
            # (length/complexity) are debuggable without docker logs.
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

        # Warehouse registration — best-effort. Operator can add the
        # warehouse by hand from Metabase Admin → Databases on failure.
        if not (pipeline_name and pipeline_user and pipeline_pass):
            log.info(
                "metabase bootstrap: no WAREHOUSE_DB_* in .env; skipping "
                "warehouse register"
            )
            return

        # Re-authenticate for a session cookie: POST /api/setup doesn't
        # return a session token on some Metabase versions, so a fresh
        # login is the portable path.
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
            "name":   "Warehouse",
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
                details={"name": "Warehouse", "dbname": pipeline_name},
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


async def _bootstrap_airbyte() -> None:
    """Skip Airbyte's first-run wizard + pre-set the workspace email; idempotent."""
    import httpx
    from . import audit
    env = _read_env_file_or_empty()
    admin_email = (
        env.get("AIRBYTE_ADMIN_EMAIL", "").strip()
        or env.get("METABASE_ADMIN_EMAIL", "").strip()
        or env.get("PGADMIN_DEFAULT_EMAIL", "").strip()
    )
    if not admin_email:
        log.info("airbyte bootstrap: no email in .env to seed workspace; skipping")
        return

    base = "http://orchestack-airbyte-server:8001"
    workspace_id: str | None = None
    async with httpx.AsyncClient(timeout=15.0) as client:
        for attempt in range(120):  # ~4 min — server can be slow to come up
            try:
                r = await client.post(
                    f"{base}/api/v1/workspaces/list",
                    headers={"Content-Type": "application/json"},
                    content="{}",
                )
                if r.status_code == 200:
                    workspaces = r.json().get("workspaces", [])
                    if workspaces:
                        workspace_id = workspaces[0]["workspaceId"]
                        log.info(
                            "airbyte bootstrap: workspace %s found after %ds",
                            workspace_id, attempt * 2,
                        )
                        break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
        if workspace_id is None:
            log.info("airbyte bootstrap: workspace never came up; skipping")
            return

        update_payload = {
            "workspaceId": workspace_id,
            "initialSetupComplete": True,
            "displaySetupWizard": False,
            "anonymousDataCollection": False,
            "email": admin_email,
        }
        try:
            r = await client.post(
                f"{base}/api/v1/workspaces/update",
                json=update_payload,
            )
            if r.status_code in (200, 204):
                log.info("airbyte bootstrap: workspace updated (email=%s)", admin_email)
                await audit.write(
                    "airbyte_bootstrapped",
                    service_name="airbyte",
                    user_id=None,
                    details={"workspace_id": workspace_id, "email": admin_email},
                )
            else:
                log.warning(
                    "airbyte bootstrap: workspaces/update returned %d: %s",
                    r.status_code, r.text[:300],
                )
        except httpx.HTTPError as e:
            log.warning("airbyte bootstrap: workspaces/update raised: %s", e)


async def _bootstrap_openmetadata() -> None:
    """Reset the OM admin password to OPENMETADATA_ADMIN_PASSWORD from .env.

    OpenMetadata 1.6.x creates the admin user from
    AUTHORIZER_ADMIN_PRINCIPALS with hardcoded password 'admin' and
    fixed email admin@open-metadata.org; the OPENMETADATA_ADMIN_* env
    vars in our compose snippet are silently ignored by OM. This hook
    calls OM's bootstrap CLI (upstream-supported) so .env's password
    actually works. Email stays at admin@open-metadata.org — changing
    it requires direct postgres edits in 1.6.x.

    Best-effort and idempotent.
    """
    import httpx
    from . import audit
    env = _read_env_file_or_empty()
    new_password = env.get("OPENMETADATA_ADMIN_PASSWORD", "").strip()
    if not new_password:
        log.info("openmetadata bootstrap: OPENMETADATA_ADMIN_PASSWORD not set; skipping")
        return

    base = "http://orchestack-openmetadata:8585"
    async with httpx.AsyncClient(timeout=10.0) as client:
        for attempt in range(180):  # ~6 min — first boot runs migrations
            try:
                r = await client.get(f"{base}/api/v1/system/version")
                if r.status_code == 200:
                    log.info("openmetadata bootstrap: API ready after %ds", attempt * 2)
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2)
        else:
            log.info("openmetadata bootstrap: API never came up; skipping")
            return

    # Use the container CLI for hashing: the OM HTTP change-password
    # endpoint stores the base64-encoded form verbatim instead of
    # decoding it, producing a password that nobody can guess.
    proc = await asyncio.create_subprocess_exec(
        "docker", "exec", "orchestack-openmetadata", "bash", "-c",
        f'./bootstrap/openmetadata-ops.sh reset-password '
        f'-e "admin@open-metadata.org" -p "{new_password}"',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = (stdout or b"").decode(errors="replace")
    if "Password updated successfully" in output:
        log.info("openmetadata bootstrap: admin password reset to OPENMETADATA_ADMIN_PASSWORD")
        await audit.write(
            "openmetadata_bootstrapped",
            service_name="openmetadata",
            user_id=None,
            details={"email": "admin@open-metadata.org"},
        )
    else:
        log.warning(
            "openmetadata bootstrap: reset-password did not confirm success; tail=%s",
            output[-300:],
        )

    # ES single-node fix: OM 1.6.x creates indices with replicas=1, but
    # we run ES single-node so every replica stays unassigned and the
    # cluster sits yellow. Some search code paths (e.g.
    # tag_search_index used by Domains → Subdomains) then return
    # 500 "all shards failed". Setting replicas=0 on existing indices
    # AND in OM's index template (for new indices) turns the cluster
    # green. Idempotent.
    async with httpx.AsyncClient(timeout=10.0) as client:
        es = "http://orchestack-openmetadata-es:9200"
        try:
            r1 = await client.put(
                f"{es}/_all/_settings",
                json={"index": {"number_of_replicas": 0}},
            )
            r2 = await client.put(
                f"{es}/_template/orchestack_no_replicas",
                json={"index_patterns": ["*"],
                      "settings": {"index": {"number_of_replicas": "0"}}},
            )
            if r1.status_code in (200, 201) and r2.status_code in (200, 201):
                log.info(
                    "openmetadata bootstrap: ES replicas → 0 (existing + template) — single-node fix applied",
                )
            else:
                log.warning(
                    "openmetadata bootstrap: ES replica reset returned %d / %d",
                    r1.status_code, r2.status_code,
                )
        except httpx.HTTPError as e:
            log.warning("openmetadata bootstrap: ES replica reset failed: %s", e)


async def _bootstrap_airflow() -> None:
    """Create the `orchestack_warehouse` Airflow Connection idempotently.

    Cosmos's PostgresUserPasswordProfileMapping reads this connection
    to build the dbt profile. Operators shouldn't have to learn about
    Airflow Connections to run dbt — same principle as the OpenMetadata
    password reset (absorb upstream-defaults overrides at the platform
    layer).

    Connection ID:        orchestack_warehouse
    Connection type:      postgres
    Host:                 orchestack-postgres
    Port:                 5432
    Schema (database):    ${WAREHOUSE_DB_NAME} (defaults to data_warehouse)
    Login (user):         warehouse_admin
    Password:             ${WAREHOUSE_DB_PASSWORD} from .env

    Idempotent: `airflow connections add` errors with code 1 if the
    connection already exists; we treat that as success.
    """
    env = _read_env_file_or_empty()
    db_name = env.get("WAREHOUSE_DB_NAME", "data_warehouse").strip() or "data_warehouse"
    db_user = env.get("WAREHOUSE_DB_USER", "warehouse_admin").strip() or "warehouse_admin"
    db_pass = env.get("WAREHOUSE_DB_PASSWORD", "").strip()
    if not db_pass:
        log.info("airflow bootstrap: WAREHOUSE_DB_PASSWORD not set; skipping connection creation")
        return

    # The Airflow CLI shells inside the container — equivalent to:
    #   docker exec orchestack-airflow airflow connections add ...
    # We wait briefly for the webserver to be reachable so the CLI
    # has a live metadata DB to write to.
    for attempt in range(60):  # ~2 minutes
        try:
            res = await asyncio.to_thread(
                _run_sync,
                ["docker", "exec", "orchestack-airflow", "airflow", "version"],
                10,
            )
            if res.returncode == 0:
                break
        except Exception:
            pass
        await asyncio.sleep(2)
    else:
        log.info("airflow bootstrap: container never reachable; skipping")
        return

    cmd = [
        "docker", "exec", "orchestack-airflow",
        "airflow", "connections", "add", "orchestack_warehouse",
        "--conn-type", "postgres",
        "--conn-host", "orchestack-postgres",
        "--conn-port", "5432",
        "--conn-schema", db_name,
        "--conn-login", db_user,
        "--conn-password", db_pass,
    ]
    try:
        res = await asyncio.to_thread(_run_sync, cmd, 30)
        if res.returncode == 0:
            log.info("airflow bootstrap: connection orchestack_warehouse created")
            from . import audit
            await audit.write(
                "airflow_bootstrapped",
                service_name="airflow",
                user_id=None,
                details={"connection_id": "orchestack_warehouse"},
            )
        else:
            # Returncode 1 with "already exists" message is the idempotent path.
            stderr_lower = (res.stderr or "").lower()
            if "already exists" in stderr_lower or "duplicate" in stderr_lower:
                log.info("airflow bootstrap: connection orchestack_warehouse already exists; no-op")
            else:
                log.warning(
                    "airflow bootstrap: connections-add returned %d: %s",
                    res.returncode, (res.stderr or res.stdout or "")[:300],
                )
    except Exception as e:
        log.warning("airflow bootstrap: connections-add raised: %s", e)


POST_START_HOOKS = {
    "metabase":     _bootstrap_metabase,
    "airbyte":      _bootstrap_airbyte,
    "openmetadata": _bootstrap_openmetadata,
    "airflow":      _bootstrap_airflow,
}


def _schedule_post_start_hook(service: str) -> None:
    """Fire-and-forget post-start hook, bounded by POST_START_HOOK_TIMEOUT."""
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

    asyncio.create_task(_run(), name=f"post-start:{service}")


async def start_service(service: str) -> CommandResult:
    """Bring a cold-tier service up. Idempotent — `up -d` no-ops if already running.

    Timeout of 5 minutes accommodates first-pull of 100-500 MB images on
    ~10 Mbps links (a 200 MB image takes ~160s to pull); cached starts
    are sub-second. PRE_START_HOOKS run first (best-effort, failures
    don't block start). Self-heals "container name is already in use"
    by `docker rm -f` of the orphan + one retry.
    """
    hook = PRE_START_HOOKS.get(service)
    if hook is not None:
        try:
            await hook()
        except Exception as e:
            log.warning("pre-start hook for %s failed: %s", service, e)

    up_args = _service_compose_args(service) + ["up", "-d", "--remove-orphans"]
    res = await asyncio.to_thread(_run_sync, up_args, 300)

    if res.ok:
        _schedule_post_start_hook(service)
        return res

    if "is already in use by container" not in res.stderr:
        return res

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

    retry = await asyncio.to_thread(_run_sync, up_args, 300)
    if retry.ok:
        _schedule_post_start_hook(service)
    return retry


async def stop_service(service: str) -> CommandResult:
    """Stop a service (keeps volumes + networks); subsequent start is ~1-2s vs ~10s for `down`.

    Compose parses the yml on every subcommand; metabase.yml uses
    `${ORCHESTACK_DB_PASSWORD:?...}` which fails parse if the var is
    missing, even for `stop`. So if .env is unusable (the bind-mount-
    as-empty-directory trap) we fall back to `docker stop <container>`
    directly, bypassing the compose parser; without this fallback an
    operator with a broken .env mount could never stop a service.
    """
    if _env_file_usable():
        return await asyncio.to_thread(
            _run_sync,
            _service_compose_args(service, need_env=True) + ["stop"],
            60,
        )
    container_name = f"orchestack-{service}"
    log.warning(
        "stop_service(%s): .env unusable; falling back to `docker stop %s` "
        "(bypassing compose parser). Restore the operator's .env on the "
        "host and restart the orchestack-orchestrator container to "
        "return to the normal compose-based stop path.",
        service, container_name,
    )
    return await asyncio.to_thread(
        _run_sync,
        ["docker", "stop", container_name],
        60,
    )


async def list_running_services() -> list[dict[str, str]]:
    """List every managed service container currently running.

    Filters on `label=orchestack.service` to scope to managed services;
    control-plane containers (proxy, postgres, auth) don't carry the
    label and are added separately from SERVICE_CATALOGUE. `started_at`
    comes from `.State.StartedAt` (NOT `.CreatedAt`) because CreatedAt
    only refreshes on `compose down`+recreate, so Stop→Start would
    report a stale multi-day uptime. Requires a batched `docker inspect`
    fallback since `docker ps --format` only exposes CreatedAt.
    """
    ps_res = await asyncio.to_thread(
        _run_sync,
        ["docker", "ps",
         "--filter", "label=orchestack.service",
         "--format", "{{.Label \"orchestack.service\"}}\t{{.Names}}\t{{.Image}}"],
        10,
    )
    if not ps_res.ok:
        log.warning("list_running_services failed: %s", ps_res.short_stderr)
        return []

    by_container: dict[str, dict[str, str]] = {}
    for line in ps_res.stdout.strip().splitlines():
        parts = line.split("\t", 2)
        if len(parts) >= 2:
            by_container[parts[1]] = {
                "service":    parts[0],
                "container":  parts[1],
                "image":      parts[2] if len(parts) == 3 else "",
                "started_at": "",
            }

    # Control-plane containers don't carry the orchestack.service label;
    # add them by-name from the catalogue so their image + StartedAt
    # surface on the dashboard's service-detail page.
    for svc_name, meta in config.SERVICE_CATALOGUE.items():
        if not meta.get("control_plane"):
            continue
        # postgresql maps to orchestack-postgres (no -ql suffix); all
        # other services follow the orchestack-{svc} convention.
        cname = f"orchestack-{svc_name.replace('postgresql', 'postgres')}"
        if cname in by_container:
            continue
        by_container[cname] = {
            "service":    svc_name,
            "container":  cname,
            "image":      "",
            "started_at": "",
        }

    if not by_container:
        return []

    inspect_res = await asyncio.to_thread(
        _run_sync,
        ["docker", "inspect",
         "--format", "{{.Name}}\t{{.State.StartedAt}}\t{{.Config.Image}}",
         *list(by_container.keys())],
        10,
    )
    if inspect_res.ok:
        for line in inspect_res.stdout.strip().splitlines():
            parts = line.split("\t", 2)
            if len(parts) >= 2:
                cname = parts[0].lstrip("/")
                if cname in by_container:
                    by_container[cname]["started_at"] = parts[1]
                    # Only overwrite image when docker ps didn't supply
                    # one (control-plane case); ps gives the resolved
                    # tag, which is the preferred display form.
                    if len(parts) == 3 and not by_container[cname]["image"]:
                        by_container[cname]["image"] = parts[2]
    else:
        log.warning("docker inspect for uptime failed: %s",
                    inspect_res.short_stderr)

    return list(by_container.values())


async def container_uptime_seconds(service: str) -> int | None:
    """Seconds since this service started, or None if not running.

    Used by the reconciler's start-grace check so a service that was
    just started isn't stopped before its first session POST lands.
    """
    res = await asyncio.to_thread(
        _run_sync,
        ["docker", "ps",
         "--filter", f"label=orchestack.service={service}",
         "--format", "{{.RunningFor}}"],
        10,
    )
    # RunningFor prints "About a minute ago" / "2 hours ago" — not
    # parseable. Use StartedAt as an ISO timestamp instead.
    res2 = await asyncio.to_thread(
        _run_sync,
        ["docker", "inspect",
         "--format", "{{.State.StartedAt}}",
         f"orchestack-{service}"],
        10,
    )
    if not res2.ok or not res2.stdout.strip():
        return None
    import datetime as _dt
    try:
        started = _dt.datetime.fromisoformat(res2.stdout.strip().replace("Z", "+00:00"))
        now = _dt.datetime.now(_dt.timezone.utc)
        return int((now - started).total_seconds())
    except ValueError:
        return None
