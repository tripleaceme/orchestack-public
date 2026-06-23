# orchestack-auth

The pre-login surface for [**OrcheStack**](https://orchestack.africa) — an
open-source containerised data platform that integrates Airbyte, Apache
Airflow, dbt Core, Great Expectations, Metabase, MinIO, OpenMetadata, and
pgAdmin behind a single operator-facing interface.

This image serves the screens operators reach **before** they have a session:

- `/signup` — first-administrator bootstrap form
- `/login` — administrator login
- `/setup/*` — four-step onboarding wizard (welcome → service selection → configuration → deploy)
- `/assets/*` — shared CSS and brand logos

It's a small nginx + static-HTML container. No Python, no JavaScript build
step. Brand consistency is enforced by baking the HTML into the image at
build time — operators cannot silently re-brand the onboarding flow without
forking and rebuilding.

## How this image is used

This is part of OrcheStack's control plane and is not designed to run
standalone. It runs alongside the rest of the platform via the
`docker-compose.yml` shipped in the OrcheStack runtime bundle.

To deploy OrcheStack:

```sh
curl -sSL https://orchestack.africa/install.sh | bash
```

Or download the [latest runtime bundle](https://github.com/tripleaceme/orchestack-public/releases/latest)
and follow its `INSTALL.md`.

## Related images

| Image | Purpose |
|---|---|
| [`tripleaceme/orchestack-orchestrator`](https://hub.docker.com/r/tripleaceme/orchestack-orchestrator) | Service lifecycle daemon |
| [`tripleaceme/orchestack-dashboard`](https://hub.docker.com/r/tripleaceme/orchestack-dashboard) | Administrator dashboard |
| [`tripleaceme/orchestack-airflow`](https://hub.docker.com/r/tripleaceme/orchestack-airflow) | Airflow 3 with dbt + Cosmos preinstalled |
| [`tripleaceme/orchestack-ge`](https://hub.docker.com/r/tripleaceme/orchestack-ge) | Great Expectations preinstalled |

## Project links

- **Website** — <https://orchestack.africa>
- **Operator docs** — <https://orchestack.africa/install.html>
- **Source code** — <https://github.com/tripleaceme/orchestack-public>
- **Releases** — <https://github.com/tripleaceme/orchestack-public/releases>
- **License** — Apache 2.0
