# M4 — Multi-database architecture for managed services

Status: **per-service DB+role shipped (Metabase, June 2026). Per-schema
isolation tested + rejected for Metabase due to upstream constraint.
Remaining M4 services land incrementally.**

## §1. The schema-isolation experiment that failed

Original plan after operator review: one `services` database with one
schema per tool, each schema owned by a scoped PG role. This works in
principle but **fails for Metabase 0.51 specifically** because Metabase's
first-boot Liquibase changeset `v00.00-000` runs
`resources/migrations/initialization/metabase_postgres.sql` which
hardcodes `public.<tablename>` in every CREATE statement:

```sql
CREATE EXTENSION IF NOT EXISTS citext WITH SCHEMA public;
CREATE TABLE public.activity (...);
CREATE TABLE public.activity_id_seq (...);
```

`MB_DB_SCHEMA` is honored for runtime queries but ignored by the
initialization migration. With Metabase pointed at
`services.metabase`, the migration tries to write to
`services.public.<tablename>` and fails with `permission denied for
schema public` regardless of grant configuration. **Verified
end-to-end June 12 — search_path was set correctly, citext was
pre-installed by the platform admin, schema ownership was clean. The
migration still fails because it explicitly targets `public`, not
search_path[0].**

Architectural lesson: tools that hardcode their schema in SQL DDL
cannot live inside a shared per-tool-schema DB. The schema-aware
contract is: the tool must respect a configurable schema across ALL
its DDL, not just runtime queries.

## §2. Revised layout

## Why

M3 ships every managed service connecting to the same PostgreSQL
instance under the same admin user (`orchestack`). That is:

