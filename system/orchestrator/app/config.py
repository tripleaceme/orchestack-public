"""Centralised configuration — read once from the environment."""

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
# The orchestrator NEVER connects to the customer warehouse DB.
DB_HOST: str = os.environ.get("POSTGRES_HOST", "orchestack-postgres")
DB_PORT: int = _int("POSTGRES_PORT", 5432)
DB_USER: str = os.environ.get("POSTGRES_USER", "orchestack_admin")
DB_PASSWORD: str = os.environ.get("POSTGRES_PASSWORD", "")
DB_NAME: str = os.environ.get("POSTGRES_DB", "orchestack_db")

DB_POOL_MIN: int = _int("ORCHESTRATOR_DB_POOL_MIN", 1)
DB_POOL_MAX: int = _int("ORCHESTRATOR_DB_POOL_MAX", 5)

# ----- Reconciler -----------------------------------------------------------
RECONCILE_INTERVAL: int = _int("ORCHESTRATOR_RECONCILE_INTERVAL", 30)

IDLE_THRESHOLD: int = _int("ORCHESTRATOR_IDLE_THRESHOLD", 600)

# Grace period after start during which the reconciler won't shut a
# service down even if no sessions exist yet. Prevents the race where a
# session POST and a reconciler tick arrive in the wrong order right after
# start-up.
START_GRACE: int = _int("ORCHESTRATOR_START_GRACE", 60)

# How recent a session has to be (last_seen_at) to count as "active" during
# the reconciler tick. Gives clients time to checkin even after brief
# network blips.
SESSION_ACTIVE_WINDOW: int = _int("ORCHESTRATOR_SESSION_ACTIVE_WINDOW", 300)

# ----- Service compose snippets --------------------------------------------
SERVICES_DIR: str = os.environ.get("ORCHESTRATOR_SERVICES_DIR", "/services")

# Distinct from the base "orchestack" project so that the base control plane
# and the cold-tier services have separate `compose ps` views.
COMPOSE_PROJECT_PREFIX: str = "orchestack-service"

# Path INSIDE the orchestrator container where the operator's `.env` is
# bind-mounted. Passed to every `docker compose --env-file <path>` invocation
# so per-service compose snippets can interpolate ${ORCHESTACK_DB_PASSWORD},
# ${WAREHOUSE_DB_*}, etc. without those variables having to live in the
# orchestrator's own process environment.
ENV_FILE: str = os.environ.get("ORCHESTRATOR_ENV_FILE", "/etc/orchestack/.env")

# ----- Default user ---------------------------------------------------------
# Every FK-constrained insert (service_sessions, service_pinning, audit_log)
# needs a valid platform.users.id. Seeded id=1 system account (see
# 20-seed-default-user.sql) is the actor for orchestrator operations.
DEFAULT_USER_ID: int = 1

