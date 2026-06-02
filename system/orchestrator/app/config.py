"""Centralised configuration — read once from the environment.

Every config value is documented in OrcheStack/system/docker/.env.example so
operators can see what's tunable without reading code. This module is
deliberately a flat namespace of constants rather than a Config class:
configuration is read-only at startup, and a class would suggest mutability
where there is none.
"""

from __future__ import annotations

import os


def _int(name: str, default: int) -> int:
    """Read an int env var with a fallback; raise if it's set but unparseable."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(
            f"environment variable {name}={raw!r} is not an integer"
        ) from exc


# ----- Logging --------------------------------------------------------------
LOG_LEVEL: str = os.environ.get("ORCHESTRATOR_LOG_LEVEL", "info").upper()

# ----- Database (OrcheStack internal DB) ------------------------------------
# These come from the compose file, sourced from ORCHESTACK_DB_* in .env.
# The orchestrator NEVER connects to the customer pipeline DB.
DB_HOST: str = os.environ.get("POSTGRES_HOST", "orchestack-postgres")
DB_PORT: int = _int("POSTGRES_PORT", 5432)
DB_USER: str = os.environ.get("POSTGRES_USER", "orchestack")
DB_PASSWORD: str = os.environ.get("POSTGRES_PASSWORD", "")
DB_NAME: str = os.environ.get("POSTGRES_DB", "orchestack")

# Pool size: 5 is enough for the reconciler tick + a handful of concurrent
# API requests. Not a high-QPS service. Bump if we add Streamlit features
# that fan out many parallel calls.
DB_POOL_MIN: int = _int("ORCHESTRATOR_DB_POOL_MIN", 1)
DB_POOL_MAX: int = _int("ORCHESTRATOR_DB_POOL_MAX", 5)

# ----- Reconciler -----------------------------------------------------------
# How often the reconciler tick fires (seconds). 30s gives a reasonable
# balance between responsiveness and DB query load.
RECONCILE_INTERVAL: int = _int("ORCHESTRATOR_RECONCILE_INTERVAL", 30)

# How long a service can be idle (no active sessions, not pinned) before
# the reconciler stops it. 10 minutes is the M2 default; we'll calibrate
# from real audit-log data during M5.
IDLE_THRESHOLD: int = _int("ORCHESTRATOR_IDLE_THRESHOLD", 600)

# Grace period after start during which the reconciler won't shut a
# service down even if no sessions exist yet. Prevents the race where a
# session POST and a reconciler tick arrive in the wrong order right after
# start-up.
START_GRACE: int = _int("ORCHESTRATOR_START_GRACE", 60)

# How recent a session has to be (last_seen_at) to count as "active" during
# the reconciler tick. 5 minutes gives clients time to checkin even after
# brief network blips.
SESSION_ACTIVE_WINDOW: int = _int("ORCHESTRATOR_SESSION_ACTIVE_WINDOW", 300)

# ----- Service compose snippets --------------------------------------------
# Directory inside the orchestrator container where per-service compose YAML
# files live. Mounted read-only from system/docker/services on the host.
SERVICES_DIR: str = os.environ.get("ORCHESTRATOR_SERVICES_DIR", "/services")

# Compose project name used when running `docker compose -p <name>` for each
# managed service. Distinct from the base "orchestack" project so that the
# base control plane and the cold-tier services have separate `compose ps`
# views — easier to reason about for operators.
COMPOSE_PROJECT_PREFIX: str = "orchestack-service"

# ----- Catalogue of services the orchestrator can manage --------------------
# Maps the symbolic service name (as it appears in the wizard selections and
# the compose snippet filename) to its tier classification. Tier governs
# reconciler behaviour: cold = stop when idle, hot = keep running.
#
# Add a new managed service by: (1) dropping its compose snippet in
# system/docker/services/<name>.yml, (2) adding an entry here. Phase 2.3 ships
# this catalogue with two starter entries; M4 fills in the rest.
SERVICE_CATALOGUE: dict[str, dict[str, str]] = {
    "metabase": {"tier": "hot",  "display_name": "Metabase"},
    "pgadmin":  {"tier": "cold", "display_name": "pgAdmin"},
}
