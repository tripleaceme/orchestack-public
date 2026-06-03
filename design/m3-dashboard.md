# M3 — Dashboard design

**Status**: Implementation complete (phases 3.1 → 3.6).
**Owner**: Ayoade.
**Last updated**: 2026-06-03.
**Framework decision**: HTMX + FastAPI + Tailwind + Jinja2, after a 5-way
evaluation in `test_ui_framework/`. See §1 for the rationale.

> **2026-06-03 retrospective** — all six phases shipped on the original
> framework choice. The bet on "no Python-wrapper framework" held up: the
> dashboard's visual identity matches the marketing site and auth pages
> exactly because we wrote the HTML ourselves with Tailwind. Total
> dashboard image: ~120 MB (python:3.12-slim + fastapi + httpx + jinja),
> well under the 500 MB target. See §11 "What we actually built" at the
> bottom of this document for the full delta vs. the original plan.

This document specifies the OrcheStack administrator dashboard: the
operator-facing UI that consumes the orchestrator's HTTP API and presents
service state, session activity, audit history, and the keep-warm controls.
It replaces M1's nginx-based dashboard stub (M1) container with a real application.

The design-doc-first pattern paid off for M2 (caught scope decisions before
they became multi-week implementation debt); this doc does the same for M3.

---

## 1. Framework decision: why HTMX + FastAPI

After evaluating Streamlit, Solara, Quasar (raw), Vuetify (raw), and Reflex
side-by-side with identical mock content, the conclusion was that none of
the Python wrapper frameworks produced output close enough to OrcheStack's
existing visual identity (the marketing site + auth pages). Streamlit's
default chrome was too obvious; Solara had visible framework branding;
Reflex was the most polished but introduced a Node.js runtime dependency
and pre-1.0 API churn risk.

**HTMX + FastAPI** is the right tradeoff for OrcheStack:

- **Architectural coherence**: the orchestrator is already FastAPI. The
  dashboard becomes a second FastAPI app (or routes mounted on the
  orchestrator). Same language, same patterns, same deployment.
- **Design freedom**: we write the HTML ourselves with Tailwind, so the
  dashboard matches the auth pages and marketing site exactly. No framework
  imposes a card style we have to fight.
- **No build step**: Tailwind via CDN or a single tailwindcss-cli call. No
  Node runtime in the dashboard image. Smallest possible image.
- **Server-rendered first paint**: pages arrive HTML-complete, no JS
  hydration delay. Operators see content in <100ms.
- **Real-time without SPA complexity**: HTMX's `hx-trigger="every 10s"` for
  polling; `hx-ext="ws"` for WebSocket updates. Server-sent events for
  the service status stream.
- **Production-tested pattern**: this is the stack 37signals (Basecamp, HEY)
  uses. Mature, not trendy.

The cost: more HTML templates to write than Reflex would require. We
accept this because the templates ARE the design — we'd be customising
anyway.

---

## 2. What M3 does in one paragraph

The dashboard is the operator's primary view into OrcheStack: which
services are running, who's using them, what's been happening lately, and
the controls to start/stop/pin individual tools. It consumes the M2
orchestrator's existing HTTP API exclusively — no direct database access —
so its concerns are presentation and HTTP, not business logic. Real-time
service state arrives via polling (every 10s) initially, switching to
server-sent events in phase 3.3 once the orchestrator exposes a stream
endpoint. The dashboard also hosts the login form for M3.5's auth flow.

---

## 3. Process model & deployment shape

One container. Python + FastAPI + Jinja2 + a static `tailwind.css` baked
into the image. Same `image:` swap pattern as M2's auth and orchestrator:
the compose service name `streamlit` initially (now renamed to `dashboard`) to
serve this dashboard during M3.1 to avoid breaking the existing Traefik
PathPrefix(`/app`) route. A folder + service-name rename to `dashboard`
ships in a separate cleanup commit after M3.5 stabilises.

Inside the container:

- **FastAPI** on port 8000. Serves three route categories:
  1. `GET /app/*` — Jinja2-rendered HTML pages
  2. `GET /api/dashboard/*` — HTMX fragment endpoints (return HTML
     partials, not JSON, because HTMX consumes HTML directly)
  3. `GET /static/*` — `tailwind.css` + favicon
- **Jinja2** templates in `templates/`. One `base.html` with the OrcheStack
  shell (header, nav, footer); one template per page extending it.
