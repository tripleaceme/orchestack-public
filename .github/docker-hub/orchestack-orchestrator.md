# orchestack-orchestrator

The control-plane daemon for [**OrcheStack**](https://orchestack.africa) —
an open-source containerised data platform that integrates Airbyte, Apache
Airflow, dbt Core, Great Expectations, Metabase, MinIO, OpenMetadata, and
pgAdmin behind a single operator-facing interface.

This image owns the platform's lifecycle decisions and metadata layer.
It is never reached directly by an operator's browser — the
[`orchestack-dashboard`](https://hub.docker.com/r/tripleaceme/orchestack-dashboard)
proxies every privileged request through this container's private API.

## What this image does

| Responsibility | What it means in practice |
|---|---|
| **Service catalogue** | A central registry of manageable services (tier, layer, compose snippet path, hook bindings, connection URL templates) — single source of truth that the reconciler, the HTTP API, and the dashboard's service grid all read from |
| **Per-service lifecycle** | Shells out to the `docker compose` CLI to start, stop, and inspect each managed service in its own compose project (`orchestack-service-<name>`) |
| **Metadata layer** | Owns the platform schema in PostgreSQL — users, roles, role-permissions, sessions, service-sessions, service-pinning, append-only audit log |
| **Reconciler** | Async background task that wakes periodically, identifies cold-tier services with no active sessions past their idle threshold, and stops them through the same code path operator stops use |
| **Post-start hooks** | Idempotent bootstraps for the integrated services — Metabase first-run setup, OpenMetadata password reset + Elasticsearch single-node replica fix, Airbyte workspace email, Airflow `orchestack_warehouse` Connection |

## Why it shells out to the Docker CLI rather than the Python SDK

The CLI handles API-version negotiation with the Docker daemon, and
operators can reproduce any orchestrator action by running the exact
command from their own shell. Debuggability beats abstraction.

## How this image is used

This is part of OrcheStack's control plane and is not designed to run
standalone. It runs alongside the rest of the platform via the
`docker-compose.yml` shipped in the OrcheStack runtime bundle, with
`/var/run/docker.sock` mounted from the host so it can manage other
containers.

To deploy OrcheStack:

```sh
curl -sSL https://orchestack.africa/install.sh | bash
```

Or download the [latest runtime bundle](https://github.com/tripleaceme/orchestack-public/releases/latest)
and follow its `INSTALL.md`.

## Related images

| Image | Purpose |
|---|---|
| [`tripleaceme/orchestack-auth`](https://hub.docker.com/r/tripleaceme/orchestack-auth) | Signup, login, setup wizard |
| [`tripleaceme/orchestack-dashboard`](https://hub.docker.com/r/tripleaceme/orchestack-dashboard) | Administrator dashboard |
| [`tripleaceme/orchestack-airflow`](https://hub.docker.com/r/tripleaceme/orchestack-airflow) | Airflow 3 with dbt + Cosmos preinstalled |
| [`tripleaceme/orchestack-ge`](https://hub.docker.com/r/tripleaceme/orchestack-ge) | Great Expectations preinstalled |

## Project links

- **Website** — <https://orchestack.africa>
- **Operator docs** — <https://orchestack.africa/install.html>
- **Architecture** — <https://orchestack.africa/architecture.html>
- **Source code** — <https://github.com/tripleaceme/orchestack-public>
- **Releases** — <https://github.com/tripleaceme/orchestack-public/releases>
- **License** — Apache 2.0
