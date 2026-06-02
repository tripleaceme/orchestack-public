# system/dbt/ — dbt starter template (M4)

**This folder is NOT where the customer's dbt code lives.** The customer's
actual dbt project lives in their own Git repository. They point OrcheStack
at it via the setup wizard's `DBT_REPO_URL` field, and OrcheStack clones
that repo into the dbt container at runtime.

This folder will eventually contain a **starter dbt project skeleton** that
OrcheStack can scaffold for new operators who don't have an existing dbt
project. M4 work; currently empty.

---

## What lands here at M4

A minimal but production-shaped dbt project an operator can fork:

```
system/dbt/
├── dbt_project.yml         Project config + paths + targets
├── profiles.yml.template    Templated profile pointing at the pipeline DB
├── packages.yml             dbt_utils + 1-2 helpful packages
├── models/
│   ├── staging/
│   │   ├── _sources.yml     Reads from raw schema (Airbyte landing tables)
│   │   ├── stg_orders.sql   Example staging model
│   │   └── ...
│   ├── marts/
│   │   ├── _models.yml      Documentation + tests
│   │   ├── fct_orders.sql   Example fact table
│   │   └── dim_customers.sql
│   └── ...
├── tests/                   Custom singular tests
├── macros/                  Custom Jinja macros
└── README.md                "How to fork this for your project" guide
```

The orchestrator's wizard handoff (M2.5) writes a copy of this skeleton
to the customer's `./config/dbt-starter/` directory if they don't supply
a DBT_REPO_URL. They can then `git init` it and push to their own repo,
returning to the dashboard to paste the URL in.

---

## What a typical CUSTOMER dbt project looks like

This is what the operator's *own* repo should contain — not what
OrcheStack ships:

```
acme-dbt-project/                    Their Git repo
├── dbt_project.yml                  name: 'acme_analytics'  ← matches DBT_PROJECT_NAME in wizard
│                                    profile: 'acme_analytics'
├── packages.yml                     dbt_utils, dbt_expectations, etc.
├── models/
│   ├── staging/
│   │   ├── stg_orders.sql
│   │   ├── stg_customers.sql
│   │   └── _sources.yml             { source: 'raw', schema: 'raw' }
│   ├── marts/
│   │   ├── fct_orders.sql
│   │   └── dim_customers.sql
│   └── README.md
├── tests/
├── macros/
├── .gitignore                       ignores target/, dbt_packages/, profiles.yml
└── README.md
```

**The customer never edits OrcheStack's `system/dbt/`.** They fork it (or
start from scratch), push to GitHub, and OrcheStack pulls from there.

---

## How OrcheStack uses this folder at runtime

When Airflow's `dbt run` task fires, the dbt container (M4) does:

1. Read `DBT_REPO_URL` from the operator's `.env`
2. If empty → use the starter project from this folder (mounted into the
   container at `/dbt/project/`)
3. If set → `git clone <url> --branch <DBT_REPO_BRANCH>` into `/dbt/project/`
4. Generate `profiles.yml` from the operator's PIPELINE_DB_* credentials
5. `dbt deps && dbt run --target $DBT_TARGET`

The starter project exists so operators can use OrcheStack on day one
without having to write a dbt project from scratch.

---

## Why this isn't M3 work

M3 is the dashboard. M4 is when dbt + Airflow actually run real jobs.
This folder gets populated when M4 lands — currently a placeholder so
future contributors see that "the folder exists for a reason."
