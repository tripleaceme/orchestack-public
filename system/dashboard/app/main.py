"""OrcheStack dashboard — phases 3.1 → 3.6.

Routes in this file are grouped by concern:
    Container liveness           /healthz
    Pages                        /, /sessions, /audit, /services/{name}, /login
    HTMX fragment endpoints      /api/dashboard/<...>
    Session lifecycle            /api/dashboard/services/<name>/open,
                                 /api/dashboard/sessions/<token>/heartbeat
                                 /api/dashboard/sessions/<token>/close
    Auth                         /api/dashboard/auth/login, /logout

URL handling: Traefik strips the `/app` prefix before forwarding to this
container. We pass `root_path="/app"` to FastAPI so url_for() reconstructs
the full external URL; internal routes stay at `/`, `/api/dashboard/*`.

See OrcheStack/design/m3-dashboard.md for the architecture overview.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .orchestrator_client import OrchestratorClient

# ---------- Configuration --------------------------------------------------
ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_URL", "http://orchestack-orchestrator:8000"
)
LOG_LEVEL = os.environ.get("DASHBOARD_LOG_LEVEL", "info").upper()
ROOT_PATH = os.environ.get("DASHBOARD_ROOT_PATH", "/app")
SESSION_COOKIE_NAME = "orchestack_session"

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
log = logging.getLogger("dashboard")

TEMPLATE_DIR = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATE_DIR))

orchestrator = OrchestratorClient(ORCHESTRATOR_URL)

app = FastAPI(
    title="OrcheStack dashboard",
    description="Administrator UI. Phases 3.1–3.6.",
    version="0.6.0",
    root_path=ROOT_PATH,
    docs_url=None,
    redoc_url=None,
)


@app.on_event("startup")
async def on_startup() -> None:
    log.info(
        "orchestack-dashboard phase=3.6 root_path=%s orchestrator=%s — ready",
        ROOT_PATH, ORCHESTRATOR_URL,
    )


# ===========================================================================
#  Auth — current user dependency
# ===========================================================================
async def current_user(request: Request) -> dict[str, object] | None:
    """Resolve the current user from the session cookie, or None.

    Doesn't 401 — that's `require_user`'s job. This helper is used by
    page handlers that want to render different states for logged-in vs.
    not-logged-in users (e.g. the header showing 'Signed in as X').
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    try:
        return await orchestrator.auth_me(cookie)
    except httpx.HTTPError:
        return None


async def require_user(request: Request) -> dict[str, object]:
    """Guard route dependency — 401-redirects to /app/login if not signed in."""
    user = await current_user(request)
    if user is None:
        # We can't return a redirect directly from a Depends — raise an
        # HTTPException that the global exception handler turns into a
        # redirect. The path the user wanted is preserved via `next`.
        raise HTTPException(
            status_code=307,
            detail="login_required",
            headers={"Location": f"{ROOT_PATH}/login?next={request.url.path}"},
        )
    return user


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Convert 307 login_required exceptions into real RedirectResponses."""
    if exc.status_code == 307 and exc.detail == "login_required":
        return RedirectResponse(url=exc.headers["Location"], status_code=307)
    # Fall through to FastAPI's default JSON response for everything else.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=dict(exc.headers or {}),
    )


# ===========================================================================
#  Container liveness
# ===========================================================================
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


# ===========================================================================
#  Pages
# ===========================================================================
@app.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """Service grid + platform health card."""
    return templates.TemplateResponse(
        "index.html",
        {"request": request, "page_title": "Home", "user": user},
    )


@app.get("/sessions", response_class=HTMLResponse, name="sessions_page")
async def sessions_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/sessions` — live table of all open service sessions."""
    return templates.TemplateResponse(
        "sessions.html",
        {"request": request, "page_title": "Sessions", "user": user},
    )


