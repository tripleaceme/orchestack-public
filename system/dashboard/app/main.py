"""OrcheStack dashboard — phase 3.1 skeleton.

What this phase ships:
  - A FastAPI app that renders one page at `/` (the home page).
  - One HTMX fragment endpoint at `/api/dashboard/health` that proxies to
    the orchestrator's `GET /api/health` and returns the result formatted
    as HTML.
  - A Docker healthcheck endpoint at `/healthz` that confirms the dashboard
    itself is alive (separate from the orchestrator's health).

What this phase does NOT yet ship (lands in later phases):
  - Service grid (phase 3.2)
  - Session check-ins (phase 3.3)
  - Audit log + pinning (phase 3.4)
  - Authentication (phase 3.5)

URL handling: Traefik strips the `/app` prefix before forwarding requests
to this container. We pass `root_path="/app"` to FastAPI so its URL
generation (Jinja2's `url_for`) reconstructs the full external URL —
internal routes stay at `/`, `/api/dashboard/*`, but HTML links go out as
`/app/`, `/app/api/dashboard/*`. One setting handles the asymmetry.

See OrcheStack/design/m3-dashboard.md for the architecture overview.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates

# ---------- Configuration --------------------------------------------------
ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL", "http://orchestack-orchestrator:8000"
)
LOG_LEVEL = os.environ.get("DASHBOARD_LOG_LEVEL", "info").upper()

# `root_path=/app` because Traefik strips `/app` before forwarding. With
# this, FastAPI's URL generation correctly reconstructs the external URL.
ROOT_PATH = os.environ.get("DASHBOARD_ROOT_PATH", "/app")

# ---------- Logging --------------------------------------------------------
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("dashboard")

# ---------- Templates ------------------------------------------------------
TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

# ---------- App ------------------------------------------------------------
app = FastAPI(
    title="OrcheStack dashboard",
    description="Administrator UI. Phase 3.1 — skeleton.",
    version="0.1.0",
    root_path=ROOT_PATH,
    # Disable interactive docs in production; pure presentation app.
    docs_url=None,
    redoc_url=None,
)


@app.on_event("startup")
async def on_startup() -> None:
    log.info(
        "orchestack-dashboard phase=3.1 root_path=%s orchestrator=%s — ready",
        ROOT_PATH, ORCHESTRATOR_URL,
    )


# ---------- /healthz (container-level health, separate from orchestrator) --
@app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
async def healthz() -> str:
    """Liveness check for Docker's HEALTHCHECK directive.

    Deliberately does NOT call the orchestrator — we want this to succeed
    even when the orchestrator is unreachable, so the dashboard container
    itself stays healthy and can display a useful "orchestrator
    unreachable" UI to operators rather than being marked unhealthy and
    cycled by Docker.
    """
    return "ok\n"


# ---------- Home page ------------------------------------------------------
@app.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request) -> HTMLResponse:
    """Render the dashboard home page.

    At phase 3.1 this is a minimal page: header + an HTMX-powered card
    that polls the orchestrator's health endpoint. The card is the proof
    that (a) the dashboard image runs, (b) Traefik routes /app/ to it,
    and (c) the dashboard can reach the orchestrator over the internal
    Docker network.
    """
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "page_title": "Home"},
    )


# ---------- HTMX fragment endpoints ----------------------------------------
@app.get("/api/dashboard/health", response_class=HTMLResponse,
          name="health_fragment")
async def health_fragment(request: Request) -> HTMLResponse:
    """Proxy to the orchestrator's /api/health, render as an HTML fragment.

    HTMX swaps this fragment into the page on a timer. The fragment is
    deliberately small — just the connection status + the orchestrator's
    subsystem checks. The full page never re-renders.

    Failure handling: if the orchestrator is unreachable, we still return
    200 with a fragment that says "orchestrator unreachable" — that way
    HTMX's afterRequest fires successfully and the connection indicator
    stays green for the dashboard itself, but the operator sees the
    accurate state of the orchestrator. (The orchestrator's reachability
    is a different signal from the dashboard's reachability.)
    """
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            response = await client.get(f"{ORCHESTRATOR_URL}/api/health")
            health = response.json()
            reachable = True
    except (httpx.HTTPError, ValueError) as e:
        log.warning("orchestrator health proxy failed: %s", e)
        health = {"ok": False, "error": str(e)}
        reachable = False

    return templates.TemplateResponse(
        "_health_fragment.html",
        {
            "request": request,
            "reachable": reachable,
            "health": health,
        },
    )
