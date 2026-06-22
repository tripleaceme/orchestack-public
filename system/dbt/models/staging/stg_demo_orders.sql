-- Staging model: raw demo_orders → cleaned columns.
--
-- Purpose for maintainer verification: one of several staging models
-- so Cosmos generates multiple staging-tier tasks in the Airflow DAG.

select
    order_id,
    customer_id,
    order_total::numeric(12, 2) as order_total,
    ordered_at::timestamp        as ordered_at
from {{ source('demo_raw', 'demo_orders') }}
