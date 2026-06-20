# system/dags/ — Airflow DAGs starter template

**This folder is NOT where the customer's DAGs live.** The customer's
actual DAGs live in their own Git repository. They point OrcheStack at
it via the setup wizard's `AIRFLOW_DAGS_REPO_URL` field, and OrcheStack
clones that repo into the Airflow container at runtime.

This folder will eventually contain a **starter DAG set** that OrcheStack
ships pre-installed for new operators who don't have an Airflow repo yet.
Currently empty.

---

## What lands here

A small set of starter DAGs an operator can fork:

```
system/dags/
├── README.md                    "How to fork these for your project"
├── 00_airbyte_to_dbt.py         Daily: trigger Airbyte → wait → run dbt build
├── 01_dbt_test.py               Hourly: dbt test against marts
├── 02_data_freshness.py         Daily: alert if any raw table is stale
└── lib/
    ├── airbyte.py               Helper for triggering Airbyte syncs via API
    ├── notifications.py         Slack/email helpers
    └── __init__.py
```

These cover the 80% case operators hit on day one: "I want a daily sync +
transform + test pipeline." Customers fork and extend; OrcheStack stops
shipping them once the customer's own DAGs are in place.

---

## What a typical CUSTOMER Airflow repo looks like

This is what the operator's *own* repo should contain — not what
OrcheStack ships:

```
acme-airflow-dags/                   Their Git repo
├── dags/                            ← AIRFLOW_DAGS_REPO_PATH default
│   ├── daily_metabase_refresh.py
│   ├── weekly_finance_export.py
│   ├── ad_hoc_backfill.py
│   └── _utils/
│       ├── slack.py
│       └── airbyte_client.py
├── tests/                           pytest unit tests for DAG helpers
├── requirements.txt                 extra Python deps (if any)
├── .gitignore
└── README.md
```

OrcheStack's wizard takes the repo URL + branch + subdirectory
(default `dags/`), and the Airflow container's git-sync sidecar
keeps it in sync.

---

## How OrcheStack uses this folder at runtime

When the Airflow container starts, it follows this resolution:

1. Read `AIRFLOW_DAGS_REPO_URL` from the operator's `.env`
2. If empty → mount this folder into the container at `/opt/airflow/dags/`
   (operator gets the starter DAGs out of the box)
3. If set → `git clone <url> --branch <AIRFLOW_DAGS_REPO_BRANCH>` and
   mount `<clone>/<AIRFLOW_DAGS_REPO_PATH>` at `/opt/airflow/dags/`
4. A git-sync sidecar refreshes the clone every ~30 seconds so DAG
   changes pushed to the repo appear in Airflow without a restart

The split between OrcheStack-shipped starter DAGs and customer DAGs is
explicit: the wizard's repo field is optional precisely so a new operator
can see things working before they need to set up their own Git repo.