- Operationally cheap (one DB, one user, one place to back up).
- Operationally dangerous (any compromised service can read every
  other service's state, including `platform.users`).
- Confusing for operators who open pgAdmin and see four databases
  with no clear scope (the symptom that prompted this design).

By the end of M4 each managed service must:

1. Own its **own database** in the platform PostgreSQL.
2. Connect as its **own PostgreSQL role** with privileges scoped to
   that database only.
3. Be **created on first start** if absent — same idempotent pattern
   `_ensure_metabase_database()` already uses.

DB-level RBAC for OrcheStack roles (Admin / Engineer / Analyst seeing
different sets of databases via pgAdmin) is **deferred to M5** pending
a design call on the pgAdmin-roles question (see §3 below).

## §2. Revised layout (target)

| Database | Owner role | Used by | Why dedicated vs shared |
|---|---|---|---|
| `orchestack` | `orchestack` | orchestrator | Platform internals (platform.users, audit_log, sessions). Always isolated. |
| `${PIPELINE_DB_NAME}` (operator-named) | `${PIPELINE_DB_USER}` | dbt writes, Metabase reads | The operator's analytical data. Always isolated. |
| `metabase` | `metabase` | Metabase | **Dedicated DB** — Metabase 0.51 hardcodes `public.<table>` in init migration; cannot live in a shared `services` DB (see §1). |
| `services` | `orchestack` | shared by schema-aware tools | New: one DB, many schemas, each schema owned by its scoped role. M4 services that honor a configurable schema for ALL their DDL live here. |
| → `services.airflow` | `airflow` | Airflow | DAG metadata, run history. Schema-aware via `[core] sql_alchemy_schema` — verify M4.5. |
| → `services.openmetadata` | `openmetadata` | OpenMetadata | Catalogue + lineage. Verify schema-aware in M4.6. |
| → `services.airbyte` | `airbyte` | Airbyte | Sync state + connector configs. Verify schema-aware in M4.4. |
| `ge` | `ge` | Great Expectations | TBD — pure-filesystem state per existing GE config. May not need a DB at all. |
| `minio` | n/a — uses filesystem | MinIO | No DB. |

**Schema-aware verification before landing in `services`**: each M4
tool's compose snippet PR must include a test that creates a schema
in a shared DB, points the tool at it, and confirms a fresh init
populates the target schema (not `public` or anything else). Tools
that fail the test get a dedicated DB instead.

`dbt` does NOT get its own DB — dbt writes to the warehouse
(`${PIPELINE_DB_NAME}`) under the `dbt_user` role. dbt's own
"metadata" is in target/ on disk and in the run history Airflow keeps.

## 2. Pre-start hook contract per service

Each service's compose snippet ships alongside an
`_ensure_<service>_database()` pre-start hook in
`docker_ops.PRE_START_HOOKS`. Each hook:

```python
async def _ensure_<service>_database() -> None:
    pool = db.get_pool()
    async with pool.acquire() as conn:
        # 1. Create role if missing. Password from .env.
        await conn.execute(...
        # 2. Create database if missing, owned by that role.
        await conn.execute(...
        # 3. GRANT explicit table-level privileges if the service
        #    has a known schema it lives in (Metabase = public,
        #    Airflow = airflow, etc).
        await conn.execute(...
```

Implementation already exists for `metabase` (creates DB, no separate
role — Metabase reuses the orchestrack admin user today). M4 work is:

- **M4.1 MinIO** — no DB hook needed.
- **M4.2 dbt** — add `dbt` role in the warehouse DB, GRANT scoped
  permissions on `raw` (read) + `marts` (write).
- **M4.3 Great Expectations** — add `ge` DB + role.
- **M4.4 Airbyte** — add `airbyte` DB + role.
- **M4.5 Airflow** — add `airflow` DB + role. Airflow uses a single
  DB but expects to migrate its own schema with Alembic.
- **M4.6 OpenMetadata** — add `openmetadata` DB + role.

Each service also gets its own pre-loaded pgAdmin server entry pointing
at its own DB.

## 3. M5 work — DB-level RBAC for OrcheStack roles

Open question: **pgAdmin shows whichever connections live in
`servers.json` to whichever OrcheStack user happens to be opening it.**
pgAdmin doesn't have a notion of "this OrcheStack user is an Analyst,
hide the orchestrack DB from them."

Four approaches, all imperfect, picking ONE is M5 design work:

| Approach | Pros | Cons |
|---|---|---|
| **(a)** Stop+start pgAdmin per OrcheStack user with a freshly-generated `servers.json` | Honors RBAC perfectly | ~10s restart per Open click; breaks any in-progress session |
| **(b)** Run one pgAdmin instance per OrcheStack role | Honors RBAC perfectly | ~3x memory; routing complexity |
| **(c)** Generate `servers.json` to reflect "highest-role of any currently-open OrcheStack session" | No restart cost | Information-leak between concurrent sessions |
| **(d)** Use pgAdmin's own multi-user "server mode" + mirror OrcheStack roles into pgAdmin's user table | Honors RBAC, no restart cost | Two parallel user lists to maintain; pgAdmin auth becomes a duplicate of our own |

Recommendation: **(d)**, with a one-way sync from OrcheStack
platform.users → pgAdmin's `users.json` on the orchestack_config volume.
Decision point on this for M5.

## 4. Migration path for existing installs

M3 testers have a single `orchestack` DB with the platform schema and
share that database for Metabase's state. On the first start under M4:

1. `metabase` DB gets created (already happens — hook exists).
2. The orchestrator runs a one-time migration script that COPIES
   Metabase's tables from `orchestack` (where they actually never were
   — Metabase auto-creates in its own DB on first start anyway) to
   `metabase`. NO-OP for existing testers because Metabase always had
   its own DB; this branch is just a sanity check.
3. Other M4 services come up empty on first start (no prior data).

**No destructive migration needed for the M3 → M4 jump.**

## 5. .env keys (additions for M4)

```
# Service-specific DB users — created on first start of each service.
AIRBYTE_DB_USER=airbyte
AIRBYTE_DB_PASSWORD=<generated>
AIRFLOW_DB_USER=airflow
AIRFLOW_DB_PASSWORD=<generated>
OPENMETADATA_DB_USER=openmetadata
OPENMETADATA_DB_PASSWORD=<generated>
GE_DB_USER=ge
GE_DB_PASSWORD=<generated>
DBT_USER=<already exists>
DBT_PASSWORD=<already exists>
```

Generated values use `secrets.token_urlsafe(32)` — operator never sees
them, never types them. The orchestrator manages them end-to-end.
