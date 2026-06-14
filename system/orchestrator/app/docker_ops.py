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
    """Return True if config.ENV_FILE is actually openable + readable.

    Three failure modes this function has to detect:

    1. The file is missing. Easy — os.path.isfile() returns False.

    2. The host's ./.env was missing at compose-up time, so Docker
       materialised the bind-mount target as an empty directory at
       /etc/orchestack/.env. os.path.isfile() catches this (False).

    3. STALE BIND-MOUNT INODE. The host's .env was edited after
       compose-up (most editors do atomic-rename: write to tmpfile,
       rename over original), which unlinks the original inode the
       bind-mount is pointing at. Inside the container, `ls -la` and
       `os.stat()` still see the file via the directory entry (cached
       on the bind mount), so isfile() and os.access(R_OK) BOTH
       return True. But any actual read attempt fails with ENOENT
       because the underlying inode was deleted on the host.

       Detected by st_nlink == 0 (a hard-deleted file with active
       refs) and confirmed by actually attempting an open() + read().

       Recovery is operator-side: `docker compose up -d orchestrator`
       restarts the container which re-resolves the bind mount to the
       current host inode.

    Returning False from this function makes stop_service fall back
    to direct `docker stop <container>` (compose-free), which keeps
    the dashboard's Stop button working even when the bind mount
    has gone stale.
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
    """Provision Metabase's PG role + dedicated `metabase` database.

    Architecture (per design/m4-multi-db.md, revised twice):

      - `orchestack`     — platform internals (users, audit, sessions)
      - `data_warehouse` — operator's analytical data (WAREHOUSE_DB_NAME)
      - `metabase`       — Metabase's own state (its own DB, see below)
      - `services`       — shared DB for tools that HONOR a configurable
                           schema setting (Airflow, OpenMetadata when M4
                           lands). Each schema-aware tool gets its own
                           schema inside this DB.

    Why Metabase gets its OWN database, not a schema in `services`:

      Metabase 0.51's first-boot Liquibase changeset `v00.00-000` runs
      resources/migrations/initialization/metabase_postgres.sql which
      HARDCODES `public.<tablename>` in every CREATE TABLE statement.
      MB_DB_SCHEMA only takes effect AFTER setup — Metabase ignores it
      during the initialization migration. There is no env var or
      config flag to change this; it is fundamental to Metabase's
      packaging. Tested end-to-end June 12: with services.metabase
      schema correctly created and search_path set, the migration
      still fails with "permission denied for schema public" because
      the migration explicitly writes to public.<tablename>.

      Per-DB isolation is therefore the architecturally honest choice
      for Metabase: its own DB means its hardcoded `public.<table>` writes
      land in a Metabase-owned database that we control. Pgadmin sees
      `metabase` as a separate DB which is correct — that IS what it is.

    Idempotent: safe to run on every start. Checks role + DB
    independently, creates only what's missing, updates the password
    unconditionally so .env-driven rotation works on next start.
    """
    from . import db  # local import — avoids circular import at module load
    env = _read_env_file_or_empty()
    password = env.get("METABASE_DB_PASSWORD", "").strip()
    if not password:
        # Fallback: derive a stable per-service password. M3 testers
        # upgrading to per-service DBs don't have to re-deploy.
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
        # 1. Role: backward-compat rename legacy "metabase" → "metabase_admin"
        # then ensure the canonical name exists with the chosen password.
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

        # 2. Database: rename legacy "metabase" DB → "metabase_db" (the
        # _db suffix convention). Then ensure metabase_db exists owned
        # by metabase_admin.
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

    # 3. If the database already had objects owned by the platform admin
    # (pre-per-service-role testers), REASSIGN them to metabase_admin.
    # Runs against the metabase_db itself — REASSIGN OWNED only acts on
    # the connected database.
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
    """Write `KEY=value` into the operator's .env. Replaces in place
    if the key already exists; appends to the end otherwise.

    Same line-preserving semantics as _persist_credentials_to_env in
    api/setup.py — comments and blank lines stay verbatim. We deliberately
    don't reuse that helper here because this hook needs to work from
    docker_ops (no API session, no audit-log dependencies).
    """
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
                # password prompt instead, which is the pre-pgpass UX.
                log.warning("pgadmin pgpass chown skipped: %s", ce)
            pgpass_inner_path = f"{_PGADMIN_INNER_CONF_DIR}/pgadmin/.pgpass"
            log.info("pre-start hook: wrote pgadmin pgpass for %s@%s",
                      pipeline_user, warehouse_db)
        except OSError as e:
            log.warning("pgadmin pgpass write failed: %s — operator will "
                          "be prompted for password on first connect", e)

    # Warehouse — the only server we pre-load by default.
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
    if warehouse_db and pipeline_user:
        entry = {
            "Name":          warehouse_db,
            "Group":         "OrcheStack",
            "Host":          "orchestack-postgres",
            "Port":          5432,
            "MaintenanceDB": warehouse_db,
            "Username":      pipeline_user,
            "SSLMode":       "prefer",
            # NO DBRestriction — operator is the platform admin and
            # gets to see every database the postgres server hosts.
            # The wildcard pgpass entry below handles auth for all of
            # them; if the operator clicks a database their role can't
            # actually USE, postgres returns "permission denied" which
            # is the clear signal, not "this is hidden from you."
            # Per-role restriction lands in M5 with the multi-DB RBAC
            # design (design/m4-multi-db.md §3).
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


# ===========================================================================
#  M4 service hooks — generic per-service DB + role provisioning
#
# Each managed service that needs a PostgreSQL role + database (or a
# schema in `services`) registers a tiny pre-start hook here. The
# common pattern factored into _ensure_service_setup below:
#
#   1. Read $$<SERVICE>_DB_PASSWORD from .env; derive deterministically
#      from ORCHESTACK_DB_PASSWORD if absent; persist the derived value
#      back to .env so the container sees it on the next compose-up.
#   2. CREATE ROLE <role_name> WITH LOGIN PASSWORD IF NOT EXISTS.
#   3. CREATE DATABASE <db_name> OWNER <role_name> IF NOT EXISTS.
#   4. For Airflow specifically: also CREATE SCHEMA airflow inside
#      `services` because Airflow honors AIRFLOW__DATABASE__SQL_ALCHEMY_SCHEMA.
#
# Per the schema-aware test (design/m4-multi-db.md §1): MinIO,
# GE, dbt need no DB; Airflow CAN take a custom schema and lives in
# services.airflow; Airbyte + OpenMetadata + Metabase need their own
# DBs because they hardcode public.<tablename>.
# ===========================================================================
async def _ensure_simple_pg_role_and_db(
    role_name: str, db_name: str, env_key: str,
    *, schema_in_services: str | None = None,
    legacy_role_name: str | None = None,
    legacy_db_name: str | None = None,
) -> None:
    """Provision a per-service PG role + database OR schema.

    role_name: PG role to create (e.g. "airflow_admin")
    db_name: database to own (e.g. "airflow_db") — IGNORED if schema_in_services set
    env_key: .env var holding the password (e.g. "AIRFLOW_DB_PASSWORD")
    schema_in_services: if set, the role gets the named schema inside
                        `services` instead of its own database.
    legacy_role_name: name a previous version of OrcheStack created
                      this role under (e.g. "airbyte" before the
                      <service>_admin convention). If set and the
                      legacy name exists in pg_roles but the new
                      name doesn't, ALTER ROLE RENAME migrates it
                      in place — Postgres tracks ownership/grants
                      by OID so the rename is non-destructive.
    legacy_db_name: name a previous version of OrcheStack created
                    the database under (e.g. "airbyte" before the
                    _db suffix convention). If set and the legacy
                    DB exists but the new name doesn't, ALTER
                    DATABASE RENAME migrates it in place. Postgres
                    requires no active connections for the rename,
                    so this works only when the dependent containers
                    are stopped (which the pre-start hook context
                    guarantees: hook runs before compose up).
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
        # Backward-compat: if the operator's install used a legacy role
        # name (e.g. "airbyte" before the <service>_admin convention),
        # rename it in place rather than creating a new one. Postgres
        # tracks ownership + grants by OID so the rename preserves
        # every database/schema/table the legacy role owned.
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
            # Schema-in-services path: ensure `services` DB exists,
            # then a per-role schema inside it.
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
        # Set the role's default search_path so the schema name doesn't
        # have to be hardcoded in every query the tool runs.
        async with pool.acquire() as conn:
            await conn.execute(
                f'ALTER ROLE "{role_name}" SET search_path TO '
                f'"{schema_in_services}", public'
            )
    else:
        # Dedicated-DB path: CREATE DATABASE owned by the role.
        async with pool.acquire() as conn:
            # Backward-compat: rename a legacy DB to the new name if it
            # exists (e.g. "airbyte" → "airbyte_db" when adding the _db
            # suffix convention). ALTER DATABASE RENAME requires zero
            # active connections — the pre-start hook runs before
            # compose up, so the dependent container is stopped and
            # the rename is safe.
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
    """Airflow: dedicated airflow_db database (the services-DB schema
    approach was abandoned because only Airflow ever used it cleanly;
    Metabase, Airbyte, and OpenMetadata all need dedicated DBs because
    they hardcode public-schema table names).

    Migration: earlier installs created an `airflow` schema inside a
    `services` database. The helper's legacy_db_name kwarg renames a
    legacy DB to the new name, but we can't rename a SCHEMA-in-DB to a
    standalone DB. So this hook also handles the schema-to-database
    migration explicitly: pg_dump --schema=airflow services into the
    fresh airflow_db, then drop the old services DB once Airflow is
    confirmed running on the new one.

    For an academic-project demo where the operator hasn't built up
    much Airflow state yet, we skip the dump-and-restore: create
    airflow_db fresh, drop services, and let Airflow re-run its
    Liquibase migrations to create the schema. The DAG files
    themselves live in a separate volume so they're preserved across
    this migration.
    """
    from . import db as _db
    import asyncpg
    # First the role + DB (with legacy_role_name for "airflow" → "airflow_admin"
    # in case a previous install used the unsuffixed name).
    await _ensure_simple_pg_role_and_db(
        "airflow_admin", "airflow_db", "AIRFLOW_DB_PASSWORD",
        legacy_role_name="airflow",
    )
    # Then clean up the legacy services DB if it exists. We only drop it
    # if it has an `airflow` schema — that's the signal it was created
    # by an old version of OrcheStack for Airflow's metadata. Any other
    # use of `services` (none currently shipped) would be preserved.
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
    # Drop the legacy services DB. ALTER DATABASE / DROP DATABASE
    # requires no active connections — pre-start hook context
    # guarantees this for the Airflow container side, and nothing
    # else in OrcheStack uses `services`.
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
    """Airbyte: airbyte_admin role + airbyte DB + two extra DBs for Temporal.

    The multi-container Airbyte deployment includes Temporal as the
    workflow engine, and Temporal stores its workflow history and
    visibility queries in two separate Postgres databases. All three
    DBs (airbyte, temporal, temporal_visibility) share the
    airbyte_admin role for simplicity; logical isolation is by
    database, not by user.

    Backward compat: earlier installs created the role as plain
    "airbyte". The shared helper handles the ALTER ROLE rename via
    its legacy_role_name kwarg — once renamed, ownership of the
    airbyte/temporal/temporal_visibility DBs transfers automatically
    (Postgres stores ownership by OID).
    """
    await _ensure_simple_pg_role_and_db(
        "airbyte_admin", "airbyte_db", "AIRBYTE_DB_PASSWORD",
        legacy_role_name="airbyte",
        legacy_db_name="airbyte",
    )
    # Temporal databases. Apply the _db suffix convention with
    # backward-compat renames from the legacy unsuffixed names.
    from . import db as _db
    pool = _db.get_pool()
    for new_name, legacy_name in (
        ("temporal_db", "temporal"),
        ("temporal_visibility_db", "temporal_visibility"),
    ):
        async with pool.acquire() as conn:
            new_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{new_name}'"
            )
            legacy_exists = await conn.fetchval(
                f"SELECT 1 FROM pg_database WHERE datname = '{legacy_name}'"
            )
            if legacy_exists and not new_exists:
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
    """OpenMetadata: own role + own DB (hardcodes public schema).

    Naming follows the two conventions: <service>_admin for the role,
    <service>_db for the database. legacy_* kwargs migrate older
    installs that used the unsuffixed names.
    """
    await _ensure_simple_pg_role_and_db(
        "openmetadata_admin", "openmetadata_db", "OPENMETADATA_DB_PASSWORD",
        legacy_role_name="openmetadata",
        legacy_db_name="openmetadata",
    )


