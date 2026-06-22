# system/dags/

This folder is **intentionally empty in the source tree**. OrcheStack
does not ship any DAGs to operators — they bring their own from their
own Git repository.

Operators populate this folder at runtime by setting
[`AIRFLOW_DAGS_REPO_URL`](https://orchestack.africa/services/airflow.html#dags-live-where)
in `.env`. The Airflow service container's entrypoint clones (or
`git fetch && git reset --hard`) the operator's repo into the
`orchestack-airflow-dags` docker volume on every start.

For operator-facing DAG examples, see [**Compose your first
pipeline**](https://orchestack.africa/first-pipeline.html) — three
concrete composition patterns documented with complete copy-paste-
ready snippets:

| Path | Ingest | Transform | BI |
| --- | --- | --- | --- |
| A | Airbyte | dbt + Cosmos | Metabase |
| B | Python (custom loader) | dbt + Cosmos | Tableau (external) |
| C | dlt | dbt + Cosmos | None — engineers query the warehouse |

The platform makes exactly one prescriptive choice: dbt-from-Airflow
runs through `astronomer-cosmos` (baked into the `orchestack-airflow`
image so it works without any runtime pip install). Every other
choice — which ingestion library, which BI tool, which schedule,
whether to have BI at all — is the operator's.