- **A small async HTTP client** (`httpx`) that calls the orchestrator's
  API. Wrapped in a thin `OrchestratorClient` class for testability.
- **No local state.** Like the orchestrator, the dashboard is stateless —
  session cookies are validated against the orchestrator on each request.

Required environment variables (added to `.env.example` during M3.1):

```
ORCHESTRATOR_URL=http://orchestrator:8000    # internal Docker network DNS
DASHBOARD_LOG_LEVEL=info
DASHBOARD_SESSION_SECRET=<32-char hex>        # for cookie signing (M3.5)
```

Required Docker socket access: **none**. The dashboard is pure presentation;
the orchestrator owns Docker.

---

## 4. Page layout & navigation

Six pages, all extending the same base layout. URLs under `/app/*` because
that's the existing Traefik prefix:

| URL | Page | Primary content |
|-----|------|-----------------|
| `/app/` | Home | Service status grid + recent activity preview |
| `/app/services/{name}` | Service detail | Status, sessions, pin toggle, action log filtered to this service |
| `/app/sessions` | Active sessions | Live table of open sessions across all services |
| `/app/audit` | Audit log | Paginated table with filters (event_type, target, date range) |
| `/app/admin/users` | Users (M3.4+) | List + invite form |
| `/app/login` | Login (M3.5+) | Username/email + password form posting to `POST /api/auth/login` |

The header bar (in `base.html`) contains:
- OrcheStack logo (left, links to `/app/`)
- Connection indicator (centre) — green dot when last orchestrator
  request succeeded, red strikethrough when failed. Listens to HTMX's
  built-in `htmx:sendError` and `htmx:afterRequest` events. ~15 lines of
  inline CSS+JS.
- Signed-in-as block (right) — same pattern as the wizard pages, reads
  the session-cookie payload during M3.5+; defaults to "system" during
  earlier phases.

Mobile responsive via Tailwind's default breakpoints. Tablet is the
realistic minimum width we'll test against; phone is "looks fine" but not
optimised (operators rarely diagnose service issues from a phone).

---

## 5. HTMX patterns we'll use

Four idiomatic patterns cover ~90% of the dashboard interactivity:

### 5.1 Auto-refresh service grid

```html
<div id="service-grid"
     hx-get="/api/dashboard/services/grid"
     hx-trigger="every 10s, load delay:50ms"
     hx-swap="innerHTML">
  <!-- The fragment endpoint returns just the 9 service cards' HTML. -->
  <!-- innerHTML swap means the parent div + scroll position is preserved. -->
</div>
```

### 5.2 Button-triggered actions (start/stop)

```html
<button hx-post="/api/dashboard/services/pgadmin/start"
        hx-target="closest .service-card"
        hx-swap="outerHTML"
        hx-indicator="#spinner-pgadmin">
  Start
</button>
```

The endpoint calls the orchestrator's POST start, then re-renders the
service card with the new state. Optimistic UI is added in phase 3.4 by
adding an `hx-on::before-request="updateOptimistic(this)"` hook.

### 5.3 Modal dialogs (confirm-destructive-action)

```html
<button hx-get="/api/dashboard/services/pgadmin/stop-confirm"
        hx-target="body"
        hx-swap="beforeend">
  Stop pgAdmin
</button>
```

The endpoint returns a `<dialog>` element with a confirm button that
issues the actual stop call. CSS handles the backdrop + focus trap.

### 5.4 Connection indicator

```html
<div id="connection-status" class="status-online">
  <span class="dot"></span>
  <span class="label">Connected</span>
</div>

<script>
  document.addEventListener('htmx:sendError', () => {
    document.getElementById('connection-status').classList.replace('status-online', 'status-offline');
  });
  document.addEventListener('htmx:afterRequest', e => {
    if (e.detail.successful) {
      document.getElementById('connection-status').classList.replace('status-offline', 'status-online');
    }
  });
</script>
```

Total JS in the dashboard: probably under 100 lines. Anything more
complex is a smell — push it to the server.

---

## 6. Auth flow (M3.5)

