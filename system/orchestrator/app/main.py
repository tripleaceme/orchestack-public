"""OrcheStack orchestrator — FastAPI control-plane service.

This module is the entry point of the orchestrator container, the service that
implements OrcheStack's event-driven hot/cold tier orchestration. It replaces
the alpine heartbeat stub that ran during M1.

Phase 2.1 (this file) is intentionally minimal: a FastAPI app with one
endpoint, /api/health, used by the container's HEALTHCHECK and (later) by
Streamlit's status pane. The reconciler loop, wizard-handoff endpoint,
service-control endpoints, and session API all land in subsequent phases —
see design/m2-orchestrator.md for the full plan.

Why FastAPI: async-friendly, integrates cleanly with asyncio (we need a
long-running background task for the reconciler), uses pydantic for request
validation (which we'll lean on heavily once the wizard-handoff endpoint
exists), and has minimal boilerplate. Alternatives (Flask, aiohttp, starlette
directly) all work but FastAPI is the smoothest path for the surface we're
about to build.
"""

from __future__ import annotations

import logging
import os

from fastapi import FastAPI

# Configuration via env vars. Keep this minimal at phase 2.1 — every config
# value is documented in OrcheStack/system/docker/.env.example so operators
# can see what's tunable without reading code.
LOG_LEVEL = os.environ.get("ORCHESTRATOR_LOG_LEVEL", "info").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("orchestrator")

app = FastAPI(
    title="OrcheStack orchestrator",
    description=(
        "Control-plane service for OrcheStack. Implements event-driven hot/cold "
        "tier service orchestration. See design/m2-orchestrator.md for the "
        "full design and design/api.md for the OpenAPI surface."
    ),
    version="0.1.0",
    # OpenAPI / Swagger UI lives at /orchestrator/docs once Traefik prefixes
    # this service at /orchestrator. During local dev (no Traefik) it's at
    # http://localhost:8000/docs.
)


@app.on_event("startup")
async def on_startup() -> None:
    """Log a single startup banner so operators can see we came up cleanly."""
    log.info(
        "orchestack-orchestrator phase=2.1 log_level=%s — ready",
        LOG_LEVEL,
    )


@app.get("/api/health")
async def health() -> dict[str, object]:
    """Return readiness state for the container's HEALTHCHECK + Streamlit.

    At phase 2.1 the only thing this service does is exist, so health is
    binary: if FastAPI is responding, we're healthy. Future phases will
    add postgres + docker socket-proxy connectivity checks here.

    Schema kept stable across phases — fields will be added to the response
    dict but never removed, so clients (Streamlit) can ignore unknown keys.
    """
    return {
        "ok": True,
        "service": "orchestack-orchestrator",
        "version": app.version,
        "phase": "2.1",
        # Subsystem checks land in later phases. Documented here so the
        # contract is visible — they'll appear with bool values once the
        # corresponding code lands. Clients should treat absence as "not
        # yet checked" rather than "failed".
        "checks": {
            # "postgres": ...  (phase 2.4)
            # "docker":   ...  (phase 2.2)
        },
    }