# ----- Service catalogue ----------------------------------------------------
# `managed` (True iff we have a compose snippet at services/<name>.yml and
# can actually start/stop it).
SERVICE_CATALOGUE: dict[str, dict[str, object]] = {
    "metabase":     {"tier": "hot",  "display_name": "Metabase",            "layer": "bi",           "managed": True},
    "pgadmin":      {"tier": "hot",  "display_name": "pgAdmin",             "layer": "admin-ui",     "managed": True, "requires": ["postgresql"]},
    # MinIO's console doesn't support subpath deployment reliably (their
    # 2024+ SPA assumes /api/v1 at root). external_url sends the operator
    # to localhost:9001 instead of /app/minio. The S3 API on 9000 stays
    # internal to the docker network — Airbyte/dbt reach it as
    # orchestack-minio:9000.
    "minio":        {"tier": "cold", "display_name": "MinIO",               "layer": "data-lake",    "managed": True, "external_url": "http://{host}:9001"},
    "dbt": {
        "tier": "cold", "display_name": "dbt Core",
        "layer": "transformation", "managed": True,
        "actions": [
            {
                "key": "docs",
                "label": "Open Docs",
                "external_url": "http://{host}:8002",
                # ready_probe is a (port, path) tuple inside the service
                # container. None means "use the default state==running
                # check".
                "ready_probe": (8080, "/index.html"),
            },
            {
                "key": "cli",
                "label": "Open Terminal",
                # ttyd is served at /app/dbt-terminal via Traefik subpath
                # routing (it honors --base-path cleanly, unlike
                # Airbyte/MinIO). The OrcheStack auth forward-auth chain
                # gates the terminal too — no separate credentials.
                "external_url": "http://{host}/app/dbt-terminal/",
                "ready_probe": (7681, "/"),
            },
        ],
    },
    "ge": {
        "tier": "cold", "display_name": "Great Expectations",
        "layer": "quality", "managed": True,
        "actions": [
            {
                "key": "docs",
                "label": "Open Data Docs",
                "external_url": "http://{host}:8003",
                "ready_probe": (8080, "/index.html"),
            },
            {
                "key": "cli",
                "label": "Open Terminal",
                "external_url": "http://{host}/app/ge-terminal/",
                "ready_probe": (7681, "/"),
            },
        ],
    },
    # Airflow's webserver honors AIRFLOW__WEBSERVER__BASE_URL and works
    # cleanly under Traefik's /app/airflow subpath (unlike MinIO/Airbyte
    # whose React SPAs emit absolute-root asset paths), so no external_url
    # override is needed.
    # scheduling_warning surfaces as a banner on the dashboard tile +
    # service-detail page. Airflow is the canonical case: cold-tier
    # services sleep after 10 min idle, but Airflow's whole job is to
    # fire schedules at specific times. A cold Airflow can't fire an
    # 8 AM DAG because it's not running at 8 AM. The fix today is to
    # pin Airflow from the dashboard; a v0.2 feature will read DAG
    # schedules + auto-wake briefly before each scheduled run.
    "airflow":      {
        "tier": "cold", "display_name": "Apache Airflow", "layer": "orchestration", "managed": True,
        "scheduling_warning": (
            "Airflow runs cold-tier — it sleeps after 10 minutes of no "
            "activity, which means scheduled DAGs won't fire while it's "
            "asleep. Pin Airflow from the Keep warm card if you rely on "
            "scheduled DAGs."
        ),
    },
    # Airbyte's webapp emits absolute-root asset paths (/assets/index-XXX.js)
    # — same subpath-incompatibility class as MinIO. external_url sends
    # operators to host port 8001 instead of the broken /app/airbyte subpath.
    "airbyte":      {"tier": "cold", "display_name": "Airbyte",             "layer": "ingestion",    "managed": True, "external_url": "http://{host}:8001"},
    # OpenMetadata: React SPA emits absolute-root asset paths that don't
    # survive Traefik's stripprefix middleware. WEB_CONF_URI is supposed to
    # control this in OM 1.6.x but doesn't — the webpack publicPath is
    # hardcoded to /. ready_probe targets the operator-facing API on 8585
    # (the /healthcheck endpoint on 8586 is the admin port — green there
    # means the JVM is up but doesn't say anything about whether /api/v1/*
    # requests can be served).
    "openmetadata": {
        "tier": "cold", "display_name": "OpenMetadata", "layer": "governance", "managed": True,
        "external_url": "http://{host}:8585",
        "ready_probe": (8585, "/api/v1/system/version"),
    },
    # PostgreSQL is part of the base control plane (orchestack-postgres),
    # so the orchestrator does NOT start/stop it via compose. The
    # `control_plane` flag tells the dashboard to report state="running"
    # unconditionally, render no Start/Stop buttons, and point Open at
    # pgAdmin since PostgreSQL has no UI of its own.
    "postgresql":   {"tier": "hot",  "display_name": "PostgreSQL",          "layer": "warehouse",    "managed": True,  "control_plane": True, "external_url": "http://{host}/app/pgadmin"},
}

# Wizard layer keys → platform.installed_services.layer CHECK-constraint values.
# Wizard uses snake_case / short forms; schema uses kebab-case. Mapping here
# rather than changing either side because both are stable contracts.
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

def tool_name_to_catalogue_key(display: str) -> str | None:
    """Map wizard display name (case-insensitive, parentheticals stripped) to catalogue key."""
    norm = display.strip().lower()
    if "(" in norm:
        norm = norm.split("(", 1)[0].strip()
    for key, meta in SERVICE_CATALOGUE.items():
        if meta["display_name"].lower() == norm:
            return key
    if norm in SERVICE_CATALOGUE:
        return norm
    # "custom python" is intentionally NOT mapped: the wizard's "Custom
    # Python (Airflow DAGs)" option means "I'll write my own DAGs, no
    # separate ingestion service" — there's nothing to install.
    aliases = {"apache airflow": "airflow", "dbt core": "dbt"}
    return aliases.get(norm)