@app.get("/audit", response_class=HTMLResponse, name="audit_page")
async def audit_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/audit` — paginated audit log with filters."""
    return templates.TemplateResponse(
        "audit.html",
        {"request": request, "page_title": "Audit log", "user": user},
    )


@app.get("/credentials", response_class=HTMLResponse, name="credentials_page")
async def credentials_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/credentials` — admin view for reading + updating .env variables.

    Sensitive values (passwords, secrets, tokens, keys) are masked until
    the operator clicks Reveal on a specific row. Read-only variables
    (image tags, the platform DB password) are rendered without an Edit
    affordance.
    """
    return templates.TemplateResponse(
        "credentials.html",
        {"request": request, "page_title": "Credentials", "user": user},
    )


@app.get("/api/dashboard/credentials/table", response_class=HTMLResponse,
          name="credentials_table_fragment")
async def credentials_table_fragment(request: Request, reveal: bool = False) -> HTMLResponse:
    """HTMX fragment — the credentials table itself, with optional reveal."""
    try:
        data = await orchestrator.list_credentials(reveal=reveal)
        credentials = data.get("credentials", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("list_credentials failed: %s", e)
        credentials = []
        error = str(e)
    return templates.TemplateResponse(
        "_credentials_table_fragment.html",
        {
            "request": request,
            "credentials": credentials,
            "reveal": reveal,
            "error": error,
        },
    )


@app.post("/api/dashboard/credentials/{key}",
           response_class=HTMLResponse, name="credentials_update_action")
async def credentials_update_action(
    request: Request, key: str, value: str = Form(...),
    user=Depends(require_user),
) -> HTMLResponse:
    """Update one .env variable + re-render its table row."""
    try:
        await orchestrator.update_credential(
            key, value, actor_user_id=user.get("user_id"),
        )
    except httpx.HTTPError as e:
        log.warning("update_credential(%s) failed: %s", key, e)
    # Re-render the full table so the row shows its new state (masked
    # again, with a brief "Updated" indicator handled in the template).
    try:
        data = await orchestrator.list_credentials(reveal=False)
        credentials = data.get("credentials", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        credentials = []
        error = str(e)
    return templates.TemplateResponse(
        "_credentials_table_fragment.html",
        {
            "request": request,
            "credentials": credentials,
            "reveal": False,
            "error": error,
            "updated_key": key,
        },
    )


@app.get("/services/{name}", response_class=HTMLResponse, name="service_detail")
async def service_detail(
    request: Request, name: str, user=Depends(require_user)
) -> HTMLResponse:
    """`/app/services/{name}` — per-service detail page with pin toggle."""
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError:
        svc = None
    return templates.TemplateResponse(
        "service_detail.html",
        {
            "request": request,
            "page_title": svc["display_name"] if svc else name,
            "service": svc,
            "service_name": name,
            "user": user,
        },
    )


@app.get("/login", response_class=HTMLResponse, name="login_page")
async def login_page(request: Request, next: str = "/") -> HTMLResponse:
    """`/app/login` — username/password form."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "page_title": "Sign in", "next": next, "error": None},
    )


# ===========================================================================
#  HTMX fragment: orchestrator health
# ===========================================================================
@app.get("/api/dashboard/health", response_class=HTMLResponse,
          name="health_fragment")