Three new endpoints on the orchestrator (we'll add them in M3.5):

```
POST /api/auth/login       body: { username_or_email, password }
                           → 200 { user_id, full_name } + Set-Cookie session_token
POST /api/auth/logout      → 204 + clear session_token cookie
GET  /api/auth/me          → 200 { user_id, full_name, email, roles[] } or 401
```

The login endpoint:
1. Looks up the user by username OR email
2. bcrypt-verifies the password against `platform.users.password_hash`
3. If valid, inserts a row into `platform.sessions` (the schema already
   has this table — set token via gen_random_uuid())
4. Returns the user info + sets HttpOnly Secure SameSite=Lax cookie

The dashboard's FastAPI app has a `require_session` dependency that:
1. Reads the session cookie from the request
2. Calls `GET /api/auth/me` against the orchestrator with that cookie
3. If 200, attaches the user to the request state; if 401, redirects
   to `/app/login`

Importantly: the dashboard does NOT validate the cookie itself. The
orchestrator is the source of truth for session state. This means a
revoked session is immediately invalid across all consumers (the
dashboard, future CLI, anything else that talks to the orchestrator).

After M3.5 lands, M2's `DEFAULT_USER_ID=1` fallback in the orchestrator
becomes background-task only — reconciler audit-log writes use it,
all dashboard-originated operations use the real user.

---

## 7. Failure modes

| Failure | Detection | Response |
|---------|-----------|----------|
| Orchestrator unreachable | httpx client raises connection error | Connection indicator → red. Cached last-known state shown with "as of HH:MM:SS" label. No buttons disabled — clicking shows "can't reach orchestrator" toast |
| Orchestrator returns 500 | non-2xx status | Toast notification with the error detail. Indicator briefly red |
| Session expired mid-page | dashboard's require_session sees 401 from `/api/auth/me` | Redirect to `/app/login?next=<current_path>` |
| HTMX polling falls behind | Browser tab backgrounded; polls suspended by browser | When tab becomes active again, immediate `htmx:trigger` fires a one-off refresh |
| Large audit log query times out | Orchestrator endpoint returns 504 (we'll add timeout middleware to that endpoint at M3.4) | UI shows "Loading took too long — try a tighter date filter". No partial rendering |
| Static asset missing (CSS not in image) | Browser console + fallback styling kicks in | The Tailwind base.css is baked into the image at build time; absence would mean a broken Dockerfile, caught by the CI smoke step before publish |

The recurring pattern, same as M2: **never crash the dashboard.** Every
failure has a visible degraded state.

---

## 8. Implementation phases

Each phase is independently testable + produces something visible. Total
~3-4 weeks.

| Phase | What ships | Time |
|-------|-----------|------|
| **3.1** Skeleton | `tripleaceme/orchestack-dashboard` image. Replaces `nginx:alpine` nginx stub in compose (removed). One page at `/app/` rendering "OrcheStack" header + a placeholder that calls `GET /orchestrator/api/health` via HTMX and shows the result. Proves the image-swap + Traefik routing + orchestrator-call pattern | 2 days |
| **3.2** Service grid | Real service status grid. 9 catalogue cards rendered server-side from a single call to `GET /orchestrator/api/services`. Start/stop buttons that POST to the orchestrator and re-render the affected card. Auto-refresh every 10s. The connection indicator | 4-5 days |
| **3.3** Sessions + tool opens | Click a service card → opens `/app/<service>` (proxied via Traefik to the actual tool container) in a new tab AND opens an orchestrator session. JavaScript heartbeat ticker fires every 30s from the open tab. Tab close → DELETE session. The active-sessions page at `/app/sessions` | 3-4 days |
| **3.4** Audit log + pin UI | `/app/audit` page with paginated table, filters by event_type + target + date range. Service detail pages get a "Pin (keep warm)" toggle that POSTs/DELETEs the orchestrator's pinning endpoints. Optimistic UI for button clicks (HTMX `before-request` hook) | 3-4 days |
| **3.5** Auth integration | New orchestrator endpoints `POST /api/auth/login`, `POST /api/auth/logout`, `GET /api/auth/me`. Dashboard login form posts to login; redirects to `/app/` on success. `require_session` dependency guards all `/app/*` routes except `/app/login`. signup.html updated to also create a `platform.users` row before redirecting to the wizard | 3-4 days |
| **3.6** Polish + docs | Empty states for "no services running yet" / "no audit entries" / "no sessions". Keyboard shortcuts (`/` focuses search, `g s` goes to sessions, etc.). Tooltips on action buttons. The dashboard's own design/m3-dashboard.md updated with "what we actually built". M5 evaluation queries documented in `docs/operations/evaluation.html` | 2-3 days |

---

## 9. What M3 does NOT include

Drawing the line clearly:

- **Multi-tenant features.** OrcheStack is single-tenant. One organisation
  per host, one dashboard, one user pool.
- **Real-time websocket service status.** Polling at 10s is fine for M3;
  WebSocket upgrade is post-M3 if needed.
- **Mobile-first UI.** Tablet is the realistic minimum.
- **Embedded BI/charts.** Operators see counts and tables. Visual analytics
  are Metabase's job once it's installed (via the orchestrator).
- **Customisable theming.** The dashboard matches OrcheStack's brand;
  customers don't theme it. White-labelling for resellers is a future
  feature, not M3.
- **Backup/restore UI.** Documented operationally; not a dashboard page.

---

## 10. Open questions to resolve before phase 3.3

1. **Tailwind: CDN or compiled?** CDN is faster to build (no Node, no
   `tailwindcss` CLI step in CI), but ~20 KB heavier in the user's
   browser. Compiled produces ~5 KB CSS tailored to the classes we
   actually use. The compose file size difference is negligible; the
   user-visible latency difference at first load is the deciding factor.
   Lean toward compiled for the final v1.0 image; CDN is fine for M3.1-3.4.
2. **Session storage: cookies-only or also localStorage?** The signup
   flow currently writes profile to localStorage. M3.5 supersedes that
   with cookies. We'll keep localStorage temporarily for the wizard's
   in-progress state (selections, credentials before submission) since
   those are pre-auth, but everything post-login flows through cookies.
3. **Per-page polling vs page-wide manager?** Each HTMX `hx-trigger` is
   independent. If a user has the home page + audit log page open in two
   tabs, they generate 2x the orchestrator load. We accept this for M3
   — operators don't typically have many dashboard tabs open. M4 can
   add a shared service-status subscription if it becomes a problem.
4. **Service detail page or modal?** I've planned for a dedicated
   `/app/services/{name}` page. Could alternatively be a HTMX modal over
   the home page. Modal is faster to develop and feels snappier; dedicated
   page is shareable via URL. Picking dedicated page for now; revisit if
   the link-sharing use case turns out not to matter.

---

## 11. What we actually built (retrospective)

Filling in the gap between the design above and what shipped. Each phase
landed roughly within the time estimate; the main delta is in shape, not
scope.

### 11.1 Phases as shipped

| Phase | Original plan | What shipped | Delta |
|-------|---------------|--------------|-------|
| 3.1 Skeleton | nginx stub replacement; `/app/` page calls `/api/health` via HTMX | Same, plus base.html design tokens reused by all later pages | — |
| 3.2 Service grid | 9 cards from `GET /api/services`, start/stop buttons, 10s refresh, connection indicator | Same + extended orchestrator API with `layer` and `managed` so unmanaged services render a disabled "Unavailable" button instead of allowing a guaranteed-500 click | Extra orchestrator API fields |
| 3.3 Sessions + tool opens | Click card → opens proxied tool URL + opens session, JS heartbeat ticker, `/app/sessions` page | Same. Heartbeats fire from the *dashboard tab*, not the tool tab (we can't inject JS into Metabase/pgAdmin); reconciler is the safety net for closed-tool-tab cleanup | Dashboard-tab-owned heartbeat lifecycle |
| 3.4 Audit + pin UI | Paginated `/app/audit` page with filters, pin toggle on service detail, optimistic UI | Same. Service detail page also embeds a filtered audit-log view (`?target=<service>`) so per-service activity is visible without leaving the page | Service-detail page composes audit fragment |
| 3.5 Auth integration | `POST /api/auth/login`, `/logout`, `GET /api/auth/me`, `require_session` dependency, signup writes to `platform.users` | Same. `auth/public/login.html` now redirects to `/app/login` (the dashboard's canonical login). Cookie has `Path=/` so it propagates to all OrcheStack routes (dashboard + orchestrator + auth) | Old `/login` becomes a redirect |
| 3.6 Polish + docs | Empty states, keyboard shortcuts, tooltips, this design doc updated | Same. Keyboard shortcuts: `g h` / `g s` / `g a` / `?`. Compiled Tailwind deferred to v1.0 (CDN still in use) | Tailwind compile deferred |

### 11.2 Route map at end of M3

Dashboard (`tripleaceme/orchestack-dashboard`) — Traefik strips `/app`:

```
Pages (all require auth; redirect to /app/login on 401)
  GET  /                              → Service grid
  GET  /sessions                      → Active-sessions table
  GET  /audit                         → Audit log with filters
  GET  /services/{name}               → Service detail (pin + activity)
  GET  /login                         → Login form (unauth OK)

HTMX fragment endpoints
  GET  /api/dashboard/health
  GET  /api/dashboard/services/grid
  GET  /api/dashboard/services/{name}/pin-button
  GET  /api/dashboard/sessions/active
  GET  /api/dashboard/audit/table

HTMX action endpoints
  POST /api/dashboard/services/{name}/start  → returns updated card
  POST /api/dashboard/services/{name}/stop   → returns updated card
  POST /api/dashboard/services/{name}/pin    → returns pin button
  DELETE /api/dashboard/services/{name}/pin  → returns pin button

Session lifecycle (consumed by the JS in base.html)
  POST /api/dashboard/services/{name}/open      → JSON { token, tool_url }
  POST /api/dashboard/sessions/{token}/heartbeat
  POST /api/dashboard/sessions/{token}/close    → DELETE via sendBeacon

Auth (proxied to orchestrator)
  POST /api/dashboard/auth/login   (form-encoded)
  POST /api/dashboard/auth/logout

Container liveness
  GET  /healthz                       → "ok" (no orchestrator call)
```

Orchestrator additions during M3:

```
GET  /api/services             — extended with layer + managed
GET  /api/services/{name}/pin  — pin state for the dashboard's toggle
GET  /api/sessions             — paginated list with users JOIN
GET  /api/audit                — paginated audit log with filters
POST /api/auth/login           — bcrypt-verify, set Set-Cookie
POST /api/auth/logout          — revoke session, clear cookie
GET  /api/auth/me              — current user + roles
POST /api/users                — signup; first user auto-Admin
```

### 11.3 Architectural choices that surprised us

- **localStorage for session tokens, not a server-side "my sessions"
  endpoint.** Until M3.5 wires up auth cookies, every session belongs to
  the seeded `DEFAULT_USER_ID`. The dashboard tab uses localStorage to
  remember which tokens it owns; the heartbeat ticker reads from there.
  Phase 3.5 could replace this with `GET /api/sessions?user_id=me` once
  the user is identifiable, but localStorage continues to work and the
  refactor wasn't worth the churn.
- **Dashboard tab owns the heartbeat, not the tool tab.** We can't inject
  JS into Metabase / pgAdmin / etc. — they're third-party apps. The
  dashboard tab fires `/api/sessions/{token}/checkin` every 30s for
  every active service. Closing the dashboard kills all your sessions
  in 5 min (reconciler's stale-heartbeat sweep); closing just one tool
  tab is invisible to us, but the reconciler also catches that case
  because nobody's heartbeating for that service.
- **`hx-on::before-request` for optimistic UI.** Instead of a separate
  pre-mutation state in the template, the start/stop buttons use the
  HTMX-emitted `before-request` event to mutate themselves *before* the
  HTTP call: button text → "Stopping…", `disabled = true`. Failure
  re-renders the whole card with the current state, which fixes the
  optimistic mutation if it was wrong. ~3 extra lines per button beat
  a full state machine.
- **JSON `target` filter for the audit log.** The audit table reuses
  exactly one fragment endpoint for the global view AND the per-service
  view — the service-detail page passes `?target=<service_name>` and
  gets the filtered subset for free. One Jinja template covers both
  use cases.

### 11.4 What we deliberately did NOT do

- **Compiled Tailwind.** CDN still in use; final image is ~70 KB heavier
  than it could be. Acceptable trade-off for now; v1.0 cleanup item.
- **WebSocket service-state stream.** 10s polling is fine at this scale.
  WebSocket upgrade is a future-work item if 10s lag ever feels slow.
- **Service worker for cross-tab session tracking.** Operators close the
  dashboard ⇒ all sessions die in 5 min. Acceptable; documented.
- **Audit retention policy.** Audit log grows unbounded. M5's evaluation
  query computes some aggregates; production-grade retention/archival
  is post-M3 work.
- **`/app/admin/users` page.** Listed in §4 but not built — user
  management beyond first-user auto-Admin is post-M3 (M4 follow-up).
- **Forgot-password flow.** The signup → login → use loop is complete;
  recovery is post-M3. Operators can `UPDATE platform.users SET
  password_hash = ...` directly via pgAdmin if they need to in the
  meantime.
