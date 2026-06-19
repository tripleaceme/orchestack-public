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


# ----------------------------------------------------------------------
# Build-info plumbing for the footer.
#
# Three sources of "which version is this?":
#   1. DASHBOARD_BUILD_SHA — set by CI in the docker image build
#      (workflow injects `--build-arg BUILD_SHA=$GITHUB_SHA`).
#      "dev" when running a locally-built image.
#   2. The runtime bundle's VERSION file — mounted into the dashboard
#      container at /etc/orchestack/VERSION by docker-compose.yml.
#      Read once at process start (it doesn't change between requests).
#   3. The orchestrator's reported SHA — fetched lazily from
#      /orchestrator/api/health and cached for 5 minutes so the footer
#      doesn't add a round-trip to every page render.
# ----------------------------------------------------------------------
DASHBOARD_BUILD_SHA = os.environ.get("DASHBOARD_BUILD_SHA", "dev")

def _read_bundle_version() -> str:
    for p in ("/etc/orchestack/VERSION", "/etc/orchestack/bundle/VERSION"):
        try:
            with open(p) as f:
                v = f.read().strip()
                if v:
                    return v
        except OSError:
            continue
    return ""

BUNDLE_VERSION = _read_bundle_version()

# Made available to every template via Jinja2's env.globals so the
# footer can render without each route having to thread it through.
# The orchestrator's SHA isn't here on purpose — it would need a cross-
# service call per render; operators can curl /orchestrator/api/health
# directly when they need it.
templates.env.globals["build_info"] = {
    "bundle_version":  BUNDLE_VERSION,
    "dashboard_sha":   DASHBOARD_BUILD_SHA,
    "orchestrator_sha": "",  # populated below if the env var is set
}

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


async def require_admin(request: Request) -> dict[str, object]:
    """Guard route dependency — require Admin role. Same redirect for not-
    signed-in users; 403 for signed-in users without the Admin role.

    Used on /app/users + /app/roles routes (the admin surfaces). Non-admin
    users see a 403 page rather than a redirect so they understand that
    the page exists but isn't theirs to access.
    """
    user = await require_user(request)
    if "Admin" not in user.get("roles", []):
        raise HTTPException(403, "Admin role required to access this page.")
    return user


def _extract_service_from_path(path: str) -> str | None:
    """Best-effort: pull a service name out of a 404'd URL.

    The most common 404 shape is `/app/<service>/<rest>` where the
    operator clicked a bookmarked deep link (e.g.,
    /app/metabase/browse/databases/2-pipeline-warehouse) but the
    target service isn't running. Traefik's router for that prefix
    only exists while the container is up — when it's stopped, the
    request falls through to the dashboard's catchall and our
    FastAPI returns a generic 404. We extract the service name so
    the error page can name what the operator was trying to reach.

    Returns None if the path doesn't match the /app/<service>/...
    shape — caller renders a generic 404 in that case.
    """
    # Strip leading slash + the dashboard's root_path prefix.
    p = path.lstrip("/")
    prefix = (ROOT_PATH or "/app").strip("/")
    if prefix and p.startswith(prefix + "/"):
        p = p[len(prefix) + 1:]
    parts = p.split("/", 1)
    if not parts or not parts[0]:
        return None
    candidate = parts[0]
    # Don't claim service-not-found for dashboard's own routes (e.g.
    # /app/sessions, /app/login). The catalogue lookup downstream
    # confirms this is actually a real service.
    return candidate


async def _service_404_response(request: Request, exc):
    """Render a friendly HTML 404 with diagnosis bullets + back button.

    Used both for FastAPI HTTPException(404) and Starlette's routing
    404 (no matched path). Detects if the URL was aimed at a
    catalogue service and tailors the bullets accordingly.

    SECURITY: request.url.path is user-controllable. The error template
    renders message + bullets with `| safe` (so we can use <code> and
    <strong> formatting), which means we MUST html-escape `path` before
    interpolating it into the strings — otherwise a crafted URL like
    /<script>alert(1)</script> would execute. We escape once here at
    the boundary, then trust the resulting string in the f-strings.
    """
    import html as _html
    # FastAPI's request.url.path strips the ASGI root_path (Traefik
    # subpath /app); we want to show what the operator actually typed
    # in the address bar, so reattach the prefix for display purposes.
    # Internal routing logic still uses the stripped path.
    internal_path = request.url.path
    display_path = (request.scope.get("root_path") or "") + internal_path
    path = _html.escape(display_path)
    candidate = _extract_service_from_path(internal_path)
    svc = None
    if candidate:
        try:
            svc = await orchestrator.get_service(candidate)
        except Exception:
            svc = None

    if svc and svc.get("display_name"):
        # Deep link to a known service — tailor the bullets to that
        # service's specific state.
        display_name = svc["display_name"]
        state = svc.get("state", "unknown")
        title = f"{display_name} isn't reachable"
        message = (
            f"OrcheStack couldn't route your request "
            f"<code class=\"font-mono\">{path}</code> "
            f"to <strong>{display_name}</strong>."
        )
        bullets = [
            f"<strong>{display_name} is currently {state}.</strong> "
            f"If it's stopped, go to the dashboard and click <em>Start</em> "
            f"on the {display_name} tile, then try this link again.",
            "<strong>The service just launched and isn't fully ready yet.</strong> "
            "Tools like Metabase take 60–90s to come online after Start; "
            "Airbyte's worker can take a couple of minutes on first boot.",
            f"<strong>The URL was deep-linked from an old install.</strong> "
            f"Container IDs and workspace slugs change between fresh deploys "
            f", the <code class=\"font-mono\">{path}</code> path may point to a "
            f"resource that no longer exists.",
            f"<strong>You don't have access to this {display_name} resource.</strong> "
            f"Inside {display_name}, the resource exists but your account "
            "isn't on the share list.",
        ]
    else:
        # Generic 404 — no service detected from path.
        title = "Page not found"
        message = (
            f"OrcheStack couldn't find a page at "
            f"<code class=\"font-mono\">{path}</code>."
        )
        bullets = [
            "<strong>The URL was typed or pasted with a typo.</strong> "
            "Check the address bar for extra spaces or wrong slashes.",
            "<strong>The page moved.</strong> The dashboard's URLs "
            "occasionally change between OrcheStack versions, try "
            "navigating from the home page.",
            "<strong>You used a stale bookmark.</strong> If this link "
            "came from an older install, the resource may no longer exist.",
        ]

    return templates.TemplateResponse(
        "error.html",
        {
            "request": request,
            "page_title": "Not found",
            "user": None,
            "status_code": 404,
            "title": title,
            "message": message,
            "bullets_heading": "A few likely reasons:",
            "bullets": bullets,
            "admin_hint": (
                "Still stuck? Reach out to your OrcheStack admin, they can "
                "check the service state, see who has access, and look at "
                "the orchestrator's audit log on the "
                f"<a href=\"{ROOT_PATH}/audit\" class=\"text-[var(--navy)] hover:underline\">Audit page</a>."
            ),
            "back_url": ROOT_PATH or "/",
            "back_label": "Back to dashboard",
        },
        status_code=404,
    )


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """Convert 307 login_required exceptions into real RedirectResponses.

    For 403/404 raised on HTML page routes (everything NOT under /api/),
    render an HTML error page rather than raw JSON — operators who typed
    a URL directly should land on a readable page that tells them what's
    wrong and how to get back, not a wall of {"detail": "..."}.
    """
    if exc.status_code == 307 and exc.detail == "login_required":
        return RedirectResponse(url=exc.headers["Location"], status_code=307)
    is_api = request.url.path.startswith("/api/")
    if exc.status_code == 403 and not is_api:
        return templates.TemplateResponse(
            "error.html",
            {
                "request": request,
                "page_title": "Access denied",
                "user": None,
                "status_code": 403,
                "title": "Access denied",
                "message": exc.detail or "You don't have permission for that page.",
                "back_url": ROOT_PATH or "/",
                "back_label": "Back to dashboard",
            },
            status_code=403,
        )
    if exc.status_code == 404 and not is_api:
        return await _service_404_response(request, exc)
    # Fall through to FastAPI's default JSON response for everything else.
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=dict(exc.headers or {}),
    )


