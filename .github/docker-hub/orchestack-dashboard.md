# orchestack-dashboard

The administrator dashboard for [**OrcheStack**](https://orchestack.africa) —
an open-source containerised data platform that integrates Airbyte, Apache
Airflow, dbt Core, Great Expectations, Metabase, MinIO, OpenMetadata, and
pgAdmin behind a single operator-facing interface.

This image renders the operator-facing UI for the platform. It's a FastAPI
application serving Jinja templates layered with HTMX-driven partials,
reachable behind a reverse-proxy path prefix at `/app`.

## What this image does

| Page | What operators do there |
|---|---|
| **Service status** | Grid of every managed service with its tier badge, container status, and quick start/stop controls |
| **Service detail** | Per-service open sessions, audit timeline, edit-config form, force-stop control |
| **Open sessions** | Live view of every session across all services with operator + service + opened-at + heartbeat |
| **Audit log** | Append-only event stream filterable by actor, target, action type, time range |
| **Credentials** | Environment-variable browser with reveal-on-click and per-service grouping |
| **Users + Roles** | Multi-user management, role-permissions matrix (effective view + explicit-grants view), bulk-save grid |
| **Profile** | The signed-in operator's own account + session management |

## Design properties worth knowing

- **HTMX over single-page-app** — server-rendered fragments, no client-side state machine to reason about, behaviour fully inspectable from the route handlers and templates
- **Owns no state** — every read and every write is a pass-through to the
  [`orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator)
  through its private API
- **No-store cache header** on every HTML response — the dashboard's pages
  change on every operator action; cached pages would be wrong
- **Runs unprivileged** — `useradd --system dashboard` inside the image;
  zero need for root since the dashboard only makes HTTP calls and serves
  HTML

## How this image is used

This is part of OrcheStack's control plane and is not designed to run
standalone. It runs alongside the rest of the platform via the
`docker-compose.yml` shipped in the OrcheStack runtime bundle.

To deploy OrcheStack:

```sh
curl -sSL https://orchestack.africa/install.sh | bash
```

Or download the [latest runtime bundle](https://github.com/tripleaceme/orchestack-public/releases/latest)
and follow its `INSTALL.md`. Once running, sign in at
`http://your-host/login` and the dashboard appears at `http://your-host/app/`.

## Related images

| Image | Purpose |
|---|---|
| [`tripleaceme/orchestack-auth`](https://hub.docker.com/r/tripleaceme/orchestack-auth) | Signup, login, setup wizard |
| [`tripleaceme/orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator) | Service lifecycle daemon |
| [`tripleaceme/orchestack-airflow`](https://hub.docker.com/r/tripleaceme/orchestack-airflow) | Airflow 3 with dbt + Cosmos preinstalled |
| [`tripleaceme/orchestack-ge`](https://hub.docker.com/r/tripleaceme/orchestack-ge) | Great Expectations preinstalled |

## Project links

- **Website** — <https://orchestack.africa>
- **Operator docs** — <https://orchestack.africa/install.html>
- **Source code** — <https://github.com/tripleaceme/orchestack-public>
- **Releases** — <https://github.com/tripleaceme/orchestack-public/releases>
- **License** — Apache 2.0
