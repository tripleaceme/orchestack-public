# M2 — Orchestrator design

**Status**: Design — implementation not started.
**Owner**: Ayoade.
**Last updated**: 2026-06-02.

This document specifies the OrcheStack orchestrator: the Python service-lifecycle
daemon that replaces the M1 alpine stub at `system/docker/docker-compose.yml`'s
`orchestrator` service. The orchestrator is the milestone that implements
OrcheStack's actual original contribution — event-driven hot/cold tier
orchestration — so this doc is written deliberately before any code. It is the
source material for Chapter 4 of the academic report.

---

## 1. What M2 does in one paragraph

The orchestrator owns the *configured → active → cold* state transitions for
every data-pipeline service in the OrcheStack stack. It receives the operator's
choices from the setup wizard, materialises the pipeline database, and from then
on runs as a reconciler: it watches a session table written by the dashboard
(M3) and the per-tool services themselves, and brings containers up or
down so that the resident-memory footprint of the host matches current activity.
Idle cold-tier services are stopped after a configurable timeout; pinned
services stay running regardless. M2 makes the proposal's three-state lifecycle
real.

---

## 2. Process model & deployment shape

One container. One Python process. Same name as the M1 stub: `orchestrator`.
The compose service entry stays identical; only the `image:` line swaps from
`alpine:3.20` to `tripleaceme/orchestack-orchestrator:latest`.

Inside the container:

- **FastAPI** serves the control-plane API on port `8000` (internal, behind
  Traefik at path `/orchestrator/*`).
- **A single asyncio background task** runs the reconciler loop every
  `ORCHESTRATOR_RECONCILE_INTERVAL` seconds (default 30s). The reconciler is
  in-process — not a separate container — to keep the failure model simple:
  one process, one PID, one log stream.
- **No local state.** Everything the orchestrator knows lives in `platform.*`
  tables. The container can be killed and restarted at any time without losing
  consistency.

Required mounts:

- `/var/run/docker.sock` — **NO**. The orchestrator talks to the socket-proxy
  service over the internal Docker network, same way Traefik does. This means
  the orchestrator's privileges are bounded by the socket-proxy's ACL.
- `system/docker/services/` (read-only) — per-tool compose snippets.

Required environment variables (added to `.env.example` during M2):

```
ORCHESTRATOR_RECONCILE_INTERVAL=30      # seconds between reconciler ticks
ORCHESTRATOR_IDLE_THRESHOLD=600         # seconds a service can be idle before
                                        # the reconciler stops it (default 10m)
ORCHESTRATOR_LOG_LEVEL=info
```

Plus the existing `ORCHESTACK_DB_*` set (already used by the M1 stub to read
platform tables).

---

## 3. API surface

All endpoints are JSON in / JSON out. Authentication is deferred to M3 (the
dashboard is the only client at M2; both run on the same trusted
internal network).

### Wizard handoff

```
POST /api/setup/deploy
```

Called once, by the deploying.html page, when the operator clicks "Create
services" on the setup wizard. Request body is the operator's full setup
state (the same shape that today lives in `localStorage.OrcheStack.setup`):

```json
{
  "profile": { "full_name": "...", "email": "...", "username": "..." },
  "selections": { "ingestion": "Airbyte", "warehouse": "PostgreSQL", ... },
  "credentials": {
    "PIPELINE_DB_USER": "...",
    "PIPELINE_DB_PASSWORD": "...",
    "PIPELINE_DB_NAME": "...",
    "AIRFLOW_FERNET_KEY": "...",
    "OPENMETADATA_JWT_SECRET": "...",
    ...
  }
}
```

What the handler does, in order:

1. **Validate.** Reject obviously broken values (empty password, malformed
   email) with 400. Don't trust the wizard's client-side validation.
2. **Create the pipeline database.** `CREATE DATABASE ${PIPELINE_DB_NAME}`,
   `CREATE ROLE ${PIPELINE_DB_USER} LOGIN PASSWORD '...'`, then
   `GRANT ALL ON DATABASE ${PIPELINE_DB_NAME} TO ${PIPELINE_DB_USER}`. Run
   under the bootstrap superuser (the `ORCHESTACK_DB_USER` from `.env`).
3. **Materialise per-service .env files.** Each selected tool gets its
   credentials written to `./config/<tool>.env` on the host (mounted from
   `system/configs/`). This is what the per-service compose snippets
   `env_file:` will read.
4. **Persist the setup state.** Write the full request body to
   `platform.setup_state` (one row, latest wins) so the orchestrator can
   re-read it on restart.
