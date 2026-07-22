# orchestack-ge

Great Expectations preinstalled for [**OrcheStack**](https://orchestack.africa) —
an open-source containerised data platform that integrates Airbyte, Apache
Airflow, dbt Core, Great Expectations, Metabase, MinIO, OpenMetadata, and
pgAdmin behind a single operator-facing interface.

This image bundles Great Expectations with the editors and terminal
tooling needed for an in-browser data-quality workflow. Pre-installing
everything at build time cuts the fresh-start time from ~30-60 minutes
(apt + pip resolution on slow networks) to ~10 seconds (just container
creation).

## What this image adds

| Package / tool | Purpose |
|---|---|
| `great-expectations==0.18.21` | The Great Expectations CLI + Python library |
| `psycopg2-binary>=2.9` | Postgres driver — connects GE to the OrcheStack warehouse |
| `sqlalchemy>=1.4,<2` | Database access layer (pinned to <2 because GE 0.18 uses the v1 SQLAlchemy API) |
| `ttyd` (1.7.7) | Static-binary web terminal — operators reach GE through a browser shell, no SSH |
| `nano`, `vim-tiny`, `less` | Editors for editing expectations and config files via the terminal |
| `git`, `curl`, `ca-certificates`, `libpq5` | Standard support packages |

Great Expectations and the postgres driver are installed into a
venv at `/opt/ge-venv`. The runtime entrypoint exports `PATH` so
`great_expectations` is on the default PATH without venv activation.

## Base image

`python:3.12-slim` — small, Debian-derived, no busybox surprises.

## Architecture note

`ttyd` is downloaded as a static `ttyd.x86_64` binary, so this image
is **linux/amd64 only**. Apple Silicon operators run the image
through Docker Desktop's amd64 emulation; native arm64 support is
deferred until upstream `ttyd` ships an official arm64 release.

## How this image is used

This is part of OrcheStack and is not designed to run standalone.
The OrcheStack orchestrator brings the container up cold-tier — it
starts when an operator opens GE through the dashboard and stops when
the GE session has been idle past its threshold.

Inside the container, operators reach the Great Expectations CLI via
the in-browser terminal at `/app/great-expectations-terminal` (routed
through the OrcheStack reverse proxy with the platform's auth chain).
Typical operator commands:

```sh
great_expectations init                          # bootstrap a GE project
great_expectations datasource new                # add the warehouse
great_expectations suite new                     # define expectations
great_expectations checkpoint new daily_checks   # automate them
```

## How to deploy OrcheStack

```sh
curl -fsSL https://orchestack.africa/install.sh | bash
```

Or download the [latest runtime bundle](https://github.com/tripleaceme/orchestack-public/releases/latest)
and follow its `INSTALL.md`.

## Related images

| Image | Purpose |
|---|---|
| [`tripleaceme/orchestack-auth`](https://hub.docker.com/r/tripleaceme/orchestack-auth) | Signup, login, setup wizard |
| [`tripleaceme/orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator) | Service lifecycle daemon |
| [`tripleaceme/orchestack-dashboard`](https://hub.docker.com/r/tripleaceme/orchestack-dashboard) | Administrator dashboard |
| [`tripleaceme/orchestack-airflow`](https://hub.docker.com/r/tripleaceme/orchestack-airflow) | Airflow 2.10 with dbt + Cosmos preinstalled |

## Upstream

This image extends and respects all licences of upstream Great
Expectations (Apache 2.0) and `ttyd` (MIT). See
<https://greatexpectations.io/> and <https://github.com/tsl0922/ttyd>
for the original projects.

## Project links

- **Website** — <https://orchestack.africa>
- **Operator docs** — <https://orchestack.africa/services/great-expectations.html>
- **Source code** — <https://github.com/tripleaceme/orchestack-public>
- **Releases** — <https://github.com/tripleaceme/orchestack-public/releases>
- **License** — Apache 2.0
