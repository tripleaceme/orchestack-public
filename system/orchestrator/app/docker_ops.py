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


def _service_compose_args(service: str) -> list[str]:
    """Build the `docker compose` argument prefix for a managed service.

    `--env-file` is passed explicitly because the orchestrator runs from
    its own working directory inside the container, NOT from a directory
    that contains `.env`. Without this flag, compose would try and fail to
    interpolate ${ORCHESTACK_DB_PASSWORD}, ${PIPELINE_DB_*}, etc., when
    bringing up any service that references those variables (see
    services/metabase.yml). The `.env` file itself is bind-mounted into
    the orchestrator at config.ENV_FILE — see system/docker/docker-compose.yml.

    If the env file doesn't exist (development edge case where the
    orchestrator is run without the canonical compose), we still emit the
    flag but with the path; docker compose will surface a clear error.
    """
    compose_file = os.path.join(config.SERVICES_DIR, f"{service}.yml")
    project_name = f"{config.COMPOSE_PROJECT_PREFIX}-{service}"
    args = [
        "docker", "compose",
        "--file", compose_file,
        "--project-name", project_name,
    ]
    if os.path.exists(config.ENV_FILE):
        args += ["--env-file", config.ENV_FILE]
    else:
        log.warning(
            "env-file not found at %s — compose interpolation may fail. "
            "Did you mount the operator's .env into the orchestrator? "
            "See system/docker/docker-compose.yml.",
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


async def start_service(service: str) -> CommandResult:
    """Bring a cold-tier service up. Idempotent — `up -d` no-ops if already running.

    Timeout is 5 minutes, not the usual 3, because the first start of a
    fresh service pulls its image which can be 100-500 MB. On a
    Nigerian-affordable VPS with a ~10 Mbps link, a 200 MB image takes
    160 seconds just to pull. Subsequent starts are sub-second once the
    image is cached, so the bigger timeout is paid only on cold cache.

    Self-heal on name conflict: when a previous bundle install left a
    container with the same name behind (typically because the operator
    re-extracted the bundle into a new directory), `docker compose up -d`
    fails with "container name '/orchestack-X' is already in use by
    container 'abc...'". Detect that specific error, `docker rm -f` the
    orphan, retry. Anything else returns the original error.
    """
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
    """
    return await asyncio.to_thread(
        _run_sync,
        _service_compose_args(service) + ["stop"],
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
