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

## [0.1.1] — 2026-06-25

### Fixed (Airflow 3 + dbt container restart-loops)

- **Airflow restart-looped on first start** because the compose snippet
  still ran `airflow webserver`, which Airflow 3 removed in favour of
  `airflow api-server`. Compounded by a YAML folding bug — the `>`
  block scalar preserves newlines before more-indented continuation
  lines, which silently split the `airflow users create --role Admin`
  command from its `--username`, `--email`, `--firstname`, `--lastname`,
  `--password` args. Bash then ran each arg as a standalone command
  (`/bin/bash: line 7: --password: command not found`) and `users create`
  itself failed with "missing required arguments." Three fixes:
  1. `airflow webserver` → `airflow api-server` at the end of the
     entrypoint
  2. All multi-arg commands (notably `users create`) put on a single
     YAML line so the folded scalar joins them with spaces instead of
     preserving newlines
  3. Added `AIRFLOW__API__*` environment variables alongside the legacy
     `AIRFLOW__WEBSERVER__*` ones — Airflow 3 reads from `[api]` and
     falls back to `[webserver]` with a DeprecationWarning. Setting
     both keeps both code paths happy + silences upgrade noise.
  4. DAG repo clone is now resilient to a stale `dags-clone` directory
     and prints a clearer failure message when the URL lacks a PAT.

- **dbt container restart-looped with**
  `No dbt_project.yml found at expected path /usr/app/dbt/dbt_project.yml`
  on every restart after a previous partial run had populated the volume.
  The entrypoint's clone logic ran `git clone X /usr/app/dbt` which fails
  with `destination path already exists and is not an empty directory`
  when the volume is non-empty (any leftover from a previous attempt
  triggered this). The fallback demo-project block was inside an `elif`,
  so it never fired when `DBT_REPO_URL` was set + the clone failed.
  Fixes:
  1. When `DBT_REPO_URL` is set but `/usr/app/dbt` has no `.git`,
     `find ... -delete` clears the directory's contents (without
     removing the mountpoint itself) before retrying the clone.
  2. The demo-project fallback moved out of `elif` to a plain `if`, so
     it fires even when a clone attempt failed. The container always
     comes up with SOMETHING usable; the operator fixes `DBT_REPO_URL`
     via Edit Config when ready.
  3. Clearer error message when the clone fails (suggests adding a
     PAT to the URL for private repos).

### Fixed (critical)

- **Session-open HTTP request no longer blocks for 5–15 min waiting on
  image pull.** `POST /api/sessions` (the endpoint behind every Open /
  Start click) used to `await docker_ops.start_service()` synchronously,
  blocking the HTTP response for the full duration of the
  `docker compose up` — up to 15 min on first-pull of heavy images like
  orchestack-airflow (~2.4 GB). The browser timed out or the operator
  reloaded; either way the audit log showed `session_opened` with no
  follow-up `session_autostart` event because the start was still in
  flight. The dashboard's Open click felt like nothing was happening.
  Refactored to fire-and-forget via `asyncio.create_task` — the response
  returns immediately with `started: true` (optimistic), and the
  background task writes `session_autostart` / `session_autostart_failed`
  to the audit log when the start completes.

### Changed

- **Deploy page redesigned with per-service status table.** Replaced the
  joke carousel + rotating status text with a live table of every
  hot-tier service being started — each row shows the service name,
  current state (Queued / Pulling image / Running / Failed), and a
  per-service error snippet when an autostart fails. The deploying page
  polls both `/api/services` and `/api/audit` so it can surface
  `session_autostart_failed` events as they happen, rather than waiting
  silently for the 25-min cap. The progress bar now reflects the real
  ratio of services-running to total-being-watched. Operators stop
  wondering "is anything happening?" — they see exactly which image is
  pulling and which service has hit an error.

