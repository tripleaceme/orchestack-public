-- Marts model: enriched order fact with customer name.

with orders as (
    select * from {{ ref('stg_demo_orders') }}
),

customers as (
    select * from {{ ref('stg_demo_customers') }}
)

select
    o.order_id,
    o.customer_id,
    c.customer_name,
    o.order_total,
    o.ordered_at
from orders o
left join customers c using (customer_id)