5. **Trigger initial pulls.** For each selected tool, run `docker compose
   pull` in the background — operator sees the image-pull progress on
   deploying.html via the activity feed (step 6). No `up` yet — tools come
   up lazily on first session.
6. **Return** 202 Accepted with a deploy ID. The client polls
   `GET /api/setup/deploy/{id}` for status (`pulling`, `ready`, `error`).

### Service control

```
GET    /api/services            -> [{ name, state, last_active_at, pinned }, ...]
POST   /api/services/{name}/start
POST   /api/services/{name}/stop
POST   /api/services/{name}/pin      body: { ttl_seconds: 7200 | null }
DELETE /api/services/{name}/pin
```

`state` is one of `stopped`, `starting`, `running`, `stopping`, `error`. The
state is **derived**, not stored: it's computed by combining `docker compose ps`
output with the reconciler's intent for that service.

### Sessions (the bit that makes the hot/cold tier work)

```
POST /api/sessions                 body: { service, session_token, user_id }
POST /api/sessions/{token}/checkin
DELETE /api/sessions/{token}
```

The dashboard calls `POST /api/sessions` when a user opens a tool UI, calls
`/checkin` on a 60s timer while they're on the page, calls `DELETE` when they
navigate away or close the tab. The orchestrator increments / refreshes /
decrements rows in `platform.service_sessions`. **The reconciler reads only
this table to decide what's idle.**

### Health

```
GET /api/health  -> { ok: true, postgres: true, docker: true, ... }
```

For the container's HEALTHCHECK (replaces the M1 stub's `while true; do echo;
done` no-op) and for the dashboard's status pane.

---

## 4. Reconciler algorithm

Single function, runs every `ORCHESTRATOR_RECONCILE_INTERVAL` seconds. Pure
read-decide-act loop:

```
def reconcile():
    now = utcnow()
    sessions = db.query("""
        SELECT service_name, COUNT(*) AS n
        FROM platform.service_sessions
        WHERE last_seen_at > NOW() - INTERVAL '5 minutes'
        GROUP BY service_name
    """)
    pinned = db.query("""
        SELECT service_name
        FROM platform.service_pinning
        WHERE expires_at IS NULL OR expires_at > NOW()
    """)
    running = docker.ps()   # list of currently-running service names

    for svc in running:
        if svc in pinned:                continue                # protected
        if sessions.get(svc, 0) > 0:     continue                # in use
        if svc.uptime < idle_threshold:  continue                # too fresh
        docker.stop(svc)
        audit_log("idle_sweep_stopped", svc, reason="no_sessions")
```

Notable choices:

- **The 5-minute window** in the session query is a grace period — gives the
  client time to call `/checkin` even if it just lost network for 30 seconds.
