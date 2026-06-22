"""01_airbyte_dbt_metabase — MAINTAINER VERIFICATION DAG.

Exercises composition pattern A from docs/first-pipeline.html:
    HttpOperator (trigger Airbyte) → HttpSensor (wait) →
    DbtDag (Cosmos per-model + tests) → HttpOperator (refresh Metabase)

NOT shipped to operators. The operator-facing equivalent is the
pattern documented at:
    https://orchestack.africa/first-pipeline.html#path-a

Maintainer setup before running:
    1. Configure an Airbyte connection in the Airbyte UI; capture the
       connection ID.
    2. In the Airflow UI, add:
         - HTTP connection "airbyte"  → http://orchestack-airbyte:8000
         - HTTP connection "metabase" → http://orchestack-metabase:3000
         - Variable "airbyte_connection_id"  = <captured UUID>
         - Variable "metabase_dashboard_id" = <ID of a dashboard to refresh>
    3. Trigger the DAG manually from the Airflow UI.

What to verify:
    - HttpOperator successfully POSTs to Airbyte's API
    - HttpSensor polls /jobs/<id> until status == "succeeded"
    - Cosmos generates one Airflow task per dbt model + per dbt test
      (the orchestack_demo project contains 4 models + several tests)
    - Per-model failure attribution: introduce a bad SQL into
      fct_demo_orders.sql and trigger; the failure attaches to that
      specific task, not the whole dbt_models group
    - Re-run-from-failed: Clear Task and Downstream on the failed
      model only re-executes that model + downstream
    - Metabase refresh task succeeds at the end
"""

from datetime import datetime
from pathlib import Path

from airflow import DAG
from airflow.providers.http.operators.http import HttpOperator
from airflow.providers.http.sensors.http import HttpSensor

from cosmos import DbtDag, ExecutionConfig, ProfileConfig, ProjectConfig
from cosmos.profiles import PostgresUserPasswordProfileMapping

DBT_PROJECT_PATH = Path("/opt/airflow/dbt-project")

profile_config = ProfileConfig(
    profile_name="orchestack_warehouse",
    target_name="prod",
    profile_mapping=PostgresUserPasswordProfileMapping(
        conn_id="orchestack_warehouse",
        profile_args={"schema": "public"},
    ),
)

with DAG(
    dag_id="verify_airbyte_dbt_metabase",
    description="Maintainer verification: HttpOperator → Cosmos → HttpOperator",
    start_date=datetime(2026, 1, 1),
    schedule=None,           # manual-trigger only; not on a cadence
    catchup=False,
    tags=["maintainer-verification", "pattern-a"],
) as dag:

    trigger_airbyte = HttpOperator(
        task_id="trigger_airbyte_sync",
        http_conn_id="airbyte",
        endpoint="api/v1/connections/sync",
        method="POST",
        data='{"connectionId": "{{ var.value.airbyte_connection_id }}"}',
        headers={"Content-Type": "application/json"},
    )

    wait_for_airbyte = HttpSensor(
        task_id="wait_for_airbyte",
        http_conn_id="airbyte",
        endpoint="api/v1/jobs/{{ ti.xcom_pull(task_ids='trigger_airbyte_sync')['job']['id'] }}",
        response_check=lambda r: r.json().get("job", {}).get("status") == "succeeded",
        poke_interval=30,
        timeout=60 * 60,
    )

    dbt_models = DbtDag(
        dag_id_prefix="dbt",
        project_config=ProjectConfig(DBT_PROJECT_PATH),
        profile_config=profile_config,
        execution_config=ExecutionConfig(dbt_executable_path="/home/airflow/.local/bin/dbt"),
    )

    refresh_metabase = HttpOperator(
        task_id="refresh_metabase",
        http_conn_id="metabase",
        endpoint="api/dashboard/{{ var.value.metabase_dashboard_id }}/refresh",
        method="POST",
    )

    trigger_airbyte >> wait_for_airbyte >> dbt_models >> refresh_metabase
