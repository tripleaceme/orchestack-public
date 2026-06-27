# OrcheStack Airflow image.
#
# Apache Airflow 2.10 with dbt-core, dbt-postgres, and astronomer-cosmos
# preinstalled so the operator's first DAG can drive dbt with per-model
# task granularity without any runtime pip-install.
#
# Why Airflow 2 (not 3): OrcheStack's target users are first-time Airflow
# operators. Every Airflow tutorial on Stack Overflow, Astronomer Academy,
# Marc Lamberti's videos, etc. shows the Airflow 2 UI — the 3.x React
# rewrite makes those resources misleading. Airflow 2.10 is actively
# patched and Cosmos 1.14 supports both 2.10+ and 3.x, so the operator
# gets a familiar UI without losing dbt-orchestration capability.
# We'll bump to 3.x when ecosystem docs catch up.
#
# Build context: this folder (system/docker/airflow-image/).
#
# Locally:
#   make image-airflow            # produces tripleaceme/orchestack-airflow:dev
#
# Tag bumping policy: bump alongside the rest of the OrcheStack release
# tags (the release.yml docker-publish matrix sets both :latest and the
# semver tag on every image at once).

FROM apache/airflow:2.10.5-python3.12

LABEL org.opencontainers.image.title="orchestack-airflow" \
      org.opencontainers.image.description="Apache Airflow 2.10 with dbt-core, dbt-postgres, and astronomer-cosmos preinstalled for OrcheStack" \
      org.opencontainers.image.vendor="OrcheStack" \
      org.opencontainers.image.source="https://github.com/tripleaceme/orchestack-public" \
      org.opencontainers.image.documentation="https://orchestack.africa" \
      org.opencontainers.image.licenses="Apache-2.0"

# ────────────────────────────────────────────────────────────────────────
# dbt + Cosmos pre-installation
# ────────────────────────────────────────────────────────────────────────
# Why bake in (vs runtime _PIP_ADDITIONAL_REQUIREMENTS):
#   - Cold start: 5-10s vs 60-90s (no per-start pip resolution)
#   - Reliability: build fails LOUDLY if a version pin breaks; runtime
#     install would fail SILENTLY mid-task
#   - Reproducibility: every operator pulling :0.1.0 gets the same
#     exact dbt + cosmos versions
#
# Version pins:
#   - dbt-core 1.8.x          : last dbt line whose deps fit cleanly on
#                               Airflow 2.10 (1.9+ jumped to protobuf 5,
#                               1.10 to protobuf 6). Cosmos 1.14 supports
#                               dbt 1.5–1.10 so dropping to 1.8 keeps
#                               orchestration intact. Bump back to 1.10
#                               when we move back to Airflow 3.
#   - dbt-postgres 1.8.x      : matches dbt-core minor (dbt convention)
#   - astronomer-cosmos 1.14.x: current stable; supports dbt 1.5–1.10 AND
#                               both Airflow 2.10+ and 3.x.
#
# Why the CUSTOM (protobuf-stripped) constraint file: Airflow's published
# constraint file pins every transitive — without it pip explodes into
# ResolutionTooDeep when resolving foreign packages like dbt + cosmos.
# But the vanilla file also pins `protobuf==4.25.6` (Airflow's tested
# floor), and dbt 1.8 requires `protobuf>=5,<6` — so pip then dies with
# ResolutionImpossible. We `grep -v ^protobuf==` the constraint file so
# dbt's protobuf 5 wins for the install; every OTHER Airflow transitive
# stays locked, so resolution still converges in one pass. Airflow 2.10
# tolerates protobuf 5.x at runtime (the 4.25 pin is a tested-stability
# floor, not a hard ABI break).
ARG AIRFLOW_VERSION=2.10.5
ARG PYTHON_VERSION=3.12
ARG CONSTRAINT_URL="https://raw.githubusercontent.com/apache/airflow/constraints-${AIRFLOW_VERSION}/constraints-${PYTHON_VERSION}.txt"

# libpq-dev is needed at build time for pip to build psycopg2 from sdist
# (something in dbt 1.8's transitive tree pulls in the non-binary
# psycopg2 sdist even though dbt-postgres itself depends on
# psycopg2-binary). Without libpq-dev the build dies with
# `pg_config executable not found`. The base airflow:2.10.5 image
# runs as the unprivileged `airflow` user; switch to root for apt-get
# then back to airflow for pip (matches the upstream image's own
# convention for build-time deps).
USER root
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq-dev && \
    rm -rf /var/lib/apt/lists/*
USER airflow

RUN curl -fsSL "${CONSTRAINT_URL}" \
      | grep -v "^protobuf==" \
      > /tmp/orchestack-constraints.txt && \
    pip install --no-cache-dir --constraint /tmp/orchestack-constraints.txt \
        "dbt-core>=1.8,<1.9" \
        "dbt-postgres>=1.8,<1.9" \
        "astronomer-cosmos>=1.14,<1.15" && \
    rm /tmp/orchestack-constraints.txt

# Build-time verification: container build fails if dbt or cosmos
# can't be imported. Catches version-pin breaks at build time, not
# at the operator's first DAG run.
#
# `dbt --version` exits with code 1 when an upstream newer version
# exists on PyPI — that's by design in dbt for end-user nag purposes,
# but for build-verification we just need "can we import these?",
# which Python imports answer cleanly without any update-check side
# effects.
RUN python -c "import dbt.version; import cosmos; \
    print(f'dbt-core OK: {dbt.version.__version__}'); \
    print(f'astronomer-cosmos OK: {cosmos.__version__}')"

# Re-set USER to airflow (the pip install above runs as root via the
# upstream image's entrypoint convention; runtime should drop back to
# the unprivileged airflow user the upstream image already created).
USER airflow