# Starlette's routing layer raises its own HTTPException when no route
# matches. Register that exception class too so the path
# /app/metabase/browse/... (no FastAPI route → Starlette 404) ends up
# in the same helpful HTML handler instead of FastAPI's default JSON.
try:
    from starlette.exceptions import HTTPException as _StarletteHTTPException
    @app.exception_handler(_StarletteHTTPException)
    async def _starlette_http_handler(request: Request, exc):
        is_api = request.url.path.startswith("/api/")
        if exc.status_code == 404 and not is_api:
            return await _service_404_response(request, exc)
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": str(exc.detail)},
        )
except ImportError:
    pass


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
async def _aggregate_kpis() -> dict:
    """Aggregate the four KPI-strip metrics from the orchestrator.

    Shared by /home (initial page render) AND the HTMX polling endpoint
    /api/dashboard/kpi-strip (every 10s refresh) so the data shape stays
    in sync across both code paths.
    """
    services_running = 0
    services_total = 0
    try:
        svc_data = await orchestrator.list_services()
        all_services = svc_data.get("services", [])
        configured = [s for s in all_services if s.get("configured")]
        services_total = len(configured)
        services_running = sum(1 for s in configured if s.get("state") == "running")
    except (httpx.HTTPError, ValueError) as e:
        log.warning("KPI list_services failed: %s", e)

    active_sessions = 0
    try:
        sess_data = await orchestrator.list_sessions(limit=200, offset=0)
        active_sessions = len(sess_data.get("sessions", []))
    except (httpx.HTTPError, ValueError) as e:
        log.warning("KPI list_sessions failed: %s", e)

    # Last audit event — capture target (service name) too so the card
    # can render "service_started · metabase" instead of bare event type.
    last_event_type = None
    last_event_target = None
    last_event_when = None
    try:
        audit_data = await orchestrator.list_audit(limit=1, offset=0)
        events = audit_data.get("events", [])
        if events:
            last_event_type   = events[0].get("event_type")
            last_event_target = events[0].get("target")
            last_event_when   = events[0].get("created_at")
    except (httpx.HTTPError, ValueError) as e:
        log.warning("KPI list_audit failed: %s", e)

    return {
        "services_running":   services_running,
        "services_total":     services_total,
        "active_sessions":    active_sessions,
        "last_event_type":    last_event_type,
        "last_event_target":  last_event_target,
        "last_event_when":    last_event_when,
    }


@app.get("/", response_class=HTMLResponse, name="home")
async def home(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """Service grid + KPI strip + platform health card."""
    kpi = await _aggregate_kpis()
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request, "page_title": "Home", "user": user,
            "kpi": kpi,
            "sidebar_counts": {
                "configured": kpi["services_total"],
                "total":      kpi["services_total"],
            } if kpi["services_total"] else None,
        },
    )


@app.get("/api/dashboard/kpi-strip", response_class=HTMLResponse,
          name="kpi_strip_fragment")
