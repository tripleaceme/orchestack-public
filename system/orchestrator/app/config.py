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
# The orchestrator NEVER connects to the customer warehouse DB.
DB_HOST: str = os.environ.get("POSTGRES_HOST", "orchestack-postgres")
DB_PORT: int = _int("POSTGRES_PORT", 5432)
DB_USER: str = os.environ.get("POSTGRES_USER", "orchestack")
DB_PASSWORD: str = os.environ.get("POSTGRES_PASSWORD", "")
DB_NAME: str = os.environ.get("POSTGRES_DB", "orchestack")

# Pool size: 5 is enough for the reconciler tick + a handful of concurrent
# API requests. Not a high-QPS service. Bump if we add dashboard features
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

# Path INSIDE the orchestrator container where the operator's `.env` is
# bind-mounted (see system/docker/docker-compose.yml). Passed to every
# `docker compose --env-file <path>` invocation so per-service compose
# snippets can interpolate ${ORCHESTACK_DB_PASSWORD}, ${WAREHOUSE_DB_*},
# etc. without those variables having to live in the orchestrator's own
# process environment. Override via ORCHESTRATOR_ENV_FILE if the mount
# location ever changes.
ENV_FILE: str = os.environ.get("ORCHESTRATOR_ENV_FILE", "/etc/orchestack/.env")

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
    # Hot vs cold per design/m2-orchestrator.md §3: hot-tier services serve
    # scheduled workloads (Metabase emails, Airflow schedulers); cold-tier
    # services are operator-on-demand (open, use, close).
    "metabase":     {"tier": "hot",  "display_name": "Metabase",            "layer": "bi",           "managed": True},
    # pgAdmin is COLD-tier — typical use is "open, debug a query, close."
    # Auto-pinning on Open (4hr default) keeps it warm for the operator's
    # session; the reconciler stops it when the pin expires AND no
    # active sessions remain. Operators who want it always-on can
    # manually extend the pin from the service detail page.
    "pgadmin":      {"tier": "cold", "display_name": "pgAdmin",             "layer": "admin-ui",     "managed": True},
    # M4 services. All managed=True as of M4 ship; pre-start hooks
    # provision per-service DBs/roles per design/m4-multi-db.md.
    # MinIO's console doesn't support subpath deployment reliably (their
    # 2024+ SPA assumes /api/v1 at root). external_url tells the
    # dashboard's Open button to send the operator to localhost:9001
    # instead of /app/minio. The compose snippet exposes 9001 on the
    # host accordingly. The S3 API on 9000 stays internal to the
    # docker network — Airbyte/dbt reach it as orchestack-minio:9000.
    "minio":        {"tier": "hot",  "display_name": "MinIO",               "layer": "data-lake",    "managed": True, "external_url": "http://{host}:9001"},
    # dbt is the only multi-action service so far. Two operator
    # workflows that genuinely deserve their own buttons:
    #
    #   - DOCS: read-only lineage + model + test documentation. What
    #     stakeholders see in demos and what analytics engineers use
    #     to confirm a column's meaning.
    #   - CLI: in-browser bash terminal (via ttyd) at /usr/app/dbt
    #     for production troubleshooting — "Airflow flagged a model
    #     failure, let me dbt run --select that_model to repro and
    #     iterate." Without this the operator's only path is SSH +
    #     docker exec.
    #
    # Both run concurrently in the same container — see
    # system/docker/services/dbt.yml. The catalogue's `actions: []`
    # field tells the dashboard to render multiple Open buttons
    # stacked on the same card. Services that omit `actions` keep
    # working via the existing `external_url` single-button flow.
    "dbt": {
        "tier": "cold", "display_name": "dbt Core",
        "layer": "transformation", "managed": True,
        "actions": [
            {
                "key": "docs",
                "label": "Open Docs",
                "external_url": "http://{host}:8002",
                # ready_probe is a (port, path) tuple inside the
                # service container. The dashboard's /ready handler
                # looks up per-action probes for multi-action
                # services. None means "use the default state==running
                # check" — matches how single-action services have
                # always behaved.
                "ready_probe": (8080, "/index.html"),
            },
            {
                "key": "cli",
                "label": "Open Terminal",
                # ttyd is served at /app/dbt-terminal via Traefik
                # subpath routing (it honors --base-path cleanly,
                # unlike Airbyte/MinIO). That means the OrcheStack
                # auth forward-auth chain gates the terminal too —
                # no separate credentials, no exposed-shell-port.
                "external_url": "http://{host}/app/dbt-terminal/",
                "ready_probe": (7681, "/"),
            },
        ],
    },
    # Great Expectations: same dual-action shape as dbt. Open Data Docs
    # opens the generated HTML site (validations + expectations + suite
    # browser); Open Terminal opens ttyd at /usr/great_expectations
    # for `great_expectations suite edit` / `checkpoint run` work.
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
    # whose React SPAs emit absolute-root asset paths). So no
    # external_url override — the default ROOT_PATH/airflow flow
    # resolves to the correct URL via the Traefik labels in
    # services/airflow.yml. Single action (Open Webserver) is enough
    # for the academic ship; a Terminal action via ttyd can be added
    # later if engineers ask for it (the official apache/airflow image
    # would need a ttyd-bundled custom image, same pattern as GE).
    "airflow":      {"tier": "hot",  "display_name": "Apache Airflow",      "layer": "orchestration","managed": True},
    # Airbyte's webapp emits absolute-root asset paths (/assets/index-XXX.js)
    # — same subpath-incompatibility class as MinIO. The compose snippet
    # exposes the webapp on host port 8001; external_url tells the
    # dashboard's Open button to send operators there instead of to
    # the broken /app/airbyte subpath.
    "airbyte":      {"tier": "hot",  "display_name": "Airbyte",             "layer": "ingestion",    "managed": True, "external_url": "http://{host}:8001"},
    "openmetadata": {"tier": "cold", "display_name": "OpenMetadata",        "layer": "governance",   "managed": True},
    # PostgreSQL is special — it's part of the base control plane (already
    # running as orchestack-postgres), so the orchestrator does NOT start
    # or stop it via compose; the base stack owns its lifecycle. The
    # `control_plane` flag tells the dashboard:
    #   - report state="running" unconditionally (the base container is
    #     always up; if it weren't, the dashboard itself couldn't render),
    #   - render no Start/Stop buttons (you don't stop the platform DB),
    #   - point Open at pgAdmin's data_warehouse view since PostgreSQL
    #     has no UI of its own.
    # external_url uses the host's pgAdmin entry point so clicking Open
    # lands the operator in an SQL UI ready to query the warehouse.
    "postgresql":   {"tier": "hot",  "display_name": "PostgreSQL",          "layer": "warehouse",    "managed": True,  "control_plane": True, "external_url": "http://{host}/app/pgadmin"},
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
