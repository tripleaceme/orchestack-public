"""OrcheStack welcome DAG.

Baked into the orchestack-airflow image so the Airflow UI shows at
least one working DAG on first start — useful when the operator
hasn't yet wired their DAGs repo, OR when their repo clone failed
silently and they'd otherwise see an empty UI and not know why.

The entrypoint copies this file into /opt/airflow/dags/ on each start
if it isn't already there. Operators can delete it once they've
connected their own DAGs repo (or leave it as a sanity check).
"""
from datetime import datetime

from airflow import DAG
from airflow.operators.bash import BashOperator

with DAG(
    dag_id="orchestack_welcome",
    description="Demo DAG provided by OrcheStack; safe to delete once your own DAGs are loaded.",
    schedule=None,
    start_date=datetime(2026, 1, 1),
    catchup=False,
    tags=["orchestack", "demo"],
) as dag:
    BashOperator(
        task_id="hello",
        bash_command="echo 'hello from OrcheStack' && date",
    )