- **Service tiles update on the next 1–2 second polling cycle after
  Start / Stop**, not the next 10s cycle. The dashboard's start + stop
  endpoints now return an `HX-Trigger: orchestack-grid-refresh` header,
  and the service grid + KPI strip both listen for that custom event
  via `hx-trigger="… orchestack-grid-refresh from:body"`. Combined with
  optimistic state rendering on the clicked card itself (returns
  state="starting" / "stopped" immediately rather than waiting for the
  orchestrator's view to update), the operator sees their click
  reflected within the same paint cycle.

- **`/setup/deploying.html` now waits for hot-tier services to be
  running before redirecting.** Previously the deploying page POSTed
  the wizard state to the orchestrator, got a fast `status: ready`
  response, then immediately redirected to `/app/`. Operators
  reached the dashboard and saw every tile as `Stopped` because the
  background image pulls had only just started. Now the page polls
  `/orchestrator/api/services` every 5 s after the deploy returns,
  updates the visible status text with the names of services still
  pulling/starting, and only redirects once every registered
  hot-tier service reports `state: running`. 25-minute cap tolerates
  first-pull on residential broadband (orchestack-airflow alone is
  ~2.4 GB on top of a ~2.3 GB base); if the cap hits with services
  still not running, the page redirects with a soft warning and lets
  the operator monitor progress from the dashboard's per-service
  detail pages.

- **Sidebar "+ Add another service" link** now matches the inline
  "+ Configure another service" affordance on the home grid:
  - Gated on `unconfigured_count > 0` — hides when every catalogue
    entry has already been configured, instead of dropping the
    operator on a dead-end wizard page.
  - Wording standardised to **"+ Configure another service"** across
    both entry points.
  - Counter shows real `configured / catalogue_total` (e.g. `5 / 9`
    when 5 of 9 services have been configured) — previously both
    halves of the counter were the same number due to a bug in
    `_aggregate_kpis`. Required adding `catalogue_total` as a
    separate KPI field, computed from the orchestrator's full
    service list rather than just the configured subset.

### Changed (continued)

- **Service tier classification revised.** `pgadmin` moved from cold to
  hot (operators reach for it dozens of times a session; the cold-start
  delay was annoying). `minio`, `airflow`, and `airbyte` moved from
  hot to cold (their resident memory cost is high enough that "always
  on" pushed the 8 GB envelope; cold-start cost is acceptable when
  they're not in continuous use). The new hot tier is exactly the
  three services operators interact with every day:
  PostgreSQL, Metabase, pgAdmin. Catalogue edit in
  `system/orchestrator/app/config.py`; the dashboard's tier badges and
  the reconciler honour the new classification automatically.

- **pgAdmin server label.** The pre-configured server in pgAdmin's
  navigator used to be named after `WAREHOUSE_DB_NAME` (e.g. `raw_data`),
  which confused operators because the server hosts multiple databases.
  The label is now the fixed string `"OrcheStack warehouse"`. The
  underlying connection is unchanged.

- **`.env` template extended** to include the four wizard-written keys
  that were previously being appended at the bottom under "Added by
  the setup wizard" (`DBT_DATABASE`, `DBT_SCHEMA`,
  `AIRFLOW_DAGS_REPO_PATH`, `MINIO_BUCKET`). They now have placeholder
  entries in their respective service sections, so the wizard updates
  them in place rather than appending — preserving the by-service
  grouping the .env.example template establishes.

- **Hot-tier services auto-start in the background after deploy.** The
  setup wizard's "Create services" used to register services in the
  platform DB and then exit; operators reached the dashboard and saw
  every tile as `Stopped`, unsure whether to wait or to click each
  one. The deploy endpoint now schedules `start_service` in the
  background for every registered hot-tier service (postgresql,
  metabase, pgadmin) so by the time the operator reaches the dashboard
  the tiles are at minimum in "Starting" state. Fire-and-forget — the
  /deploy response doesn't block on image pulls, which can take 10+ min
  on slow links. Cold-tier services remain on-demand: operators open
  them when they need them.

- **dbt operator docs** now include an explicit callout on
  `DBT_DATABASE` vs `DBT_SCHEMA` semantics — dbt creates schemas (any
  name works without pre-creation) but does NOT create databases (must
  pre-create in PostgreSQL before starting dbt if you point
  `DBT_DATABASE` at a name different from `WAREHOUSE_DB_NAME`). The
  Configure step's DBT_DATABASE hint carries the same warning.


First patch release. Closes both install-time bugs surfaced during
end-to-end verification of v0.1.0, plus a fifth round of operator-
visible polish discovered while walking a second operator through a
fresh install. Operators upgrade with
`docker compose pull && docker compose up -d` — no destructive
changes, no `.env` edits required.

### Fixed

- **Airflow service start timeout** — bumped from 300 s to 900 s in
  `system/orchestrator/app/docker_ops.py`. The orchestack-airflow image
  is ~2.4 GB and its base apache/airflow image is ~2.3 GB; on slower
  connections the first-pull plus container create exceeded the
  previous 5-minute cap, leaving the service stuck with a
  `session_autostart_failed` audit event and no operator-facing
  signal. The new 15-minute cap accommodates first-pull on typical
  African residential broadband. Cached starts remain sub-second.

- **`WAREHOUSE_DB_NAME` validation error message** — when an operator
  typed a hyphenated name (e.g. `raw-data`), the wizard rejected it
  with the bare regex pattern in the error. The error now explains
  what's allowed (letters, digits, underscores; start with letter;
  3–31 chars) and shows a concrete corrected example (`raw_data`).
  The validation itself is unchanged — hyphenated identifiers force
  PostgreSQL quoting in every downstream tool's connection string and
  reliably break dbt and Airflow when they hit them.

- **Wizard placeholders + admin-email defaults removed** — the signup
  form previously placeholder'd a real name (`Ayoade Adegbite`,
  `ayoade`, `you@company.ng`); replaced with `John Doe`, `johndoe`,
  `you@example.com`. The Configure step's Metabase and pgAdmin
  sections previously pre-filled `metabase_admin@orchestack.local`
  and `pgadmin_admin@orchestack.local` into the admin-email fields;
  removed both defaults so operators see empty fields with
  `you@example.com`-style placeholders and consciously enter a real
  address. (Operators were leaving the defaults in place and then
  forgetting which credential signed in to which tool.)

- **Metabase admin-password field clearer + warned** — the field's
  hint + placeholder now explicitly call out Metabase's
  "not too common" password rule and warn against typing your email
  as the password (which Metabase silently rejects with a confusing
  `400 password is too common` response that names the email in the
  payload). No validation change — Metabase enforces its own rule;
  this is just clearer guidance.

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