async def health_fragment(request: Request) -> HTMLResponse:
    """Proxy to the orchestrator's /api/health, render as an HTML fragment.

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
        {"request": request, "reachable": reachable, "health": health},
    )


# ===========================================================================
#  HTMX fragment: service grid
# ===========================================================================
@app.get("/api/dashboard/services/grid", response_class=HTMLResponse,
          name="services_grid")
async def services_grid_fragment(request: Request) -> HTMLResponse:
    """Render the full service grid (called every 10s by HTMX)."""
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
        {"request": request, "services": services, "error": error},
    )


# ===========================================================================
#  HTMX action: start a service
# ===========================================================================
@app.post("/api/dashboard/services/{name}/start", response_class=HTMLResponse,
           name="start_service_action")
async def start_service_action(request: Request, name: str) -> HTMLResponse:
    """Tell the orchestrator to start `name`, return the updated card.

    Error path: if start fails (orchestrator down, compose error), we
    re-fetch the service to render its actual current state — the visual
    cue of the still-stopped dot is the error feedback. Phase 3.6 may add
    a toast for explicit error feedback.
    """
    try:
        await orchestrator.start_service(name)
    except httpx.HTTPError as e:
        log.warning("start_service(%s) failed: %s", name, e)
    return await _render_card(request, name)


# ===========================================================================
#  HTMX action: stop a service
# ===========================================================================
@app.post("/api/dashboard/services/{name}/stop", response_class=HTMLResponse,
           name="stop_service_action")
async def stop_service_action(request: Request, name: str) -> HTMLResponse:
    """Tell the orchestrator to stop `name`, return the updated card."""
    try:
        await orchestrator.stop_service(name)
    except httpx.HTTPError as e:
        log.warning("stop_service(%s) failed: %s", name, e)
    return await _render_card(request, name)


# ===========================================================================
#  HTMX action: pin / unpin
# ===========================================================================
@app.post("/api/dashboard/services/{name}/pin", response_class=HTMLResponse,
           name="pin_service_action")
async def pin_service_action(request: Request, name: str) -> HTMLResponse:
    try:
        await orchestrator.pin_service(name)
    except httpx.HTTPError as e:
        log.warning("pin_service(%s) failed: %s", name, e)
    return await _render_pin_button(request, name)


@app.get("/api/dashboard/services/{name}/pin-button", response_class=HTMLResponse,
          name="pin_initial_button")
async def pin_initial_button(request: Request, name: str) -> HTMLResponse:
    """Initial render of the pin button — used on the service detail page
    when it first loads. The action endpoints (POST/DELETE) reuse the
    same fragment template so subsequent state changes look identical."""
    return await _render_pin_button(request, name)


@app.delete("/api/dashboard/services/{name}/pin", response_class=HTMLResponse,
            name="unpin_service_action")
async def unpin_service_action(request: Request, name: str) -> HTMLResponse:
    try:
        await orchestrator.unpin_service(name)
    except httpx.HTTPError as e:
        log.warning("unpin_service(%s) failed: %s", name, e)
    return await _render_pin_button(request, name)


# ===========================================================================
#  Session lifecycle (Open / heartbeat / close)
# ===========================================================================
@app.post("/api/dashboard/services/{name}/open", name="open_service_session")
async def open_service_session(name: str) -> JSONResponse:
    """Open an orchestrator session against `name` and return the tool URL.

    Returns JSON `{token, tool_url, service}`. The dashboard's client-side
    JS stores `token` in localStorage so the heartbeat ticker can refresh
    it, and opens `tool_url` in a new tab.

    The `tool_url` is constructed from the dashboard's ROOT_PATH (so it
    works regardless of where Traefik mounts the dashboard) plus the
    service name — e.g. `/app/metabase`. The actual tool container's
    Traefik label decides what's there; the dashboard doesn't care.
    """
    try:
        result = await orchestrator.open_session(name, auto_start=True)
    except httpx.HTTPError as e:
        log.warning("open_session(%s) failed: %s", name, e)
        return JSONResponse(
            status_code=502,
            content={"error": "orchestrator unreachable", "detail": str(e)},
        )
    # Tool URL: tools are mounted under the dashboard's same root.
    # /app/metabase, /app/pgadmin, etc. — Traefik handles the dispatch.
    tool_url = f"{ROOT_PATH}/{name}"
    return JSONResponse({
        "token": result.get("token"),
        "service": name,
        "tool_url": tool_url,
        "started": result.get("started", False),
    })


@app.post("/api/dashboard/sessions/{token}/heartbeat",
           name="session_heartbeat")
async def session_heartbeat(token: str) -> JSONResponse:
    """Forward a session heartbeat to the orchestrator's checkin endpoint."""
    try:
        result = await orchestrator.checkin_session(token)
    except httpx.HTTPError as e:
        log.warning("checkin(%s) failed: %s", token[:8], e)
        return JSONResponse(
            status_code=502,
            content={"error": "orchestrator unreachable"},
        )
    return JSONResponse(result)


@app.post("/api/dashboard/sessions/{token}/close", name="session_close")
async def session_close(token: str) -> Response:
    """Close a session — proxies to the orchestrator's DELETE.

    Note: POST (not DELETE) because this endpoint is the target of
    `navigator.sendBeacon()` calls from beforeunload handlers, and
    sendBeacon doesn't support DELETE. The forwarded orchestrator call
    is the real DELETE.
    """
    try:
        await orchestrator.close_session(token)
    except httpx.HTTPError as e:
        log.warning("close_session(%s) failed: %s", token[:8], e)
    return Response(status_code=204)


# ===========================================================================
#  HTMX fragment: active sessions table
# ===========================================================================
@app.get("/api/dashboard/sessions/active", response_class=HTMLResponse,
          name="sessions_active_fragment")
async def sessions_active_fragment(
    request: Request, limit: int = 20, offset: int = 0,
) -> HTMLResponse:
    """Render the active-sessions table fragment (polled every 10s).

    Page size defaults to 20 to keep the polled response small; operator
    can bump via the page-size selector on the sessions page.
    """
    try:
        data = await orchestrator.list_sessions(active=True, limit=limit, offset=offset)
        sessions = data.get("sessions", [])
        total = data.get("total", 0)
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("list_sessions failed: %s", e)
        sessions = []
        total = 0
        error = str(e)
    return templates.TemplateResponse(
        "_sessions_table_fragment.html",
        {
            "request": request,
            "sessions": sessions,
            "total": total,
            "limit": limit,
            "offset": offset,
            "error": error,
        },
    )


