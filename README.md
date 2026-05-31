# OrcheStack

> A containerised open-source data platform for Nigerian organisations.
> Modern Data Stack on a single host, with hot/cold-tier service orchestration
> so the resident-memory footprint scales with current activity rather than
> the size of the installed toolset.

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: pre-release (M1)](https://img.shields.io/badge/Status-Pre--release%20(M1)-orange.svg)](#status)

OrcheStack bundles Apache Airflow, dbt Core, Airbyte, PostgreSQL and pgAdmin
(default stack), plus MinIO, Great Expectations, OpenMetadata and Metabase
(optional extensions), behind a unified Streamlit administrator dashboard.
Most services start on demand and stop when idle — letting the same Modern
Data Stack run on an 8 GB Nigerian-affordable VPS that would otherwise need
16 GB just to idle.

## Status

OrcheStack is in active pre-release development as the implementation
artefact of an MIT Professional Master's Project at Miva Open University.

| Milestone | Window | Status |
|---|---|---|
| **M1 — Foundation** | Week 1 | 🛠 in progress |
| **M2 — Orchestrator** | Weeks 2–3 | ⬜ pending |
| **M3 — Control plane** | Weeks 4–5 | ⬜ pending |
| **M4 — Stack integration** | Weeks 6–7 | ⬜ pending |
| **M5 — Evaluation + write-up** | Week 8 | ⬜ pending |

The marketing site, documentation site, in-package auth/setup-wizard nginx
container, and the base `docker-compose.yml` for the control plane are
implemented. The Python service orchestrator (M2) and Streamlit dashboard
(M3) ship as stubs in the M1 compose file and get replaced by real
implementations in their respective milestones.

## Quick start

The base control plane brings up five containers — a reverse proxy, a
PostgreSQL instance, the auth/setup nginx, and stubs for the orchestrator
and Streamlit dashboard:

```sh
cd system/docker
cp .env.example .env       # set POSTGRES_PASSWORD before running
docker compose up -d
```

Then visit:

| URL | What you'll see |
|---|---|
| `http://localhost/signup` | First-administrator bootstrap form |
| `http://localhost/login` | Administrator login |
| `http://localhost/setup/welcome.html` | 4-step onboarding wizard |
| `http://localhost/app` | Streamlit dashboard (M1 stub; real dashboard at M3) |
| `http://localhost:8080/dashboard/` | Traefik routing dashboard (dev only) |

If port 80 is in use on your host, override the host-side mapping in `.env`:

```
PROXY_HTTP_PORT=1993
```

…then `http://localhost:1993` instead.

## Repository layout

```
.
├── README.md, LICENSE                    This file + Apache 2.0 licence
├── _generate_docs.py                     Static generator for the docs site
│
├── index.html, services.html,            Marketing site (deployed to
│   contact.html, assets/                  orchestack.africa via Cloudflare Pages)
├── docs/                                 28-page documentation site (generated)
│
└── system/                               Runtime code for the platform
    ├── docs-portal/                      Auth + setup wizard nginx container
    │   ├── Dockerfile                    →  tripleaceme/orchestack-auth
    │   ├── nginx.conf                    Routes scoped to /signup, /login, /setup/*, /assets/*
    │   └── public/                       signup.html, login.html, setup/*.html
    ├── docker/                           Base Docker Compose specification (M1)
    │   ├── docker-compose.yml            5-service control plane
    │   ├── .env.example                  Template (copy to .env, set POSTGRES_PASSWORD)
    │   ├── traefik/                      Traefik static config + dynamic dir
    │   ├── postgres-init/                00-init.sql + 10-platform-schema.sql
    │   ├── stubs/                        M1 placeholder HTML for streamlit stub
    │   └── README.md                     Per-service details + troubleshooting
    ├── streamlit-app/                    (M3) Real Streamlit dashboard
    ├── orchestrator/                     (M2) Python service-lifecycle daemon
    ├── dbt/                              (M4) Default dbt project skeleton
    ├── dags/                             (M4) Default Airflow DAGs
    └── configs/                          (M1/M4) Per-service config templates
```

## How OrcheStack is published

- **Container images** → Docker Hub under [`tripleaceme/orchestack-*`](https://hub.docker.com/u/tripleaceme):
  - [`tripleaceme/orchestack-auth`](https://hub.docker.com/r/tripleaceme/orchestack-auth) (built from `system/docs-portal/`)
  - [`tripleaceme/orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator) (M2)
  - [`tripleaceme/orchestack-streamlit`](https://hub.docker.com/r/tripleaceme/orchestack-streamlit) (M3)
  - [`tripleaceme/orchestack-airflow`](https://hub.docker.com/r/tripleaceme/orchestack-airflow) (M4)
- **Source code** → this repository ([`github.com/tripleaceme/orchestack`](https://github.com/tripleaceme/orchestack))
- **Marketing + docs** → [`https://orchestack.africa`](https://orchestack.africa) (Cloudflare Pages, publishing from this folder)

## Architecture summary

OrcheStack is structured around three architectural decisions documented
in the project's design log:

1. **Three-state lifecycle.** *Base install* (~2 GB) → *Configured* (~4–5 GB)
   → *Active pipeline* (~6–10 GB peak, transient). Resource consumption
   scales with what's actually in use, not what's installed.
2. **Hot/cold tier service classification.** Hot-tier services
   (PostgreSQL, Airflow scheduler, Metabase, MinIO) stay resident; cold-tier
   services (Airbyte, dbt, pgAdmin, Great Expectations, OpenMetadata) are
   activated by event triggers and stopped when idle.
3. **Unified control plane.** A Streamlit dashboard behind a Traefik
   reverse proxy provides a single URL space — `/app`, `/app/metabase`,
   `/app/airflow`, etc. — for every bundled tool.

The cold→hot override happens through a per-service "Keep warm" toggle in
the dashboard, which writes to `platform.service_pinning` in PostgreSQL.
The orchestrator's idle-timeout sweep consults this table on every tick
and skips the shutdown for pinned services.

Full details, plus the diagrams Figure 1 / 2 / 3, are in the project
report (not included in this repository; the academic write-up lives
separately).

## Hosting the marketing + docs site

The marketing pages (`index.html`, `services.html`, `contact.html`) and
the generated `docs/` site are deployed to Cloudflare Pages, publishing
from this folder directly (build output directory = `.`). The CSS, tool
logos, and 28-page docs site are pure static HTML/SVG — no server-side
runtime is needed.

Regenerating the docs:

```sh
python3 _generate_docs.py     # writes docs/*.html from a single SIDEBAR list
```

## Contributing

OrcheStack is a one-person Master's project in its initial development;
contributions are not actively solicited at this stage. The codebase will
be opened to contributions after the project's M5 evaluation milestone
completes and a stable 1.0 release is tagged.

If you find a security issue at any point, please report it privately to
`hello@orchestack.africa` rather than via a public issue.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).

Bundled upstream open-source services (Apache Airflow, dbt Core, Airbyte,
PostgreSQL, MinIO, Metabase, Great Expectations, OpenMetadata, pgAdmin,
Traefik, Streamlit) retain their respective upstream licences (Apache 2.0,
AGPL, MIT, PostgreSQL Licence, BSD) — OrcheStack only orchestrates these
images and does not modify or redistribute their source code.
