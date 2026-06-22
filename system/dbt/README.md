# system/dbt/

This folder is **intentionally empty in the source tree**. OrcheStack
does not ship a dbt project to operators — they bring their own from
their own Git repository.

Operators populate the operator's dbt project at runtime by setting
[`DBT_REPO_URL`](https://orchestack.africa/services/dbt.html#populating-your-project)
in `.env`. The dbt service container's entrypoint clones the
operator's repo into the `orchestack-dbt-repo` docker volume; the
Airflow service container then sees the same project at
`/opt/airflow/dbt-project` (read-only) so Cosmos can parse the
project's manifest at DAG-parse time.

For project-layout guidance, see [**dbt Core →
Project layout**](https://orchestack.africa/services/dbt.html#project-layout)
on the operator docs site.

If the operator does not configure `DBT_REPO_URL`, the dbt service
container writes a tiny demo dbt project on first start so the
dbt-docs server has something to render. That demo project lives in
the container's entrypoint script, not in this folder — see
[`../docker/services/dbt.yml`](../docker/services/dbt.yml).
