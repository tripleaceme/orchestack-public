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

    # OrcheStack platform DB — always present (the orchestrator's own
    # internal database). Useful for advanced operators inspecting
    # platform.users, platform.audit_log, etc.
    by_idx["1"] = {
        "Name":          "OrcheStack platform",
        "Group":         "OrcheStack",
        "Host":          "orchestack-postgres",
        "Port":          5432,
        "MaintenanceDB": env.get("ORCHESTACK_DB_NAME", "orchestack"),
        "Username":      env.get("ORCHESTACK_DB_USER", "orchestack"),
        "SSLMode":       "prefer",
        "Comment":       "OrcheStack's internal database — platform.users, "
                          "sessions, audit log. Read-only inspection only.",
    }

    # Pipeline warehouse — present once the operator completes the wizard.
    pipeline_db   = env.get("PIPELINE_DB_NAME")
    pipeline_user = env.get("PIPELINE_DB_USER")
    if pipeline_db and pipeline_user:
        by_idx["2"] = {
            "Name":          "Pipeline warehouse",
            "Group":         "OrcheStack",
            "Host":          "orchestack-postgres",
            "Port":          5432,
            "MaintenanceDB": pipeline_db,
            "Username":      pipeline_user,
            "SSLMode":       "prefer",
            "Comment":       "Your pipeline marts. dbt writes here; "
                              "Metabase reads here.",
        }

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

    if res.ok or "is already in use by container" not in res.stderr:
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
    return await asyncio.to_thread(_run_sync, up_args, 300)


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
