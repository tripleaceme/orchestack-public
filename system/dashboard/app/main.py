"""OrcheStack dashboard. Traefik strips `/app` prefix before forwarding; root_path="/app" reconstructs external URLs while internal routes stay at `/`."""

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

# orchestrator_sha intentionally omitted — would need a cross-service call per render.
templates.env.globals["build_info"] = {
    "bundle_version":  BUNDLE_VERSION,
    "dashboard_sha":   DASHBOARD_BUILD_SHA,
    "orchestrator_sha": "",
}

orchestrator = OrchestratorClient(ORCHESTRATOR_URL)

app = FastAPI(
    title="OrcheStack dashboard",
    description="Administrator UI.",
    version="0.6.0",
    root_path=ROOT_PATH,
    docs_url=None,
    redoc_url=None,
)


@app.middleware("http")
async def no_html_cache(request: Request, call_next):
    """Set Cache-Control: no-store on HTML responses — browser heuristics cached dynamic dashboard HTML for ~5 minutes. Scoped to text/html so static assets stay cacheable."""
    response = await call_next(request)
    ct = response.headers.get("content-type", "")
    if ct.startswith("text/html"):
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
    return response


@app.on_event("startup")
async def on_startup() -> None:
    log.info(
        "orchestack-dashboard root_path=%s orchestrator=%s — ready",
        ROOT_PATH, ORCHESTRATOR_URL,
    )


# ===========================================================================
#  Auth — current user dependency
# ===========================================================================
async def current_user(request: Request) -> dict[str, object] | None:
    """Resolve current user from session cookie, or None. Does not 401 — see require_user."""
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
        # Can't return a redirect directly from a Depends — raise HTTPException that
        # the global exception handler turns into a redirect, preserving `next`.
        raise HTTPException(
            status_code=307,
            detail="login_required",
            headers={"Location": f"{ROOT_PATH}/login?next={request.url.path}"},
        )
    return user


async def require_admin(request: Request) -> dict[str, object]:
    """Guard route dependency — require Admin role. Redirects unauth'd; 403s non-admins so they understand the page exists but isn't theirs."""
    user = await require_user(request)
    if "Admin" not in user.get("roles", []):
        raise HTTPException(403, "Admin role required to access this page.")
    return user


def _extract_service_from_path(path: str) -> str | None:
    """Pull service name from a 404'd URL of shape /app/<service>/<rest>. Traefik's per-service router only exists while the container is up; stopped services fall through to dashboard's catchall."""
    p = path.lstrip("/")
    prefix = (ROOT_PATH or "/app").strip("/")
    if prefix and p.startswith(prefix + "/"):
        p = p[len(prefix) + 1:]
    parts = p.split("/", 1)
    if not parts or not parts[0]:
        return None
    candidate = parts[0]
    # Catalogue lookup downstream confirms this is actually a real service
    # (vs. dashboard's own routes like /app/sessions).
    return candidate


async def _service_404_response(request: Request, exc):
    """Render HTML 404 with diagnosis bullets. SECURITY: request.url.path is user-controllable and the template renders bullets with `| safe`, so we MUST html-escape `path` at this boundary to prevent XSS."""
    import html as _html
    # FastAPI strips ASGI root_path; reattach so display matches the operator's address bar.
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
    """Convert 307 login_required to RedirectResponse; render HTML error pages for 403/404 on non-API routes."""
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
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=dict(exc.headers or {}),
    )


# Starlette raises its own HTTPException for unmatched routes — register
# the class too so /app/<svc>/... 404s route to our HTML handler, not FastAPI's JSON.
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
    """Liveness check. Deliberately does NOT call the orchestrator — dashboard stays healthy to display "orchestrator unreachable" UI rather than getting cycled by Docker."""
    return "ok\n"


# ===========================================================================
#  Pages
# ===========================================================================
async def _aggregate_kpis() -> dict:
    """Aggregate the four KPI-strip metrics from the orchestrator. Shared by /home and the HTMX polling endpoint so the data shape stays in sync."""
    services_running = 0
    services_total = 0
    catalogue_total = 0
    try:
        svc_data = await orchestrator.list_services()
        all_services = svc_data.get("services", [])
        catalogue_total = len(all_services)
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

    last_event_type   = None
    last_event_target = None
    last_event_actor  = None
    last_event_when   = None
    last_event_ago    = None
    try:
        audit_data = await orchestrator.list_audit(limit=1, offset=0)
        events = audit_data.get("events", [])
        if events:
            last_event_type   = events[0].get("event_type")
            last_event_target = events[0].get("target")
            last_event_actor  = (events[0].get("actor_full_name")
                                 or events[0].get("actor_username"))
            last_event_when   = events[0].get("created_at")
            last_event_ago    = _format_relative(last_event_when)
    except (httpx.HTTPError, ValueError) as e:
        log.warning("KPI list_audit failed: %s", e)

    return {
        "services_running":   services_running,
        "services_total":     services_total,
        "catalogue_total":    catalogue_total,
        "active_sessions":    active_sessions,
        "last_event_type":    last_event_type,
        "last_event_target":  last_event_target,
        "last_event_actor":   last_event_actor,
        "last_event_when":    last_event_when,
        "last_event_ago":     last_event_ago,
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
                "total":      kpi.get("catalogue_total") or kpi["services_total"],
            } if kpi.get("catalogue_total") else None,
        },
    )


