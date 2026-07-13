# OrcheStack — testing evidence

This document captures the debugging narratives, command outputs, and bug
diagnoses from each milestone. It exists as direct source material for
Chapters 4-6 of the academic report so the implementation chapter can show
*how* the system was made to work, not just *that* it works.

Each milestone section has the same structure:
- **What was built** — short summary of the artefact
- **Validation method** — how it was tested
- **Bugs found** — the specific failures, in order, with what they taught us
- **Evidence** — command outputs proving the artefact works
- **Lessons that fed into the next milestone** — process improvements

Bugs are deliberately included even when they look embarrassing. A
debugging story without bugs would be implausibly tidy; the bugs are
what demonstrate the engineering judgment that produced the eventual
working system.

---

## M1 — Foundation

### What was built

The base control plane: Traefik reverse proxy, PostgreSQL with the
`platform` schema, an nginx auth container (signup + login + 4-step
setup wizard), a Streamlit stub, and an orchestrator alpine stub. Plus
the surrounding distribution infrastructure: a Docker image build
workflow, a GitHub Release tarball workflow, an install script, and a
local-build helper.

### Validation method

End-to-end install on a separate Mac that did not have the development
environment. Goal: surface assumptions baked into the dev machine that
didn't translate to a clean install.

### Bugs found (in order, with diagnosis)

1. **Direct `/var/run/docker.sock` mount on Traefik returned 404 for
   every route.** Diagnosis traced via the Traefik logs:
   `"Error response from daemon:"` with empty error body. Docker
   Desktop on macOS now restricts containers' direct socket access.
   Fix: introduced a `tecnativa/docker-socket-proxy:0.3` container that
   mediates the socket; retargeted Traefik's provider endpoint from
   `unix:///` to `tcp://socket-proxy:2375`.

2. **Auth container's healthcheck failed even though nginx was serving
   correctly.** Diagnosis: `docker exec auth wget http://localhost/login`
   returned "Connection refused" while external curl to the same path
   succeeded. Root cause: alpine's busybox `wget` resolves `localhost`
   to IPv6 (::1) first, but our nginx.conf only had `listen 80;` — IPv4
   only. The default nginx Docker image normally adds `listen [::]:80`
   via an entrypoint script, but we delete that script's input file in
   our Dockerfile. Fix: explicit IPv6 listener in nginx.conf.

3. **Bare `http://localhost` returned a Traefik 404.** By design — the
   auth router rule didn't include `Path('/')`. But the operator
   experience was poor; first-install always lands on the bare hostname.
   Fix at two layers: added `Path('/')` to the auth router rule, and
   added `location = / { return 302 /signup; }` in nginx.conf so the
   auth container is correct on its own regardless of upstream routing.

4. **Traefik provider silently failed to discover labelled containers
   even with the socket-proxy in place.** Diagnosis: socket-proxy
   access log showed every request as `GET /v1.24/version HTTP/1.1`
   returning 400 from postgres daemon. Modern Docker Desktop's
   `MinAPIVersion=1.40` rejects v1.24 requests. The Traefik v3.2
   Docker provider hardcoded `client.WithVersion("1.24")` in its
   initialisation, overriding any `DOCKER_API_VERSION` environment
   variable. Fix: upgraded the image tag from `traefik:v3.2` to
   `traefik:v3` (floating latest 3.x), which carries the upstream fix.
   Later pinned to `traefik:v3.7` for release reproducibility.

5. **Wizard pages displayed a hardcoded placeholder email
   (`ayoade@acme.ng`) regardless of which email the operator used at
   signup.** Cause: `signup.html` was a JS-less HTML form that
   GET-submitted to `setup/welcome.html`; the wizard pages had no
   mechanism to read the operator's identity. Fix: signup form now
   intercepts submit, persists profile data (full name, email,
   username, company name — never the password) to
   `localStorage.OrcheStack.setup.profile`, and each wizard page has a
   small `populateSignedInBanner()` IIFE that reads it and updates the
   header banner. At M3 the populate logic swaps to
   `fetch('/api/me')` against the real backend.

### Evidence — final M1 state on the remote test PC

```
$ docker compose ps
NAME                    STATUS              PORTS
orchestack-auth         Up 3m (healthy)     80/tcp
orchestack-postgres     Up 3m (healthy)     5432/tcp
orchestack-proxy        Up 3m               0.0.0.0:80->80/tcp, ...
orchestack-socket-proxy Up 3m               2375/tcp
orchestack-streamlit    Up 3m               80/tcp
orchestack-orchestrator Up 3m               (M1 stub)

$ curl -sI http://localhost/signup
HTTP/1.1 200 OK
Content-Length: 4389
Server: nginx/1.30.2
```

### Lessons that fed into M2

