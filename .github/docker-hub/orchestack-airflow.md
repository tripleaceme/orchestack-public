# orchestack-airflow

Apache Airflow 2.10 customised for [**OrcheStack**](https://orchestack.africa) —
an open-source containerised data platform that integrates Airbyte, Apache
Airflow, dbt Core, Great Expectations, Metabase, MinIO, OpenMetadata, and
pgAdmin behind a single operator-facing interface.

This image extends the upstream `apache/airflow` image with the tooling an
OrcheStack operator needs to run dbt from Airflow with per-model task
granularity, without any runtime pip install.

## What this image adds to upstream Airflow

| Package | Purpose |
|---|---|
| `dbt-core` (1.8.0) | The dbt CLI for running transformations |
| `dbt-postgres` (1.8.0) | dbt adapter for the OrcheStack PostgreSQL warehouse |
| `astronomer-cosmos` (1.8.x) | Generates one Airflow task per dbt model + per dbt test, with per-model failure attribution and re-run-from-failed support |

Everything is baked in at build time so cold-start time is sub-15s on
subsequent boots. The first start is slower because Airflow's metadata
migrations run.

## Base image

`apache/airflow:2.10.5-python3.12`

Apache 2.0 license inherited from upstream.

## What this image does NOT add

Custom DAGs, custom dbt projects, ingestion libraries (dlt, requests,
pandas, etc.). The platform is intentionally unopinionated about
those choices:

- **DAGs** — operators bring their own from a Git repository via
  `AIRFLOW_DAGS_REPO_URL`. The
  [first-pipeline guide](https://orchestack.africa/first-pipeline.html)
  documents three composition patterns operators can copy + adapt.
- **dbt project** — operators bring their own from a Git repository via
  `DBT_REPO_URL`. Mounted into both the dbt service container and this
  Airflow container so Cosmos can parse the manifest at DAG-parse time.
- **Ingestion libraries** — install per-DAG via Airflow's
  `PythonVirtualenvOperator` (works for dlt, requests, pandas, anything
  on PyPI; isolated per task; cached after first run).

## Connections

The orchestrator's post-start hook creates the
`orchestack_warehouse` Airflow Connection on first start using the
warehouse credentials from `.env`. Cosmos's
`PostgresUserPasswordProfileMapping` reads it directly — operators
don't need to learn about Airflow Connections to run dbt.

Operators add other connections (Airbyte, Metabase, Fivetran, Tableau,
etc.) through the Airflow UI per their stack.

## How this image is used

This is part of OrcheStack and is not designed to run standalone. It
runs alongside the rest of the platform via the `docker-compose.yml`
shipped in the OrcheStack runtime bundle.

To deploy OrcheStack:

```sh
curl -fsSL https://orchestack.africa/install.sh | bash
```

Or download the [latest runtime bundle](https://github.com/tripleaceme/orchestack-public/releases/latest)
and follow its `INSTALL.md`. After setup, Airflow is reachable at
`http://your-host/app/airflow/`.

## Related images

| Image | Purpose |
|---|---|
| [`tripleaceme/orchestack-auth`](https://hub.docker.com/r/tripleaceme/orchestack-auth) | Signup, login, setup wizard |
| [`tripleaceme/orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator) | Service lifecycle daemon |
| [`tripleaceme/orchestack-dashboard`](https://hub.docker.com/r/tripleaceme/orchestack-dashboard) | Administrator dashboard |
| [`tripleaceme/orchestack-ge`](https://hub.docker.com/r/tripleaceme/orchestack-ge) | Great Expectations preinstalled |

## Upstream

This image extends and respects all licences of upstream Apache Airflow.
See <https://airflow.apache.org/> for the original project, and
<https://github.com/astronomer/astronomer-cosmos> for Cosmos.

## Project links

- **Website** — <https://orchestack.africa>
- **Operator docs** — <https://orchestack.africa/services/airflow.html>
- **First pipeline guide** — <https://orchestack.africa/first-pipeline.html>
- **Source code** — <https://github.com/tripleaceme/orchestack-public>
- **Releases** — <https://github.com/tripleaceme/orchestack-public/releases>
- **License** — Apache 2.0
