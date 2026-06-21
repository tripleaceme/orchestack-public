# OrcheStack Architecture

OrcheStack is a containerised, single-host data platform that integrates
the contemporary open-source data tools — Airbyte, Apache Airflow, dbt
Core, Great Expectations, Metabase, MinIO, OpenMetadata, and pgAdmin —
under a single operator-facing dashboard. Its central design feature is
a hot-cold tier orchestration model that activates services on operator
demand and deactivates them when idle, so that a stack whose aggregate
resource footprint would otherwise exceed eight gigabytes can run within
that envelope on commodity hardware.

This document is a starting point for contributors who want to understand
how the platform is put together. It does not replace the operator
documentation at https://orchestack.africa; that documentation is the
right place to start if you want to *use* OrcheStack rather than work on
its source code.

## Top-level layout

```
orchestack-public/
├── system/                 ← runtime code and configuration
│   ├── auth/               ← signup, login, setup wizard (nginx + static)
│   ├── dashboard/          ← administrator UI (FastAPI + HTMX + Jinja)
│   ├── orchestrator/       ← lifecycle daemon (FastAPI + asyncpg)
│   ├── docker/             ← compose specs + per-service snippets
│   ├── dags/               ← Airflow DAGs shipped with the platform
│   └── dbt/                ← dbt project skeleton served to operators
├── docs/                   ← operator-facing documentation (HTML)
├── assets/                 ← shared CSS + logos (single source of truth)
├── scripts/                ← release-bundle builder
├── Makefile                ← convenience commands (see `make help`)
└── _generate_docs.py       ← regenerates docs/*.html from Python sources
```

## The four subsystems

OrcheStack has four top-level subsystems that interact through well-
defined interfaces. The split is what makes the platform contributable in
small, focused pull requests.

### Orchestrator (`system/orchestrator/`)

A FastAPI application that owns the platform's lifecycle decisions and
the metadata layer. It is never reached directly by an operator's
browser; the dashboard proxies every privileged request through the
orchestrator's private API.

Responsibilities partition into four areas:

- **Service catalogue** — a Python dictionary (`SERVICE_CATALOGUE`) that
  records each manageable service with its tier classification (hot or
  cold), its layer (business intelligence, transformation, governance),
  its compose snippet path, and its connection-URL template. The
  catalogue is the single source of truth that the reconciler, the HTTP
  API, and the dashboard's service grid all read from. Adding a new
  service is a catalogue entry, a compose snippet, and an optional
  pre-start hook; no further integration code is required.
- **Per-service lifecycle** — `docker_ops.py` shells out to the
  `docker compose` CLI for service start, stop, and inspection. Each
  managed service has its own compose project (`orchestack-service-<name>`)
  so that operating on one service does not affect the others. The
  decision to shell out rather than use the Python Docker SDK is
  deliberate: the CLI handles API-version negotiation with the daemon and
  preserves the property that operators can reproduce any orchestrator
  action from their own shell.
- **Metadata layer** — the orchestrator owns the platform schema in
  PostgreSQL (users, roles, role-permissions, sessions, service-sessions,
  service-pinning, audit log). The data-access layer is a thin wrapper
  around an `asyncpg` connection pool. Every privileged write is followed
  by a call to an audit helper that records the action, the actor, and
  the affected target.
- **Reconciler** — an asynchronous background task that wakes
  periodically, identifies services with no active sessions that have
  exceeded their idle threshold, and stops them through the same code
  path operator-initiated stops use. The reconciler honours active
  service pins and active session heartbeats; it never starts a service.
  The asymmetry is deliberate: operator-initiated starts are never
  overridden, and the worst-case behaviour of a defect is over-aggressive
  stopping rather than service flapping.

### Dashboard (`system/dashboard/`)

A sibling FastAPI application served behind a reverse-proxy path prefix
at `/app`. It renders Jinja templates layered with HTMX-driven partials
and communicates with the orchestrator's private API through an `httpx`
client. The dashboard owns no state of its own; every read and every
write is a pass-through to the orchestrator.

The choice to render in HTMX rather than as a single-page application
preserves an inspection property operators value: the dashboard's
behaviour can be understood entirely from the server-side templates and
the route handlers, without a separate client-side state machine.
Tailwind utility classes supply typography and layout. There is no
JavaScript build step in the operator deployment.

### Auth (`system/auth/`)

A small nginx container that serves the platform's pre-login surface:
the signup page (first-administrator bootstrap), the login page, and the
four-step setup wizard (welcome → service selection → configuration →
deployment). The container is intentionally minimal — static HTML and
the shared CSS from `assets/css/`, no Python, no JavaScript framework.

Auth is a separate subsystem rather than a route inside the dashboard
because the two surfaces have different lifecycles and different threat
models. Auth is reached *before* the operator has a session; the
dashboard is reached only *after* the orchestrator has issued one.
Keeping the surfaces in separate containers means the dashboard's
privileged routes are never on the same port as the public-internet-
facing signup form, and the auth container can be hardened independently.

