# OrcheStack — read-only database audit

Bash script that materialises the fourth verification layer described
in Section 4.5 of the project report:

> During development, representative user interactions were followed
> by queries against the audit log, session tables, and permission
> tables to verify that the platform's persistent state accurately
> reflected the actions performed through the dashboard.

`verify.sh` runs a curated set of read-only queries against the
platform database and reports whether the persisted state matches the
invariants the orchestrator promises. This is the layer that surfaced
the JSONB serialisation defect described in Section 4.4 — the
dashboard was showing empty step_results while the DB itself
contained the executor's writes, a discrepancy invisible to any
single-layer check.

## What each query answers

The script prints one section per invariant and a green tick / red
cross per row inspected:

1. **Schema completeness** — every expected `platform.*` table exists
   and the applied-migrations tracker is consistent with the SQL
   files shipped in the bundle.
2. **Audit log attribution** — every audit row has a non-null actor
   (either a real user id or the system default), a non-empty
   event_type, and a well-formed JSONB details payload. The report
   cites this as the guarantee that supports operational traceability.
3. **Session invariants** — no operator holds two open sessions for
   the same service; every open service has at least one session (per
   Section 4.2.4's session lifecycle invariants).
4. **Permission consistency** — no role holds BOTH a wildcard row
   AND per-service rows for the same permission set (that combination
   was the class of state the wildcard-to-explicit rewrite on save is
   designed to prevent).
5. **Pipeline runs' JSONB shape** — every `step_results` column
   decodes as a JSON array of objects (not a JSON-encoded string).
   This is the exact defect §4.4 describes; the query below is the
   canonical regression check.
6. **Orphan detection** — sessions without a user, permissions
   without a role, pipeline_steps without a pipeline. Non-empty
   result = referential integrity broken.

## When to run

- **After a merge that touches the orchestrator's DB writers** — any
  new INSERT into audit_log, service_sessions, role_permissions, or
  pipeline_runs should keep the invariants above green.
- **After a v0.1.1 stabilisation-style cycle** — new features often
  add new columns; the JSONB shape check catches the class of
  double-encoding drift the report §4.4 flags.
- **When triaging an operator-reported "the dashboard says X but the
  DB says Y" bug** — this script's queries are the reproducible
  baseline the maintainer runs against the operator's install to
  reproduce.

## How to run

From the repo root, with an OrcheStack install running locally:

```bash
bash testing/db-audit/verify.sh
```

The script connects via `docker exec orchestack-postgres psql`, so no
`psql` client is needed on the host. It uses the credentials the
orchestrator container itself uses, read from the running container's
env (never hard-coded).

Optional overrides:

```bash
POSTGRES_CONTAINER=orchestack-postgres  \
POSTGRES_DB=orchestack_db                \
bash testing/db-audit/verify.sh
```

Exit code:
- `0` — all invariants green
- `1` — at least one invariant violation (details printed above the
  summary line)
- `2` — infrastructure problem (container missing, psql unavailable)

## What this script does NOT do

- It does NOT modify any data (deliberate — read-only for safety
  under `set -u` even against a production DB).
- It does NOT test the orchestrator's Python code — that's what the
  `_smoke_test.py` at `system/dashboard/_smoke_test.py` and the
  targeted Playwright drives in `../PlayWright/` do.
- It does NOT verify container-level health — see `../health-checks/`
  for that layer.
