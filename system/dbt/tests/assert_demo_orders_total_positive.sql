-- Singular test: every order_total must be strictly positive.
--
-- Surfaces in Airflow as its own Cosmos-generated task downstream of
-- fct_demo_orders. Demonstrates "tests are first-class tasks", not
-- buried inside dbt run.

select order_id, order_total
from {{ ref('fct_demo_orders') }}
where order_total <= 0