# ===========================================================================
#  HTMX fragment: audit log table
# ===========================================================================
@app.get("/api/dashboard/audit/table", response_class=HTMLResponse,
          name="audit_table_fragment")
async def audit_table_fragment(
    request: Request,
    event_type: str | None = None, target: str | None = None,
    since: str | None = None, until: str | None = None,
    limit: int = 20, offset: int = 0,
) -> HTMLResponse:
    """Render the audit-log table fragment with optional filters."""
    try:
        data = await orchestrator.list_audit(
            event_type=event_type, target=target,
            since=since, until=until,
            limit=limit, offset=offset,
        )
        events = data.get("events", [])
        total = data.get("total", 0)
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("list_audit failed: %s", e)
        events = []
        total = 0
        error = str(e)
    return templates.TemplateResponse(
        "_audit_table_fragment.html",
        {
            "request": request,
            "events": events, "total": total, "error": error,
            "limit": limit, "offset": offset,
            "event_type": event_type, "target": target,
            "since": since, "until": until,
        },
    )


# ===========================================================================
#  Auth — login + logout (proxies to orchestrator)
# ===========================================================================
@app.post("/api/dashboard/auth/login", name="login_action")
async def login_action(
    request: Request,
    username_or_email: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
) -> Response:
    """Forward credentials to the orchestrator's login endpoint and
    propagate the Set-Cookie response back to the browser.

    Returns a 303 redirect to `next` on success (or `/`); falls back to
    re-rendering the login page with an error on failure. 303 (not 302)
    is the canonical "post-redirect-get" status that turns a form POST
    into a GET — prevents the browser from double-submitting if the user
    refreshes the page after login.
    """
    try:
        body, set_cookie = await orchestrator.auth_login(username_or_email, password)
    except httpx.HTTPStatusError as e:
        log.info("login failed for %r (status %d)", username_or_email, e.response.status_code)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "page_title": "Sign in",
                "next": next, "error": "Invalid username or password.",
            },
            status_code=401,
        )
    except httpx.HTTPError as e:
        log.warning("login transport error: %s", e)
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request, "page_title": "Sign in",
                "next": next, "error": "Sign-in service is unreachable. Try again shortly.",
            },
            status_code=502,
        )

    # Construct the response — redirect to `next` (validated to start with
    # `/` to prevent open-redirect attacks against external URLs).
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(url=f"{ROOT_PATH}{safe_next}", status_code=303)
    if set_cookie:
        # Forward the orchestrator's Set-Cookie header verbatim. The cookie's
        # Path is set by the orchestrator (typically Path=/); HttpOnly +
        # SameSite=Lax are set there too.
        response.headers["set-cookie"] = set_cookie
    return response


@app.post("/api/dashboard/auth/logout", name="logout_action")
async def logout_action(request: Request) -> Response:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.auth_logout(cookie)
    except httpx.HTTPError as e:
        log.warning("logout proxy failed: %s", e)
    response = RedirectResponse(url=f"{ROOT_PATH}/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


# ===========================================================================
#  Helpers
# ===========================================================================
async def _render_card(request: Request, name: str) -> HTMLResponse:
    """Look up a single service by name and render its card fragment.

    If the service has vanished from the catalogue (shouldn't happen) we
    render an inert stopped+unmanaged card so HTMX still gets valid HTML
    to swap in.
    """
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError as e:
        log.warning("re-fetch after action failed for %s: %s", name, e)
        svc = None

    if svc is None:
        svc = {
            "name": name, "display_name": name, "tier": "cold",
            "layer": None, "state": "stopped", "container": None,
            "managed": False,
        }

    return templates.TemplateResponse(
        "_service_card_fragment.html",
        {"request": request, "service": svc},
    )


async def _render_pin_button(request: Request, name: str) -> HTMLResponse:
    """Re-render the pin/unpin button for the service detail page."""
    try:
        pin = await orchestrator.get_pin(name)
    except httpx.HTTPError as e:
        log.warning("get_pin(%s) failed: %s", name, e)
        pin = None
    return templates.TemplateResponse(
        "_pin_button_fragment.html",
        {"request": request, "service_name": name, "pin": pin},
    )
