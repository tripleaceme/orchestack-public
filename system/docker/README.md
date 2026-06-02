# OrcheStack — Docker Compose specification (M1)

This folder contains the base Docker Compose specification for OrcheStack's
control plane. Running `docker compose up -d` from this directory brings up
five containers that together form the platform's foundation: a reverse proxy,
a PostgreSQL instance, an auth/setup nginx container, and stubs for the
dashboard and the service orchestrator.

This is the M1 deliverable. The stubs (dashboard, orchestrator) are placeholders
that get replaced with real implementations at M2 and M3 respectively.

## Quick start

```sh
# One-time: copy the env template and set a real password for OrcheStack's
# internal database. These credentials bootstrap OrcheStack's own state
# (users, sessions, roles) — NOT your pipeline data. Your pipeline DB is
# configured later via the in-browser setup wizard.
cp .env.example .env
$EDITOR .env                  # change ORCHESTACK_DB_PASSWORD

# Bring up the five base services.
docker compose up -d

# Confirm everything is healthy (postgres takes ~20s on first boot).
docker compose ps

# Open the smoke-test surfaces in a browser:
#   http://localhost/signup            → first-admin bootstrap (auth nginx)
#   http://localhost/login             → admin login form
#   http://localhost/setup/welcome.html → onboarding wizard
#   http://localhost/app                → dashboard (HTMX + FastAPI)
#   http://localhost:8080/dashboard/   → Traefik dashboard (routing inspection)

# Stop everything (preserves data volume).
docker compose down

# Wipe everything including the PostgreSQL data volume (destructive).
docker compose down --volumes
```

## What's in this folder

| Path | Purpose |
|---|---|
| `docker-compose.yml` | The base compose specification — 5 services, 1 network, 1 volume |
| `.env.example` | Template for environment variables (ORCHESTACK_DB_*, image tags). OrcheStack's bootstrap credentials only — pipeline DB credentials are collected in the wizard |
| `.env` | Your actual secrets (gitignored — never commit) |
| `traefik/traefik.yml` | Traefik static configuration (entry points, providers, dashboard) |
| `traefik/dynamic/` | Dynamic Traefik config files (empty at M1, populated at M4 if needed) |
| `postgres-init/00-init.sql` | Creates the platform/raw/marts schemas on first boot |
| `postgres-init/10-platform-schema.sql` | 10 platform.* tables, indexes, triggers, and seeded Admin/Engineer/Analyst roles |
| `stubs/(removed in M3 — dashboard image replaces it)` | Static placeholder served at /app until M3 |

## The five base services

| Service | Image | Role | Replaced at |
|---|---|---|---|
| `proxy` | `traefik:v3.2` | Reverse proxy on :80 / :443 | — (Traefik stays) |
| `postgres` | `postgres:16-alpine` | Platform metadata + warehouse | — (PostgreSQL stays) |
| `auth` | `tripleaceme/orchestack-auth` | nginx serving signup/login/setup | — (this is the real image already) |
| `dashboard` | `tripleaceme/orchestack-dashboard` | HTMX + FastAPI admin UI at `/app` | shipped at M3 |
| `orchestrator` | `tripleaceme/orchestack-orchestrator` | Service-lifecycle daemon | shipped at M2 |

## Routing — how requests reach each service

Traefik routes by URL path. The rules live as `labels:` on each service in
`docker-compose.yml`:

| Path pattern | Goes to | Notes |
|---|---|---|
| `/signup`, `/login` | `orchestack-auth` | Exact match |
| `/setup/*` | `orchestack-auth` | Prefix match (welcome, select, configure, deploying) |
| `/assets/*` | `orchestack-auth` | Prefix match (CSS, fonts, etc.) |
| `/app/*` | `orchestack-dashboard` | Prefix stripped before forwarding (M1 stub only; M3 removes the stripping) |
| everything else | (404 — unrouted) | M4 adds routes for optional tools like `/app/metabase`, `/app/airflow`, etc. |

The `postgres` and `orchestrator` services have no Traefik labels — they are
not HTTP-routable. `postgres` is reached by service name (`orchestack-postgres`)
on the internal network; the orchestrator only consumes (it reads the database
and controls containers via the Docker socket, but exposes no HTTP surface).

## Troubleshooting

