# OrcheStack Changelog

All notable changes to OrcheStack are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each release lists changes in the categories **Added**, **Changed**,
**Deprecated**, **Removed**, **Fixed**, and **Security**, in that order.
Entries omit categories that have no changes for that release.

## [Unreleased]

Pending changes will be listed here and rolled into the next tagged
release.

## [0.1.1] — 2026-06-23

First patch release. Closes both install-time bugs surfaced during
end-to-end verification of v0.1.0. Operators upgrade with
`docker compose pull && docker compose up -d` — no destructive
changes, no `.env` edits required.

### Fixed

- **[#1](https://github.com/tripleaceme/orchestack-public/issues/1)** —
  Airflow now starts independently of dbt. Removed the `external: true`
  declaration on the shared `orchestack-dbt-repo` volume in
  `system/docker/services/airflow.yml`. Docker now creates the named
  volume on first reference from either service; whichever service
  starts first creates the (empty) volume, and the other joins it.
  The dbt service populates the volume on its own start (cloning from
  `DBT_REPO_URL` or writing the built-in demo project). Airflow
  operators no longer have to remember to open dbt before Airflow.

- **[#2](https://github.com/tripleaceme/orchestack-public/issues/2)** —
  Airbyte's Temporal sidecar now starts cleanly on a fresh install.
  The orchestrator's Airbyte pre-start hook was provisioning the two
  Temporal databases as `temporal_db` and `temporal_visibility_db`,
  following the platform-wide `<service>_db` naming convention.
  Temporal's upstream binary, however, hardcodes the unsuffixed
  names `temporal` and `temporal_visibility` (via its compose env
  defaults), and crash-looped with `pq: database "temporal" does
  not exist`. Carved Temporal out of the naming convention — the
  hook now provisions the unsuffixed names directly. For operators
  upgrading from the broken v0.1.0 install with the legacy database
  names already on disk, the existing migration loop in the same
  hook renames `temporal_db` → `temporal` (and the visibility one)
  automatically on next start; no operator action required.

## [0.1.0] — 2026-06-22

Inaugural public release. OrcheStack moves from a maintainer-only,
academically-housed project to a publicly-installable open-source data
platform.

### Added

- **Platform core** — four-subsystem architecture (orchestrator,
  dashboard, auth, integrated stack) that integrates eight third-party
  services behind a single operator-facing interface.
- **Apache Airflow 3 with dbt + Cosmos baked in** — published as
  `tripleaceme/orchestack-airflow`. Airflow 3.2 base with `dbt-core`,
  `dbt-postgres`, and `astronomer-cosmos` preinstalled at build time
  so the operator never runs `pip install` at task time. Cosmos
  generates one Airflow task per dbt model and per dbt test, giving
  per-model failure attribution in the Airflow UI instead of
  whole-DAG opaque failures.
- **Shared dbt-project volume** — `orchestack-dbt-repo` mounted
  read-write into the dbt service container and read-only into the
  Airflow container. Operators populate it once (via `DBT_REPO_URL`
  or via the dbt service's in-browser terminal) and both services
  see it.
- **Airflow Connection auto-creation** — the orchestrator's
  post-start hook creates the `orchestack_warehouse` Airflow
  Connection idempotently on first Airflow start, using the
  warehouse credentials from `.env`. Cosmos's
  `PostgresUserPasswordProfileMapping` reads it directly; operators
  don't need to learn about Airflow Connections to run dbt.
- **Composition-pattern documentation** — the operator-facing
  `first-pipeline.html` page documents three composition patterns
  (Airbyte → dbt → Metabase, Python ingest → dbt → Tableau, dlt →
  dbt → engineer-queries-warehouse) with complete copy-paste DAG
  snippets. Each pattern is independent; operators compose to fit
  their actual stack.
- **Service catalogue** — central registry of manageable services with
  tier classification (hot or cold), layer, compose snippet path,
  pre-start and post-start hook bindings, and connection-URL templates.
  Adding a new service is a catalogue entry plus a compose snippet plus
  optional hooks; no further integration code is required.
- **Hot-cold tier orchestration** — reconciler loop that stops idle
  cold-tier services on a periodic tick, honouring active sessions and
  active service pins. Brings an aggregate-on resource footprint of
  approximately 7.3 GB within an eight-gigabyte single-host envelope.
- **Setup wizard** — five-step operator-onboarding flow (welcome →
  service selection → configuration → review → deploy) that bootstraps
  the operator's first administrator account, writes the environment
  file, and starts the selected hot-tier services.
- **Administrator dashboard** — operator-facing UI built with FastAPI,
  HTMX, Jinja, and Tailwind. Pages: Service status, Service detail,
  Open sessions, Audit log, Credentials, Users, Roles, Profile.
  Cache-Control no-store middleware ensures the dashboard never serves
  stale state.
- **Role-based access control** — role-permissions model with four
  per-service grants (start, use, force-stop, edit-config) and wildcard
  service support. Effective-permissions matrix and explicit-grants
  matrix surfaced separately so revocation works correctly against
  wildcard-derived grants.
- **Session lifecycle** — one-session-per-operator-per-service
  invariant, cascade through declared service `requires` (opening
  pgAdmin opens its PostgreSQL session), automatic close on service
  stop, four-hour auto-pin on first open of cold-tier services to
  prevent reconciler interruption during long analytical sessions.
- **Audit log** — append-only event stream covering every privileged
  state transition (lifecycle, sessions, permissions, credentials).
  Surfaced through the dashboard's Audit page with target and actor
  filtering.
- **Integrated services** — pre-start hook + post-start hook +
  catalogue entry + operator-facing docs page for each of: PostgreSQL,
  pgAdmin, Metabase, Apache Airflow, Airbyte, dbt Core, MinIO,
  OpenMetadata, Great Expectations. OpenMetadata's post-start hook
  applies the single-node Elasticsearch replica-count fix and resets
  the administrator password idempotently on every start.
- **Operator documentation site** — 28 pages covering installation,
  configuration, hot-cold tiers, roles and permissions, sessions,
  account management, credentials, backup and restore, upgrading,
  troubleshooting, and one page per managed service with a runbook-
  style troubleshooting section.
- **Container images** — published to Docker Hub at
  `tripleaceme/orchestack-{auth,orchestrator,dashboard,ge}` with full
  OCI labels (title, description, vendor, source, documentation,
  licenses, revision, version, created).
- **Runtime bundle** — minimal operator install tarball
  (`orchestack-runtime.tar.gz`) attached to each GitHub Release.
  Contains docker-compose.yml, .env.example, traefik config,
  postgres-init scripts, per-service compose snippets, INSTALL.md, and
  a VERSION file. ~30 KB.
- **One-line installer** — `curl -sSL https://orchestack.africa/install.sh | bash`
  end-to-end install that prompts for the platform database password,
  writes the environment file, and starts the stack.
- **Contribution scaffolding** — CONTRIBUTING.md (development setup,
  branch and commit conventions, PR submission flow, release process,
  maintainer-only areas), ARCHITECTURE.md (subsystem-level
  documentation for contributors), CODEOWNERS, pull request template,
  issue templates (bug report, feature request, config), Apache 2.0
  LICENSE.
- **CI workflows** — PR-gate workflow (`ci.yml`) that builds all four
  images and runs container healthchecks on every PR against main;
  release workflow (`release.yml`) that builds and pushes images to
  Docker Hub and attaches the runtime bundle to the GitHub Release on
  every `v*.*.*` tag push.

### Known issues

End-to-end verification on a fresh install (`orchestack-runtime-0.1.0.tar.gz`)
surfaced two install-time bugs that block parts of the canonical
pipeline. Both are scheduled for v0.1.1.

- **[#1](https://github.com/tripleaceme/orchestack-public/issues/1)** —
  Airflow start fails with `external volume orchestack-dbt-repo not found`
  if Airflow is opened before dbt. The volume is declared `external: true`
  in `system/docker/services/airflow.yml` but only created by the dbt
  service. **Workaround**: open the dbt service tile in the dashboard
  first, then Airflow.
- **[#2](https://github.com/tripleaceme/orchestack-public/issues/2)** —
  Airbyte's Temporal container crash-loops with `pq: database "temporal"
  does not exist`. The orchestrator's pre-start hook creates `temporal_db`
  following the platform-wide `<service>_db` naming convention, but
  Temporal's binary hardcodes the unsuffixed name `temporal`.
  **Workaround**: `ALTER DATABASE temporal_db RENAME TO temporal;` and
  `ALTER DATABASE temporal_visibility_db RENAME TO temporal_visibility;`
  then restart the Airbyte service. See the issue for the full command.

What v0.1.0 verifies end-to-end **without** workarounds: install (download,
checksum, extract, control-plane up), signup, setup wizard, audit-log
event recording, .env write-back, dashboard authentication, Metabase
start + bootstrap + warehouse registration, pgAdmin start with
PostgreSQL cascade, MinIO start, dbt service start.

### Security

- **No credentials in images** — `.dockerignore` excludes `**/.env`
  (allow-listing only `.env.example`) so build contexts never carry
  resolved credentials into image layers.
- **Per-service database roles** — each integrated service owns a
  dedicated PostgreSQL role and database; no tool ever sees another
  tool's data. The platform's privileged `orchestack_admin` role
  bootstraps the others and is not used at runtime.
- **Opaque session tokens** — authentication uses opaque tokens stored
  in PostgreSQL rather than self-validating JWTs, enabling immediate
  revocation through the dashboard's Sessions page.
- **bcrypt password hashing** — cost factor twelve, verification
  executed inside an asynchronous thread pool to avoid blocking the
  event loop during login.
- **Audit-log credential masking** — credential-update events record
  the affected key name and the actor identity but not the credential
  value itself; exported audit logs do not leak credential material.

[Unreleased]: https://github.com/tripleaceme/orchestack-public/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/tripleaceme/orchestack-public/releases/tag/v0.1.1
[0.1.0]: https://github.com/tripleaceme/orchestack-public/releases/tag/v0.1.0
