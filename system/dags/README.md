# system/dags/

This folder holds Airflow DAG examples that exist for **maintainer
verification only** — they let us exercise the Airflow + dbt + Cosmos
integration end-to-end without depending on operator-supplied content.

**OrcheStack does not ship any of these DAGs to operators.** The
operator runtime bundle (`orchestack-runtime.tar.gz`) does not include
this directory. Operators receive an empty `/opt/airflow/dags/` folder
and populate it from their own Git repository — see
[`AIRFLOW_DAGS_REPO_URL`](https://orchestack.africa/services/airflow.html)
for the canonical pattern.

If you're an operator looking for *example* DAGs to copy-paste, look at
[**Compose your first pipeline**](https://orchestack.africa/first-pipeline.html)
on the docs site. Three concrete composition patterns (Airbyte→dbt→Metabase,
Python→dbt→Tableau, dlt→dbt→warehouse-only) are documented there with
complete, copy-paste-ready DAG snippets.

## What's in this folder (maintainer artefacts)

Each file demonstrates a specific composition pattern at the level needed
to verify the integration works:

| File | Verifies |
| --- | --- |
| `01_airbyte_dbt_metabase.py` | HttpOperator triggering Airbyte + Cosmos-driven dbt + Metabase refresh end-to-end |
| `02_python_dbt.py` | PythonVirtualenvOperator with `requests` + dbt+Cosmos — proves arbitrary Python deps install correctly |
| `03_dbt_tests_only.py` | A schedule that runs `dbt test` independently of model runs |

These files are tracked in this repo so contributors can see how the
patterns are composed, and so we can verify end-to-end behaviour
during release preparation. The `release.yml` workflow's bundle
assembly does **not** copy `system/dags/` into the runtime tarball.

## How an operator actually gets DAGs into their Airflow

Two paths, both documented at <https://orchestack.africa/services/airflow.html#dags-live-where>:

1. **`AIRFLOW_DAGS_REPO_URL` in `.env`** (or via the dashboard's Edit
   Config). On every Airflow start, the entrypoint clones the repo
   (or `git fetch && git reset --hard` on subsequent starts) into the
   container's `/opt/airflow/dags/` volume.
2. **Bind mount a host directory** at the same path for single-developer
   iteration. Edit a `.py` file locally; Airflow discovers it within
   ~30 seconds.

The operator's DAGs live in their own Git repo for the same reasons
their dbt project does — review history, blame, branching, the
operator's normal Git workflow. OrcheStack does not edit operator DAGs,
does not ship "starter" DAGs that bake in our assumptions about their
stack, and does not maintain DAG content on their behalf.
