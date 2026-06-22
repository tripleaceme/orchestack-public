# system/dbt/

This folder holds a small **demo dbt project** that exists for
**maintainer verification only** — it lets us exercise Cosmos's
per-model task generation against a real project structure without
depending on operator-supplied content.

**OrcheStack does not ship this demo project to operators.** The
operator runtime bundle (`orchestack-runtime.tar.gz`) does not include
this directory. Operators populate `/usr/app/dbt` inside the dbt
service container from their own Git repository — see
[`DBT_REPO_URL`](https://orchestack.africa/services/dbt.html#populating-your-project)
for the canonical pattern.

If you're an operator looking for project-layout guidance, the
[**dbt Core**](https://orchestack.africa/services/dbt.html#project-layout)
docs page shows the expected directory shape.

## What's in this folder (maintainer artefacts)

A minimal but realistic dbt project:

```
system/dbt/
├── dbt_project.yml         # name: orchestack_demo, profile: orchestack_warehouse
├── packages.yml            # dbt_utils
├── models/
│   ├── staging/
│   │   ├── _sources.yml    # declares raw.demo_orders, raw.demo_customers
│   │   ├── stg_demo_orders.sql
│   │   └── stg_demo_customers.sql
│   └── marts/
│       ├── _models.yml     # tests + docs
│       ├── fct_demo_orders.sql
│       └── dim_demo_customers.sql
└── tests/
    └── assert_demo_orders_total_positive.sql
```

This is enough surface to exercise:

- Cosmos's per-model task generation (5 models + tests)
- Per-model failure attribution (introduce a bad SQL → specific task fails)
- Re-run only failed model (Clear Task and Downstream)
- dbt tests as separate Airflow tasks

## Why this is not in the runtime bundle

Two reasons:

1. **Prescription**: shipping a demo dataset implies operators should
   start from it. They shouldn't — they have their own data. The dbt
   service container writes a minimal demo project on first start *if*
   the operator has not pointed `DBT_REPO_URL` at their own repo, but
   that minimal project is generated at runtime (in the dbt service's
   compose snippet), not bundled.
2. **Bundle size**: keeping the bundle to ~35 KB makes the install
   experience clean. Demo project SQL would bloat that with content
   no operator actually wants in production.

The maintainer verification flow uses this folder by copying it into
the `orchestack-dbt-repo` volume before running the verification DAGs
in `system/dags/`.