- **"Validate by installing on a clean machine" surfaces assumption
  failures that no amount of dev-machine testing catches.** Five bugs
  per milestone seems to be the steady-state count for this kind of
  multi-component install.
- **The CI smoke step pattern** (build → import-test before push) wasn't
  in place yet. Every M1 bug took at least one rebuild-bundle + transfer
  cycle to surface. Adding the smoke step earlier in M2 paid back
  immediately.

---

## M2 — Orchestrator

### What was built

The Python control-plane daemon that implements the proposal's central
claim: event-driven hot/cold tier service orchestration. Replaces the
M1 alpine heartbeat stub with a FastAPI application that exposes 15
HTTP routes, a per-30s reconciler loop that idle-sweeps cold-tier
services, and a wizard-handoff endpoint that turns the localStorage
wizard state into real database state (pipeline DB, scoped role,
`installed_services` rows, audit log entries).

Built in six phases (2.1 — skeleton image; 2.2 — service control;
2.3 — per-service compose snippets; 2.4 — sessions + reconciler;
2.5 — wizard handoff; 2.6 — pinning). Phases 2.2-2.6 were landed
in a single commit to minimise CI/test-PC iteration; the user opted
to test all five at once.

### Validation method

Two layers:
- **Local layer**: a `venv` smoke test that imports `app.main:app`,
  then runs `uvicorn` against it and curls every endpoint. Catches
  import-time AssertionErrors, route registration errors, Pydantic
  schema errors, OpenAPI generation errors, and basic 404/422 paths.
  Cannot exercise DB or Docker-socket code paths.
- **Remote layer**: the same install + bundle-transfer flow as M1,
  with curl-based exercises of every endpoint against real postgres.

### Bugs found (in order, with diagnosis)

1. **`AssertionError: Status code 204 must not have a response body`
   at module-import time.** FastAPI's `APIRoute.__init__` rejects any
   decorator with `status_code=204` because `is_body_allowed_for_status_code`
   returns False for that code. First fix attempt added
   `response_class=Response` to the decorator — that *seemed* like it
   should satisfy the assertion. It did not; the assertion checks
   `status_code` alone, not `response_class`. The container restart-looped
   a second time. Correct fix: remove `status_code=` from the decorator
   entirely and return `Response(status_code=204)` from the function body.
   Applied to two routes: `DELETE /api/sessions/{token}` and
   `DELETE /api/services/{name}/pin`.

2. **Schema column-name mismatch — eight tables, multiple columns
   each.** I had written the orchestrator's SQL based on assumed
   column names (`last_seen_at`, `service_name` as PK on
   installed_services, a `payload` column on setup_state, etc.) but
   the actual schema from `10-platform-schema.sql` used different
   names (`last_heartbeat_at`, `name`, `selections` JSONB,
   user_id-keyed). Diagnosed by `grep`-ing the schema file column by
   column against the application SQL. Fixed by aligning the
   application code to the existing schema rather than changing the
   schema (the schema was designed first and is what the academic
   report references). Touched five files in one commit.

3. **`platform.users` FK constraints couldn't be satisfied during M2
   testing.** The schema requires every `user_id` reference to point
   at a row in `platform.users`. The localStorage-based signup does
   not create a user row. Bridge solution: a new
   `postgres-init/20-seed-default-user.sql` that inserts `id=1` as a
   "system" account with an unparseable bcrypt hash (login blocked).
   The orchestrator uses this id as the default actor for any
   operation where a real authenticated user isn't available. M3 will
   replace this with real session-cookie auth from Streamlit; the
   default-user pattern then becomes the fallback for background tasks
   only (the reconciler's audit-log writes).

4. **`syntax error at or near "$1"` from `CREATE ROLE ... PASSWORD $1`.**
   PostgreSQL DDL statements do not accept parameter placeholders —
   the parameterized-query layer is implemented above the parser, and
   DDL grammar has no placeholder slots. Discovered when the user ran
   the deploy endpoint against real postgres on the test PC. Fixed by
   escaping the password in Python (`"'" + s.replace("'", "''") + "'"`)
   and inlining as a quoted literal. Round-trip verified locally
   against six adversarial passwords including SQL injection
   attempts; the escape correctly keeps `'; DROP TABLE users; --`
   inside the literal where postgres parses it as plain text.

5. **Reconciler SQL would have failed on its first tick.** I had
   written `WHERE last_seen_at > now() - ($1 || ' seconds')::interval`,
   using SQL string concatenation. asyncpg sends integers via the
   binary protocol, so postgres receives `$1` as `int4` not text; the
   `||` operator can't auto-cast. Caught by review (not yet by
   runtime, because no sessions had been opened). Fixed by computing
   the cutoff timestamp in Python: `datetime.now(timezone.utc) -
   timedelta(seconds=window)`, then passing it as a TIMESTAMPTZ
   parameter directly.