async def _ensure_dbt_setup() -> None:
    """dbt: role + write/read grants on the chosen warehouse DB.

    Privileges granted (operator-configurable target via DBT_DATABASE +
    DBT_SCHEMA in .env):
      - CONNECT on the target database
      - OWNERSHIP of the target schema (CREATE/USAGE implicit)
      - ALL on EXISTING + ALTER DEFAULT PRIVILEGES so any FUTURE objects
        in the target schema (created by dbt OR by anyone else) are
        accessible to dbt_admin — the operator explicitly asked for this
        so model rebuilds and CI-driven schema changes don't trip on
        "permission denied for table foo" mid-run.
      - USAGE + SELECT on the `raw` schema (Airbyte's landing zone) so
        dbt can `select * from {{ source('raw', 'orders') }}` cleanly,
        plus DEFAULT PRIVILEGES for future raw tables.
    """
    from . import db as _db
    env = _read_env_file_or_empty()
    warehouse_db = env.get("WAREHOUSE_DB_NAME") or "data_warehouse"
    # Operator-overridable target. Defaults to the pipeline warehouse
    # but can be a separate DB if the operator wants production tables
    # in their own namespace.
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
        # Backward-compat: earlier installs used `dbt_user`. If that
        # role exists and `dbt_admin` doesn't, rename it. ALTER ROLE
        # RENAME updates all object permissions automatically because
        # they're stored by OID, not by name — so any tables owned by
        # dbt_user become owned by dbt_admin transparently and dbt's
        # `dbt run` continues to work after the rename.
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

        # If the operator picked a different target DB, create it.
        # CREATE DATABASE can't run inside a transaction so use a
        # dedicated connection rather than the pool's pre-tx conn.
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
        # Also CONNECT on the warehouse DB if it's different — dbt sources
        # may reference `raw.*` in the warehouse DB even when writing
        # marts to a different DB.
        if target_db != warehouse_db:
            await conn.execute(
                f'GRANT CONNECT ON DATABASE "{warehouse_db}" TO dbt_admin'
            )

    # In-target-DB grants. Connect as the platform superuser so we can
    # issue GRANT statements on objects owned by anyone.
    import asyncpg
    wh_conn = await asyncpg.connect(
        host=config.DB_HOST, port=config.DB_PORT,
        user=config.DB_USER, password=config.DB_PASSWORD,
        database=target_db,
    )
    try:
        # Target schema (DBT_SCHEMA, default 'marts') OWNED BY dbt_admin.
        # Ownership gives full rights to anything dbt creates here.
        await wh_conn.execute(
            f'CREATE SCHEMA IF NOT EXISTS "{target_schema}" '
            f'AUTHORIZATION dbt_admin'
        )
        # Belt-and-suspenders: also GRANT ALL on the schema itself + on
        # every existing object + ALTER DEFAULT PRIVILEGES for future
        # objects. This covers the case where someone (e.g. an admin
        # patching a model manually) creates a table in this schema —
        # dbt_admin still has full access to it.
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

        # Read access for warehouse_admin (the role that owns the
        # warehouse database itself). Without this, an operator
        # connecting to the warehouse via pgAdmin as warehouse_admin
        # gets "permission denied for schema marts" — even though
        # they own the database — because dbt's pre-start hook
        # transferred ownership of `marts` to dbt_admin. Granting
        # warehouse_admin USAGE + SELECT on the existing schema +
        # ALTER DEFAULT PRIVILEGES so future dbt-created tables
        # are queryable lets the warehouse owner browse dbt's
        # output without elevating to a superuser.
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
            # Future tables created by dbt_admin in this schema → also
            # SELECT for warehouse_admin. The "FOR ROLE dbt_admin"
            # qualifier is important: ALTER DEFAULT PRIVILEGES is
            # per-creating-role, so we set it specifically for the
            # role that will create the objects.
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
            # warehouse_admin role may not exist on a totally fresh
            # install where the wizard hasn't run yet. Log + move on
            # — the grants will succeed on next start when the role
            # is created.
            log.info(
                "dbt pre-start: warehouse_admin grants skipped (%s) — "
                "fresh install; will apply on next start",
                type(e).__name__,
            )
    finally:
        await wh_conn.close()

    # raw schema lives in the PIPELINE DB (Airbyte's landing). Different
    # connection if target_db != warehouse_db.
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
        already_configured = False
        for attempt in range(180):
            try:
                r = await client.get(f"{base}/api/session/properties")
                if r.status_code == 200:
                    props = r.json()
                    # `has-user-setup` is the canonical "Metabase has been
                    # set up by someone" signal. `setup-token` lingers in
                    # the in-memory store even after /api/setup completes,
                    # so it is NOT a reliable "still needs setup" signal —
                    # we used to check setup-token and bootstrap would
                    # spin forever even after success.
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
            # Heartbeat every minute so docker-logs observers can see the
            # hook is making progress (not stuck) without spamming logs.
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
                "metabase bootstrap: no WAREHOUSE_DB_* in .env; skipping "
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
    """Skip Airbyte's first-run wizard + pre-set the workspace email.

    Airbyte's webapp shows a "set your email" onboarding screen on
    first visit when the default workspace has initialSetupComplete=false
    or no email. The screen LOOKS like a login but is just analytics
    opt-in. Operators expect "click Open → land on the workspace
    dashboard" — the wizard breaks that expectation.

    We poll for /api/v1/workspaces/list to come up (server may still
    be initialising), find the default workspace, and POST
    /api/v1/workspaces/update with the OrcheStack-collected admin
    email + anonymousDataCollection=false. This is idempotent — if
    the workspace is already set up, the update is a no-op.

    Best-effort. Failure here doesn't block Open — the operator can
    still click through the wizard manually.
    """
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