**The auth container fails to pull.** The `tripleaceme/orchestack-auth:latest`
image has not been pushed yet (that's M1 step 1.7). For now, uncomment the
`build:` block in the `auth` service definition to build from the local
Dockerfile at `OrcheStack/system/auth/`. The build context is the OrcheStack
repo root because the Dockerfile pulls in the shared `assets/css/` from there
(single canonical CSS source, shared with the marketing site):

```yaml
auth:
  # image: tripleaceme/orchestack-auth:${AUTH_TAG:-latest}
  build:
    context: ../..
    dockerfile: system/auth/Dockerfile
  ...
```

Then run `docker compose up -d --build auth`.

**PostgreSQL fails to start with "ORCHESTACK_DB_PASSWORD must be set".** You
haven't created `.env` yet. Copy from `.env.example` and set
`ORCHESTACK_DB_PASSWORD` to a strong value. This bootstraps OrcheStack's
internal database — not your pipeline DB (which the wizard handles).

**Port 80 is already in use.** Another service is binding port 80 on your host.
Either stop it (`lsof -i :80` to find the culprit) or temporarily change the
proxy's port mapping to `"8000:80"` and use `http://localhost:8000` instead.

**Traefik dashboard shows the auth or dashboard router as unhealthy.** Check
the upstream container's logs (`docker compose logs auth`) — the most common
cause is the container isn't on the `orchestack-net` network, but every
service in this compose file is explicitly bound to it, so this shouldn't
happen unless the compose file has been edited.

## The `platform.*` schema

After PostgreSQL finishes its first boot, the platform schema contains 10
tables that the orchestrator (M2), the dashboard (M3), and the
in-package auth pages will read and write. Quick reference:

| Table | What it holds | Owners (read/write) |
|---|---|---|
| `users` | Account records (bcrypt hash, full_name, email, company_name, onboarding flag) | Auth pages write; dashboard reads |
| `sessions` | Login session tokens (UUID cookies, 12h TTL by default) | Auth pages write; every request reads |
| `roles` | Built-in (Admin/Engineer/Analyst) + custom roles | Dashboard RBAC panel reads/writes |
| `user_roles` | M2M between users and roles | Dashboard RBAC panel reads/writes |
| `role_permissions` | Per-role × per-service `can_start`/`can_use`/`can_force_stop`/`can_edit_config` matrix; `service_name='*'` is a wildcard | Dashboard reads on every auth check |
| `installed_services` | Registry of services the operator has configured (one row per tool, with tier + idle_timeout_seconds) | Setup wizard writes on completion; orchestrator + dashboard read |
| `service_sessions` | Reference-counted active sessions per service per user (heartbeat-based stale detection) | Dashboard writes when a user opens a tool; orchestrator reads on every tick |
| `service_pinning` | "Keep warm" pins suppressing the idle-timeout shutdown | Dashboard toggle writes; orchestrator reads on every tick |
| `audit_log` | Append-only record of privileged actions (role changes, force-stops, pin set/unset, etc.) | Every privileged path writes |
| `setup_state` | Resumable in-progress wizard state per user (current step + tool selections) | Setup wizard writes on every step transition |

Built-in roles are seeded with `'*'` wildcard permissions so they apply to
every service, including ones added later. Specific-service rows override
the wildcard when present.

To inspect the schema after first boot:

```sh
docker compose exec postgres psql -U orchestack -d orchestack \
  -c "\dt platform.*"

# Or see the seeded roles + permissions:
docker compose exec postgres psql -U orchestack -d orchestack \
  -c "SELECT r.name, rp.service_name, rp.can_start, rp.can_use, rp.can_force_stop, rp.can_edit_config FROM platform.role_permissions rp JOIN platform.roles r ON r.id = rp.role_id ORDER BY r.id;"

# Or check bootstrap log to confirm init ran end-to-end:
docker compose exec postgres psql -U orchestack -d orchestack \
  -c "SELECT * FROM platform.bootstrap_log ORDER BY id;"
```

### What is NOT in the schema yet

- **Database roles** (separate PostgreSQL users for the orchestrator,
  dashboard, and dbt). M1 uses the superuser everywhere for simplicity;
  M5 polish adds least-privilege role separation.
- **Row-level security policies.** Not needed for the single-tenant
  single-operator design.
- **`raw.*` and `marts.*` tables.** Those are owned by Airbyte (raw) and dbt
  (marts) and only appear when M4 integrates those services.
