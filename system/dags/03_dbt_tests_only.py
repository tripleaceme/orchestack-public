"""03_dbt_tests_only — MAINTAINER VERIFICATION DAG.

Exercises Cosmos's ability to run tests on their own schedule,
independent of model runs.

NOT shipped to operators. Documented as a "common variation" of the
canonical first-pipeline patterns:
    https://orchestack.africa/services/dbt.html#running-from-airflow

Use case: operator wants nightly model runs but hourly test-only
runs (so anomalies surface faster than the next full pipeline).

What this DAG verifies:
    - Cosmos's render_config select filter works
    - The test-only DAG produces test tasks but no model run tasks
    - Tests fail loudly when the underlying data violates expectations
      (introduce a row with order_total = 0 to fct_demo_orders and
       confirm the assert_demo_orders_total_positive task fails)
"""

from datetime import datetime
from pathlib import Path

from airflow import DAG
from cosmos import DbtDag, ExecutionConfig, ProfileConfig, ProjectConfig, RenderConfig
from cosmos.profiles import PostgresUserPasswordProfileMapping

profile_config = ProfileConfig(
    profile_name="orchestack_warehouse",
    target_name="prod",
    profile_mapping=PostgresUserPasswordProfileMapping(
        conn_id="orchestack_warehouse",
        profile_args={"schema": "public"},
    ),
)

# Cosmos lets us scope the generated DAG to a subset of resources via
# select. The "test" resource type produces only test tasks, leaving
# model-build tasks out of this DAG. Operators with the same need
# write the same shape against their own project.
test_only_dag = DbtDag(
    dag_id="verify_dbt_tests_only",
    description="Maintainer verification: dbt tests on their own schedule (no model builds)",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["maintainer-verification", "tests-only"],
    project_config=ProjectConfig(Path("/opt/airflow/dbt-project")),
    profile_config=profile_config,
    execution_config=ExecutionConfig(dbt_executable_path="/home/airflow/.local/bin/dbt"),
    render_config=RenderConfig(
        # The "test" resource type tells Cosmos to render only test
        # tasks. Documented at:
        # https://astronomer.github.io/astronomer-cosmos/configuration/selecting-excluding.html
        select=["resource_type:test"],
    ),
)