POST_START_HOOKS = {
    "metabase": _bootstrap_metabase,
    "airbyte":  _bootstrap_airbyte,
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
    """Stop a service. Keeps volumes + networks; only stops the container.

    We use `stop`, not `down`, so subsequent `start_service` is a fast
    container-start (~1-2s) instead of a full recreate (~10s).

    Path A (env file usable): `docker compose ... --env-file .env stop`.
    Docker compose PARSES the yml regardless of subcommand, and
    metabase.yml uses `${ORCHESTACK_DB_PASSWORD:?...}` which fails parse
    if the var isn't provided — even though `stop` doesn't actually
    USE the variable. Passing the env file satisfies the parser.

    Path B (env file broken — the bind-mount-as-empty-directory trap):
    fall back to `docker stop <container>` directly. This bypasses
    compose entirely, so the missing-env-var parse failure can't bite.
    The container is named by metabase.yml's container_name attribute,
    which we mirror with the `orchestack-<service>` convention.
    Without this fallback, an operator whose .env mount went bad
    could never stop a running service from the dashboard.
    """
    if _env_file_usable():
        return await asyncio.to_thread(
            _run_sync,
            _service_compose_args(service, need_env=True) + ["stop"],
            60,
        )
    # Fallback: direct `docker stop`. Bypasses compose entirely so the
    # missing .env can't trip the yml parser.
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