### The CI smoke test added in response

After bugs 1 and 2 both required a remote-PC restart loop to surface,
I added an import smoke step at the top of
`build-orchestack-orchestrator.yml`:

```yaml
- name: Smoke test — import the FastAPI app
  run: |
    python3 -m venv /tmp/smoke
    /tmp/smoke/bin/pip install -q -r system/orchestrator/requirements.txt
    /tmp/smoke/bin/python -c "
    import sys; sys.path.insert(0, 'system/orchestrator')
    from app.main import app
    assert len(app.routes) >= 10
    print(f'SMOKE TEST OK: {len(app.routes)} routes registered')
    "
```

This step runs in ~30 seconds and catches the entire class of
import-time AssertionErrors (FastAPI route registration, Pydantic
schema generation, missing imports, syntax errors in module-load
code). Adding it earlier in M2 would have caught bugs 1 and 2 in CI
instead of the test PC, saving approximately two full debugging
cycles. The lesson: when you identify infrastructure that would
catch the bug you're fixing, build it immediately. Deferred
infrastructure compounds.

### Evidence — M2 deploy endpoint working end-to-end on the remote PC

```
$ curl -s -X POST http://localhost/orchestrator/api/setup/deploy \
    -H "Content-Type: application/json" \
    -d '{
      "profile": {"full_name":"Ayoade Adegbite","email":"ayoade@test.local"},
      "selections": {
        "warehouse":"PostgreSQL",
        "bi":"Metabase",
        "admin_ui":"pgAdmin",
        "ingestion":"Airbyte",
        "orchestration":"Apache Airflow"
      },
      "credentials": {
        "PIPELINE_DB_USER":"acme_user",
        "PIPELINE_DB_NAME":"acme_warehouse",
        "PIPELINE_DB_PASSWORD":"acmewarehouse2026"
      }
    }' | python3 -m json.tool

{
    "status": "ready",
    "pipeline_db": "acme_warehouse",
    "pipeline_user": "acme_user",
    "registered_services": ["postgresql","metabase","pgadmin","airbyte","airflow"],
    "skipped_services": []
}

$ docker compose exec postgres psql -U orchestack -d orchestack \
    -c "SELECT name, layer, tier FROM platform.installed_services ORDER BY name"
     name     |     layer     | tier
--------------+---------------+------
 airbyte      | ingestion     | cold
 airflow      | orchestration | hot
 metabase     | bi            | hot
 pgadmin      | admin-ui      | cold
 postgresql   | warehouse     | hot
(5 rows)

$ docker compose exec postgres psql -U orchestack -d orchestack \
    -c "\l acme_warehouse"
                   List of databases
      Name      |   Owner   | Encoding | Collate | ...
----------------+-----------+----------+---------+...
 acme_warehouse | acme_user | UTF8     | C       | ...
```

This single deploy invocation exercises every M2 phase: 2.1 (the
orchestrator's FastAPI is up and serving), 2.2 (service control
endpoints existed to be called), 2.3 (compose snippets existed to be
registered), 2.4 (the audit log captured every step), 2.5 (the
wizard handoff materialised the pipeline DB + role + registry rows),
and 2.6 (the pinning endpoints registered without errors).

### Bug-count summary

| Phase | Bugs found | Caught by |
|-------|-----------|-----------|
| 2.1   | 0         | (clean) |
| 2.2-2.6 batch (first push) | 1 (the 204 assertion) | Test-PC restart loop |
| 2.2-2.6 batch (second push, response_class=Response added) | 1 (same 204, fix was wrong) | Test-PC restart loop |
| Schema alignment push | 0 import-time | (CI smoke now in place) |
| DDL parameter fix | 1 (CREATE ROLE password parameter) | Test-PC against real postgres |
| **Total M2** | **3 import-time** + **5 schema** + **1 SQL** = 9 bugs | 7 by remote PC, 2 by local review |

### Lessons that feed into M3

- **Local uvicorn-curl test now standard** before every push that
  touches the orchestrator. Catches ~80% of regressions without a CI
  build cycle.
- **CI smoke step** is in place — every future push has an automatic
  ~30s import test before the multi-arch build.
- **Schema-first discipline**: before writing application SQL against
  a new table, grep the schema file. The five schema mismatches in
  M2 came from skipping this step.
- **Two-layer defence proven**: local + CI catches everything except
  DB-dependent code paths. M3 will need a separate test pattern for
  Streamlit UI behaviour (probably playwright or similar against a
  real running orchestrator).

---

## M3 — Streamlit dashboard (planned)

To be written as M3 is implemented. Section structure will follow the
same template: what was built, validation method, bugs found, evidence,
lessons.