async def kpi_strip_fragment(
    request: Request, user=Depends(require_user)
) -> HTMLResponse:
    """KPI strip fragment — HTMX-polled every 10s from the home page.

    Returns just the 4-card strip (no surrounding page chrome). The
    home template wraps its KPI strip in a hx-get-this-endpoint div so
    actions (start/stop/pin/open) reflect in the strip without a full
    page reload.
    """
    kpi = await _aggregate_kpis()
    return templates.TemplateResponse(
        "_kpi_strip_fragment.html", {"request": request, "kpi": kpi},
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
async def credentials_page(request: Request, user=Depends(require_admin)) -> HTMLResponse:
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


# ----------------------------------------------------------------------
# Service grouping for the Credentials page
#
# Operators asked for a per-service view of credentials: pick the service
# from a dropdown, see only its keys. This is purely a UX grouping —
# under the hood every key still lives in a single flat .env file.
#
# Bucketing rule: longest matching prefix wins (so e.g. "MB_DB_USER"
# resolves to "Metabase" rather than landing in "Other"). Keys with no
# match fall into "Other". The order of CREDENTIAL_SERVICE_GROUPS is
# both the prefix-match order AND the dropdown display order, so put
# the platform group first.
# ----------------------------------------------------------------------
CREDENTIAL_SERVICE_GROUPS: list[tuple[str, list[str]]] = [
    ("OrcheStack platform", ["ORCHESTACK_"]),
    ("Image tags",          ["_TAG"]),  # suffix-match handled specially
    # "Warehouse" is the operator-facing label for the warehouse DB
    # credentials. Env keys keep the WAREHOUSE_DB_ prefix for backward
    # compat with existing .env files; only the display name changed
    # to reduce confusion ("pipeline" sounded like data-pipeline software,
    # not "the database holding pipeline output tables").
    ("Warehouse",           ["WAREHOUSE_DB_"]),
    ("Airbyte",             ["AIRBYTE_"]),
    ("Apache Airflow",      ["AIRFLOW_"]),
    ("dbt Core",            ["DBT_"]),
    ("Metabase",            ["METABASE_", "MB_"]),
    ("Apache Superset",     ["SUPERSET_"]),
    ("Lightdash",           ["LIGHTDASH_"]),
    ("MinIO",               ["MINIO_"]),
    ("OpenMetadata",        ["OPENMETADATA_"]),
    ("Great Expectations",  ["GE_"]),
    ("Soda Core",           ["SODA_"]),
    ("SQLMesh",             ["SQLMESH_"]),
    ("ClickHouse",          ["CLICKHOUSE_"]),
    ("DuckDB",              ["DUCKDB_"]),
    ("pgAdmin",             ["PGADMIN_"]),
    ("Adminer",             ["ADMINER_"]),
    ("pgweb",               ["PGWEB_"]),
    ("DataHub",             ["DATAHUB_"]),
]


def _service_for_credential(key: str) -> str:
    """Return the operator-facing service name for a given .env key.

    Image-tag suffix has priority over prefix matches — every service
    has a *_TAG variable that we want grouped together under one header
    so operators see image versions in one place.
    """
    if key.endswith("_TAG"):
        return "Image tags"
    for group, prefixes in CREDENTIAL_SERVICE_GROUPS:
        if group == "Image tags":
            continue
        for prefix in prefixes:
            if key.startswith(prefix):
                return group
    return "Other"


@app.get("/api/dashboard/credentials/table", response_class=HTMLResponse,
          name="credentials_table_fragment")
async def credentials_table_fragment(
    request: Request,
    reveal: bool = False,
    service: str = "All",
    user=Depends(require_admin),
) -> HTMLResponse:
    """HTMX fragment — the credentials table, optionally filtered by service.

    ?service=Metabase narrows the table to keys whose prefix maps to that
    group. ?service=All (default) shows every key. The dropdown lives in
    the fragment itself so HTMX swap preserves the selection state across
    re-renders (no out-of-band updates needed).
    """
    try:
        data = await orchestrator.list_credentials(reveal=reveal)
        credentials = data.get("credentials", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("list_credentials failed: %s", e)
        credentials = []
        error = str(e)

    # Annotate each credential with its service group, then filter. We
    # always annotate (even on the All view) so the template can render
    # service labels next to each row — operators get a visual cue when
    # browsing the unfiltered list.
    for c in credentials:
        c["service"] = _service_for_credential(c["key"])

    # Distinct services PRESENT in the .env (not every group we know of).
    # An empty group ("DataHub" when DataHub isn't installed) shouldn't
    # appear in the dropdown — it'd give the operator the impression that
    # selecting it would do something.
    services_present = sorted(
        {c["service"] for c in credentials},
        key=lambda s: ([g for g, _ in CREDENTIAL_SERVICE_GROUPS].index(s)
                       if s in [g for g, _ in CREDENTIAL_SERVICE_GROUPS]
                       else len(CREDENTIAL_SERVICE_GROUPS)),
    )

    if service != "All":
        credentials = [c for c in credentials if c["service"] == service]

    return templates.TemplateResponse(
        "_credentials_table_fragment.html",
        {
            "request": request,
            "credentials": credentials,
            "reveal": reveal,
            "error": error,
            "selected_service": service,
            "services_present": services_present,
        },
    )


@app.post("/api/dashboard/credentials/{key}",
           response_class=HTMLResponse, name="credentials_update_action")
async def credentials_update_action(
    request: Request, key: str, value: str = Form(...),
    service: str = Form("All"),
    user=Depends(require_admin),
) -> HTMLResponse:
    """Update one .env variable + re-render its table row.

    The service filter is threaded through the form so the operator
    stays on the same filtered view after saving — otherwise an edit
    on the Metabase filter would jump them back to All.
    """
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
    for c in credentials:
        c["service"] = _service_for_credential(c["key"])
    services_present = sorted(
        {c["service"] for c in credentials},
        key=lambda s: ([g for g, _ in CREDENTIAL_SERVICE_GROUPS].index(s)
                       if s in [g for g, _ in CREDENTIAL_SERVICE_GROUPS]
                       else len(CREDENTIAL_SERVICE_GROUPS)),
    )
    if service != "All":
        credentials = [c for c in credentials if c["service"] == service]
    return templates.TemplateResponse(
        "_credentials_table_fragment.html",
        {
            "request": request,
            "credentials": credentials,
            "reveal": False,
            "error": error,
            "updated_key": key,
            "selected_service": service,
            "services_present": services_present,
        },
    )


# ===========================================================================
#  Self-service Profile — every signed-in user
# ===========================================================================
@app.get("/profile", response_class=HTMLResponse, name="profile_page")
async def profile_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/profile` — edit your own full name, email, company, password."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        profile = await orchestrator.get_my_profile(cookie)
        error = None
    except httpx.HTTPError as e:
        log.warning("get_my_profile failed: %s", e)
        profile = {}
        error = str(e)
    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "page_title": "Profile",
          "user": user, "profile": profile, "error": error,
          "saved": False, "save_error": None},
    )


@app.post("/profile", response_class=HTMLResponse, name="profile_save_action")
async def profile_save_action(
    request: Request,
    full_name:        str = Form(""),
    email:            str = Form(""),
    company_name:     str = Form(""),
    current_password: str = Form(""),
    new_password:     str = Form(""),
    user=Depends(require_user),
) -> HTMLResponse:
    """Save profile changes. Renders the page back with a success/error banner.

    Only sends fields the operator actually changed — passing every form
    field unconditionally would overwrite e.g. company_name with the empty
    string when they only meant to update full_name.
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    save_error = None
    saved = False
    try:
        current = await orchestrator.get_my_profile(cookie)
        # Only send fields the operator actually changed.
        kwargs: dict = {}
        if full_name and full_name != current.get("full_name"):
            kwargs["full_name"] = full_name
        if email and email != current.get("email"):
            kwargs["email"] = email
        if company_name != (current.get("company_name") or ""):
            kwargs["company_name"] = company_name
        if new_password:
            kwargs["current_password"] = current_password
            kwargs["new_password"]     = new_password

        if kwargs:
            await orchestrator.update_my_profile(cookie, **kwargs)
            saved = True
    except httpx.HTTPStatusError as e:
        try:
            save_error = e.response.json().get("detail") or str(e)
        except Exception:
            save_error = str(e)
    except httpx.HTTPError as e:
        save_error = str(e)

    # Re-fetch the profile so the form shows the latest persisted state.
    try:
        profile = await orchestrator.get_my_profile(cookie)
        error = None
    except httpx.HTTPError as e:
        profile = {}
        error = str(e)

    return templates.TemplateResponse(
        "profile.html",
        {"request": request, "page_title": "Profile",
          "user": user, "profile": profile, "error": error,
          "saved": saved, "save_error": save_error},
    )


# ===========================================================================
#  Admin — Users page
# ===========================================================================
@app.get("/users", response_class=HTMLResponse, name="users_page")
async def users_page(request: Request, user=Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        "users.html",
        {"request": request, "page_title": "Users", "user": user},
    )


@app.get("/api/dashboard/users/table", response_class=HTMLResponse,
          name="users_table_fragment")
async def users_table_fragment(request: Request, user=Depends(require_admin)) -> HTMLResponse:
    """HTMX fragment for the Users table.

    invite_result is intentionally None on plain loads — the only path
    that carries an invite_result is the invite POST handler, which
    renders this same template directly with the result in context. We
    used to read request.session.get(...) here as a defensive cross-tab
    handoff, but SessionMiddleware isn't installed (cookies + the
    orchestrator are the source of truth, not server-side session state),
    so the access raised AssertionError and the fragment 500'd. The
    handler's catch was scoped to httpx.HTTPError, so the 500 surfaced as
    "Loading users…" forever in the browser.
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        users_data = await orchestrator.admin_list_users(cookie)
        roles_data = await orchestrator.admin_list_roles(cookie)
        return templates.TemplateResponse(
            "_users_table_fragment.html",
            {
                "request": request,
                "users": users_data.get("users", []),
                "roles": roles_data.get("roles", []),
                "current_user_id": user.get("user_id"),
                "error": None,
                "invite_result": None,
            },
        )
    except httpx.HTTPError as e:
        log.warning("users_table_fragment failed: %s", e)
        return templates.TemplateResponse(
            "_users_table_fragment.html",
            {"request": request, "users": [], "roles": [],
              "current_user_id": user.get("user_id"),
              "error": str(e), "invite_result": None},
        )


@app.post("/api/dashboard/users/invite", response_class=HTMLResponse,
           name="users_invite_action")
async def users_invite_action(
    request: Request,
    username: str = Form(...), email: str = Form(...),
    full_name: str = Form(...),
    # Accept role_id as a str so the "No role yet" option (sends "")
    # doesn't trigger FastAPI's int parser and 422 the request — that
    # 422 was what was flipping the connection indicator to "disconnected"
    # in red on every invite click that didn't pick a starter role.
    role_id: str = Form(""),
    user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    invite_result = None
    invite_error = None
    role_names: list[str] = []
    # Coerce role_id from string here so empty string becomes None
    # without FastAPI rejecting the request.
    role_id_int: int | None = None
    if role_id.strip():
        try:
            role_id_int = int(role_id)
        except ValueError:
            invite_error = f"Invalid role id: {role_id!r}"
    if role_id_int is not None:
        # Resolve role_id → role name for the orchestrator API (which takes names).
        try:
            roles_data = await orchestrator.admin_list_roles(cookie)
            for r in roles_data.get("roles", []):
                if r["id"] == role_id_int:
                    role_names = [r["name"]]
                    break
        except httpx.HTTPError as e:
            log.warning("role lookup failed during invite: %s", e)

    try:
        invite_result = await orchestrator.admin_invite_user(
            cookie, username=username, email=email,
            full_name=full_name, role_names=role_names,
        )
    except httpx.HTTPStatusError as e:
        try:
            invite_error = e.response.json().get("detail", str(e))
        except Exception:
            invite_error = str(e)
    except httpx.HTTPError as e:
        invite_error = str(e)

    # Re-render the table fragment with the invite result for one-time
    # display of the starter password.
    try:
        users_data = await orchestrator.admin_list_users(cookie)
        roles_data = await orchestrator.admin_list_roles(cookie)
        users = users_data.get("users", [])
        roles = roles_data.get("roles", [])
        error = None
    except httpx.HTTPError as e:
        users, roles, error = [], [], str(e)

    return templates.TemplateResponse(
        "_users_table_fragment.html",
        {
            "request": request,
            "users": users, "roles": roles,
            "current_user_id": user.get("user_id"),
            "error": error,
            "invite_result": invite_result,
            "invite_error": invite_error,
        },
    )


@app.post("/api/dashboard/users/{user_id}/toggle",
           response_class=HTMLResponse, name="users_toggle_action")
async def users_toggle_action(
    request: Request, user_id: int,
    enable: bool = Form(...), user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_toggle_user(cookie, user_id, enable)
    except httpx.HTTPError as e:
        log.warning("toggle user %d failed: %s", user_id, e)
    return await users_table_fragment(request, user)


@app.post("/api/dashboard/users/{user_id}/roles",
           response_class=HTMLResponse, name="users_grant_role_action")
async def users_grant_role_action(
    request: Request, user_id: int,
    role_id: int = Form(...), user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_grant_user_role(cookie, user_id, role_id)
    except httpx.HTTPError as e:
        log.warning("grant role %d to user %d failed: %s", role_id, user_id, e)
    return await users_table_fragment(request, user)


@app.delete("/api/dashboard/users/{user_id}/roles/{role_id}",
             response_class=HTMLResponse, name="users_revoke_role_action")
async def users_revoke_role_action(
    request: Request, user_id: int, role_id: int,
    user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_revoke_user_role(cookie, user_id, role_id)
    except httpx.HTTPError as e:
        log.warning("revoke role %d from user %d failed: %s", role_id, user_id, e)
    return await users_table_fragment(request, user)


# ===========================================================================
#  Admin — Roles page
# ===========================================================================
@app.get("/roles", response_class=HTMLResponse, name="roles_page")
async def roles_page(request: Request, user=Depends(require_admin)) -> HTMLResponse:
    return templates.TemplateResponse(
        "roles.html",
        {"request": request, "page_title": "Roles", "user": user},
    )


async def _roles_render_context(request: Request, user: dict) -> dict:
    """Common context for roles fragment renders."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        roles_data = await orchestrator.admin_list_roles(cookie)
        perms_data = await orchestrator.admin_list_permissions(cookie)
        services_data = await orchestrator.list_services()
        error = None
    except httpx.HTTPError as e:
        log.warning("roles fragment failed: %s", e)
        roles_data = {"roles": []}
        perms_data = {"permissions": []}
        services_data = {"services": []}
        error = str(e)

    # Bucket permissions by role for easy template iteration.
    perms_by_role: dict[int, list[dict]] = {}
    for p in perms_data.get("permissions", []):
        perms_by_role.setdefault(p["role_id"], []).append(p)

    return {
        "request": request,
        "roles": roles_data.get("roles", []),
        "perms_by_role": perms_by_role,
        "services": services_data.get("services", []),
        "error": error,
    }


@app.get("/api/dashboard/roles/list", response_class=HTMLResponse,
          name="roles_list_fragment")
async def roles_list_fragment(request: Request, user=Depends(require_admin)) -> HTMLResponse:
    ctx = await _roles_render_context(request, user)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.post("/api/dashboard/roles/create", response_class=HTMLResponse,
           name="roles_create_action")
async def roles_create_action(
    request: Request, name: str = Form(...),
    description: str = Form(""), user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_create_role(cookie, name, description or None)
    except httpx.HTTPError as e:
        log.warning("create role failed: %s", e)
    ctx = await _roles_render_context(request, user)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.delete("/api/dashboard/roles/{role_id}", response_class=HTMLResponse,
             name="roles_delete_action")
async def roles_delete_action(
    request: Request, role_id: int, user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_delete_role(cookie, role_id)
    except httpx.HTTPError as e:
        log.warning("delete role %d failed: %s", role_id, e)
    ctx = await _roles_render_context(request, user)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.post("/api/dashboard/roles/{role_id}/permissions",
           response_class=HTMLResponse, name="roles_grant_permission_action")
async def roles_grant_permission_action(
    request: Request, role_id: int,
    service_name: str = Form(...),
    can_start: bool = Form(False),
    can_use: bool = Form(False),
    can_force_stop: bool = Form(False),
    can_edit_config: bool = Form(False),
    user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_grant_permission(
            cookie, role_id=role_id, service_name=service_name,
            can_start=can_start, can_use=can_use,
            can_force_stop=can_force_stop, can_edit_config=can_edit_config,
        )
    except httpx.HTTPError as e:
        log.warning("grant permission failed: %s", e)
    ctx = await _roles_render_context(request, user)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.delete("/api/dashboard/roles/permissions/{permission_id}",
             response_class=HTMLResponse, name="roles_revoke_permission_action")
async def roles_revoke_permission_action(
    request: Request, permission_id: int, user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        await orchestrator.admin_revoke_permission(cookie, permission_id)
    except httpx.HTTPError as e:
        log.warning("revoke permission %d failed: %s", permission_id, e)
    ctx = await _roles_render_context(request, user)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


# ----------------------------------------------------------------------
# Mapping from orchestrator service-catalogue keys to the operator-facing
# group name used in CREDENTIAL_SERVICE_GROUPS. The catalogue uses short
# slugs ("metabase", "pgadmin") while the credentials page groups by
# display name ("Metabase", "pgAdmin"). This is the bridge for the
# per-service Edit-config view: given a service slug, show ONLY that
# service's keys plus the pipeline-warehouse keys (so Postgres-backed
# services show the warehouse creds they actually use to connect).
# ----------------------------------------------------------------------
# Per-service credential groupings rendered on the service-config page.
# Each tuple is (group_title, group_subtitle, key_substring_patterns).
# Rules apply in order; the first match wins per credential. Variables
# matching no rule fall into "Other". Subtitles match the approved
# mock's tone ("auto-regenerated on every start from these values").
CREDENTIAL_GROUP_RULES: list[tuple[str, str, list[str]]] = [
    ("Repository",
     "Where the project clones from on every start",
     ["REPO"]),
    ("Admin",
     "Operator-facing login for the tool's web UI",
     ["ADMIN_EMAIL", "ADMIN_USER", "ADMIN_PASSWORD"]),
    ("Database connection",
     "Auto-regenerated on every start from these values",
     ["DATABASE", "SCHEMA", "DB_NAME", "DB_USER", "DB_PASSWORD"]),
    ("Secrets",
     "Token/secret material; rotate periodically",
     ["JWT_SECRET", "API_KEY", "_SECRET", "_TOKEN"]),
    ("Service topology",
     "Fixed by the deployment",
     ["_HOST", "_PORT"]),
]


def _group_credentials(creds: list[dict]) -> list[dict]:
    """Group a flat credential list by the rules above.

    Returns a list of `{title, sub, creds}` dicts in the order defined
    by CREDENTIAL_GROUP_RULES, skipping any empty groups. Unmatched
    credentials are bundled into a trailing "Other" group so nothing
    falls off the page.
    """
    buckets: dict[str, list[dict]] = {t: [] for t, _, _ in CREDENTIAL_GROUP_RULES}
    other: list[dict] = []
    for c in creds:
        matched_title: str | None = None
        for title, _, patterns in CREDENTIAL_GROUP_RULES:
            if any(p in c["key"] for p in patterns):
                matched_title = title
                break
        if matched_title:
            buckets[matched_title].append(c)
        else:
            other.append(c)
    groups: list[dict] = []
    for title, sub, _ in CREDENTIAL_GROUP_RULES:
        if buckets[title]:
            groups.append({"title": title, "sub": sub, "creds": buckets[title]})
    if other:
        groups.append({"title": "Other", "sub": "Uncategorized variables", "creds": other})
    return groups


SERVICE_CREDENTIAL_GROUPS: dict[str, list[str]] = {
    "metabase":     ["Metabase"],
    "pgadmin":      ["pgAdmin"],
    "airbyte":      ["Airbyte"],
    "airflow":      ["Apache Airflow"],
    "dbt":          ["dbt Core"],
    "minio":        ["MinIO"],
    "openmetadata": ["OpenMetadata"],
    "ge":           ["Great Expectations"],
    "postgresql":   ["Warehouse"],
    "clickhouse":   ["ClickHouse"],
    "duckdb":       ["DuckDB"],
    "superset":     ["Apache Superset"],
    "lightdash":    ["Lightdash"],
    "sqlmesh":      ["SQLMesh"],
    "soda":         ["Soda Core"],
    "adminer":      ["Adminer"],
    "pgweb":        ["pgweb"],
    "datahub":      ["DataHub"],
}


@app.get("/services/{name}/config", response_class=HTMLResponse,
          name="service_config_page")
async def service_config_page(
    request: Request, name: str, user=Depends(require_user)
) -> HTMLResponse:
    """`/app/services/{name}/config` — per-service credentials editor.

    The docs' workflow says "click the service tile → Edit config." This
    is the destination. Renders ONLY the .env keys grouped under this
    service's CREDENTIAL_SERVICE_GROUPS bucket — no fishing through the
    global flat list to find METABASE_*. Save writes back via the same
    update_credential path the global page uses.
    """
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError:
        svc = None
    try:
        data = await orchestrator.list_credentials(reveal=False)
        all_creds = data.get("credentials", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("service_config_page list_credentials failed: %s", e)
        all_creds = []

    # Annotate, then filter to this service's group(s).
    groups_for_service = SERVICE_CREDENTIAL_GROUPS.get(name, [])
    for c in all_creds:
        c["service"] = _service_for_credential(c["key"])
    creds = [c for c in all_creds if c["service"] in groups_for_service]
    grouped = _group_credentials(creds)

    # "Last edited" data — pulled from the audit log. We look for the
    # most recent credential_updated event whose target falls inside
    # this service's credential keys; that maps a credential edit back
    # to which service-config page the operator was on. Falls back to
    # None if no edits ever happened, so the template hides the line.
    last_edited_at = None
    last_edited_by = None
    try:
        own_keys = {c["key"] for c in creds}
        audit_data = await orchestrator.list_audit(limit=50, offset=0)
        for ev in audit_data.get("events", []):
            if ev.get("event_type") != "credential_updated":
                continue
            tgt = ev.get("target") or ""
            if tgt in own_keys:
                last_edited_at = ev.get("created_at")
                last_edited_by = (
                    ev.get("actor_full_name") or ev.get("actor_username")
                )
                break
    except (httpx.HTTPError, ValueError):
        pass

    display_name = (svc or {}).get("display_name", name)
    is_running = (svc or {}).get("state") == "running"
    return templates.TemplateResponse(
        "service_config.html",
        {
            "request": request,
            "page_title": f"Edit config · {display_name}",
            "user": user,
            "service": svc,
            "service_name": name,
            "display_name": display_name,
            "credentials": creds,
            "credential_groups": grouped,
            "is_running": is_running,
            "last_edited_at": last_edited_at,
            "last_edited_by": last_edited_by,
            "saved_keys": [],
            "save_error": None,
        },
    )


@app.post("/services/{name}/config", response_class=HTMLResponse,
           name="service_config_save")
async def service_config_save(
    request: Request, name: str, user=Depends(require_user)
) -> HTMLResponse:
    """Save the per-service config form.

    Form is keyed by ENV_VAR_NAME → new value. Skips read-only keys and
    skips keys whose value didn't change (so the audit log doesn't see
    spurious updates). Returns the same page with a summary banner.
    """
    form = await request.form()
    cookie = request.cookies.get(SESSION_COOKIE_NAME)

    try:
        existing = (await orchestrator.list_credentials(reveal=True)).get("credentials", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("service_config_save couldn't fetch existing: %s", e)
        existing = []
    by_key = {c["key"]: c for c in existing}

    saved_keys: list[str] = []
    save_error: str | None = None
    test_failures: list[dict] = []  # surfaced to the operator
    for raw_key, raw_val in form.items():
        if not raw_key or raw_key.startswith("__"):
            continue
        if raw_key not in by_key:
            continue                    # don't accept new keys from the form
        cur = by_key[raw_key]
        if cur.get("is_readonly"):
            continue
        if cur.get("value", "") == raw_val:
            continue                    # no change → no write

        # Live connection test for DB-typed credentials BEFORE we
        # persist. The orchestrator returns {"testable": false, ...}
        # for keys we can't verify in-band (image tags, secrets used
        # only by tools we can't reach) — those just save without test.
        # When the test runs AND fails, we DON'T save the value; we
        # collect the error and surface it to the operator.
        try:
            tr = await orchestrator.test_credential(raw_key, raw_val)
            if tr.get("testable") and tr.get("ok") is False:
                test_failures.append({
                    "key":   raw_key,
                    "as":    tr.get("tested_as"),
                    "db":    tr.get("tested_db"),
                    "error": tr.get("error") or tr.get("error_class")
                              or "connection refused",
                })
                continue  # skip save for this key
        except httpx.HTTPError as e:
            # Test endpoint unreachable. Don't block the save — log
            # and proceed (the post-save Stop/Start of the service is
            # the operator's safety net).
            log.warning("test_credential %s endpoint failed: %s", raw_key, e)

        try:
            await orchestrator.update_credential(
                raw_key, raw_val, actor_user_id=user.get("user_id"),
            )
            saved_keys.append(raw_key)
        except httpx.HTTPError as e:
            log.warning("update_credential %s failed: %s", raw_key, e)
            save_error = str(e)
            break

    # If we collected test failures but no save error, surface the
    # test failures as the user-visible error. They're the actionable
    # signal — "your password is wrong" tells the operator what to fix.
    if test_failures and not save_error:
        save_error = "Live connection test failed for: " + ", ".join(
            f"{f['key']} (as {f['as']}: {f['error']})" for f in test_failures
        )

    # Re-render with the latest values + summary banner.
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError:
        svc = None
    try:
        data = await orchestrator.list_credentials(reveal=False)
        all_creds = data.get("credentials", [])
    except (httpx.HTTPError, ValueError) as e:
        all_creds = []
    groups_for_service = SERVICE_CREDENTIAL_GROUPS.get(name, [])
    for c in all_creds:
        c["service"] = _service_for_credential(c["key"])
    creds = [c for c in all_creds if c["service"] in groups_for_service]

    display_name = (svc or {}).get("display_name", name)
    return templates.TemplateResponse(
        "service_config.html",
        {
            "request": request,
            "page_title": f"Edit config · {display_name}",
            "user": user,
            "service": svc,
            "service_name": name,
            "display_name": display_name,
            "credentials": creds,
            "is_running": (svc or {}).get("state") == "running",
            "saved_keys": saved_keys,
            "save_error": save_error,
        },
    )


@app.get("/services/{name}", response_class=HTMLResponse, name="service_detail")
async def service_detail(
    request: Request, name: str, user=Depends(require_user)
) -> HTMLResponse:
    """`/app/services/{name}` — per-service detail page.

    Server-side aggregates everything the approved mock shows above the
    fold: service detail, open sessions for this service, and pin state.
    The audit/activity list streams in via HTMX from the dedicated
    service_activity_fragment endpoint (so the filter form can drive it).
    """
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError:
        svc = None

    # Open sessions for THIS service. Filter client-side from the active
    # list — orchestrator doesn't expose a per-service filter yet, but
    # the active-session set is bounded (capped at MAX_PER_USER × users).
    # NB: orchestrator returns `service`, not `service_name` — match
    # exactly what its /api/sessions schema emits.
    #
    # We dedupe by user_id, keeping ONLY the most recent session per
    # user. The session-lifecycle layer issues a fresh session token
    # every time the operator opens a new tab against the same tool,
    # so a single operator browsing dbt across two windows shows up
    # as two rows even though they're one human. The Open sessions
    # card answers "who is using this service?" — listing the same
    # name twice obscures that. Most-recent-per-user keeps the answer
    # crisp.
    #
    # Hard rule: if the service is stopped, we render the empty-state
    # regardless of what the orchestrator returns. An "active" session
    # against a stopped container is by definition stale — the operator's
    # tab can't reach the tool anyway. Showing stale rows with an active
    # Force-end button gave operators conflicting signals ("service is
    # stopped, but there's a live session?"). Cleaner to treat stopped
    # ⇒ no sessions in the UI; orphan rows get swept by the next stop_service
    # call or by the backfill that ran with this fix.
    open_sessions = []
    is_running = (svc or {}).get("state") == "running"
    if is_running:
        try:
            sess_data = await orchestrator.list_sessions(limit=200, offset=0)
            all_for_this_svc = [
                s for s in sess_data.get("sessions", [])
                if s.get("service") == name
            ]
            latest_by_user: dict = {}
            for s in all_for_this_svc:
                uid = s.get("user_id") or s.get("username") or s.get("token")
                existing = latest_by_user.get(uid)
                # Compare by last_heartbeat_at if present, else opened_at.
                new_key = s.get("last_heartbeat_at") or s.get("opened_at") or ""
                old_key = (
                    (existing.get("last_heartbeat_at") or existing.get("opened_at") or "")
                    if existing else ""
                )
                if existing is None or new_key > old_key:
                    latest_by_user[uid] = s
            open_sessions = list(latest_by_user.values())
        except (httpx.HTTPError, ValueError) as e:
            log.warning("service_detail(%s) list_sessions failed: %s", name, e)

    # Compute a humanised uptime ("2h 14m", "3m", "1d 4h") from the
    # orchestrator's started_at ISO string. None when the service isn't
    # running OR docker ps didn't return started_at (e.g. control-plane
    # services that don't carry the orchestack.service label).
    uptime_display = None
    if svc and svc.get("started_at"):
        try:
            from datetime import datetime, timezone
            # Two Docker timestamp formats we need to handle:
            #   docker ps CreatedAt:    "2026-06-18 23:56:55 +0000 UTC"
            #   docker inspect .StartedAt: "2026-06-19T01:23:45.123456789Z"
            # We switched the orchestrator to use StartedAt (accurate
            # last-start) but defend against both here so dashboards
            # against older orchestrators still parse.
            iso = svc["started_at"].strip()
            # 1. Strip trailing " UTC" if present (CreatedAt format).
            iso = iso.replace(" UTC", "")
            # 2. Trailing `Z` → `+00:00` (StartedAt format; Python's
            #    fromisoformat before 3.11 doesn't accept Z directly).
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            # 3. Truncate fractional seconds to 6 digits — Docker's
            #    StartedAt emits nanoseconds (9 digits) which Python
            #    rejects. Slice from the dot to the next non-digit run.
            if "." in iso:
                dot = iso.index(".")
                tail_start = dot + 1
                tail_end = tail_start
                while tail_end < len(iso) and iso[tail_end].isdigit():
                    tail_end += 1
                if tail_end - tail_start > 6:
                    iso = iso[:tail_start + 6] + iso[tail_end:]
            # 4. Handle "+0000" vs "+00:00" — fromisoformat needs the colon.
            if len(iso) >= 5 and (iso[-5] in "+-") and iso[-3] != ":":
                iso = iso[:-2] + ":" + iso[-2:]
            started = datetime.fromisoformat(iso)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            delta = datetime.now(timezone.utc) - started
            total_min = int(delta.total_seconds() // 60)
            days  = total_min // 1440
            hours = (total_min % 1440) // 60
            mins  = total_min % 60
            if days:
                uptime_display = f"{days}d {hours}h"
            elif hours:
                uptime_display = f"{hours}h {mins}m"
            else:
                uptime_display = f"{mins}m"
        except (ValueError, TypeError) as e:
            log.warning("uptime parse failed for %s started_at=%r: %s",
                        name, svc.get("started_at"), e)

    return templates.TemplateResponse(
        "service_detail.html",
        {
            "request": request,
            "page_title": svc["display_name"] if svc else name,
            "service": svc,
            "service_name": name,
            "open_sessions": open_sessions,
            "uptime_display": uptime_display,
            "user": user,
        },
    )


@app.get("/api/dashboard/services/{name}/activity", response_class=HTMLResponse,
          name="service_activity_fragment")
async def service_activity_fragment(
    request: Request, name: str,
    event_type: str | None = None, since: str | None = None,
    until: str | None = None, limit: int = 10,
) -> HTMLResponse:
    """Render activity rows scoped to a single service in the COMPACT
    layout used by service_detail.html (90px when | event | who grid).

    Distinct from audit_table_fragment because the visual treatment is
    different — service_detail wants a list-style read, not a full
    data-table. The filter form on service_detail posts here and the
    response replaces the activity list in place.
    """
    events = []
    error = None
    try:
        data = await orchestrator.list_audit(
            target=name, event_type=event_type or None,
            since=since or None, until=until or None,
            limit=limit, offset=0,
        )
        events = data.get("events", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("service_activity_fragment(%s) list_audit failed: %s", name, e)
        error = "Couldn't load activity from the orchestrator."

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).date().isoformat()
    return templates.TemplateResponse(
        "_service_activity_fragment.html",
        {"request": request, "events": events, "error": error,
         "service_name": name, "limit": limit, "today": today},
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
async def services_grid_fragment(
    request: Request, user=Depends(require_user),
) -> HTMLResponse:
    """Render the full service grid (called every 10s by HTMX).

    Visibility rules:
      - Only services the operator CONFIGURED at wizard time are shown
        on the main grid. Services they didn't pick are hidden so the
        dashboard reflects their actual stack, not the catalogue of
        everything OrcheStack could deploy.
      - A "Configure another service" link below the grid takes them
        back into the wizard scoped to add one of the unconfigured
        layers — that's how they grow the stack later.
      - For non-Admin users, services are further filtered by per-role
        permission (see admin's role-permissions table). Admins see
        everything that's configured.
    """
    try:
        data = await orchestrator.list_services()
        all_services = data.get("services", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("orchestrator list_services failed: %s", e)
        all_services = []
        error = str(e)

    # 1. Hide unconfigured services from the main grid. Operators who
    # skipped (say) Airflow during setup don't see an Airflow tile —
    # they reach Airflow's onboarding via the "Configure another
    # service" link below the grid.
    services = [s for s in all_services if s.get("configured")]

    # 2. Role-based filtering. Admins see everything; for everyone else
    # we ask the orchestrator's role-permission table which services
    # this user's roles grant `can_use` on, then keep only those.
    is_admin = "Admin" in (user.get("roles") or [])
    if not is_admin:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        try:
            perms_data = await orchestrator.list_my_service_permissions(cookie)
            allowed = set(perms_data.get("allowed_services", []))
            services = [s for s in services if s["name"] in allowed]
        except (httpx.HTTPError, ValueError) as e:
            # Fail-closed for non-admin users: if we can't determine
            # their permissions, show nothing rather than over-grant.
            log.warning(
                "service permission lookup failed for non-admin user %s: %s",
                user.get("user_id"), e,
            )
            services = []

    return templates.TemplateResponse(
        "_service_grid_fragment.html",
        {
            "request": request,
            "services": services,
            "error": error,
            "configured_count": len([s for s in all_services if s.get("configured")]),
            "unconfigured_count": len([s for s in all_services if not s.get("configured")]),
            "is_admin": is_admin,
        },
    )


# ===========================================================================
#  HTMX action: start a service
# ===========================================================================
@app.post("/api/dashboard/services/{name}/start", response_class=HTMLResponse,
           name="start_service_action")
async def start_service_action(
    request: Request, name: str, user=Depends(require_user),
) -> HTMLResponse:
    """Tell the orchestrator to start `name` AND open a session for the
    operator, return the updated card.

    Why both: pressing Start signals "I want to use this." A bare
    docker-start with no session row left the dashboard's Open sessions
    card empty for the operator who just started the service —
    semantically wrong (they're the reason it's running) and
    operationally bad (the reconciler's idle check would see "no
    sessions" and try to stop the service right back down again after
    the IDLE_THRESHOLD elapses).

    We delegate to open_session because it does both jobs atomically:
      1. INSERT a session row attributed to the operator (or reuse
         the operator's existing open session — same dedup path as the
         Open button).
      2. auto_start=True → docker compose start if not already running.

    Error path: if the orchestrator is unreachable we re-fetch the
    service to render its actual current state — the visual cue of
    the still-stopped dot is the error feedback.
    """
    try:
        await orchestrator.open_session(
            name, auto_start=True, user_id=user.get("user_id"),
        )
    except httpx.HTTPError as e:
        log.warning("start_service(%s) via open_session failed: %s", name, e)
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
async def pin_service_action(
    request: Request, name: str, ttl_seconds: int = Form(7200),
) -> HTMLResponse:
    """Pin a service (or extend an existing pin) with a TTL from the form.

    The approved mock's pin card has a select with values: +1h / +4h /
    +1d / +7d / Never. The select posts here with ttl_seconds as the
    submitted value; "Never" maps to None (orchestrator stores the pin
    without an expiry).
    """
    try:
        await orchestrator.pin_service(
            name, ttl_seconds=None if ttl_seconds == 0 else ttl_seconds,
        )
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
async def open_service_session(
    request: Request, name: str, action: str | None = None,
    user=Depends(require_user),
) -> JSONResponse:
    """Open an orchestrator session against `name` and return the tool URL.

    Returns JSON `{token, tool_url, service}`. The dashboard's client-side
    JS stores `token` in localStorage so the heartbeat ticker can refresh
    it, and opens `tool_url` in a new tab.

    Forwards the authenticated user's user_id to the orchestrator so the
    session row is attributed to the actual operator — without this the
    orchestrator falls back to DEFAULT_USER_ID (the platform's system
    user), which made every session show up as "OrcheStack system user"
    on the service-detail Open sessions card. The fallback was meant
    for batch/cron-style internal calls, not for operator-driven Open
    clicks from the dashboard.

    Tool URL resolution, in priority order:
      1. If `?action=<key>` is given AND the service has actions[],
         use that action's external_url (dbt has 'docs' + 'cli').
      2. Else if the service has external_url, use that (MinIO,
         Airbyte, PostgreSQL).
      3. Else fall back to ROOT_PATH/<name> via Traefik subpath
         (Metabase, pgAdmin).
    """
    try:
        result = await orchestrator.open_session(
            name, auto_start=True, user_id=user.get("user_id"),
        )
    except httpx.HTTPError as e:
        log.warning("open_session(%s) failed: %s", name, e)
        return JSONResponse(
            status_code=502,
            content={"error": "orchestrator unreachable", "detail": str(e)},
        )
    try:
        svc = await orchestrator.get_service(name) or {}
    except httpx.HTTPError:
        svc = {}

    host = request.url.hostname or "localhost"
    tool_url = None

    if action and svc.get("actions"):
        for a in svc["actions"]:
            if a.get("key") == action and a.get("external_url"):
                tool_url = a["external_url"].replace("{host}", host)
                break
    if tool_url is None and svc.get("external_url"):
        tool_url = svc["external_url"].replace("{host}", host)
    if tool_url is None:
        tool_url = f"{ROOT_PATH}/{name}"

    return JSONResponse({
        "token": result.get("token"),
        "service": name,
        "action": action,
        "tool_url": tool_url,
        "started": result.get("started", False),
    })


# Services with extra-long first-run setup (Metabase's Liquibase migration
# is the canonical case). The JS poller waits longer + shows specific copy
# for these so the operator doesn't think the system is stuck.
SLOW_BOOTSTRAP_SERVICES = {"metabase"}


@app.get("/api/dashboard/services/{name}/ready", name="service_ready_probe")
async def service_ready_probe(
    request: Request, name: str, action: str | None = None,
) -> JSONResponse:
    """Service readiness check the dashboard's Open button polls.

    Returns:
      {"ready": true}                                   service serves requests
      {"ready": false, "phase": "starting"}             container not yet healthy
      {"ready": false, "phase": "bootstrapping"}        container healthy, app still setting up
      {"ready": false, "phase": "unknown"}              we can't tell — operator should refresh

    The dashboard's open flow polls this endpoint instead of trying to
    hit the tool URL directly because cross-origin redirects from the
    tools (Metabase's first-run wizard 302s to /setup) confuse browser
    fetch readiness checks.
    """
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError as e:
        return JSONResponse({"ready": False, "phase": "unknown",
                              "detail": str(e)}, status_code=502)

    if not svc or svc.get("state") != "running":
        return JSONResponse({"ready": False, "phase": "starting"})

    # Container is running. For Metabase, additionally check setup
    # completion. Three distinct sub-phases reported up to the JS so the
    # operator sees what's actually happening during the long first boot:
    #
    #   "migrating"     — /api/health returns 503 {"status": "initializing"}.
    #                     Liquibase is running its 420 changesets against
    #                     the empty `metabase` database. 4-5 minutes on
    #                     Docker Desktop for macOS.
    #   "bootstrapping" — /api/health is 200, /api/session/properties has
    #                     setup-token. Migration done; orchestrator's
    #                     post-start hook is about to POST /api/setup.
    #                     Brief — usually under 5 seconds.
    #   ready=true      — setup-token is null. Operator can sign in.
    if name == "metabase":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                h = await client.get("http://orchestack-metabase:3000/api/health")
                if h.status_code != 200:
                    # 503 during init; surface as "migrating" so the JS
                    # can show the long-wait copy instead of the short
                    # "starting" copy.
                    return JSONResponse(
                        {"ready": False, "phase": "migrating"},
                    )
                r = await client.get(
                    "http://orchestack-metabase:3000/api/session/properties",
                )
                if r.status_code != 200:
                    return JSONResponse(
                        {"ready": False, "phase": "starting"},
                    )
                # Metabase's `setup-token` field persists in the in-memory
                # store even after /api/setup completes — the real signal
                # for "setup is done" is `has-user-setup: true`. M3 testing
                # discovered the orchestrator's bootstrap was finishing
                # successfully (POST /api/setup returned 200, audit log
                # had metabase_bootstrapped) but the dashboard kept polling
                # "bootstrapping" forever because we were watching the
                # wrong field.
                props = r.json()
                if not props.get("has-user-setup"):
                    return JSONResponse(
                        {"ready": False, "phase": "bootstrapping"},
                    )
                return JSONResponse({"ready": True})
        except httpx.HTTPError:
            return JSONResponse({"ready": False, "phase": "starting"})

    # pgAdmin: same pattern as Metabase. The Docker container healthcheck
    # already gates state==running on /misc/ping returning 200, but
    # there's a 5-10s window between gunicorn starting and the first
    # successful ping where the container reports "starting" health
    # status — and Traefik happily routes during that window, the user
    # sees a 502 Bad Gateway or the dashboard's FastAPI 404 in the new
    # tab. Probe /misc/ping ourselves so the dashboard waits until
    # pgAdmin can actually serve requests before opening the tab.
    if name == "pgadmin":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # /misc/ping must include the SCRIPT_NAME prefix because
                # pgAdmin rejects any path that doesn't start with it.
                r = await client.get(
                    "http://orchestack-pgadmin:80/app/pgadmin/misc/ping",
                )
                if r.status_code == 200:
                    return JSONResponse({"ready": True})
                return JSONResponse({"ready": False, "phase": "starting"})
        except httpx.HTTPError:
            return JSONResponse({"ready": False, "phase": "starting"})

    # M4 services — each tool's "I'm actually serving" signal differs.
    # Add a branch per service so the operator's Open click waits for
    # the real readiness instead of just container=running.
    _M4_READY_PROBES = {
        "minio":        ("orchestack-minio",        9000, "/minio/health/ready"),
        # Airflow's webserver enforces AIRFLOW__WEBSERVER__BASE_URL —
        # /health (no prefix) returns 404 when BASE_URL is set to a
        # subpath; the actual health endpoint is BASE_URL + /health.
        # Match the Traefik subpath we set in services/airflow.yml.
        "airflow":      ("orchestack-airflow",      8080, "/app/airflow/health"),
        # Airbyte's multi-container deployment exposes the API on
        # orchestack-airbyte-server:8001 (not a single 'airbyte'
        # container). The server is the right gate for "the whole
        # stack is ready" — the webapp's nginx returns 200 before the
        # API is up, so probing the webapp would race.
        "airbyte":      ("orchestack-airbyte-server", 8001, "/api/v1/health"),
        # dbt's default probe (no ?action= specified) hits the docs
        # server — that's the older single-action behaviour. The
        # action-specific probes below kick in when the dashboard JS
        # passes ?action=docs or ?action=cli (matching the catalogue
        # actions: list).
        "dbt":          ("orchestack-dbt",          8080, "/"),
        # OpenMetadata serves /healthcheck on its ADMIN port (8586) not
        # the operator-facing API port (8585). On 8585 the path is
        # unknown to the React router → 404 → the dashboard's ready loop
        # never gets a 200 → Open click never fires window.open(). The
        # /api/v1/system/version endpoint is the right gate for "the
        # actual API is serving" on port 8585 (returns version JSON in
        # ~30ms once the SPA + API are both up).
        "openmetadata": ("orchestack-openmetadata", 8585, "/api/v1/system/version"),
    }
    # Per-action probes for multi-action services. dbt-docs takes
    # 30-90s to come up (runs `dbt deps + dbt run + dbt docs generate`
    # in the entrypoint); ttyd is up in ~2s. Probing them separately
    # means the Open Terminal button is usable as soon as ttyd is
    # listening, even while docs is still generating — exactly the
    # case where an analytics engineer needs to drop in fast to debug.
    _M4_ACTION_PROBES = {
        ("dbt", "docs"): ("orchestack-dbt", 8080, "/index.html"),
        # ttyd serves at its --base-path, NOT at /. Probing /
        # returns 404 because ttyd's router only matches the
        # configured prefix. Match the Traefik subpath we set in
        # services/dbt.yml: --base-path /app/dbt-terminal.
        ("dbt", "cli"):  ("orchestack-dbt", 7681, "/app/dbt-terminal/"),
        # Great Expectations follows dbt's pattern exactly: Python
        # http.server serving generated data docs on 8080, ttyd on
        # 7681 with --base-path /app/ge-terminal.
        ("ge", "docs"):  ("orchestack-ge", 8080, "/index.html"),
        ("ge", "cli"):   ("orchestack-ge", 7681, "/app/ge-terminal/"),
    }
    probe = None
    if action is not None and (name, action) in _M4_ACTION_PROBES:
        probe = _M4_ACTION_PROBES[(name, action)]
    elif name in _M4_READY_PROBES:
        probe = _M4_READY_PROBES[name]
    if probe is not None:
        host, port, path = probe
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"http://{host}:{port}{path}")
                if r.status_code == 200:
                    return JSONResponse({"ready": True})
                return JSONResponse({"ready": False, "phase": "starting"})
        except httpx.HTTPError:
            return JSONResponse({"ready": False, "phase": "starting"})

    # dbt + GE are CLI tool containers (no HTTP UI). state==running is
    # the right signal for them — `docker exec` is the operator's entry
    # point. Fall through to the default ready=true below.

    # Default for any other managed service: state==running is the signal.
    # When M4 lands more managed services, each should add its own probe
    # branch above with a service-specific readiness check.
    return JSONResponse({"ready": True})


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
    request: Request, limit: int = 10, offset: int = 0,
) -> HTMLResponse:
    """Render the active-sessions table fragment (polled every 10s).

    Page size defaults to 10 to keep the polled response small + the
    table compact. Operator can bump via the page-size selector on the
    sessions page.
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
    limit: int = 10, offset: int = 0,
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