@app.get("/api/dashboard/kpi-strip", response_class=HTMLResponse,
          name="kpi_strip_fragment")
async def kpi_strip_fragment(
    request: Request, user=Depends(require_user)
) -> HTMLResponse:
    """KPI strip fragment — HTMX-polled every 10s from the home page."""
    kpi = await _aggregate_kpis()
    return templates.TemplateResponse(
        "_kpi_strip_fragment.html", {"request": request, "kpi": kpi},
    )


@app.get("/sessions", response_class=HTMLResponse, name="sessions_page")
async def sessions_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/sessions` — KPI strip + Open sessions table + Keep-warm pins."""
    from datetime import datetime, timezone

    sessions: list[dict] = []
    try:
        sess_data = await orchestrator.list_sessions(limit=200, offset=0)
        sessions = sess_data.get("sessions", [])
    except (httpx.HTTPError, ValueError) as e:
        log.warning("sessions_page list_sessions failed: %s", e)

    open_count       = len(sessions)
    unique_services  = len({s.get("service") for s in sessions if s.get("service")})

    unique_users = len({s.get("user_id") for s in sessions if s.get("user_id")})
    sorted_by_recent = sorted(
        sessions,
        key=lambda s: s.get("last_heartbeat_at") or s.get("opened_at") or "",
        reverse=True,
    )
    most_active_user = (
        sorted_by_recent[0].get("full_name") or sorted_by_recent[0].get("username")
        if sorted_by_recent else None
    )

    oldest_age   = None
    oldest_what  = None
    if sessions:
        sorted_by_open = sorted(sessions, key=lambda s: s.get("opened_at") or "")
        oldest = sorted_by_open[0]
        oldest_what = oldest.get("service")
        try:
            o = (oldest.get("opened_at") or "").replace("Z", "+00:00")
            ts = datetime.fromisoformat(o)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            delta = int((datetime.now(timezone.utc) - ts).total_seconds())
            if delta < 60:
                oldest_age = f"{delta}s"
            elif delta < 3600:
                oldest_age = f"{delta // 60}m"
            elif delta < 86400:
                oldest_age = f"{delta // 3600}h {(delta % 3600) // 60}m"
            else:
                oldest_age = f"{delta // 86400}d {(delta % 86400) // 3600}h"
        except (ValueError, TypeError):
            pass

    idle_count = sum(1 for s in sessions if (s.get("idle_seconds") or 0) > 600)

    pins: list[dict] = []
    try:
        svc_data = await orchestrator.list_services()
        pinned_services = [
            s for s in svc_data.get("services", []) if s.get("pinned")
        ]
        for svc in pinned_services:
            try:
                pin_info = await orchestrator.get_pin(svc["name"])
                if pin_info:
                    pins.append({
                        "service":      svc["name"],
                        "display_name": svc["display_name"],
                        "pinned_by_username":  pin_info.get("pinned_by_username"),
                        "pinned_by_full_name": pin_info.get("pinned_by_full_name"),
                        "pinned_at":           pin_info.get("pinned_at"),
                        "expires_at":          pin_info.get("expires_at"),
                    })
            except (httpx.HTTPError, ValueError):
                pass
    except (httpx.HTTPError, ValueError) as e:
        log.warning("sessions_page list_services failed: %s", e)

    return templates.TemplateResponse(
        "sessions.html",
        {
            "request": request, "page_title": "Sessions", "user": user,
            "kpi_open":           open_count,
            "kpi_services":       unique_services,
            "kpi_users":          unique_users,
            "kpi_user_who":       most_active_user,
            "kpi_oldest_age":     oldest_age,
            "kpi_oldest_what":    oldest_what,
            "kpi_idle":           idle_count,
            "pins":               pins,
        },
    )


@app.get("/audit", response_class=HTMLResponse, name="audit_page")
async def audit_page(request: Request, user=Depends(require_user)) -> HTMLResponse:
    """`/app/audit` — paginated audit log with filters. Event-types dropdown is derived from the last 500 audit rows so it tracks the catalogue without a hard-coded list."""
    event_types: list[str] = []
    try:
        recent = await orchestrator.list_audit(limit=500, offset=0)
        event_types = sorted({
            ev.get("event_type") for ev in recent.get("events", [])
            if ev.get("event_type")
        })
    except (httpx.HTTPError, ValueError) as e:
        log.warning("audit_page event_types fetch failed: %s", e)

    services: list[dict] = []
    try:
        svc_data = await orchestrator.list_services()
        services = sorted(
            svc_data.get("services", []),
            key=lambda s: s.get("display_name", ""),
        )
    except (httpx.HTTPError, ValueError):
        pass

    return templates.TemplateResponse(
        "audit.html",
        {
            "request": request, "page_title": "Audit log", "user": user,
            "event_types": event_types,
            "services": services,
        },
    )


@app.get("/credentials", response_class=HTMLResponse, name="credentials_page")
async def credentials_page(
    request: Request,
    reveal: bool = False, service: str = "All", search: str = "",
    user=Depends(require_admin),
) -> HTMLResponse:
    """`/app/credentials` — admin view for reading + updating .env variables."""
    context = await _build_credentials_context(reveal, service, search)
    return templates.TemplateResponse(
        "credentials.html",
        {**context, "request": request, "page_title": "Credentials", "user": user},
    )


# Bucketing: longest matching prefix wins ("MB_DB_USER" → "Metabase"
# not "Other"). List order is both prefix-match order AND dropdown order.
CREDENTIAL_SERVICE_GROUPS: list[tuple[str, list[str]]] = [
    ("OrcheStack platform", ["ORCHESTACK_"]),
    ("Image tags",          ["_TAG"]),  # suffix-match handled specially
    # Env keys keep WAREHOUSE_DB_ prefix for .env backward compat; only
    # display name changed (avoid "pipeline" sounding like a pipeline tool).
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
    """Return operator-facing service name for a .env key. *_TAG suffix takes priority so image versions group together."""
    if key.endswith("_TAG"):
        return "Image tags"
    for group, prefixes in CREDENTIAL_SERVICE_GROUPS:
        if group == "Image tags":
            continue
        for prefix in prefixes:
            if key.startswith(prefix):
                return group
    return "Other"


CREDENTIAL_SERVICE_TAG_MAP: dict[str, str] = {
    "OrcheStack platform": "platform",
    "Image tags":          "image",
    "Warehouse":           "warehouse",
    "Airbyte":             "airbyte",
    "Apache Airflow":      "airflow",
    "dbt Core":            "dbt",
    "Metabase":            "metabase",
    "MinIO":               "minio",
    "OpenMetadata":        "openmetadata",
    "Great Expectations":  "ge",
    "pgAdmin":             "pgadmin",
    "Other":               "other",
}


async def _build_credentials_context(
    reveal: bool, service: str, search: str,
) -> dict:
    """Shared context-builder for credentials page + table fragment. KPI metrics aggregate over the FULL set (pre-filter) before service+search filters apply."""
    try:
        data = await orchestrator.list_credentials(reveal=reveal)
        credentials = data.get("credentials", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("list_credentials failed: %s", e)
        credentials = []
        error = str(e)

    for c in credentials:
        svc = _service_for_credential(c["key"])
        c["service"] = svc
        c["service_tag"] = CREDENTIAL_SERVICE_TAG_MAP.get(svc, svc.lower())

    total_count     = len(credentials)
    sensitive_count = sum(1 for c in credentials if c.get("is_sensitive"))
    readonly_count  = sum(1 for c in credentials if c.get("is_readonly"))

    last_edited_key = None
    last_edited_at  = None
    try:
        audit_data = await orchestrator.list_audit(limit=10, offset=0)
        for ev in audit_data.get("events", []):
            if ev.get("event_type") == "credential_updated":
                last_edited_key = ev.get("target")
                last_edited_at  = ev.get("created_at")
                break
    except (httpx.HTTPError, ValueError):
        pass

    services_present = sorted(
        {c["service"] for c in credentials},
        key=lambda s: ([g for g, _ in CREDENTIAL_SERVICE_GROUPS].index(s)
                       if s in [g for g, _ in CREDENTIAL_SERVICE_GROUPS]
                       else len(CREDENTIAL_SERVICE_GROUPS)),
    )
    service_counts: dict[str, int] = {}
    for c in credentials:
        service_counts[c["service"]] = service_counts.get(c["service"], 0) + 1

    if service != "All":
        credentials = [c for c in credentials if c["service"] == service]
    if search:
        needle = search.upper()
        credentials = [c for c in credentials if needle in c["key"].upper()]

    return {
        "credentials":      credentials,
        "reveal":           reveal,
        "error":            error,
        "selected_service": service,
        "search":           search,
        "services_present": services_present,
        "service_counts":   service_counts,
        "total_count":      total_count,
        "sensitive_count":  sensitive_count,
        "readonly_count":   readonly_count,
        "last_edited_key":  last_edited_key,
        "last_edited_at":   last_edited_at,
    }


@app.get("/api/dashboard/credentials/table", response_class=HTMLResponse,
          name="credentials_table_fragment")
async def credentials_table_fragment(
    request: Request,
    reveal: bool = False,
    service: str = "All",
    search: str = "",
    user=Depends(require_admin),
) -> HTMLResponse:
    """HTMX fragment — the credentials table, optionally filtered by
    service AND/OR a substring search over key names."""
    context = await _build_credentials_context(reveal, service, search)
    return templates.TemplateResponse(
        "_credentials_table_fragment.html",
        {**context, "request": request},
    )


@app.post("/api/dashboard/credentials/{key}",
           response_class=HTMLResponse, name="credentials_update_action")
async def credentials_update_action(
    request: Request, key: str, value: str = Form(...),
    service: str = Form("All"),
    user=Depends(require_admin),
) -> HTMLResponse:
    """Update one .env variable + re-render its table row. Service filter is threaded through the form so an edit doesn't snap the view back to All."""
    try:
        await orchestrator.update_credential(
            key, value, actor_user_id=user.get("user_id"),
        )
    except httpx.HTTPError as e:
        log.warning("update_credential(%s) failed: %s", key, e)
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
    """Save profile changes. Only sends fields the operator actually changed — passing every form field would overwrite e.g. company_name with empty when they only meant to update full_name."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    save_error = None
    saved = False
    try:
        current = await orchestrator.get_my_profile(cookie)
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
    """Users page — aggregates KPI metrics at first paint; table loads via HTMX fragment so invite/grant swaps the table without re-rendering the strip."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    total_users   = 0
    admin_count   = 0
    active_24h    = 0
    not_signed_in = 0
    most_recent_login_username = None
    try:
        users_data = await orchestrator.admin_list_users(cookie)
        users = users_data.get("users", [])
        total_users = len(users)
        admin_count = sum(1 for u in users if "Admin" in (u.get("roles") or []))
        from datetime import datetime, timedelta, timezone
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        for u in users:
            lla = u.get("last_login_at")
            if lla:
                try:
                    last = datetime.fromisoformat(lla.replace("Z", "+00:00"))
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    if last >= cutoff:
                        active_24h += 1
                        if (not most_recent_login_username or
                                last > datetime.fromisoformat(
                                    (most_recent_login_username[1] or "").replace("Z", "+00:00")
                                    if most_recent_login_username[1] else "1970-01-01T00:00:00+00:00")):
                            most_recent_login_username = (u.get("full_name") or u.get("username"), lla)
                except (ValueError, TypeError):
                    pass
            else:
                not_signed_in += 1
    except (httpx.HTTPError, ValueError) as e:
        log.warning("users_page KPI fetch failed: %s", e)

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request, "page_title": "Users", "user": user,
            "kpi_total":        total_users,
            "kpi_admins":       admin_count,
            "kpi_active_24h":   active_24h,
            "kpi_not_signed":   not_signed_in,
            "kpi_active_who":   most_recent_login_username[0] if most_recent_login_username else None,
        },
    )


@app.get("/api/dashboard/users/table", response_class=HTMLResponse,
          name="users_table_fragment")
async def users_table_fragment(request: Request, user=Depends(require_admin)) -> HTMLResponse:
    """HTMX fragment for the Users table. invite_result is None on plain loads — only the invite POST handler carries one. Do NOT read request.session here: SessionMiddleware isn't installed (cookies + orchestrator are the source of truth), and access raises AssertionError, hangs UI on "Loading users…"."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        users_data = await orchestrator.admin_list_users(cookie)
        roles_data = await orchestrator.admin_list_roles(cookie)
        users_list = users_data.get("users", [])
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        for u in users_list:
            u["last_login_relative"] = _format_relative(u.get("last_login_at"), now)
        return templates.TemplateResponse(
            "_users_table_fragment.html",
            {
                "request": request,
                "users": users_list,
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


def _format_relative(iso_ts: str | None, now=None) -> str:
    """Humanise a timestamp into 'Ns/Nm/Nh/Nd ago'. Returns '—' for None or unparseable input."""
    if not iso_ts:
        return "—"
    try:
        from datetime import datetime, timezone
        iso = iso_ts.replace("Z", "+00:00") if iso_ts.endswith("Z") else iso_ts
        ts = datetime.fromisoformat(iso)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if now is None:
            now = datetime.now(timezone.utc)
        delta = int((now - ts).total_seconds())
        if delta < 60:   return f"{max(1, delta)}s ago"
        if delta < 3600: return f"{delta // 60}m ago"
        if delta < 86400: return f"{delta // 3600}h ago"
        return f"{delta // 86400}d ago"
    except (ValueError, TypeError):
        return "—"


@app.post("/api/dashboard/users/invite", response_class=HTMLResponse,
           name="users_invite_action")
async def users_invite_action(
    request: Request,
    username: str = Form(...), email: str = Form(...),
    full_name: str = Form(...),
    # role_id is str (not int) so the "No role yet" option (sends "") doesn't
    # trigger FastAPI's int parser and 422 the request.
    role_id: str = Form(""),
    user=Depends(require_admin),
) -> HTMLResponse:
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    invite_result = None
    invite_error = None
    role_names: list[str] = []
    role_id_int: int | None = None
    if role_id.strip():
        try:
            role_id_int = int(role_id)
        except ValueError:
            invite_error = f"Invalid role id: {role_id!r}"
    if role_id_int is not None:
        # Orchestrator API takes role names, not IDs.
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

    # Re-render with invite_result so the template can one-time-display the starter password.
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


async def _roles_render_context(
    request: Request, user: dict,
    selected_role_id: int | None = None,
) -> dict:
    """Common context for roles fragment renders. matrix_by_role merges role-specific + wildcard (`*`) grants so a `*` row shows checks across all services."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    try:
        roles_data = await orchestrator.admin_list_roles(cookie)
        perms_data = await orchestrator.admin_list_permissions(cookie)
        services_data = await orchestrator.list_services()
        users_data = await orchestrator.admin_list_users(cookie)
        error = None
    except httpx.HTTPError as e:
        log.warning("roles fragment failed: %s", e)
        roles_data = {"roles": []}
        perms_data = {"permissions": []}
        services_data = {"services": []}
        users_data  = {"users": []}
        error = str(e)

    perms_by_role: dict[int, list[dict]] = {}
    for p in perms_data.get("permissions", []):
        perms_by_role.setdefault(p["role_id"], []).append(p)

    services = services_data.get("services", [])

    matrix_by_role: dict[int, dict[str, dict]] = {}
    for role in roles_data.get("roles", []):
        role_perms = perms_by_role.get(role["id"], [])
        wildcard = next((p for p in role_perms if p.get("service_name") == "*"), None)
        by_service: dict[str, dict] = {}
        for svc in services:
            svc_perm = next(
                (p for p in role_perms if p.get("service_name") == svc["name"]),
                None,
            )
            effective = svc_perm or wildcard or {}
            by_service[svc["name"]] = {
                "can_start":       bool(effective.get("can_start")),
                "can_use":         bool(effective.get("can_use")),
                "can_force_stop":  bool(effective.get("can_force_stop")),
                "can_edit_config": bool(effective.get("can_edit_config")),
                "has_any":         any(
                    effective.get(k) for k in
                    ("can_start", "can_use", "can_force_stop", "can_edit_config")
                ),
            }
        matrix_by_role[role["id"]] = by_service

    member_count_by_role: dict[int, int] = {}
    for role in roles_data.get("roles", []):
        count = sum(
            1 for u in users_data.get("users", [])
            if role.get("name") in (u.get("roles") or [])
        )
        member_count_by_role[role["id"]] = count

    all_roles = roles_data.get("roles", [])

    # Default to first role on first paint so the page renders meaningfully.
    selected_role = None
    if selected_role_id is not None:
        selected_role = next(
            (r for r in all_roles if r["id"] == selected_role_id), None
        )
    if selected_role is None and all_roles:
        selected_role = all_roles[0]

    return {
        "request": request,
        "all_roles": all_roles,
        "selected_role": selected_role,
        "selected_role_id": (selected_role or {}).get("id"),
        "roles": [selected_role] if selected_role else [],
        "perms_by_role": perms_by_role,
        "matrix_by_role": matrix_by_role,
        "member_count_by_role": member_count_by_role,
        "services": services,
        "error": error,
    }


@app.get("/api/dashboard/roles/list", response_class=HTMLResponse,
          name="roles_list_fragment")
async def roles_list_fragment(
    request: Request,
    selected_role_id: int | None = None,
    user=Depends(require_admin),
) -> HTMLResponse:
    ctx = await _roles_render_context(request, user, selected_role_id=selected_role_id)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.post("/api/dashboard/roles/create", response_class=HTMLResponse,
           name="roles_create_action")
async def roles_create_action(
    request: Request, name: str = Form(...),
    description: str = Form(""), user=Depends(require_admin),
) -> HTMLResponse:
    """Create a role; on success select it + fire `roleCreated` HX-Trigger so page JS closes the form and toasts."""
    import json as _json
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    new_role_id: int | None = None
    create_error: str | None = None
    try:
        result = await orchestrator.admin_create_role(cookie, name, description or None)
        # Orchestrator returns {"role_id": int}; fall back to "id" for future variants.
        new_role_id = (result or {}).get("role_id") or (result or {}).get("id")
    except httpx.HTTPError as e:
        log.warning("create role failed: %s", e)
        create_error = (
            getattr(getattr(e, "response", None), "text", None) or str(e)
        )

    ctx = await _roles_render_context(request, user, selected_role_id=new_role_id)
    ctx["create_error"] = create_error
    ctx["just_created_name"] = name if new_role_id else None
    resp = templates.TemplateResponse("_roles_list_fragment.html", ctx)
    if new_role_id:
        resp.headers["HX-Trigger"] = _json.dumps({
            "roleCreated": {"role_id": new_role_id, "role_name": name},
        })
    return resp


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


@app.post("/api/dashboard/roles/{role_id}/permissions/bulk-set",
           response_class=HTMLResponse, name="roles_bulk_set_permissions_action")
async def roles_bulk_set_permissions_action(
    request: Request, role_id: int, user=Depends(require_admin),
) -> HTMLResponse:
    """Bulk replace the role's per-service permission set. Form fields are `<svc>__can_<perm>` (double underscore). All-false revokes the row (keeps permission table semantically clean: "no row" == "no permissions")."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    form = await request.form()

    try:
        perms_data = await orchestrator.admin_list_permissions(cookie)
        existing_by_service = {
            p["service_name"]: p
            for p in perms_data.get("permissions", [])
            if p["role_id"] == role_id
        }
    except (httpx.HTTPError, ValueError):
        existing_by_service = {}

    # Services with no form fields are treated as all-false so un-ticking every box revokes.
    try:
        svc_data = await orchestrator.list_services()
        all_services = svc_data.get("services", [])
    except (httpx.HTTPError, ValueError):
        all_services = []

    for svc in all_services:
        sn = svc["name"]
        can_start       = form.get(f"{sn}__can_start") == "true"
        can_use         = form.get(f"{sn}__can_use") == "true"
        can_force_stop  = form.get(f"{sn}__can_force_stop") == "true"
        can_edit_config = form.get(f"{sn}__can_edit_config") == "true"
        any_perm = can_start or can_use or can_force_stop or can_edit_config

        try:
            if any_perm:
                await orchestrator.admin_grant_permission(
                    cookie, role_id=role_id, service_name=sn,
                    can_start=can_start, can_use=can_use,
                    can_force_stop=can_force_stop, can_edit_config=can_edit_config,
                )
            else:
                existing = existing_by_service.get(sn)
                if existing:
                    await orchestrator.admin_revoke_permission(
                        cookie, existing["id"],
                    )
        except httpx.HTTPError as e:
            log.warning(
                "bulk-set permissions failed for %s/%s: %s", role_id, sn, e,
            )

    ctx = await _roles_render_context(request, user, selected_role_id=role_id)
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


@app.post("/api/dashboard/roles/{role_id}/permissions/set",
           response_class=HTMLResponse, name="roles_set_permissions_action")
async def roles_set_permissions_action(
    request: Request, role_id: int,
    service_name: str = Form(...),
    can_start: bool = Form(False),
    can_use: bool = Form(False),
    can_force_stop: bool = Form(False),
    can_edit_config: bool = Form(False),
    selected_role_id: int | None = Form(None),
    user=Depends(require_admin),
) -> HTMLResponse:
    """Replace per-service permission set for a role. All-false revokes (keeps table clean). selected_role_id is threaded back so the fragment doesn't snap to the first role on every click."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    any_perm = can_start or can_use or can_force_stop or can_edit_config
    try:
        if any_perm:
            await orchestrator.admin_grant_permission(
                cookie, role_id=role_id, service_name=service_name,
                can_start=can_start, can_use=can_use,
                can_force_stop=can_force_stop, can_edit_config=can_edit_config,
            )
        else:
            perms_data = await orchestrator.admin_list_permissions(cookie)
            existing = next(
                (p for p in perms_data.get("permissions", [])
                 if p["role_id"] == role_id and p["service_name"] == service_name),
                None,
            )
            if existing:
                await orchestrator.admin_revoke_permission(cookie, existing["id"])
    except httpx.HTTPError as e:
        log.warning("set permissions failed: %s", e)
    ctx = await _roles_render_context(
        request, user, selected_role_id=selected_role_id or role_id,
    )
    return templates.TemplateResponse("_roles_list_fragment.html", ctx)


# Legacy grant-only endpoint kept for backwards-compat with any callers
# (audit, scripted ops). Forwards to set-permissions semantics.
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
    ctx = await _roles_render_context(request, user, selected_role_id=role_id)
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


# Per-service credential groupings: (title, subtitle, key_substring_patterns).
# Rules apply in order; first match wins. Unmatched → "Other".
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
    """Group flat credential list by CREDENTIAL_GROUP_RULES. Returns ordered list of `{title, sub, creds}`, empty groups skipped, unmatched bucketed into trailing "Other"."""
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
    """`/app/services/{name}/config` — per-service credentials editor scoped to this service's CREDENTIAL_SERVICE_GROUPS bucket."""
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

    groups_for_service = SERVICE_CREDENTIAL_GROUPS.get(name, [])
    for c in all_creds:
        c["service"] = _service_for_credential(c["key"])
    creds = [c for c in all_creds if c["service"] in groups_for_service]
    grouped = _group_credentials(creds)

    # Last edit on this service = most recent credential_updated audit event whose target is one of this service's keys.
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
    """Save the per-service config form. Form is keyed ENV_VAR_NAME → new value. Skips read-only keys and unchanged values (no spurious audit entries)."""
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
    test_failures: list[dict] = []
    for raw_key, raw_val in form.items():
        if not raw_key or raw_key.startswith("__"):
            continue
        if raw_key not in by_key:
            continue
        cur = by_key[raw_key]
        if cur.get("is_readonly"):
            continue
        if cur.get("value", "") == raw_val:
            continue

        # Test BEFORE persist for DB-typed creds. Orchestrator returns testable=false
        # for un-verifiable keys (image tags, etc.); a failed test skips the save
        # so the operator can fix the value instead of bricking the service.
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
                continue
        except httpx.HTTPError as e:
            # Test endpoint unreachable: don't block save — post-save Stop/Start is the safety net.
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

    # Surface test failures as user-visible error when no save error — they're the actionable signal.
    if test_failures and not save_error:
        save_error = "Live connection test failed for: " + ", ".join(
            f"{f['key']} (as {f['as']}: {f['error']})" for f in test_failures
        )

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
    """`/app/services/{name}` — per-service detail page. Activity list streams via HTMX from service_activity_fragment so the filter form can drive it."""
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError:
        svc = None

    # Orchestrator returns `service` not `service_name` — match its schema.
    # Dedupe by user_id, keeping most-recent session per user: one human opening
    # multiple tabs against the same tool shouldn't render as multiple rows.
    # Stopped service ⇒ render empty: active session against stopped container
    # is stale and conflicting signal ("service stopped but live session?").
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

    uptime_display = None
    if svc and svc.get("started_at"):
        try:
            from datetime import datetime, timezone
            # Defend against two Docker timestamp formats so dashboards against
            # older orchestrators still parse:
            #   docker ps CreatedAt:       "2026-06-18 23:56:55 +0000 UTC"
            #   docker inspect StartedAt:  "2026-06-19T01:23:45.123456789Z"
            iso = svc["started_at"].strip()
            iso = iso.replace(" UTC", "")
            # fromisoformat (pre-3.11) doesn't accept trailing Z.
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            # Docker emits nanoseconds (9 digits); Python rejects >6.
            if "." in iso:
                dot = iso.index(".")
                tail_start = dot + 1
                tail_end = tail_start
                while tail_end < len(iso) and iso[tail_end].isdigit():
                    tail_end += 1
                if tail_end - tail_start > 6:
                    iso = iso[:tail_start + 6] + iso[tail_end:]
            # fromisoformat needs the colon in "+00:00".
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
    """Render activity rows scoped to a single service in compact layout (distinct from audit_table_fragment's data-table treatment)."""
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
    """Proxy to orchestrator's /api/health as HTML fragment. Returns 200 even on orchestrator failure so HTMX afterRequest fires and the dashboard's connection indicator stays green — orchestrator-reachability is a distinct signal."""
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
    """Render the full service grid (polled every 10s). Shows only configured services; non-admins are further filtered by per-role can_use permission."""
    try:
        data = await orchestrator.list_services()
        all_services = data.get("services", [])
        error = None
    except (httpx.HTTPError, ValueError) as e:
        log.warning("orchestrator list_services failed: %s", e)
        all_services = []
        error = str(e)

    services = [s for s in all_services if s.get("configured")]

    is_admin = "Admin" in (user.get("roles") or [])
    if not is_admin:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        try:
            perms_data = await orchestrator.list_my_service_permissions(cookie)
            allowed = set(perms_data.get("allowed_services", []))
            services = [s for s in services if s["name"] in allowed]
        except (httpx.HTTPError, ValueError) as e:
            # Fail-closed for non-admin: show nothing rather than over-grant on perm lookup error.
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
    """Start `name` AND open a session for the operator. A bare docker-start with no session row leaves Open-sessions empty for the operator who just started the service — and the reconciler's idle check then stops the service after IDLE_THRESHOLD.

    Optimistically renders the card in state="starting" so the operator sees
    immediate feedback. The actual start now runs in a background task on
    the orchestrator side (no longer blocks the HTTP response), so by the
    time we render here the orchestrator still reports state="stopped" —
    optimistic override + grid auto-poll reconcile to the true state in
    the next 10-second cycle.

    The HX-Trigger header tells the surrounding grid to refresh itself
    immediately (don't wait for the next 10s poll), so the operator sees
    state transitions as soon as the orchestrator records them.
    """
    try:
        await orchestrator.open_session(
            name, auto_start=True, user_id=user.get("user_id"),
        )
    except httpx.HTTPError as e:
        log.warning("start_service(%s) via open_session failed: %s", name, e)
    response = await _render_card(request, name, optimistic_state="starting")
    response.headers["HX-Trigger"] = "orchestack-grid-refresh"
    return response


# ===========================================================================
#  HTMX action: stop a service
# ===========================================================================
@app.post("/api/dashboard/services/{name}/stop", response_class=HTMLResponse,
           name="stop_service_action")
async def stop_service_action(request: Request, name: str) -> HTMLResponse:
    """Tell the orchestrator to stop `name`, return the updated card.

    Optimistic state="stopped" + HX-Trigger for grid refresh — same pattern
    as start_service_action.
    """
    try:
        await orchestrator.stop_service(name)
    except httpx.HTTPError as e:
        log.warning("stop_service(%s) failed: %s", name, e)
    response = await _render_card(request, name, optimistic_state="stopped")
    response.headers["HX-Trigger"] = "orchestack-grid-refresh"
    return response


# ===========================================================================
#  HTMX action: pin / unpin
# ===========================================================================
@app.post("/api/dashboard/services/{name}/pin", response_class=HTMLResponse,
           name="pin_service_action")
async def pin_service_action(
    request: Request, name: str, ttl_seconds: int = Form(7200),
) -> HTMLResponse:
    """Pin a service (or extend an existing pin) with TTL. ttl_seconds=0 maps to None (no expiry — "Never" option)."""
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
    """Initial render of the pin button on service detail page first load."""
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
    """Open session against `name` and return tool URL. Forwards user_id so the row is attributed to the operator (otherwise orchestrator falls back to DEFAULT_USER_ID — system user). Tool URL priority: ?action=<key>.external_url → service.external_url → ROOT_PATH/<name>."""
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


# Long first-run setup (Metabase's Liquibase migration). JS poller shows specific copy so operator doesn't think the system is stuck.
SLOW_BOOTSTRAP_SERVICES = {"metabase"}


@app.get("/api/dashboard/services/{name}/ready", name="service_ready_probe")
async def service_ready_probe(
    request: Request, name: str, action: str | None = None,
) -> JSONResponse:
    """Readiness probe polled by the Open button. Used instead of hitting the tool URL directly because cross-origin redirects (e.g. Metabase /setup 302) confuse browser fetch readiness checks."""
    try:
        svc = await orchestrator.get_service(name)
    except httpx.HTTPError as e:
        return JSONResponse({"ready": False, "phase": "unknown",
                              "detail": str(e)}, status_code=502)

    if not svc or svc.get("state") != "running":
        return JSONResponse({"ready": False, "phase": "starting"})

    # Metabase first-boot sub-phases ("migrating" → "bootstrapping" → ready)
    # surfaced to JS so operator sees what's happening during the long Liquibase
    # migration (4-5 min on Docker Desktop for macOS).
    if name == "metabase":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                h = await client.get("http://orchestack-metabase:3000/api/health")
                if h.status_code != 200:
                    # 503 during init → "migrating" so JS shows the long-wait copy.
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
                # `setup-token` persists in Metabase's in-memory store even after
                # /api/setup completes — the real "setup done" signal is `has-user-setup`.
                props = r.json()
                if not props.get("has-user-setup"):
                    return JSONResponse(
                        {"ready": False, "phase": "bootstrapping"},
                    )
                return JSONResponse({"ready": True})
        except httpx.HTTPError:
            return JSONResponse({"ready": False, "phase": "starting"})

    # pgAdmin: 5-10s window between gunicorn start and first /misc/ping success
    # where Traefik routes a "starting" container, surfacing 502 in the new tab.
    if name == "pgadmin":
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # /misc/ping MUST include the SCRIPT_NAME prefix — pgAdmin rejects any path that doesn't start with it.
                r = await client.get(
                    "http://orchestack-pgadmin:80/app/pgadmin/misc/ping",
                )
                if r.status_code == 200:
                    return JSONResponse({"ready": True})
                return JSONResponse({"ready": False, "phase": "starting"})
        except httpx.HTTPError:
            return JSONResponse({"ready": False, "phase": "starting"})

    _SERVICE_READY_PROBES = {
        "minio":        ("orchestack-minio",        9000, "/minio/health/ready"),
        # Airflow 3 moved the API health endpoint to /api/v2/monitor/health.
        # The base_url config affects the URLs Airflow GENERATES in responses
        # (redirects, links), NOT the internal listen path — so probing the
        # endpoint at the root works regardless of base_url. Previously this
        # was /app/airflow/health which 404s on Airflow 3.
        "airflow":      ("orchestack-airflow",      8080, "/api/v2/monitor/health"),
        # Multi-container deployment; airbyte-server is the right gate — webapp's
        # nginx returns 200 before the API is up, so probing the webapp would race.
        "airbyte":      ("orchestack-airbyte-server", 8001, "/api/v1/health"),
        "dbt":          ("orchestack-dbt",          8080, "/"),
        # /healthcheck is on ADMIN port 8586, not operator-facing 8585. On 8585
        # the React router 404s the path → ready loop never gets 200.
        "openmetadata": ("orchestack-openmetadata", 8585, "/api/v1/system/version"),
    }
    # Per-action probes: dbt-docs takes 30-90s (runs deps + run + docs generate);
    # ttyd is up in ~2s. Probing separately lets Open Terminal work while docs build.
    _SERVICE_ACTION_PROBES = {
        ("dbt", "docs"): ("orchestack-dbt", 8080, "/index.html"),
        # ttyd serves at its --base-path, NOT at /; probing / returns 404
        # because ttyd's router only matches the configured prefix.
        ("dbt", "cli"):  ("orchestack-dbt", 7681, "/app/dbt-terminal/"),
        ("ge", "docs"):  ("orchestack-ge", 8080, "/index.html"),
        ("ge", "cli"):   ("orchestack-ge", 7681, "/app/ge-terminal/"),
    }
    probe = None
    if action is not None and (name, action) in _SERVICE_ACTION_PROBES:
        probe = _SERVICE_ACTION_PROBES[(name, action)]
    elif name in _SERVICE_READY_PROBES:
        probe = _SERVICE_READY_PROBES[name]
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

    # Default: state==running is the signal for services with no probe.
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
    """Close a session — proxies to the orchestrator's DELETE. Endpoint is POST (not DELETE) because navigator.sendBeacon() in beforeunload doesn't support DELETE."""
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
    """Render the active-sessions table fragment (polled every 10s)."""
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

    SERVICE_META = {
        s["name"]: s for s in (await _safe_list_services()).get("services", [])
    }
    for s in sessions:
        meta = SERVICE_META.get(s.get("service"), {})
        s["display_name"] = meta.get("display_name") or s.get("service")
        s["layer"]        = meta.get("layer")
        s["state"]        = meta.get("state")
        s["heartbeat_relative"] = _format_relative(s.get("last_heartbeat_at"))

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


async def _safe_list_services() -> dict:
    """Wrap orchestrator.list_services in try/except, returning `{"services": []}` on error so annotation handlers don't break the page."""
    try:
        return await orchestrator.list_services()
    except (httpx.HTTPError, ValueError):
        return {"services": []}


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
    """Forward credentials to orchestrator and propagate Set-Cookie. Uses 303 (not 302) so the browser doesn't double-submit the POST on refresh."""
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

    # `next` validated to start with `/` (not `//`) to prevent open-redirect to external URLs.
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    response = RedirectResponse(url=f"{ROOT_PATH}{safe_next}", status_code=303)
    if set_cookie:
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
async def _render_card(
    request: Request, name: str, optimistic_state: str | None = None,
) -> HTMLResponse:
    """Look up a service and render its card fragment. Renders inert stopped+unmanaged card if service vanished, so HTMX still gets valid HTML to swap.

    `optimistic_state` overrides the orchestrator-reported state. Used by the
    start/stop action endpoints to render an immediate optimistic transition
    (e.g. "starting") instead of the still-current orchestrator state, because
    the background autostart hasn't completed yet by the time we render.
    The grid's regular 10s polling reconciles to the true state shortly after.
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

    if optimistic_state is not None:
        svc = dict(svc)
        svc["state"] = optimistic_state

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
