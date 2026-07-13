# OrcheStack — container health-check verification

Bash script that materialises the first of the four verification
layers Section 4.5 of the project report describes:

> Container-level health checks are declared on each service's compose
> snippet through Docker's healthcheck directive. Each managed service
> has a healthcheck that runs every 30 seconds, times out at 5
> seconds, and retries three times before marking the container
> unhealthy.

The script confirms three things end-to-end:

1. **Every managed service's compose snippet declares a HEALTHCHECK**
   — the "smallest meaningful unit of service availability" test the
   report describes (`pg_isready` for Postgres-based services,
   `/api/health` / `/api/v1/system/version` for HTTP services, etc.).
2. **Every currently-running container reports a healthy state** —
   read from Docker's own view via `docker inspect --format
   {{.State.Health.Status}}`. This distinguishes "container's process
   is running" from "container's application is genuinely operational."
3. **The orchestrator's control-plane `/api/health` returns ok** —
   the endpoint the dashboard's platform-health card polls. Confirms
   the orchestrator can reach its own dependencies (Postgres and the
   Docker daemon).

## When to run

- **Before cutting a release** — the whole matrix must be green.
- **After changing a service's compose snippet** — confirms the
  healthcheck directive survived the edit and still probes the right
  path.
- **When triaging a "why is my dashboard slow?" report** — an
  UNHEALTHY row in the output is the reason, and the row's container
  name points at which service's logs to open next.

## How to run

From the repo root, with an OrcheStack install running locally:

```bash
bash testing/health-checks/verify.sh
```

Optional environment overrides:

```bash
ORCHESTACK_URL=http://localhost:8080  \
COMPOSE_DIR=~/orchestack               \
bash testing/health-checks/verify.sh
```

The script prints a table and exits non-zero if any managed
container is unhealthy or the orchestrator health endpoint returns a
failing `checks:` payload. Suitable for CI use.

## What the script does NOT check

- Traefik loadbalancer probes are not part of the container-health
  layer; they belong to the smoke runbooks in `../runbooks/`. The
  metabase.yml note about "Do NOT re-add a Traefik loadbalancer
  healthcheck" explains why: Traefik-level checks race with the
  container's own `start_period` and cause 502s during Liquibase
  init.
- The dashboard's platform-health card is an operator-facing
  presentation of what this script reads. If the script says every
  container is healthy but the card says something different, the
  bug is in the card's data pipeline, not in the containers — file
  it against the dashboard, not the services.
