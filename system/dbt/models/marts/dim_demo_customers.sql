-- Marts model: customer dimension.

select
    customer_id,
    customer_name,
    signup_at
from {{ ref('stg_demo_customers') }}
