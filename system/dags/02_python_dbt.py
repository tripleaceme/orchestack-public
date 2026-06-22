"""02_python_dbt — MAINTAINER VERIFICATION DAG.

Exercises composition pattern B/C from docs/first-pipeline.html:
    PythonVirtualenvOperator (custom Python ingest) →
    DbtDag (Cosmos per-model)

NOT shipped to operators. The operator-facing equivalents are the
patterns documented at:
    https://orchestack.africa/first-pipeline.html#path-b   (Python ingest)
    https://orchestack.africa/first-pipeline.html#path-c   (dlt-based ingest)

What this DAG verifies:
    - PythonVirtualenvOperator successfully creates an isolated venv
      with the listed `requirements=[...]` packages
    - First task run installs deps (~30-60s for requests + psycopg2)
    - Subsequent task runs reuse the cached venv (sub-second)
    - The ingested rows land in raw.api_data and are visible to dbt
    - DbtDag picks up the new data on the next run

Maintainer setup before running:
    1. Ensure the `raw` schema exists in the warehouse:
         psql -h orchestack-postgres -U warehouse_admin -d data_warehouse \
              -c "CREATE SCHEMA IF NOT EXISTS raw"
    2. In the Airflow UI, add Variable "warehouse_db_password" with
       the value from .env's WAREHOUSE_DB_PASSWORD.
    3. Trigger manually.

(For the dlt variant, replace ingest_to_warehouse below with the dlt
loader shown in docs/first-pipeline.html#path-c. The DAG shape is
identical; only the venv requirements + the loader function change.)
"""

from datetime import datetime
from pathlib import Path

from airflow import DAG
from airflow.operators.python import PythonVirtualenvOperator

from cosmos import DbtDag, ExecutionConfig, ProfileConfig, ProjectConfig
from cosmos.profiles import PostgresUserPasswordProfileMapping


def ingest_to_warehouse() -> None:
    """Runs inside an ephemeral venv with requests + psycopg2-binary.

    Pulls some synthetic rows from a public test API and writes them
    to raw.api_data. The actual API choice doesn't matter — we're
    verifying the venv mechanism, not the data quality.
    """
    import os

    import psycopg2
    import requests

    response = requests.get("https://jsonplaceholder.typicode.com/posts", timeout=30)
    response.raise_for_status()
    rows = response.json()

    conn = psycopg2.connect(
        host="orchestack-postgres",
        port=5432,
        dbname=os.environ.get("WAREHOUSE_DB_NAME", "data_warehouse"),
        user=os.environ.get("WAREHOUSE_DB_USER", "warehouse_admin"),
        password=os.environ["WAREHOUSE_DB_PASSWORD"],
    )
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS raw.api_data (
                    id INT PRIMARY KEY,
                    user_id INT,
                    title TEXT,
                    body TEXT,
                    ingested_at TIMESTAMP DEFAULT now()
                )
            """)
            for row in rows:
                cur.execute("""
                    INSERT INTO raw.api_data (id, user_id, title, body)
                    VALUES (%(id)s, %(userId)s, %(title)s, %(body)s)
                    ON CONFLICT (id) DO UPDATE SET
                        user_id = EXCLUDED.user_id,
                        title   = EXCLUDED.title,
                        body    = EXCLUDED.body
                """, row)
        conn.commit()
        print(f"Wrote {len(rows)} rows to raw.api_data")
    finally:
        conn.close()


profile_config = ProfileConfig(
    profile_name="orchestack_warehouse",
    target_name="prod",
    profile_mapping=PostgresUserPasswordProfileMapping(
        conn_id="orchestack_warehouse",
        profile_args={"schema": "public"},
    ),
)

with DAG(
    dag_id="verify_python_dbt",
    description="Maintainer verification: PythonVirtualenvOperator → Cosmos",
    start_date=datetime(2026, 1, 1),
    schedule=None,
    catchup=False,
    tags=["maintainer-verification", "pattern-b"],
) as dag:

    ingest = PythonVirtualenvOperator(
        task_id="python_ingest",
        python_callable=ingest_to_warehouse,
        requirements=["requests>=2.31", "psycopg2-binary>=2.9"],
        system_site_packages=False,
        env_vars={
            "WAREHOUSE_DB_PASSWORD": "{{ var.value.warehouse_db_password }}",
        },
    )

    dbt_models = DbtDag(
        dag_id_prefix="dbt",
        project_config=ProjectConfig(Path("/opt/airflow/dbt-project")),
        profile_config=profile_config,
        execution_config=ExecutionConfig(dbt_executable_path="/home/airflow/.local/bin/dbt"),
    )

    ingest >> dbt_models
