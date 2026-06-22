-- Staging model: raw demo_customers → cleaned columns.

select
    customer_id,
    customer_name,
    signup_at::timestamp as signup_at
from {{ source('demo_raw', 'demo_customers') }}