Auth has no dependencies of its own — it does not talk to the
orchestrator or the database directly. The signup form posts to the
orchestrator's `/api/auth/signup` endpoint through the reverse proxy,
and the setup wizard posts its accumulated state to the orchestrator's
`/api/setup/deploy` endpoint at the end of the flow. Auth's
responsibility ends at the form-submission boundary; everything after
is the orchestrator's.

### Integrated stack (`system/docker/services/`)

The eight third-party services the orchestrator brings up on operator
demand. Each one has:

- A compose snippet at `system/docker/services/<name>.yml`
- An optional pre-start hook in the orchestrator's `docker_ops.py`
  (typically provisioning a per-service PostgreSQL role and database)
- An optional post-start hook in the same module (typically running an
  upstream bootstrap step the operator should not have to learn about)
- A catalogue entry recording the service's tier, layer, and dependencies
- An operator-facing service page at `docs/services/<name>.html`

The pattern is intentionally uniform. The same five elements describe
PostgreSQL (control-plane, always-on, no hooks needed), Metabase (hot
tier, both hooks present), and OpenMetadata (cold tier, three containers,
both hooks present including the Elasticsearch single-node replica fix).
Once the pattern is internalised, every new service follows the same
shape.

## How a request flows through the system

An operator clicks "Open" on the Metabase tile in the dashboard:

1. The browser sends `POST /app/api/services/metabase/open` to the
   dashboard.
2. The dashboard forwards to the orchestrator at
   `POST /api/services/metabase/open` over the internal docker network.
3. The orchestrator checks the operator's permissions against the
   role-permissions table; aborts with `403` if the operator lacks
   `can_use` for Metabase.
4. The orchestrator's session manager checks for an existing
   `(operator, metabase)` session; reuses it if found, creates a new
   session row if not.
5. If Metabase is not running, the orchestrator runs the pre-start hook
   (provisioning the `metabase_admin` role and `metabase_db` if missing),
   then `docker compose -f services/metabase.yml up -d` for the
   `orchestack-service-metabase` project.
6. The orchestrator polls for Metabase's HTTP health check to return
   healthy, then runs the post-start hook (bootstrapping the Metabase
   administrator account so the operator skips Metabase's first-run
   wizard).
7. The orchestrator records the open action in the audit log.
8. The orchestrator returns the session token and the operator-facing
   URL (`http://localhost/metabase/`) to the dashboard.
9. The dashboard returns a server-rendered HTML fragment with a link
   the operator clicks, taking them to a working Metabase instance.

Every step is observable: the audit log records what happened, the
reconciler records when the session expires, the dashboard's Sessions
page shows the live session, and the operator-facing Metabase URL is
reachable through the reverse proxy.

## Where data is stored

OrcheStack runs three categories of database inside its
`orchestack-postgres` container:

| Database | Owner | Purpose |
| --- | --- | --- |
| `orchestack_db` | `orchestack_admin` | Platform metadata (users, roles, sessions, audit) |
| `data_warehouse` | `warehouse_admin` | The operator's analytical warehouse — dbt populates it, Metabase queries it |
| `<service>_db` (one per service that needs backing storage) | `<service>_admin` | Backing store for that integrated tool's own state (Metabase dashboards, Airflow DAG runs, OpenMetadata catalogue, etc.) |

The per-service partition is what allows each tool to be reset or
upgraded independently. It is also what allows the platform to enforce
least-privilege credentials: no tool ever sees another tool's data.

## Front-facing assets

The marketing site at https://orchestack.africa is not in this
repository. It is deployed separately to Cloudflare Pages from a
maintainer-owned source. The shared brand assets (`assets/css/main.css`
and `assets/logos/`) are mirrored in both places, with this repository
as the source of truth for the in-container pages (signup, login, setup
wizard) and the operator documentation. Changes to brand assets are
maintainer-only — see `CONTRIBUTING.md`.

## Where to start reading the code

If you want to make a substantive contribution, the recommended reading
order is:

1. `system/orchestrator/app/main.py` — the FastAPI app and its routes.
2. `system/orchestrator/app/docker_ops.py` — every interaction with the
   Docker daemon. The pre-start and post-start hooks live here.
3. `system/orchestrator/app/db.py` — the platform schema and the
   `asyncpg` access patterns.
4. `system/dashboard/app/main.py` — the dashboard's routes and the
   pass-through to the orchestrator.
5. `system/dashboard/app/templates/` — the Jinja templates and the HTMX
   partials.
6. `system/docker/docker-compose.yml` — the top-level compose spec; how
   the always-on services are wired.
7. `system/docker/services/*.yml` — one per integrated service. Pick a
   service you're interested in and trace from its compose snippet to
   the corresponding catalogue entry, hooks, and docs page.

Reading these in order takes about a focused half-day and gives you the
mental model needed to navigate any part of the codebase.
