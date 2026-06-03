"""OrcheStack dashboard — phases 3.1 → 3.2.

Phase 3.1 shipped:
  - FastAPI skeleton + base.html
  - /healthz (container liveness, NOT orchestrator)
  - /api/dashboard/health → orchestrator health proxy

Phase 3.2 adds:
  - GET /api/dashboard/services/grid — full grid fragment (auto-refresh)
  - POST /api/dashboard/services/{name}/start → returns updated card
  - POST /api/dashboard/services/{name}/stop  → returns updated card

What this phase does NOT yet ship (lands in later phases):
  - Session check-ins (phase 3.3)
  - Audit log + pinning (phase 3.4)
  - Authentication (phase 3.5)
  - Compiled Tailwind, polish (phase 3.6)

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

from .orchestrator_client import OrchestratorClient

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

# ---------- Orchestrator client (shared) ----------------------------------
# One instance for the whole app — the client itself is stateless beyond
# its base_url, and creates a fresh httpx.AsyncClient per call.
orchestrator = OrchestratorClient(ORCHESTRATOR_URL)

# ---------- App ------------------------------------------------------------
app = FastAPI(
    title="OrcheStack dashboard",
    description="Administrator UI. Phase 3.2 — service grid + actions.",
    version="0.2.0",
    root_path=ROOT_PATH,
    # Disable interactive docs in production; pure presentation app.
    docs_url=None,
    redoc_url=None,
)


@app.on_event("startup")
async def on_startup() -> None:
    log.info(
        "orchestack-dashboard phase=3.2 root_path=%s orchestrator=%s — ready",
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

    Server-rendered shell + an HTMX grid that polls for live service
    state. First paint shows the empty grid (with "Loading…") and HTMX
    fills it in ~50ms later.
    """
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "page_title": "Home"},
    )


# ---------- HTMX fragment: orchestrator health ----------------------------
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
        health = await orchestrator.health()
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


# ---------- HTMX fragment: service grid -----------------------------------
@app.get("/api/dashboard/services/grid", response_class=HTMLResponse,
          name="services_grid")
async def services_grid_fragment(request: Request) -> HTMLResponse:
    """Render the full service grid.

    Called every 10s by HTMX's `hx-trigger="every 10s"`. Returns the
    whole grid's innerHTML — the parent div is preserved by HTMX, so
    only the contents (the cards themselves) flicker on refresh.

    Error handling: if the orchestrator is unreachable, we render a
    single banner via the same fragment template. The grid container
    keeps polling and recovers automatically once the orchestrator is
    back. We deliberately do NOT cache the last good response — showing
    stale state during an outage is worse than showing the outage.
    """
    try:
        data = await orchestrator.list_services()
        services = data.get("services", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("orchestrator list_services failed: %s", e)
        services = []
        error = str(e)

    return templates.TemplateResponse(
        "_service_grid_fragment.html",
        {
            "request": request,
            "services": services,
            "error": error,
        },
    )


# ---------- HTMX action: start a service ----------------------------------
@app.post("/api/dashboard/services/{name}/start", response_class=HTMLResponse,
           name="start_service_action")
async def start_service_action(request: Request, name: str) -> HTMLResponse:
    """Tell the orchestrator to start `name`, return the updated card.

    Two HTTP calls per request: POST start, then GET list to fetch the
    new state. We could optimise by trusting the start response's
    `{state: "running"}` payload, but re-listing keeps the card's
    container name accurate — that comes from `docker ps`, not from the
    catalogue.

    Error path: if start fails (orchestrator down, compose error), we
    re-fetch the service to render it in its actual current state, then
    rely on the visual cue of the still-stopped dot. Phase 3.4 will add
    a toast/banner for explicit error feedback.
    """
    try:
        await orchestrator.start_service(name)
    except httpx.HTTPError as e:
        log.warning("start_service(%s) failed: %s", name, e)

    return await _render_card(request, name)


# ---------- HTMX action: stop a service -----------------------------------
@app.post("/api/dashboard/services/{name}/stop", response_class=HTMLResponse,
           name="stop_service_action")
async def stop_service_action(request: Request, name: str) -> HTMLResponse:
    """Tell the orchestrator to stop `name`, return the updated card."""
    try:
        await orchestrator.stop_service(name)
    except httpx.HTTPError as e:
        log.warning("stop_service(%s) failed: %s", name, e)

    return await _render_card(request, name)


# ---------- Helper --------------------------------------------------------
async def _render_card(request: Request, name: str) -> HTMLResponse:
    """Look up a single service by name and render its card fragment.

    Used by both start and stop action endpoints — they both need to
    return the updated card after their respective state change. If the
    service has vanished from the catalogue (shouldn't happen) we render
    an inert stopped+unmanaged card so HTMX still gets valid HTML to
    swap in.
    """
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError as e:
        log.warning("re-fetch after action failed for %s: %s", name, e)
        svc = None

    if svc is None:
        svc = {
            "name": name,
            "display_name": name,
            "tier": "cold",
            "layer": None,
            "state": "stopped",
            "container": None,
            "managed": False,
        }

    return templates.TemplateResponse(
        "_service_card_fragment.html",
        {"request": request, "service": svc},
    )
