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

# ----- Default user (until M3 introduces real session cookies) --------------
# Every FK-constrained insert (service_sessions, service_pinning, audit_log)
# needs a valid platform.users.id. The wizard saves profile data to
# localStorage only — no user row exists during M2. We use this seeded
# id=1 system account (see 20-seed-default-user.sql) as the actor for all
# orchestrator operations until M3 wires up real per-request auth.
DEFAULT_USER_ID: int = 1

# ----- Service catalogue ----------------------------------------------------
# Maps the symbolic service name to: tier (hot/cold), display name, layer
# (must match the platform.installed_services CHECK constraint), and
# `managed` (True iff we have a compose snippet at services/<name>.yml and
# can actually start/stop it; False = registered in installed_services but
# orchestration deferred to M4).
SERVICE_CATALOGUE: dict[str, dict[str, object]] = {
    # Managed services — orchestrator can start/stop these via compose snippets.
    "metabase":     {"tier": "hot",  "display_name": "Metabase",            "layer": "bi",           "managed": True},
    "pgadmin":      {"tier": "cold", "display_name": "pgAdmin",             "layer": "admin-ui",     "managed": True},
    # Registered-only — wizard can pick these but the orchestrator can't yet
    # start/stop them (no compose snippet exists, that's M4 work).
    "airbyte":      {"tier": "cold", "display_name": "Airbyte",             "layer": "ingestion",    "managed": False},
    "airflow":      {"tier": "hot",  "display_name": "Apache Airflow",      "layer": "orchestration","managed": False},
    "dbt":          {"tier": "cold", "display_name": "dbt Core",            "layer": "transformation","managed": False},
    "minio":        {"tier": "hot",  "display_name": "MinIO",               "layer": "data-lake",    "managed": False},
    "ge":           {"tier": "cold", "display_name": "Great Expectations",  "layer": "quality",      "managed": False},
    "openmetadata": {"tier": "cold", "display_name": "OpenMetadata",        "layer": "governance",   "managed": False},
    # PostgreSQL is special — it's part of the base control plane (already
    # running as orchestack-postgres), but the wizard's warehouse layer lets
    # the operator pick it as the analytical warehouse too. Catalogue entry
    # exists so installed_services records the choice; managed=False because
    # the orchestrator doesn't start/stop the base postgres.
    "postgresql":   {"tier": "hot",  "display_name": "PostgreSQL",          "layer": "warehouse",    "managed": False},
}

# Wizard layer keys → platform.installed_services.layer CHECK-constraint values.
# The wizard uses snake_case / short forms; the schema uses kebab-case for
# multi-word layers. Mapping here rather than changing either side because
# both are stable contracts referenced in many places.
WIZARD_LAYER_TO_SCHEMA: dict[str, str] = {
    "ingestion":    "ingestion",
    "orchestration": "orchestration",
    "warehouse":    "warehouse",
    "lake":         "data-lake",
    "quality":      "quality",
    "governance":   "governance",
    "bi":           "bi",
    "admin_ui":     "admin-ui",
}

# Wizard display name → catalogue key. The wizard stores "Metabase",
# "Airbyte", "Apache Airflow", etc. — we need to map back to the lowercase
# catalogue key. Case-insensitive, strips trailing parentheticals.
def tool_name_to_catalogue_key(display: str) -> str | None:
    norm = display.strip().lower()
    # Strip a trailing parenthetical like "(recommended)" or "(Airflow DAGs)"
    if "(" in norm:
        norm = norm.split("(", 1)[0].strip()
    # Match by display_name (case-insensitive)
    for key, meta in SERVICE_CATALOGUE.items():
        if meta["display_name"].lower() == norm:
            return key
    # Fallback: maybe it's already a catalogue key
    if norm in SERVICE_CATALOGUE:
        return norm
    # Special cases — wizard display names that don't lowercase to a key.
    # "custom python" is intentionally NOT mapped: the wizard's "Custom
    # Python (Airflow DAGs)" option means "I'll write my own DAGs, no
    # separate ingestion service" — there's nothing to install.
    aliases = {"apache airflow": "airflow", "dbt core": "dbt"}
    return aliases.get(norm)
