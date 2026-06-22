# OrcheStack

> Containerised open-source data platform — the modern data stack on a single
> host, with hot/cold-tier service orchestration so the resident-memory
> footprint scales with what the operator is actually using rather than with
> the size of the installed toolset.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Operator docs](https://img.shields.io/badge/Operator_docs-orchestack.africa-2563eb.svg)](https://orchestack.africa)
[![CI](https://github.com/tripleaceme/orchestack-public/actions/workflows/ci.yml/badge.svg)](https://github.com/tripleaceme/orchestack-public/actions/workflows/ci.yml)

OrcheStack integrates Airbyte, Apache Airflow, dbt Core, Great Expectations,
Metabase, MinIO, OpenMetadata, and pgAdmin behind a single operator-facing
dashboard. Operators do not write integration glue between services — the
orchestrator handles per-service provisioning, credentials, network plumbing,
and lifecycle.

Most services start on demand and stop when idle, so the same modern data
stack that would otherwise need a sixteen-gigabyte host to idle runs
comfortably within an eight-gigabyte envelope.

## Quick start (operators)

The fastest path is the install script:

```sh
curl -sSL https://orchestack.africa/install.sh | bash
```

It downloads the latest runtime bundle, prompts for the platform database
password, writes `.env`, and runs `docker compose up -d`. About thirty
seconds end-to-end on a decent connection. Visit `http://localhost/signup`
afterwards to bootstrap your administrator account.

If you'd rather inspect what you're running before you run it, download
the runtime bundle from the [latest release](https://github.com/tripleaceme/orchestack-public/releases/latest)
instead and follow the included `INSTALL.md`.

Full operator documentation: <https://orchestack.africa>

## What's inside

| Service | Role | Tier |
| --- | --- | --- |
| PostgreSQL | Warehouse + platform metadata | Hot (always on) |
| Metabase | Business-intelligence dashboards | Hot |
| Apache Airflow | Pipeline orchestration | Hot |
| Airbyte | Source-to-warehouse ingestion | Cold |
| dbt Core | SQL transformations | Cold |
| Great Expectations | Data-quality testing | Cold |
| MinIO | S3-compatible object storage | Cold |
| OpenMetadata | Lineage and metadata catalogue | Cold |
| pgAdmin | PostgreSQL administration | Cold |

Hot-tier services bear continuous resource cost in exchange for
continuous availability. Cold-tier services are started on operator
demand and stopped automatically when idle. The reconciler enforces the
cold-side discipline without operator intervention; pinning a service
keeps it warm until the pin expires.

## Architecture (one paragraph)

Four subsystems: the **orchestrator** (FastAPI + asyncpg) owns the
platform's metadata layer and lifecycle decisions; the **dashboard**
(FastAPI + HTMX + Jinja) renders operator state behind a reverse-proxy
path prefix at `/app`; the **auth** container (nginx + static) serves
the signup and setup-wizard surface; the **integrated stack** is the
nine third-party services above, each declared as a compose snippet
plus optional pre-start and post-start hooks. The orchestrator's
reconciler stops idle cold-tier services on a periodic tick. Airflow
ships with `dbt-core` + `astronomer-cosmos` baked in so the operator
can run dbt models with per-model task granularity from any DAG —
see [Compose your first pipeline](https://orchestack.africa/first-pipeline.html)
for the canonical patterns.

Deeper detail is in [ARCHITECTURE.md](ARCHITECTURE.md) — start there if
you want to contribute code.

## Repository tour

```
.
├── system/                Runtime code and configuration
│   ├── auth/              Signup, login, setup wizard (nginx + static)
│   ├── dashboard/         Administrator UI (FastAPI + HTMX + Jinja)
│   ├── orchestrator/      Lifecycle daemon (FastAPI + asyncpg)
│   ├── docker/            Compose specs + per-service snippets
│   ├── dags/              Starter Airflow DAGs (see README inside)
│   └── dbt/               Starter dbt project skeleton (see README inside)
├── docs/                  Operator-facing documentation (28 generated pages)
├── assets/                Shared CSS and brand logos
├── scripts/               Release-bundle builder
├── _generate_docs.py      Regenerates docs/*.html from Python sources
├── Makefile               Convenience commands — run `make help`
├── ARCHITECTURE.md        Subsystem-level architecture for contributors
├── CONTRIBUTING.md        How to contribute
├── CHANGELOG.md           Notable changes per release
└── LICENSE                Apache 2.0
```

`system/dags/` and `system/dbt/` ship starter content so operators have
a working pipeline on day one. Both are designed to be replaced: the
operator points the setup wizard at their own Git repository, and the
starter falls away. See the README inside each folder for the operator-
facing pattern.

## How releases work

| Artefact | Where |
| --- | --- |
| Container images | Docker Hub: [`tripleaceme/orchestack-*`](https://hub.docker.com/u/tripleaceme) |
| Runtime bundle | GitHub Releases: [`orchestack-runtime.tar.gz`](https://github.com/tripleaceme/orchestack-public/releases/latest) |
| Operator documentation | <https://orchestack.africa> |

A maintainer cuts a release by running `make tag-release VERSION=X.Y.Z`
and pushing the tag. That fires
[release.yml](.github/workflows/release.yml), which builds and pushes
the four OrcheStack-owned images to Docker Hub (`:X.Y.Z` and `:latest`)
and attaches the runtime bundle to the GitHub Release. Merges to `main`
between releases do not ship to operators.

## Contributing

OrcheStack welcomes contributions. The full guide is in
[CONTRIBUTING.md](CONTRIBUTING.md); the short version is:

1. Fork the repo, branch from `main`, write your change.
2. Run the relevant smoke procedure documented at `docs/services/<name>.html`.
3. Open a pull request. CI builds all four images and runs healthchecks.
4. A maintainer reviews. Once approved, your change is merged into `main`.
5. It ships to operators when the next release is cut.

If you find a security vulnerability, please [report it privately](https://github.com/tripleaceme/orchestack-public/security/advisories/new)
rather than in the public issue tracker.

## Licence

Apache 2.0 — see [LICENSE](LICENSE).

Bundled upstream services (Apache Airflow, dbt Core, Airbyte, PostgreSQL,
MinIO, Metabase, Great Expectations, OpenMetadata, pgAdmin, Traefik) retain
their upstream licences. OrcheStack orchestrates these images; it does not
modify or redistribute their source code.