- **The `uptime < idle_threshold` check** prevents the reconciler from
  immediately stopping a service that was just started (e.g., during a
  reconcile tick that happens to fire right after the dashboard's `/sessions` POST
  but before the user's request actually opens the page).
- **No spin-up in the reconciler.** Services start only via explicit
  `POST /api/services/{name}/start` (called from the dashboard or directly from
  the wizard handoff). The reconciler is shutdown-only. This keeps the loop
  simple and one-directional.

---

## 5. Database access patterns

The orchestrator is the only writer of these tables (M3 dashboard reads them
through the orchestrator's API, never directly):

| Table | Read | Write |
|-------|------|-------|
| `platform.service_sessions` | reconciler tick (5m window) | sessions API |
| `platform.service_pinning`  | reconciler tick | pin/unpin API |
| `platform.installed_services` | service-listing API | wizard handoff + start/stop |
| `platform.setup_state` | restart recovery | wizard handoff |
| `platform.audit_log`   | (M3 admin view) | every state change |

Connection: one async `asyncpg` pool, sized 5 connections (enough for the
reconciler + a handful of concurrent API requests; this isn't a high-QPS
service).

The orchestrator does NOT touch the customer pipeline database. Reading from
or writing to `raw.*` / `marts.*` is dbt and Airflow's job — the orchestrator
just creates the database, the role, and the compose-managed connection
strings.

---

## 6. Failure modes

| Failure | Detection | Response |
|---------|-----------|----------|
| Docker socket-proxy unreachable | `docker.ps()` raises | Mark all services `state: unknown`. Reconciler skips this tick. Retry next tick. Health endpoint returns `docker: false` |
| Postgres unreachable | `asyncpg` connection fails | Reconciler waits; API requests that need DB return 503 |
| Pipeline DB creation fails | `CREATE DATABASE` raises | Return 500 from `/api/setup/deploy` with stderr. Wizard surfaces error. No partial state — the operator re-runs after fixing the cause |
| Compose pull fails (no image, network error) | subprocess returns non-zero | 3 retries with exponential backoff (5s, 15s, 45s). Then mark service `state: error`. Next user click re-triggers |
| Compose up fails (image present but won't start, e.g. config error) | container exits within 10s | Capture last 50 lines of `docker logs`, write to audit log, mark `state: error` |
| Reconciler loop crashes (unhandled exception) | uvicorn supervises the task | Restart the loop after 5s pause. Don't crash the API server — operator can still see the error in `/api/health` |
| Service stopped externally (operator ran `docker stop` manually) | reconciler tick observes state mismatch | Update `installed_services` row, log to audit. No attempt to revive — assume operator knew what they were doing |

The recurring pattern: **never crash the orchestrator.** Every failure is
loggable, observable, and recoverable on the next tick. A degraded orchestrator
is always better than a dead one because the dashboard depends on it.

---

## 7. Implementation phases

Each phase is testable independently and produces something visible. M3 can't
start meaningfully until phase 4 is done.

| Phase | What ships | Time |
|-------|-----------|------|
| **2.1** Skeleton image | FastAPI app, `/api/health`, Docker image builds via CI same way auth does, compose `image:` swaps from alpine stub | 1 day |
| **2.2** Service control (no reconciler) | `GET /api/services`, `POST /api/services/{name}/{start,stop}`, shells out to compose CLI, writes audit log | 2-3 days |
| **2.3** Per-service compose snippets | `system/docker/services/{airbyte,airflow,dbt,metabase}.yml`. Each one self-contained, references `./config/<tool>.env`. Manually testable: `docker compose -f services/airbyte.yml up -d` | 2 days |
| **2.4** Sessions table + reconciler | `POST /api/sessions/*` endpoints, reconciler loop reading sessions, idle-sweep stops idle services. End-to-end test: open then close a synthetic session, watch the service stop | 3 days |
| **2.5** Wizard handoff | `POST /api/setup/deploy` creates pipeline DB, writes per-service .env files, kicks off pulls. End-to-end test: walk through wizard on test PC, click Create services, watch DB + .env files appear | 2 days |
| **2.6** Pinning + keep-warm | `POST/DELETE /api/services/{name}/pin`, reconciler respects pins, default 2h TTL | 1 day |

**Total**: ~2 weeks if uninterrupted. ~3-4 weeks realistic.

---

## 8. What M2 does NOT include

Drawing the line clearly so this scope doesn't grow:

- **Authentication / authorisation.** All endpoints are open on the internal
  network. M3 layers session-cookie auth on top.
- **The dashboard.** That's M3. The orchestrator returns JSON;
  The dashboard decides how to render it.
- **Default Airflow DAGs, default dbt project.** Those are M4. M2 only provides
  the *containers*; M4 provides what runs *inside* them.
- **TLS / Let's Encrypt.** M5. M2 runs on plain HTTP behind Traefik.
- **Multi-tenant isolation.** OrcheStack is single-tenant by design. One
  orchestrator per host, one platform DB, one pipeline DB.
- **Rolling upgrades.** Restart-in-place is the upgrade model. Image-pull +
  compose `up -d` is enough for M2.

---

## 9. Open questions to resolve before phase 2.4

1. **Where do per-service compose snippets live in the released bundle?**
   Currently the M1 runtime tarball doesn't ship them. M2's build-bundle.sh
   needs to include `system/docker/services/*.yml`. Implication: the runtime
   bundle grows from ~13 KB to ~30 KB.
2. **Idle threshold default — 10 minutes too aggressive?** Worth measuring
   what typical "switch back to a tool" intervals look like during the M5
   evaluation. If operators routinely tab away for 15 minutes mid-flow, the
   default should be 20 minutes. Adjust later from the audit log data.
3. **What's the reconciler's behaviour during the wizard handoff?** While
   `POST /api/setup/deploy` is running (DB creation, image pulls, etc.), the
   reconciler shouldn't tick. Either pause it, or have it skip when
   `setup_state.status = 'deploying'`. The second is simpler.
4. **Compose subprocess vs. Docker API directly?** Phase 2.2 uses
   `subprocess.run(["docker", "compose", ...])` because it's simple and
   debuggable. If that adds noticeable latency to service-start (>2s overhead
   per call), switch to the Docker SDK. Defer the decision until we measure.
